# SPDX-License-Identifier: Apache-2.0
"""LangGraph-based PRD (Product Requirements Document) agent for NAT/NCMS.

Deterministic pipeline: read_document → ask_experts → synthesize_prd → publish_prd → verify_and_trigger.
LLM called exactly once (synthesis). All other nodes are pure Python.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncGenerator
from typing import TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import Field

from nat.builder.builder import Builder
from nat.builder.framework_enum import LLMFrameworkEnum
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.component_ref import LLMRef
from nat.data_models.function import FunctionBaseConfig

from .http_client import NCMSHttpClient

logger = logging.getLogger(__name__)


# ── State ─────────────────────────────────────────────────────────────────────


class PRDState(TypedDict):
    """Graph state for the PRD pipeline."""

    topic: str  # Research topic / PRD subject
    source_doc_id: str | None  # Researcher's doc ID (parsed from input)
    source_content: str  # Content from source document
    expert_input: dict[str, str]  # {"architect": "...", "security": "..."}
    prd: str  # Synthesized PRD markdown
    document_id: str | None  # Published PRD doc ID
    messages: list[BaseMessage]  # LangGraph compat


# ── Prompts ───────────────────────────────────────────────────────────────────

SYNTHESIZE_PRD_PROMPT = """\
You are a senior product owner writing a Product Requirements Document (PRD). \
Synthesize the source research and expert input into a structured, actionable PRD. \
Ground security and architecture sections in the expert input provided.

## Topic
{topic}

## Source Document (Researcher's Report)
{source_content}

## Expert Input

### Architect
{architect_input}

### Security
{security_input}

Write the PRD with these sections:

# {topic} — Product Requirements Document

## Problem Statement and Scope
(Define the problem being solved, boundaries, and what is out of scope)

## Goals and Non-Goals
### Goals
(Numbered list of measurable goals)
### Non-Goals
(Explicit list of what this effort will NOT address)

## Functional Requirements
(Numbered requirements, each with acceptance criteria)

## Non-Functional Requirements
### Performance
(Latency, throughput, concurrency targets)
### Scalability
(Growth projections, scaling strategy)
### Compliance
(Regulatory requirements, standards adherence)

## Security Requirements
(Grounded in security expert input — threats, controls, mitigations)

## Architecture Alignment
(Grounded in architect expert input — patterns, decisions, constraints)

## Risk Matrix
| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
(Identify key risks with likelihood, impact, and mitigation strategies)

## Success Metrics
(Numbered list of measurable success criteria)

## References
(Numbered list of sources referenced)
"""


# ── Agent ─────────────────────────────────────────────────────────────────────


class PRDAgent:
    """Deterministic LangGraph PRD pipeline.

    Nodes: read_document → ask_experts → synthesize_prd → publish_prd → verify_and_trigger
    LLM called once: synthesize_prd.
    All other nodes are pure Python.
    """

    def __init__(
        self,
        llm: BaseChatModel,
        hub_url: str,
        from_agent: str,
        client: NCMSHttpClient,
        trigger_next_agent: bool = True,
    ) -> None:
        self.llm = llm
        self.hub_url = hub_url
        self.from_agent = from_agent
        self.client = client
        self.trigger_next_agent = trigger_next_agent

    async def build_graph(self) -> StateGraph:
        """Build and compile the deterministic PRD pipeline."""
        graph = StateGraph(PRDState)

        graph.add_node("read_document", self.read_document)
        graph.add_node("ask_experts", self.ask_experts)
        graph.add_node("synthesize_prd", self.synthesize_prd)
        graph.add_node("publish_prd", self.publish_prd)
        graph.add_node("verify_and_trigger", self.verify_and_trigger)

        # All edges unconditional — deterministic flow
        graph.add_edge(START, "read_document")
        graph.add_edge("read_document", "ask_experts")
        graph.add_edge("ask_experts", "synthesize_prd")
        graph.add_edge("synthesize_prd", "publish_prd")
        graph.add_edge("publish_prd", "verify_and_trigger")
        graph.add_edge("verify_and_trigger", END)

        compiled = graph.compile()
        logger.info(
            "[prd_agent] Graph compiled: read_document → ask_experts → synthesize_prd"
            " → publish_prd → verify_and_trigger"
        )
        return compiled

    # ── Node 1: Read Document (Pure Python) ──────────────────────────────

    async def read_document(self, state: PRDState) -> PRDState:
        """Parse doc_id from input and fetch the researcher's report. No LLM."""
        topic = state["topic"]
        logger.info("[prd_agent] Reading source document for topic: %s", topic[:100])

        # Parse (doc_id: XXXX) from the input message
        match = re.search(r"\(doc_id:\s*([^)]+)\)", topic)
        if match:
            doc_id = match.group(1).strip()
            state["source_doc_id"] = doc_id
            logger.info("[prd_agent] Found doc_id in input: %s", doc_id)

            try:
                doc = await self.client.read_document(doc_id)
                content = doc.get("content", "")
                state["source_content"] = content
                logger.info(
                    "[prd_agent] Read source document: %s (%d chars)", doc_id, len(content)
                )
            except Exception as e:
                logger.warning("[prd_agent] Failed to read document %s: %s", doc_id, e)
                state["source_content"] = ""
        else:
            logger.info("[prd_agent] No doc_id found in input — standalone mode")
            state["source_doc_id"] = None
            state["source_content"] = ""

        return state

    # ── Node 2: Ask Experts (Pure Python) ────────────────────────────────

    async def ask_experts(self, state: PRDState) -> PRDState:
        """Parallel bus_ask to architect and security experts. No LLM."""
        topic = state["topic"]
        logger.info("[prd_agent] Asking experts about: %s", topic[:100])

        # Announce progress to bus
        try:
            await self.client.bus_announce(
                content=f"Consulting architecture and security experts for: {topic[:80]}",
                domains=["product", "requirements"],
                from_agent=self.from_agent,
            )
        except Exception:
            pass  # Non-fatal

        async def _ask_architect() -> str:
            try:
                result = await self.client.bus_ask(
                    question=f"What architectural decisions apply to: {topic}",
                    domains=["architecture", "decisions"],
                    from_agent=self.from_agent,
                    timeout_ms=120000,
                )
                response = result.get("response", result.get("content", ""))
                logger.info("[prd_agent] Architect response: %d chars", len(response))
                return response
            except Exception as e:
                logger.warning("[prd_agent] Architect ask failed: %s", e)
                return ""

        async def _ask_security() -> str:
            try:
                result = await self.client.bus_ask(
                    question=f"What security requirements apply to: {topic}",
                    domains=["security", "threats"],
                    from_agent=self.from_agent,
                    timeout_ms=120000,
                )
                response = result.get("response", result.get("content", ""))
                logger.info("[prd_agent] Security response: %d chars", len(response))
                return response
            except Exception as e:
                logger.warning("[prd_agent] Security ask failed: %s", e)
                return ""

        architect_resp, security_resp = await asyncio.gather(
            _ask_architect(), _ask_security()
        )

        state["expert_input"] = {
            "architect": architect_resp,
            "security": security_resp,
        }

        logger.info(
            "[prd_agent] Expert input collected — architect: %d chars, security: %d chars",
            len(architect_resp),
            len(security_resp),
        )

        # Announce completion
        try:
            await self.client.bus_announce(
                content=(
                    f"Expert consultation complete for: {topic[:80]}\n"
                    f"Architect: {len(architect_resp)} chars | Security: {len(security_resp)} chars"
                ),
                domains=["product", "requirements"],
                from_agent=self.from_agent,
            )
        except Exception:
            pass

        return state

    # ── Node 3: Synthesize PRD (LLM) ─────────────────────────────────────

    async def synthesize_prd(self, state: PRDState) -> PRDState:
        """LLM synthesizes source document and expert input into a structured PRD."""
        topic = state["topic"]
        logger.info("[prd_agent] Synthesizing PRD for: %s", topic[:100])

        # Truncate inputs to fit context window
        source_content = state.get("source_content", "")
        if len(source_content) > 15000:
            source_content = source_content[:15000] + "\n\n[... truncated for context window ...]"

        expert = state.get("expert_input", {})
        architect_input = expert.get("architect", "No architect input available.")
        if len(architect_input) > 3000:
            architect_input = architect_input[:3000] + "\n\n[... truncated ...]"

        security_input = expert.get("security", "No security input available.")
        if len(security_input) > 3000:
            security_input = security_input[:3000] + "\n\n[... truncated ...]"

        if not source_content:
            source_content = "No source document provided. Generate PRD from topic and expert input."

        prompt = SYNTHESIZE_PRD_PROMPT.format(
            topic=topic,
            source_content=source_content,
            architect_input=architect_input,
            security_input=security_input,
        )

        try:
            response = await self.llm.ainvoke([
                SystemMessage(
                    content=(
                        "You are a senior product owner. Write precise, actionable PRDs "
                        "with measurable acceptance criteria and success metrics."
                    )
                ),
                HumanMessage(content=prompt),
            ])
            state["prd"] = response.content
            logger.info("[prd_agent] PRD synthesized: %d chars", len(state["prd"]))
            logger.debug("[prd_agent] PRD preview: %s", state["prd"][:500])
        except Exception as e:
            logger.error("[prd_agent] PRD synthesis failed: %s", e)
            # Emergency fallback — return a skeleton PRD
            state["prd"] = (
                f"# {topic} — Product Requirements Document\n\n"
                f"## Source Content\n\n{source_content[:5000]}\n\n"
                f"## Architect Input\n\n{architect_input}\n\n"
                f"## Security Input\n\n{security_input}\n\n"
                f"*PRD synthesis failed — raw inputs provided above.*"
            )

        return state

    # ── Node 4: Publish PRD (Pure Python) ─────────────────────────────────

    async def publish_prd(self, state: PRDState) -> PRDState:
        """Publish the PRD to the NCMS document store. No LLM."""
        topic = state["topic"]
        prd = state["prd"]
        title = f"{topic} — PRD"
        logger.info("[prd_agent] Publishing PRD: %d chars", len(prd))

        try:
            result = await self.client.publish_document(
                content=prd,
                title=title,
                from_agent=self.from_agent,
                format="markdown",
            )
            doc_id = result.get("document_id", "unknown")
            state["document_id"] = doc_id
            logger.info("[prd_agent] Document published: %s", doc_id)

            # Announce to the bus
            try:
                await self.client.bus_announce(
                    content=(
                        f"PRD published: {topic}\n"
                        f"Document ID: {doc_id}\n"
                        f"Size: {len(prd)} chars"
                    ),
                    domains=["product", "requirements"],
                    from_agent=self.from_agent,
                )
            except Exception:
                pass

        except Exception as e:
            logger.error("[prd_agent] Publish failed: %s", e)
            state["document_id"] = None

        return state

    # ── Node 5: Verify and Trigger (Pure Python) ─────────────────────────

    async def verify_and_trigger(self, state: PRDState) -> PRDState:
        """Verify pipeline completion and optionally trigger the builder agent."""
        topic = state["topic"]
        doc_id = state.get("document_id")
        prd = state.get("prd", "")

        if doc_id:
            logger.info(
                "[prd_agent] Pipeline complete. Topic: '%s' | Doc: %s | PRD: %d chars",
                topic[:60],
                doc_id,
                len(prd),
            )
        else:
            logger.warning(
                "[prd_agent] Pipeline complete but no document published for: %s",
                topic[:60],
            )

        # Trigger builder agent if configured
        if self.trigger_next_agent and doc_id:
            title = f"{topic} — PRD"
            logger.info("[prd_agent] Triggering builder with PRD doc_id: %s", doc_id)
            try:
                await self.client.trigger_agent(
                    "builder",
                    f'Create implementation design based on PRD: "{title}" (doc_id: {doc_id})',
                )
                logger.info("[prd_agent] Builder triggered successfully")
            except Exception as e:
                logger.warning("[prd_agent] Failed to trigger builder: %s", e)

        # Verify memory will be saved by auto_memory (log what we're returning)
        logger.info(
            "[prd_agent] Returning PRD (%d chars) to auto_memory_agent for persistence",
            len(prd),
        )

        return state


# ── NAT Registration ──────────────────────────────────────────────────────────


class PRDAgentConfig(FunctionBaseConfig, name="prd_agent"):
    """Configuration for the LangGraph PRD agent."""

    llm_name: LLMRef = Field(..., description="LLM to use for PRD synthesis")
    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub URL for document publishing, bus asks, and announcements",
    )
    from_agent: str = Field(
        default="product_owner",
        description="Agent ID for bus announcements and document attribution",
    )
    trigger_next_agent: bool = Field(
        default=True,
        description="Whether to trigger the builder agent after PRD is published",
    )


@register_function(config_type=PRDAgentConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def prd_agent_fn(
    config: PRDAgentConfig, builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """Build the LangGraph PRD pipeline and register as a NAT function."""
    logger.info("[prd_agent] Initializing LangGraph PRD agent")

    # Get LangChain-compatible LLM from NAT builder
    llm = await builder.get_llm(config.llm_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)
    client = NCMSHttpClient(hub_url=config.hub_url)

    # Build the LangGraph pipeline
    agent = PRDAgent(
        llm=llm,
        hub_url=config.hub_url,
        from_agent=config.from_agent,
        client=client,
        trigger_next_agent=config.trigger_next_agent,
    )
    graph = await agent.build_graph()
    logger.info("[prd_agent] LangGraph pipeline ready")

    async def _prd(input_message: str) -> str:
        """Run the full PRD pipeline and return the synthesized PRD.

        This is the function that auto_memory_agent calls. The returned
        string (the full markdown PRD) gets saved to NCMS memory
        automatically by the auto_memory wrapper.

        Args:
            input_message: The PRD topic from the user (may contain doc_id reference).

        Returns:
            The synthesized markdown PRD.
        """
        logger.info("[prd_agent] === Starting PRD pipeline ===")
        logger.info("[prd_agent] Topic: %s", input_message[:200])

        result = await graph.ainvoke({
            "topic": input_message,
            "source_doc_id": None,
            "source_content": "",
            "expert_input": {},
            "prd": "",
            "document_id": None,
            "messages": [HumanMessage(content=input_message)],
        })

        prd = result.get("prd", "PRD pipeline produced no output.")
        doc_id = result.get("document_id")

        logger.info("[prd_agent] === Pipeline complete ===")
        logger.info("[prd_agent] PRD: %d chars | Doc ID: %s", len(prd), doc_id)
        logger.info("[prd_agent] Returning to auto_memory for persistence")

        return prd

    try:
        yield FunctionInfo.from_fn(
            _prd,
            description=(
                "Product Requirements Document (PRD) agent. Reads a researcher's "
                "report (if doc_id provided), consults architecture and security "
                "experts via the knowledge bus, and synthesizes a structured PRD "
                "with requirements, acceptance criteria, risk matrix, and success "
                "metrics. Publishes to the document store and optionally triggers "
                "the builder agent. Returns the full PRD."
            ),
        )
    finally:
        await client.close()
        logger.info("[prd_agent] Cleaned up HTTP client")
