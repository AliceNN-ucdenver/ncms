# SPDX-License-Identifier: Apache-2.0
"""LangGraph-based expert agent for NAT/NCMS.

Deterministic pipeline with conditional routing:
  classify → search_memory → [synthesize_answer | structured_review]

Used by BOTH architect and security expert agents. The constructor takes
domain-specific prompts as parameters; NAT registration creates two configs.
"""

from __future__ import annotations

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
from .pipeline_utils import extract_project_id, extract_doc_id, emit_telemetry, fetch_and_cache_document, build_entity_search_query, traced_llm_call
from .expert_prompts import (
    ARCHITECT_KNOWLEDGE_PROMPT,
    ARCHITECT_REVIEW_PROMPT,
    SECURITY_KNOWLEDGE_PROMPT,
    SECURITY_REVIEW_PROMPT,
)

logger = logging.getLogger(__name__)


# ── State ─────────────────────────────────────────────────────────────────────


class ExpertState(TypedDict):
    """Graph state for the expert pipeline."""

    input: str  # Original question or review request
    request_type: str  # "question" or "review"
    memory_context: str  # Retrieved from NCMS memory
    review_document: str  # Design content fetched by doc_id (review mode only)
    response: str  # Final response
    messages: list[BaseMessage]  # LangGraph compat
    project_id: str | None  # PRJ-XXXXXXXX for pipeline tracking


# ── Review detection pattern ────────────────────────────────────────────────

_REVIEW_PATTERN = re.compile(
    r"DESIGN TO REVIEW|IMPLEMENTATION DESIGN TO REVIEW|"
    r"You are a (?:security|architecture) reviewer|"
    r"Review design document|Review.*criteria",
    re.IGNORECASE,
)


# ── Agent ─────────────────────────────────────────────────────────────────────


class ExpertAgent:
    """Deterministic LangGraph expert pipeline with conditional routing.

    Nodes: classify -> search_memory -> [synthesize_answer | structured_review]
    LLM called once (synthesis or review). classify and search_memory are pure Python.
    """

    def __init__(
        self,
        llm: BaseChatModel,
        hub_url: str,
        from_agent: str,
        client: NCMSHttpClient,
        primary_domain: str,
        knowledge_prompt: str,
        review_prompt: str,
    ) -> None:
        self.llm = llm
        self.hub_url = hub_url
        self.from_agent = from_agent
        self.client = client
        self.primary_domain = primary_domain
        self.knowledge_prompt = knowledge_prompt
        self.review_prompt = review_prompt

    async def build_graph(self) -> StateGraph:
        """Build and compile the expert pipeline with conditional routing."""
        graph = StateGraph(ExpertState)

        graph.add_node("classify", self.classify)
        graph.add_node("search_memory", self.search_memory)
        graph.add_node("synthesize_answer", self.synthesize_answer)
        graph.add_node("structured_review", self.structured_review)

        graph.add_edge(START, "classify")
        graph.add_edge("classify", "search_memory")
        graph.add_conditional_edges("search_memory", self.route)
        graph.add_edge("synthesize_answer", END)
        graph.add_edge("structured_review", END)

        compiled = graph.compile()
        logger.info(
            "[expert_agent:%s] Graph compiled: classify -> search_memory -> [answer|review]",
            self.from_agent,
        )
        return compiled

    # ── Node 1: Classify (Pure Python) ───────────────────────────────────

    async def classify(self, state: ExpertState) -> ExpertState:
        """Classify the input as a question or a review request. No LLM.

        Scans the FULL input text because NAT's auto_memory wrapper may
        prepend memory context, pushing the actual request past any fixed
        prefix window. With document-by-reference, review messages are
        short ("Review design document (doc_id: xxx)") so false positives
        from prepended context are unlikely.
        """
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "classify", "started")
        input_text = state["input"]

        if _REVIEW_PATTERN.search(input_text):
            state["request_type"] = "review"
        else:
            state["request_type"] = "question"

        logger.info(
            "[expert_agent:%s] Classified as: %s (input: %d chars)",
            self.from_agent,
            state["request_type"],
            len(input_text),
        )
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "classify", "completed", state["request_type"])
        return state

    # ── Node 2: Search Memory (Pure Python) ──────────────────────────────

    async def search_memory(self, state: ExpertState) -> ExpertState:
        """Retrieve relevant memories from NCMS using document-by-reference.

        Document-by-reference pattern (Feature 19):
        - Extract doc_id from the input message
        - Fetch document + entity metadata from hub
        - Search NCMS memory with entity keywords (not raw document content)
        - Cache document locally for LLM context in structured_review/answer

        For reviews: fetches the design document, caches it in state, searches
        with entity keywords to find ADRs, threat models, governance knowledge.
        For questions: extracts doc_id if present, uses entity keywords for search.
        """
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "search_memory", "started")
        input_text = state["input"]
        request_type = state.get("request_type", "question")

        all_results: list[dict] = []
        doc_id = extract_doc_id(input_text)
        entity_query_used: str | None = None  # Track for grounding log

        if doc_id:
            # ── Document-by-reference: fetch document + entities ──
            try:
                content, entities = await fetch_and_cache_document(self.client, doc_id)

                if request_type == "review":
                    # Store document for structured_review to use
                    state["review_document"] = content
                    logger.info(
                        "[expert_agent:%s] Review mode: fetched doc_id=%s (%d chars, %d entities)",
                        self.from_agent, doc_id, len(content), len(entities),
                    )

                # Search memory with entity keywords (primary search)
                if entities:
                    entity_query_used = build_entity_search_query(
                        entities, domain=self.primary_domain,
                    )
                    entity_query = entity_query_used
                    logger.info(
                        "[expert_agent:%s] Entity-enriched search: %s",
                        self.from_agent, entity_query[:120],
                    )
                    results = await self.client.recall_memory(
                        query=entity_query[:2000],
                        domain=self.primary_domain,
                        limit=10,
                    )
                    all_results.extend(results or [])

            except Exception as e:
                logger.warning(
                    "[expert_agent:%s] Document fetch/entity search failed for %s: %s",
                    self.from_agent, doc_id, e,
                )

        # ── Fallback: domain-specific keyword search ──
        if len(all_results) < 3:
            try:
                domain_query = {
                    "architecture": "ADR architecture decisions CALM service boundary quality attributes",
                    "security": "STRIDE threat model OWASP security controls authentication",
                }.get(self.primary_domain, self.primary_domain)

                if not doc_id:
                    # No doc_id — append first 300 chars of input for context
                    domain_query += f" {input_text[:300]}"

                logger.info(
                    "[expert_agent:%s] Domain fallback search: %s",
                    self.from_agent, domain_query[:120],
                )
                fallback_results = await self.client.recall_memory(
                    query=domain_query[:2000],
                    domain=self.primary_domain,
                    limit=10,
                )
                # Deduplicate
                existing_ids = {r.get("id", r.get("memory_id", i)) for i, r in enumerate(all_results)}
                for r in (fallback_results or []):
                    rid = r.get("id", r.get("memory_id", ""))
                    if rid not in existing_ids:
                        all_results.append(r)
                        existing_ids.add(rid)
            except Exception as e:
                logger.warning("[expert_agent:%s] Fallback recall failed: %s", self.from_agent, e)

        # Format results into context string
        parts = []
        for i, r in enumerate(all_results):
            content = r.get("content", "") if isinstance(r, dict) else str(r)
            parts.append(f"[{i + 1}] {content[:1500]}")

        context = "\n\n".join(parts) if parts else "No relevant knowledge found in memory."
        state["memory_context"] = context

        # Record grounding entries for audit trail (#37)
        if doc_id and all_results:
            for r in all_results:
                memory_id = r.get("id", r.get("memory_id", ""))
                score = r.get("score", r.get("total_activation"))
                if memory_id:
                    try:
                        await self.client.record_grounding(
                            document_id=doc_id, memory_id=str(memory_id),
                            retrieval_score=float(score) if score is not None else None,
                            entity_query=entity_query_used[:200] if entity_query_used else None,
                            domain=self.primary_domain,
                        )
                    except Exception:
                        pass  # Non-fatal

        logger.info(
            "[expert_agent:%s] Memory search: %d results (%d chars) [mode=%s, doc_id=%s]",
            self.from_agent,
            len(all_results),
            len(context),
            request_type,
            doc_id,
        )
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "search_memory", "completed", f"{len(all_results)} results")
        return state

    # ── Conditional router ───────────────────────────────────────────────

    def route(self, state: ExpertState) -> str:
        """Route to synthesize_answer or structured_review based on classification."""
        return "synthesize_answer" if state["request_type"] == "question" else "structured_review"

    # ── Node 3a: Synthesize Answer (LLM) ─────────────────────────────────

    async def synthesize_answer(self, state: ExpertState) -> ExpertState:
        """LLM answers the question grounded in retrieved knowledge."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "synthesize_answer", "started")
        input_text = state["input"]
        memory_context = state["memory_context"]

        logger.info(
            "[expert_agent:%s] Synthesizing answer for question",
            self.from_agent,
        )

        prompt = self.knowledge_prompt.format(
            memory_context=memory_context,
            input=input_text,
        )

        try:
            response = await traced_llm_call(
                self.llm, [
                    SystemMessage(content="You are a domain expert. Answer grounded in the retrieved knowledge."),
                    HumanMessage(content=prompt),
                ],
                hub_url=self.hub_url, client=self.client,
                project_id=state.get("project_id"),
                agent=self.from_agent, node="synthesize_answer",
            )
            state["response"] = response.content
            logger.info(
                "[expert_agent:%s] Answer synthesized: %d chars",
                self.from_agent,
                len(state["response"]),
            )
        except Exception as e:
            logger.error("[expert_agent:%s] Answer synthesis failed: %s", self.from_agent, e)
            state["response"] = (
                f"Expert answer generation failed ({e}). "
                f"Retrieved context:\n{memory_context}"
            )

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "synthesize_answer", "completed", f"{len(state['response'])} chars")
        return state

    # ── Node 3b: Structured Review (LLM) ─────────────────────────────────

    async def structured_review(self, state: ExpertState) -> ExpertState:
        """LLM produces a structured SCORE/SEVERITY/COVERED/MISSING/CHANGES review."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "structured_review", "started")
        memory_context = state["memory_context"]

        # Use fetched document if available (doc-by-reference), otherwise input text
        design_content = state.get("review_document", "") or state["input"]

        logger.info(
            "[expert_agent:%s] Review input: %d chars design + %d chars governance context",
            self.from_agent, len(design_content), len(memory_context),
        )

        prompt = self.review_prompt.format(
            memory_context=memory_context,
            design_content=design_content,
        )

        try:
            response = await traced_llm_call(
                self.llm, [
                    SystemMessage(
                        content=(
                            "You are a domain expert performing a structured review. "
                            "Follow the output format exactly."
                        ),
                    ),
                    HumanMessage(content=prompt),
                ],
                hub_url=self.hub_url, client=self.client,
                project_id=state.get("project_id"),
                agent=self.from_agent, node="structured_review",
            )
            state["response"] = response.content
            logger.info(
                "[expert_agent:%s] Review complete: %d chars",
                self.from_agent,
                len(state["response"]),
            )
        except Exception as e:
            logger.error("[expert_agent:%s] Structured review failed: %s", self.from_agent, e)
            state["response"] = (
                f"SCORE: 0\n"
                f"SEVERITY: Critical\n"
                f"COVERED: Review generation failed ({e})\n"
                f"MISSING: Unable to evaluate\n"
                f"CHANGES: 1. Retry review with working LLM endpoint"
            )

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "structured_review", "completed", f"{len(state['response'])} chars")
        return state


# ── NAT Registration ──────────────────────────────────────────────────────────


class ArchitectExpertConfig(FunctionBaseConfig, name="architect_expert"):
    """Configuration for the architect expert agent."""

    llm_name: LLMRef = Field(..., description="LLM to use for answer synthesis and reviews")
    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub URL for memory recall",
    )
    from_agent: str = Field(
        default="architect",
        description="Agent ID for logging and attribution",
    )


@register_function(config_type=ArchitectExpertConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def architect_expert_fn(
    config: ArchitectExpertConfig, builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """Build the architect expert LangGraph pipeline and register as a NAT function."""
    logger.info("[expert_agent:architect] Initializing architect expert agent")

    llm = await builder.get_llm(config.llm_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)
    client = NCMSHttpClient(hub_url=config.hub_url)

    agent = ExpertAgent(
        llm=llm,
        hub_url=config.hub_url,
        from_agent=config.from_agent,
        client=client,
        primary_domain="architecture",
        knowledge_prompt=ARCHITECT_KNOWLEDGE_PROMPT,
        review_prompt=ARCHITECT_REVIEW_PROMPT,
    )
    graph = await agent.build_graph()
    logger.info("[expert_agent:architect] LangGraph pipeline ready")

    async def _expert(input_message: str) -> str:
        """Run the expert pipeline and return the response.

        Called by auto_memory_agent. The returned string gets saved to NCMS
        memory automatically by the auto_memory wrapper.

        Args:
            input_message: The question or design review request.

        Returns:
            The expert answer or structured review.
        """
        logger.info("[expert_agent:architect] === Starting expert pipeline ===")
        logger.info("[expert_agent:architect] Input: %s", input_message[:200])

        project_id = extract_project_id(input_message)
        result = await graph.ainvoke({
            "input": input_message,
            "request_type": "",
            "memory_context": "",
            "review_document": "",
            "response": "",
            "messages": [HumanMessage(content=input_message)],
            "project_id": project_id,
        })

        response = result.get("response", "No response generated.")

        logger.info("[expert_agent:architect] === Pipeline complete ===")
        logger.info("[expert_agent:architect] Response: %d chars", len(response))

        return response

    try:
        yield FunctionInfo.from_fn(
            _expert,
            description=(
                "Architecture expert agent. Answers questions about ADRs, CALM "
                "models, quality attributes, and C4 diagrams grounded in retrieved "
                "knowledge. Performs structured architecture reviews with "
                "SCORE/SEVERITY/COVERED/MISSING/CHANGES format."
            ),
        )
    finally:
        await client.close()
        logger.info("[expert_agent:architect] Cleaned up HTTP client")


class SecurityExpertConfig(FunctionBaseConfig, name="security_expert"):
    """Configuration for the security expert agent."""

    llm_name: LLMRef = Field(..., description="LLM to use for answer synthesis and reviews")
    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub URL for memory recall",
    )
    from_agent: str = Field(
        default="security",
        description="Agent ID for logging and attribution",
    )


@register_function(config_type=SecurityExpertConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def security_expert_fn(
    config: SecurityExpertConfig, builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """Build the security expert LangGraph pipeline and register as a NAT function."""
    logger.info("[expert_agent:security] Initializing security expert agent")

    llm = await builder.get_llm(config.llm_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)
    client = NCMSHttpClient(hub_url=config.hub_url)

    agent = ExpertAgent(
        llm=llm,
        hub_url=config.hub_url,
        from_agent=config.from_agent,
        client=client,
        primary_domain="security",
        knowledge_prompt=SECURITY_KNOWLEDGE_PROMPT,
        review_prompt=SECURITY_REVIEW_PROMPT,
    )
    graph = await agent.build_graph()
    logger.info("[expert_agent:security] LangGraph pipeline ready")

    async def _expert(input_message: str) -> str:
        """Run the expert pipeline and return the response.

        Called by auto_memory_agent. The returned string gets saved to NCMS
        memory automatically by the auto_memory wrapper.

        Args:
            input_message: The question or design review request.

        Returns:
            The expert answer or structured review.
        """
        logger.info("[expert_agent:security] === Starting expert pipeline ===")
        logger.info("[expert_agent:security] Input: %s", input_message[:200])

        project_id = extract_project_id(input_message)
        result = await graph.ainvoke({
            "input": input_message,
            "request_type": "",
            "memory_context": "",
            "review_document": "",
            "response": "",
            "messages": [HumanMessage(content=input_message)],
            "project_id": project_id,
        })

        response = result.get("response", "No response generated.")

        logger.info("[expert_agent:security] === Pipeline complete ===")
        logger.info("[expert_agent:security] Response: %d chars", len(response))

        return response

    try:
        yield FunctionInfo.from_fn(
            _expert,
            description=(
                "Security expert agent. Answers questions about STRIDE threat "
                "models, OWASP Top 10, security controls, and compliance "
                "requirements grounded in retrieved knowledge. Performs structured "
                "security reviews with SCORE/SEVERITY/COVERED/MISSING/CHANGES format."
            ),
        )
    finally:
        await client.close()
        logger.info("[expert_agent:security] Cleaned up HTTP client")
