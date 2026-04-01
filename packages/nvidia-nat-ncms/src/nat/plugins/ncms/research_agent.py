# SPDX-License-Identifier: Apache-2.0
"""LangGraph-based deep market research agent for NAT/NCMS.

Deterministic pipeline: plan_queries → parallel_search → synthesize → publish → verify.
LLM called exactly twice (planning + synthesis). Search and publish are pure Python.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator
from typing import TypedDict

import httpx
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
from .pipeline_utils import extract_project_id, extract_topic, emit_telemetry, build_prd_trigger, check_interrupt
from .research_prompts import PLAN_QUERIES_PROMPT, SYNTHESIZE_PROMPT

logger = logging.getLogger(__name__)


# ── State ─────────────────────────────────────────────────────────────────────


class ResearchState(TypedDict):
    """Graph state for the research pipeline."""

    topic: str  # Original research topic
    search_queries: list[str]  # 5 planned queries
    search_results: list[dict]  # Tavily results per query
    synthesis: str  # Markdown report
    document_id: str | None  # Published doc ID
    messages: list[BaseMessage]  # LangGraph compat
    project_id: str | None  # PRJ-XXXXXXXX for pipeline tracking
    interrupted: bool  # Set by check_interrupt, causes early exit


# ── Agent ─────────────────────────────────────────────────────────────────────


class ResearchAgent:
    """Deterministic LangGraph research pipeline.

    Nodes: plan_queries → parallel_search → synthesize → publish → verify
    LLM called twice: plan_queries and synthesize.
    All other nodes are pure Python.
    """

    def __init__(
        self,
        llm: BaseChatModel,
        hub_url: str,
        from_agent: str,
        tavily_api_key: str,
        client: NCMSHttpClient,
        max_search_results: int = 5,
        trigger_next_agent: bool = True,
    ) -> None:
        self.llm = llm
        self.hub_url = hub_url
        self.from_agent = from_agent
        self.trigger_next_agent = trigger_next_agent
        self.tavily_api_key = tavily_api_key
        self.client = client
        self.max_search_results = max_search_results

    async def _check_and_interrupt(self, state: ResearchState, node: str) -> bool:
        """Check for interrupt signal. Returns True if interrupted."""
        if state.get("interrupted"):
            return True
        if await check_interrupt(self.hub_url, self.from_agent):
            state["interrupted"] = True
            logger.info("[research_agent] Interrupted at node %s", node)
            await emit_telemetry(
                self.hub_url, state.get("project_id"),
                self.from_agent, node, "interrupted",
            )
            return True
        return False

    async def check_guardrails(self, state: ResearchState) -> ResearchState:
        """Check input guardrails before pipeline starts."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "check_guardrails", "started")
        from .guardrails import run_input_guardrails
        can_proceed, violations = await run_input_guardrails(self.hub_url, state["topic"], self.from_agent)
        if not can_proceed:
            logger.warning("[research_agent] Guardrails BLOCKED: %s", violations)
            state["synthesis"] = f"Pipeline blocked by guardrails: {[str(v) for v in violations]}"
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "check_guardrails", "completed")
        return state

    async def build_graph(self) -> StateGraph:
        """Build and compile the deterministic research pipeline."""
        graph = StateGraph(ResearchState)

        graph.add_node("check_guardrails", self.check_guardrails)
        graph.add_node("plan_queries", self.plan_queries)
        graph.add_node("parallel_search", self.parallel_search)
        graph.add_node("synthesize", self.synthesize)
        graph.add_node("publish", self.publish)
        graph.add_node("verify", self.verify)

        # All edges unconditional — deterministic flow
        graph.add_edge(START, "check_guardrails")
        graph.add_edge("check_guardrails", "plan_queries")
        graph.add_edge("plan_queries", "parallel_search")
        graph.add_edge("parallel_search", "synthesize")
        graph.add_edge("synthesize", "publish")
        graph.add_edge("publish", "verify")
        graph.add_edge("verify", END)

        compiled = graph.compile()
        logger.info("[research_agent] Graph compiled: guardrails → plan → search → synthesize → publish → verify")
        return compiled

    # ── Node 1: Plan Queries (LLM) ───────────────────────────────────────

    async def plan_queries(self, state: ResearchState) -> ResearchState:
        """LLM generates 5 search queries covering different research angles."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "plan_queries", "started")
        if await self._check_and_interrupt(state, "plan_queries"):
            return state
        topic = state["topic"]
        logger.info("[research_agent] Planning queries for topic: %s", topic[:100])

        try:
            prompt = PLAN_QUERIES_PROMPT.format(topic=topic)
            response = await self.llm.ainvoke([
                SystemMessage(content="You output only valid JSON arrays. No markdown, no explanation."),
                HumanMessage(content=prompt),
            ])
            text = response.content.strip()

            # Strip markdown code fences if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3].strip()

            queries = json.loads(text)
            if isinstance(queries, list) and len(queries) >= 5:
                state["search_queries"] = [str(q) for q in queries[:5]]
                logger.info("[research_agent] LLM planned %d queries", len(state["search_queries"]))
                for i, q in enumerate(state["search_queries"]):
                    logger.debug("[research_agent]   Query %d: %s", i + 1, q)
                await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "plan_queries", "completed", f"{len(state['search_queries'])} queries planned")
                return state
        except Exception as e:
            logger.warning("[research_agent] LLM query planning failed: %s — using templates", e)

        # Fallback: template-based queries
        state["search_queries"] = [
            f"{topic} overview current landscape 2025 2026",
            f"{topic} industry standards frameworks best practices",
            f"{topic} security compliance regulatory requirements OWASP NIST",
            f"{topic} implementation patterns architecture technology choices",
            f"{topic} case studies real-world examples lessons learned",
        ]
        logger.info("[research_agent] Using %d template queries (fallback)", len(state["search_queries"]))
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "plan_queries", "completed", f"{len(state['search_queries'])} queries (fallback)")
        return state

    # ── Node 2: Parallel Search (Pure Python) ────────────────────────────

    async def parallel_search(self, state: ResearchState) -> ResearchState:
        """Run 5 concurrent Tavily searches. No LLM."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "parallel_search", "started")
        if await self._check_and_interrupt(state, "parallel_search"):
            return state
        queries = state["search_queries"]
        logger.info("[research_agent] Starting %d parallel Tavily searches", len(queries))

        if not self.tavily_api_key:
            logger.error("[research_agent] TAVILY_API_KEY not set — skipping search")
            state["search_results"] = [{"query": q, "results": [], "answer": "No API key"} for q in queries]
            return state

        # Announce progress to hub
        try:
            await self.client.bus_announce(
                content=f"🔍 Starting {len(queries)} parallel web searches for: {state['topic'][:80]}",
                domains=["research", "product"],
                from_agent=self.from_agent,
            )
        except Exception:
            pass  # Non-fatal

        async def _search_one(query: str, index: int) -> dict:
            """Single Tavily search."""
            try:
                async with httpx.AsyncClient(timeout=30.0) as http:
                    resp = await http.post(
                        "https://api.tavily.com/search",
                        headers={
                            "Authorization": f"Bearer {self.tavily_api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "query": query,
                            "search_depth": "basic",
                            "max_results": self.max_search_results,
                            "include_answer": "basic",
                            "include_raw_content": "markdown",
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()

                logger.info(
                    "[research_agent] Search %d/%d complete: %d results for '%s'",
                    index + 1, len(queries), len(data.get("results", [])), query[:60],
                )

                return {
                    "query": query,
                    "answer": data.get("answer", ""),
                    "results": [
                        {
                            "title": r.get("title", ""),
                            "url": r.get("url", ""),
                            "content": r.get("content", "")[:2000],
                        }
                        for r in data.get("results", [])
                    ],
                }
            except Exception as e:
                logger.warning("[research_agent] Search %d failed: %s", index + 1, e)
                return {"query": query, "results": [], "answer": f"Search failed: {e}"}

        # Run all 5 concurrently
        results = await asyncio.gather(
            *[_search_one(q, i) for i, q in enumerate(queries)],
            return_exceptions=False,
        )
        state["search_results"] = list(results)

        total_results = sum(len(r.get("results", [])) for r in results)
        logger.info("[research_agent] All searches complete: %d total results", total_results)

        # Announce completion
        try:
            await self.client.bus_announce(
                content=f"✅ Web research complete: {total_results} results from {len(queries)} searches",
                domains=["research", "product"],
                from_agent=self.from_agent,
            )
        except Exception:
            pass

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "parallel_search", "completed", f"{total_results} results")
        return state

    # ── Node 3: Synthesize (LLM) ─────────────────────────────────────────

    async def synthesize(self, state: ResearchState) -> ResearchState:
        """LLM synthesizes all search results into a structured report."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "synthesize", "started")
        if await self._check_and_interrupt(state, "synthesize"):
            return state
        topic = state["topic"]
        logger.info("[research_agent] Synthesizing report for: %s", topic[:100])

        # Format search results for the prompt
        parts = []
        for i, sr in enumerate(state["search_results"]):
            section = f"### Search {i + 1}: {sr['query']}\n"
            if sr.get("answer"):
                section += f"**Summary:** {sr['answer'][:500]}\n\n"
            for r in sr.get("results", []):
                section += f"- **{r['title']}** ({r['url']})\n  {r['content'][:500]}\n\n"
            parts.append(section)

        search_text = "\n".join(parts)
        # Truncate to fit context window (~20K chars for search content)
        if len(search_text) > 80000:
            search_text = search_text[:80000] + "\n\n[... truncated for context window ...]"

        prompt = SYNTHESIZE_PROMPT.format(topic=topic, search_results=search_text)

        try:
            response = await self.llm.ainvoke([
                SystemMessage(content="You are a thorough market research analyst. Write detailed, cited reports."),
                HumanMessage(content=prompt),
            ])
            state["synthesis"] = response.content
            logger.info("[research_agent] Synthesis complete: %d chars", len(state["synthesis"]))
            logger.debug("[research_agent] Synthesis preview: %s", state["synthesis"][:500])
        except Exception as e:
            logger.error("[research_agent] Synthesis failed: %s", e)
            # Emergency fallback — return raw results as markdown
            state["synthesis"] = f"# {topic} — Research Results (Raw)\n\n{search_text}"

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "synthesize", "completed", f"{len(state['synthesis'])} chars")
        return state

    # ── Node 4: Publish (Pure Python) ─────────────────────────────────────

    async def publish(self, state: ResearchState) -> ResearchState:
        """Publish the report to the NCMS document store. No LLM."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "publish", "started")
        if await self._check_and_interrupt(state, "publish"):
            return state
        topic = state["topic"]
        clean_topic = extract_topic(topic)
        synthesis = state["synthesis"]
        # Tag content with project_id for traceability
        project_id = state.get("project_id")
        if project_id:
            synthesis = f"<!-- project_id: {project_id} -->\n{synthesis}"
        logger.info("[research_agent] Publishing document: %d chars", len(synthesis))

        try:
            result = await self.client.publish_document(
                content=synthesis,
                title=f"{clean_topic} — Market Research Report",
                from_agent=self.from_agent,
                format="markdown",
            )
            doc_id = result.get("document_id", "unknown")
            state["document_id"] = doc_id
            logger.info("[research_agent] ✅ Document published: %s", doc_id)

            # Announce to the bus
            try:
                await self.client.bus_announce(
                    content=(
                        f"📄 Market research report published: {topic}\n"
                        f"Document ID: {doc_id}\n"
                        f"Size: {len(synthesis)} chars"
                    ),
                    domains=["research", "product"],
                    from_agent=self.from_agent,
                )
            except Exception:
                pass

        except Exception as e:
            logger.error("[research_agent] ❌ Publish failed: %s", e)
            state["document_id"] = None

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "publish", "completed", f"doc_id={state.get('document_id')}")
        return state

    # ── Node 5: Verify (Debug — Pure Python) ──────────────────────────────

    async def verify(self, state: ResearchState) -> ResearchState:
        """Verify publication and auto-trigger Product Owner if enabled."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "verify", "started")
        if await self._check_and_interrupt(state, "verify"):
            return state
        topic = state["topic"]
        doc_id = state.get("document_id")

        if doc_id:
            logger.info(
                "[research_agent] ✅ Pipeline complete. Topic: '%s' | Doc: %s | Synthesis: %d chars",
                topic[:60], doc_id, len(state.get("synthesis", "")),
            )
        else:
            logger.warning(
                "[research_agent] ⚠️ Pipeline complete but no document published for: %s",
                topic[:60],
            )

        # Auto-trigger Product Owner (fire-and-forget — don't block the return)
        if self.trigger_next_agent and doc_id:
            import asyncio

            clean_topic = extract_topic(topic)
            logger.info("[research_agent] 🔗 Triggering product_owner (async) with doc_id: %s", doc_id)

            # Announce handoff so it's visible in the dashboard
            try:
                await self.client.bus_announce(
                    content=f"🔗 Handing off to Product Owner → Create PRD from research (doc_id: {doc_id})",
                    domains=["research", "product"],
                    from_agent=self.from_agent,
                )
            except Exception:
                pass

            async def _trigger() -> None:
                try:
                    msg = build_prd_trigger(
                        clean_topic,
                        research_id=doc_id,
                        project_id=state.get("project_id"),
                    )
                    await self.client.bus_announce(
                        content=msg,
                        domains=["trigger-product_owner", "research", "product"],
                        from_agent=self.from_agent,
                    )
                    logger.info("[research_agent] ✅ Product owner triggered via bus announce")
                except Exception as e:
                    logger.warning("[research_agent] ⚠️ Failed to trigger product_owner: %s", e)

            asyncio.create_task(_trigger())

        logger.info(
            "[research_agent] Returning synthesis (%d chars) to auto_memory_agent for persistence",
            len(state.get("synthesis", "")),
        )

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "verify", "completed", f"doc_id={doc_id}")
        return state


# ── NAT Registration ──────────────────────────────────────────────────────────


class ResearchAgentConfig(FunctionBaseConfig, name="research_agent"):
    """Configuration for the LangGraph research agent."""

    llm_name: LLMRef = Field(..., description="LLM to use for query planning and synthesis")
    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub URL for document publishing and bus announcements",
    )
    from_agent: str = Field(
        default="researcher",
        description="Agent ID for bus announcements and document attribution",
    )
    max_search_results: int = Field(
        default=5,
        description="Max results per Tavily search query",
    )
    trigger_next_agent: bool = Field(
        default=True,
        description="Auto-trigger product_owner after research completes",
    )


@register_function(config_type=ResearchAgentConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def research_agent_fn(
    config: ResearchAgentConfig, builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """Build the LangGraph research pipeline and register as a NAT function."""
    logger.info("[research_agent] Initializing LangGraph research agent")

    # Get LangChain-compatible LLM from NAT builder
    llm = await builder.get_llm(config.llm_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)
    tavily_api_key = os.environ.get("TAVILY_API_KEY", "")
    client = NCMSHttpClient(hub_url=config.hub_url)

    if not tavily_api_key:
        logger.warning("[research_agent] TAVILY_API_KEY not set — searches will fail")

    # Build the LangGraph pipeline
    agent = ResearchAgent(
        llm=llm,
        hub_url=config.hub_url,
        from_agent=config.from_agent,
        tavily_api_key=tavily_api_key,
        client=client,
        max_search_results=config.max_search_results,
        trigger_next_agent=config.trigger_next_agent,
    )
    graph = await agent.build_graph()
    logger.info("[research_agent] LangGraph pipeline ready")

    async def _research(input_message: str) -> str:
        """Run the full research pipeline and return the synthesis.

        This is the function that auto_memory_agent calls. The returned
        string (the full markdown report) gets saved to NCMS memory
        automatically by the auto_memory wrapper.

        Args:
            input_message: The research topic from the user.

        Returns:
            The synthesized markdown research report.
        """
        logger.info("[research_agent] === Starting research pipeline ===")
        logger.info("[research_agent] Topic: %s", input_message[:200])

        project_id = extract_project_id(input_message)
        result = await graph.ainvoke({
            "topic": input_message,
            "search_queries": [],
            "search_results": [],
            "synthesis": "",
            "document_id": None,
            "messages": [HumanMessage(content=input_message)],
            "project_id": project_id,
            "interrupted": False,
        }, config={"recursion_limit": 30})

        synthesis = result.get("synthesis", "Research pipeline produced no output.")
        doc_id = result.get("document_id")

        logger.info("[research_agent] === Pipeline complete ===")
        logger.info("[research_agent] Synthesis: %d chars | Doc ID: %s", len(synthesis), doc_id)
        logger.info("[research_agent] Returning to auto_memory for persistence")

        return synthesis

    try:
        yield FunctionInfo.from_fn(
            _research,
            description=(
                "Deep market research agent. Runs 5 parallel web searches "
                "covering broad overview, industry standards, security/compliance, "
                "implementation patterns, and case studies. Synthesizes results "
                "into a structured markdown report and publishes to the document "
                "store. Returns the full report."
            ),
        )
    finally:
        await client.close()
        logger.info("[research_agent] Cleaned up HTTP client")
