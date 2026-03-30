# SPDX-License-Identifier: Apache-2.0
"""LangGraph-based implementation design agent for NAT/NCMS.

Deterministic pipeline: read_document -> ask_experts -> synthesize_design -> publish_design -> verify.
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


# -- State -------------------------------------------------------------------


class DesignState(TypedDict):
    """Graph state for the design pipeline."""

    topic: str  # Design subject
    source_doc_id: str | None  # PO's PRD doc ID (parsed from input)
    source_content: str  # PRD content
    expert_input: dict[str, str]  # {"architect": "...", "security": "..."}
    design: str  # Implementation design markdown
    document_id: str | None  # Published design doc ID
    messages: list[BaseMessage]  # LangGraph compat


# -- Prompts -----------------------------------------------------------------

SYNTHESIZE_DESIGN_PROMPT = """\
Role: You are an implementation architect creating a detailed coding design \
from a product requirements document and expert input.

Context:
- Product Requirements Document (PRD):
{prd_content}

- Architecture Expert Input:
{architect_input}

- Security Expert Input:
{security_input}

Task: Create a comprehensive TypeScript/Node.js implementation design for: {topic}

Requirements:
- Target stack: TypeScript, Node.js, Express or Fastify
- Include concrete code snippets throughout (TypeScript)
- Every section must be actionable — a developer should be able to code from this

Output the design as markdown with these sections:

# {topic} — Implementation Design

## Project Structure
(Directory tree, files, modules, and their responsibilities)

## API Endpoint Specifications
(Routes, HTTP methods, request/response TypeScript interfaces, status codes)

## Data Models
(TypeScript interfaces, database schemas with column types, indexes)

## Authentication Middleware Implementation
(Token validation flow, middleware code, session management)

## Security Control Implementations
(Rate limiting, input validation, CSRF protection, token rotation — with code)

## Configuration and Environment Variables
(All required env vars, defaults, validation)

## Error Handling Patterns
(Error classes, middleware, consistent error response format — with code)

## Testing Strategy with Example Test Cases
(Unit test examples, integration test patterns, mocking strategy)

## Deployment Configuration
(Dockerfile, docker-compose, environment setup, health checks)
"""


# -- Agent -------------------------------------------------------------------


class DesignAgent:
    """Deterministic LangGraph design pipeline.

    Nodes: read_document -> ask_experts -> synthesize_design -> publish_design -> verify
    LLM called once: synthesize_design.
    All other nodes are pure Python.
    """

    def __init__(
        self,
        llm: BaseChatModel,
        hub_url: str,
        from_agent: str,
        client: NCMSHttpClient,
    ) -> None:
        self.llm = llm
        self.hub_url = hub_url
        self.from_agent = from_agent
        self.client = client

    async def build_graph(self) -> StateGraph:
        """Build and compile the deterministic design pipeline."""
        graph = StateGraph(DesignState)

        graph.add_node("read_document", self.read_document)
        graph.add_node("ask_experts", self.ask_experts)
        graph.add_node("synthesize_design", self.synthesize_design)
        graph.add_node("publish_design", self.publish_design)
        graph.add_node("verify", self.verify)

        # All edges unconditional — deterministic flow
        graph.add_edge(START, "read_document")
        graph.add_edge("read_document", "ask_experts")
        graph.add_edge("ask_experts", "synthesize_design")
        graph.add_edge("synthesize_design", "publish_design")
        graph.add_edge("publish_design", "verify")
        graph.add_edge("verify", END)

        compiled = graph.compile()
        logger.info(
            "[design_agent] Graph compiled: read_document -> ask_experts "
            "-> synthesize_design -> publish_design -> verify"
        )
        return compiled

    # -- Node 1: Read Document (Pure Python) ---------------------------------

    async def read_document(self, state: DesignState) -> DesignState:
        """Parse doc_id from input and fetch the PRD. No LLM."""
        topic = state["topic"]
        logger.info("[design_agent] Reading source document for topic: %s", topic[:100])

        # Parse (doc_id: XXXX) from the input message
        match = re.search(r"\(doc_id:\s*([^)]+)\)", topic)
        if match:
            doc_id = match.group(1).strip()
            state["source_doc_id"] = doc_id
            try:
                result = await self.client.read_document(doc_id)
                content = result.get("content", "")
                state["source_content"] = content
                logger.info(
                    "[design_agent] Read source document: %s (%d chars)", doc_id, len(content)
                )
            except Exception as e:
                logger.warning("[design_agent] Failed to read document %s: %s", doc_id, e)
                state["source_content"] = ""
        else:
            logger.info("[design_agent] No doc_id found in input — standalone mode")
            state["source_doc_id"] = None
            state["source_content"] = ""

        return state

    # -- Node 2: Ask Experts (Pure Python) -----------------------------------

    async def ask_experts(self, state: DesignState) -> DesignState:
        """Parallel bus_ask to architecture and security experts. No LLM."""
        topic = state["topic"]
        logger.info("[design_agent] Querying experts for: %s", topic[:100])

        # Announce progress to hub
        try:
            await self.client.bus_announce(
                content=f"Querying architecture and security experts for: {topic[:80]}",
                domains=["implementation", "identity-service"],
                from_agent=self.from_agent,
            )
        except Exception:
            pass  # Non-fatal

        async def _ask_architect() -> str:
            try:
                result = await self.client.bus_ask(
                    question=f"What architectural patterns and ADRs apply to: {topic}",
                    domains=["architecture", "decisions"],
                    from_agent=self.from_agent,
                    timeout_ms=120000,
                )
                answer = result.get("answer", "") or result.get("content", "")
                logger.info("[design_agent] Architect response: %d chars", len(answer))
                return answer
            except Exception as e:
                logger.warning("[design_agent] Architect query failed: %s", e)
                return ""

        async def _ask_security() -> str:
            try:
                result = await self.client.bus_ask(
                    question=f"What security threats and controls apply to: {topic}",
                    domains=["security", "threats"],
                    from_agent=self.from_agent,
                    timeout_ms=120000,
                )
                answer = result.get("answer", "") or result.get("content", "")
                logger.info("[design_agent] Security response: %d chars", len(answer))
                return answer
            except Exception as e:
                logger.warning("[design_agent] Security query failed: %s", e)
                return ""

        architect_response, security_response = await asyncio.gather(
            _ask_architect(),
            _ask_security(),
        )

        state["expert_input"] = {
            "architect": architect_response,
            "security": security_response,
        }

        logger.info(
            "[design_agent] Expert input collected — architect: %d chars, security: %d chars",
            len(architect_response),
            len(security_response),
        )

        # Announce completion
        try:
            await self.client.bus_announce(
                content=(
                    f"Expert consultation complete for: {topic[:80]}\n"
                    f"Architect: {len(architect_response)} chars | "
                    f"Security: {len(security_response)} chars"
                ),
                domains=["implementation", "identity-service"],
                from_agent=self.from_agent,
            )
        except Exception:
            pass

        return state

    # -- Node 3: Synthesize Design (LLM) ------------------------------------

    async def synthesize_design(self, state: DesignState) -> DesignState:
        """LLM synthesizes PRD + expert input into an implementation design."""
        topic = state["topic"]
        logger.info("[design_agent] Synthesizing design for: %s", topic[:100])

        # Truncate inputs to fit context window
        prd_content = state.get("source_content", "") or ""
        if len(prd_content) > 15000:
            prd_content = prd_content[:15000] + "\n\n[... truncated for context window ...]"

        expert_input = state.get("expert_input", {})
        architect_input = expert_input.get("architect", "") or "(no architect input available)"
        security_input = expert_input.get("security", "") or "(no security input available)"
        if len(architect_input) > 3000:
            architect_input = architect_input[:3000] + "\n[... truncated ...]"
        if len(security_input) > 3000:
            security_input = security_input[:3000] + "\n[... truncated ...]"

        if not prd_content:
            prd_content = "(no PRD document provided — design from topic description only)"

        prompt = SYNTHESIZE_DESIGN_PROMPT.format(
            topic=topic,
            prd_content=prd_content,
            architect_input=architect_input,
            security_input=security_input,
        )

        try:
            response = await self.llm.ainvoke([
                SystemMessage(
                    content=(
                        "You are an expert implementation architect. "
                        "Write detailed, actionable TypeScript implementation designs "
                        "with concrete code snippets."
                    )
                ),
                HumanMessage(content=prompt),
            ])
            state["design"] = response.content
            logger.info("[design_agent] Design synthesized: %d chars", len(state["design"]))
            logger.debug("[design_agent] Design preview: %s", state["design"][:500])
        except Exception as e:
            logger.error("[design_agent] Synthesis failed: %s", e)
            # Emergency fallback — return structured placeholder
            state["design"] = (
                f"# {topic} — Implementation Design (Synthesis Failed)\n\n"
                f"**Error:** LLM synthesis failed: {e}\n\n"
                f"## PRD Content\n\n{prd_content[:5000]}\n\n"
                f"## Architect Input\n\n{architect_input}\n\n"
                f"## Security Input\n\n{security_input}\n"
            )

        return state

    # -- Node 4: Publish Design (Pure Python) --------------------------------

    async def publish_design(self, state: DesignState) -> DesignState:
        """Publish the design to the NCMS document store. No LLM."""
        topic = state["topic"]
        design = state["design"]
        logger.info("[design_agent] Publishing design document: %d chars", len(design))

        try:
            result = await self.client.publish_document(
                content=design,
                title=f"{topic} — Implementation Design",
                from_agent=self.from_agent,
                format="markdown",
            )
            doc_id = result.get("document_id", "unknown")
            state["document_id"] = doc_id
            logger.info("[design_agent] Document published: %s", doc_id)

            # Announce to the bus
            try:
                await self.client.bus_announce(
                    content=(
                        f"Implementation design published: {topic}\n"
                        f"Document ID: {doc_id}\n"
                        f"Size: {len(design)} chars"
                    ),
                    domains=["implementation", "identity-service"],
                    from_agent=self.from_agent,
                )
            except Exception:
                pass

        except Exception as e:
            logger.error("[design_agent] Publish failed: %s", e)
            state["document_id"] = None

        return state

    # -- Node 5: Verify (Pure Python) ----------------------------------------

    async def verify(self, state: DesignState) -> DesignState:
        """Verify pipeline completion and log results. No LLM."""
        topic = state["topic"]
        doc_id = state.get("document_id")

        if doc_id:
            logger.info(
                "[design_agent] Pipeline complete. Topic: '%s' | Doc: %s | Design: %d chars",
                topic[:60],
                doc_id,
                len(state.get("design", "")),
            )
        else:
            logger.warning(
                "[design_agent] Pipeline complete but no document published for: %s",
                topic[:60],
            )

        # Announce completion
        try:
            await self.client.bus_announce(
                content=(
                    f"Pipeline complete. Implementation design published: {doc_id}"
                    if doc_id
                    else f"Pipeline complete but publish failed for: {topic[:60]}"
                ),
                domains=["implementation", "identity-service"],
                from_agent=self.from_agent,
            )
        except Exception:
            pass

        logger.info(
            "[design_agent] Returning design (%d chars) to auto_memory_agent for persistence",
            len(state.get("design", "")),
        )

        return state


# -- NAT Registration -------------------------------------------------------


class DesignAgentConfig(FunctionBaseConfig, name="design_agent"):
    """Configuration for the LangGraph design agent."""

    llm_name: LLMRef = Field(..., description="LLM to use for design synthesis")
    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub URL for document publishing and bus announcements",
    )
    from_agent: str = Field(
        default="builder",
        description="Agent ID for bus announcements and document attribution",
    )


@register_function(config_type=DesignAgentConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def design_agent_fn(
    config: DesignAgentConfig, builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """Build the LangGraph design pipeline and register as a NAT function."""
    logger.info("[design_agent] Initializing LangGraph design agent")

    # Get LangChain-compatible LLM from NAT builder
    llm = await builder.get_llm(config.llm_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)
    client = NCMSHttpClient(hub_url=config.hub_url)

    # Build the LangGraph pipeline
    agent = DesignAgent(
        llm=llm,
        hub_url=config.hub_url,
        from_agent=config.from_agent,
        client=client,
    )
    graph = await agent.build_graph()
    logger.info("[design_agent] LangGraph pipeline ready")

    async def _design(input_message: str) -> str:
        """Run the full design pipeline and return the implementation design.

        This is the function that auto_memory_agent calls. The returned
        string (the full markdown design) gets saved to NCMS memory
        automatically by the auto_memory wrapper.

        Args:
            input_message: The design topic, optionally with (doc_id: XXXX).

        Returns:
            The synthesized markdown implementation design.
        """
        logger.info("[design_agent] === Starting design pipeline ===")
        logger.info("[design_agent] Input: %s", input_message[:200])

        result = await graph.ainvoke({
            "topic": input_message,
            "source_doc_id": None,
            "source_content": "",
            "expert_input": {},
            "design": "",
            "document_id": None,
            "messages": [HumanMessage(content=input_message)],
        })

        design = result.get("design", "Design pipeline produced no output.")
        doc_id = result.get("document_id")

        logger.info("[design_agent] === Pipeline complete ===")
        logger.info("[design_agent] Design: %d chars | Doc ID: %s", len(design), doc_id)
        logger.info("[design_agent] Returning to auto_memory for persistence")

        return design

    try:
        yield FunctionInfo.from_fn(
            _design,
            description=(
                "Implementation design agent. Reads a PRD document (if doc_id provided), "
                "queries architecture and security experts via the knowledge bus, "
                "then synthesizes a detailed TypeScript implementation design with "
                "code snippets. Publishes the design to the document store. "
                "Returns the full implementation design markdown."
            ),
        )
    finally:
        await client.close()
        logger.info("[design_agent] Cleaned up HTTP client")
