# SPDX-License-Identifier: Apache-2.0
"""LangGraph-based Archeologist agent for NAT/NCMS.

Combined pipeline handling both research and archaeology paths:

  START -> check_guardrails -> [conditional: _route_after_guardrails]
    |-- "research"    -> plan_queries -> parallel_search -> arxiv_search
    |                    -> synthesize_research -> publish -> verify_and_trigger -> END
    |-- "archaeology" -> clone_and_index -> analyze_architecture -> identify_gaps
    |                    -> web_research -> synthesize_report -> publish -> verify_and_trigger -> END
    |-- "blocked"     -> END

Research path: LLM called twice (plan_queries + synthesize_research).
Archaeology path: LLM called 3 times (analyze + gaps + synthesize_report).
All other nodes are pure Python/API calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator
from typing import Any, TypedDict

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

from .github_provider import GitHubProvider
from .http_client import NCMSHttpClient
from .pipeline_utils import (
    build_prd_trigger,
    check_interrupt,
    emit_telemetry,
    extract_goal,
    extract_project_id,
    extract_repo_url,
    extract_topic,
    snapshot_agent_config,
    traced_llm_call,
)
from .archeologist_prompts import (
    ANALYZE_ARCHITECTURE_PROMPT,
    IDENTIFY_GAPS_PROMPT,
    SYNTHESIZE_REPORT_PROMPT,
)
from .research_prompts import PLAN_QUERIES_PROMPT, SYNTHESIZE_PROMPT

logger = logging.getLogger(__name__)


# -- State -------------------------------------------------------------------


class ArcheologistState(TypedDict):
    """Graph state for the combined archeologist pipeline (research + archaeology)."""

    # Routing
    source_type: str  # "research" or "archaeology"

    # Shared
    topic: str
    synthesis: str
    document_id: str | None
    messages: list[BaseMessage]
    project_id: str | None
    interrupted: bool

    # Research path
    search_queries: list[str]
    search_results: list[dict]
    arxiv_results: list[dict]

    # Archaeology path
    repository_url: str
    project_goal: str
    repo_info: dict
    file_tree: list[dict]
    key_files: dict[str, str]
    architecture_analysis: str
    gap_analysis: str
    web_research: list[dict]


# -- Agent -------------------------------------------------------------------


class ArcheologistAgent:
    """Combined LangGraph pipeline: research + archaeology.

    Routes via source_type after guardrails:
      "research"    -> plan_queries -> parallel_search -> arxiv_search
                       -> synthesize_research -> publish -> verify_and_trigger
      "archaeology" -> clone_and_index -> analyze_architecture -> identify_gaps
                       -> web_research -> synthesize_report -> publish -> verify_and_trigger
    """

    def __init__(
        self,
        llm: BaseChatModel,
        hub_url: str,
        from_agent: str,
        tavily_api_key: str,
        client: NCMSHttpClient,
        github: GitHubProvider | None = None,
        max_search_results: int = 5,
        trigger_next_agent: bool = True,
    ) -> None:
        self.llm = llm
        self.hub_url = hub_url
        self.from_agent = from_agent
        self.trigger_next_agent = trigger_next_agent
        self.tavily_api_key = tavily_api_key
        self.client = client
        self.github = github
        self.max_search_results = max_search_results

    async def _check_and_interrupt(self, state: ArcheologistState, node: str) -> bool:
        """Check for interrupt signal. Returns True if interrupted."""
        if state.get("interrupted"):
            return True
        if await check_interrupt(self.hub_url, self.from_agent):
            state["interrupted"] = True
            logger.info("[archeologist] Interrupted at node %s", node)
            await emit_telemetry(
                self.hub_url, state.get("project_id"),
                self.from_agent, node, "interrupted",
            )
            return True
        return False

    # -- Graph builder -------------------------------------------------------

    async def build_graph(self) -> StateGraph:
        """Build and compile the combined research + archaeology pipeline."""
        graph = StateGraph(ArcheologistState)

        # Shared nodes
        graph.add_node("check_guardrails", self.check_guardrails)
        graph.add_node("publish", self.publish)
        graph.add_node("verify_and_trigger", self.verify_and_trigger)

        # Research path nodes
        graph.add_node("plan_queries", self.plan_queries)
        graph.add_node("parallel_search", self.parallel_search)
        graph.add_node("arxiv_search", self.arxiv_search)
        graph.add_node("synthesize_research", self.synthesize_research)

        # Archaeology path nodes
        graph.add_node("clone_and_index", self.clone_and_index)
        graph.add_node("analyze_architecture", self.analyze_architecture)
        graph.add_node("identify_gaps", self.identify_gaps)
        graph.add_node("web_research", self.web_research)
        graph.add_node("synthesize_report", self.synthesize_report)

        # Edges
        graph.add_edge(START, "check_guardrails")
        graph.add_conditional_edges(
            "check_guardrails",
            self._route_after_guardrails,
            {
                "research": "plan_queries",
                "archaeology": "clone_and_index",
                "blocked": END,
            },
        )

        # Research path edges
        graph.add_edge("plan_queries", "parallel_search")
        graph.add_edge("parallel_search", "arxiv_search")
        graph.add_edge("arxiv_search", "synthesize_research")
        graph.add_edge("synthesize_research", "publish")

        # Archaeology path edges
        graph.add_edge("clone_and_index", "analyze_architecture")
        graph.add_edge("analyze_architecture", "identify_gaps")
        graph.add_edge("identify_gaps", "web_research")
        graph.add_edge("web_research", "synthesize_report")
        graph.add_edge("synthesize_report", "publish")

        # Shared tail
        graph.add_edge("publish", "verify_and_trigger")
        graph.add_edge("verify_and_trigger", END)

        compiled = graph.compile()
        logger.info(
            "[archeologist] Graph compiled: guardrails -> "
            "[research: plan -> search -> arxiv -> synthesize] | "
            "[archaeology: clone -> analyze -> gaps -> research -> synthesize] "
            "-> publish -> verify"
        )
        return compiled

    def _route_after_guardrails(self, state: ArcheologistState) -> str:
        """Route after guardrails: research, archaeology, or blocked."""
        if state.get("interrupted"):
            return "blocked"
        synthesis = state.get("synthesis", "")
        if synthesis.startswith("Pipeline blocked") or synthesis.startswith("Pipeline denied"):
            return "blocked"
        if state.get("source_type") == "archaeology":
            return "archaeology"
        return "research"

    # ========================================================================
    # SHARED NODES
    # ========================================================================

    # -- Node: Check Guardrails ----------------------------------------------

    async def check_guardrails(self, state: ArcheologistState) -> ArcheologistState:
        """Check input guardrails. Block/reject violations pause for human approval."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "check_guardrails", "started",
        )
        await snapshot_agent_config(self.client, state.get("project_id"), self.from_agent, self.llm)
        from .guardrails import request_approval, run_input_guardrails, wait_for_approval

        # Use topic for research path, project_goal for archaeology path
        check_text = state.get("project_goal", "") or state.get("topic", "")
        can_proceed, violations = await run_input_guardrails(
            self.hub_url, check_text, self.from_agent,
        )
        if not can_proceed:
            logger.warning("[archeologist] Guardrails BLOCKED: %s", violations)
            blocking = [v for v in violations if v.escalation in ("block", "reject")]
            if blocking:
                await emit_telemetry(
                    self.hub_url, state.get("project_id"), self.from_agent,
                    "check_guardrails", "awaiting_approval",
                    f"{len(blocking)} violation(s) require human approval",
                )
                approval_id = await request_approval(
                    self.hub_url, self.from_agent, "check_guardrails",
                    state.get("project_id"), blocking,
                    context={"topic": check_text},
                )
                if approval_id:
                    decision, comment = await wait_for_approval(
                        self.hub_url, approval_id, self.from_agent,
                    )
                    if decision == "approved":
                        logger.info("[archeologist] Human approved guardrail override")
                        can_proceed = True
                    else:
                        state["synthesis"] = f"Pipeline denied by human: {decision}"
                        state["interrupted"] = True
                        await emit_telemetry(
                            self.hub_url, state.get("project_id"), self.from_agent,
                            "check_guardrails", "denied", f"Human denied: {comment or decision}",
                        )
                        return state
                else:
                    state["synthesis"] = f"Pipeline blocked by guardrails: {[str(v) for v in violations]}"
            else:
                state["synthesis"] = f"Pipeline blocked by guardrails: {[str(v) for v in violations]}"
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "check_guardrails", "completed",
        )
        return state

    # -- Node: Publish (shared) ----------------------------------------------

    async def publish(self, state: ArcheologistState) -> ArcheologistState:
        """Publish the report to the NCMS document store. No LLM."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "publish", "started",
        )
        if await self._check_and_interrupt(state, "publish"):
            return state

        synthesis = state.get("synthesis", "")
        if not synthesis or synthesis.startswith("Pipeline blocked") or synthesis.startswith("Pipeline denied"):
            logger.warning("[archeologist] No synthesis to publish")
            await emit_telemetry(
                self.hub_url, state.get("project_id"),
                self.from_agent, "publish", "completed", "skipped",
            )
            return state

        # Derive title from topic
        topic = state.get("topic", "")
        source_type = state.get("source_type", "research")
        if source_type == "archaeology":
            repo_name = state.get("repo_info", {}).get("name", "unknown")
            title = f"Archaeology: {repo_name}"
        else:
            clean_topic = extract_topic(topic)
            title = f"{clean_topic} — Market Research Report"

        # Embed project_id in content for traceability
        project_id = state.get("project_id")
        content = synthesis
        if project_id:
            content = f"<!-- project_id: {project_id} -->\n{content}"

        logger.info("[archeologist] Publishing document: %d chars", len(content))

        try:
            result = await self.client.publish_document(
                content=content,
                title=title,
                from_agent=self.from_agent,
                doc_type="research",
                format="markdown",
            )
            doc_id = result.get("document_id", "unknown")
            state["document_id"] = doc_id
            logger.info("[archeologist] Report published: %s (%d chars)", doc_id, len(content))

            # Announce
            try:
                await self.client.bus_announce(
                    content=(
                        f"Report published: {title}\n"
                        f"Document ID: {doc_id} | Size: {len(content)} chars"
                    ),
                    domains=["archaeology", "research", "product"],
                    from_agent=self.from_agent,
                )
            except Exception:
                pass

        except Exception as e:
            logger.error("[archeologist] Failed to publish report: %s", e)
            state["document_id"] = None
            await emit_telemetry(
                self.hub_url, state.get("project_id"),
                self.from_agent, "publish", "failed", str(e),
            )
            return state

        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "publish", "completed",
            f"doc_id={state.get('document_id')}",
        )
        return state

    # -- Node: Verify & Trigger (shared) -------------------------------------

    async def verify_and_trigger(self, state: ArcheologistState) -> ArcheologistState:
        """Verify publication and auto-trigger Product Owner if enabled."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "verify_and_trigger", "started",
        )
        if await self._check_and_interrupt(state, "verify_and_trigger"):
            return state

        topic = state.get("topic", "")
        doc_id = state.get("document_id")

        if doc_id:
            logger.info(
                "[archeologist] Pipeline complete. Topic: '%s' | Doc: %s | Synthesis: %d chars",
                topic[:60], doc_id, len(state.get("synthesis", "")),
            )
        else:
            logger.warning(
                "[archeologist] Pipeline complete but no document published for: %s",
                topic[:60],
            )

        # Auto-trigger Product Owner (fire-and-forget)
        if self.trigger_next_agent and doc_id:
            source_type = state.get("source_type", "research")
            if source_type == "archaeology":
                clean_goal = state.get("project_goal", "") or state.get("repo_info", {}).get("name", topic)
            else:
                clean_goal = extract_topic(topic)
            project_id = state.get("project_id")

            logger.info("[archeologist] Triggering product_owner (async) with doc_id: %s", doc_id)

            # Announce handoff so it's visible in the dashboard
            try:
                await self.client.bus_announce(
                    content=f"Handing off to Product Owner -> Create PRD from research (doc_id: {doc_id})",
                    domains=["research", "product"],
                    from_agent=self.from_agent,
                )
            except Exception:
                pass

            async def _trigger() -> None:
                try:
                    msg = build_prd_trigger(
                        clean_goal,
                        research_id=doc_id,
                        project_id=project_id,
                    )
                    await self.client.bus_announce(
                        content=msg,
                        domains=["trigger-product_owner", "archaeology", "research", "product"],
                        from_agent=self.from_agent,
                    )
                    logger.info(
                        "[archeologist] Triggered product_owner: %s", msg[:120],
                    )
                except Exception as e:
                    logger.warning("[archeologist] Failed to trigger PO: %s", e)

            asyncio.create_task(_trigger())

        logger.info(
            "[archeologist] Returning synthesis (%d chars) to auto_memory_agent for persistence",
            len(state.get("synthesis", "")),
        )

        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "verify_and_trigger", "completed",
            f"doc_id={doc_id}",
        )
        return state

    # ========================================================================
    # RESEARCH PATH NODES
    # ========================================================================

    # -- Node: Plan Queries (LLM) -------------------------------------------

    async def plan_queries(self, state: ArcheologistState) -> ArcheologistState:
        """LLM generates 5 search queries covering different research angles."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "plan_queries", "started",
        )
        if await self._check_and_interrupt(state, "plan_queries"):
            return state
        topic = state["topic"]
        logger.info("[archeologist/research] Planning queries for topic: %s", topic[:100])

        try:
            prompt = PLAN_QUERIES_PROMPT.format(topic=topic)
            response = await traced_llm_call(
                self.llm, [
                    SystemMessage(content="You output only valid JSON arrays. No markdown, no explanation."),
                    HumanMessage(content=prompt),
                ],
                hub_url=self.hub_url, client=self.client,
                project_id=state.get("project_id"),
                agent=self.from_agent, node="plan_queries",
            )
            text = response.content.strip()

            # Strip markdown code fences if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3].strip()

            queries = json.loads(text)
            if isinstance(queries, list) and len(queries) >= 5:
                state["search_queries"] = [str(q) for q in queries[:5]]
                logger.info("[archeologist/research] LLM planned %d queries", len(state["search_queries"]))
                for i, q in enumerate(state["search_queries"]):
                    logger.debug("[archeologist/research]   Query %d: %s", i + 1, q)
                await emit_telemetry(
                    self.hub_url, state.get("project_id"),
                    self.from_agent, "plan_queries", "completed",
                    f"{len(state['search_queries'])} queries planned",
                )
                return state
        except Exception as e:
            logger.warning("[archeologist/research] LLM query planning failed: %s — using templates", e)

        # Fallback: template-based queries
        state["search_queries"] = [
            f"{topic} overview current landscape 2025 2026",
            f"{topic} industry standards frameworks best practices",
            f"{topic} security compliance regulatory requirements OWASP NIST",
            f"{topic} implementation patterns architecture technology choices",
            f"{topic} case studies real-world examples lessons learned",
        ]
        logger.info("[archeologist/research] Using %d template queries (fallback)", len(state["search_queries"]))
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "plan_queries", "completed",
            f"{len(state['search_queries'])} queries (fallback)",
        )
        return state

    # -- Node: Parallel Search (Pure Python) ---------------------------------

    async def parallel_search(self, state: ArcheologistState) -> ArcheologistState:
        """Run 5 concurrent Tavily searches. No LLM."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "parallel_search", "started",
        )
        if await self._check_and_interrupt(state, "parallel_search"):
            return state
        queries = state["search_queries"]
        logger.info("[archeologist/research] Starting %d parallel Tavily searches", len(queries))

        if not self.tavily_api_key:
            logger.error("[archeologist/research] TAVILY_API_KEY not set — skipping search")
            state["search_results"] = [{"query": q, "results": [], "answer": "No API key"} for q in queries]
            return state

        # Announce progress to hub
        try:
            await self.client.bus_announce(
                content=f"Starting {len(queries)} parallel web searches for: {state['topic'][:80]}",
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
                    "[archeologist/research] Search %d/%d complete: %d results for '%s'",
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
                logger.warning("[archeologist/research] Search %d failed: %s", index + 1, e)
                return {"query": query, "results": [], "answer": f"Search failed: {e}"}

        # Run all concurrently
        results = await asyncio.gather(
            *[_search_one(q, i) for i, q in enumerate(queries)],
            return_exceptions=False,
        )
        state["search_results"] = list(results)

        total_results = sum(len(r.get("results", [])) for r in results)
        logger.info("[archeologist/research] All searches complete: %d total results", total_results)

        # Announce completion
        try:
            await self.client.bus_announce(
                content=f"Web research complete: {total_results} results from {len(queries)} searches",
                domains=["research", "product"],
                from_agent=self.from_agent,
            )
        except Exception:
            pass

        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "parallel_search", "completed",
            f"{total_results} results",
        )
        return state

    # -- Node: ArXiv Search (Pure Python) ------------------------------------

    async def arxiv_search(self, state: ArcheologistState) -> ArcheologistState:
        """Search ArXiv for recent academic papers on the topic. No LLM."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "arxiv_search", "started",
        )
        if await self._check_and_interrupt(state, "arxiv_search"):
            return state

        topic = state["topic"]
        state["arxiv_results"] = []

        try:
            import arxiv as _arxiv
            from datetime import datetime, timedelta, timezone

            # Generate 3 academic-focused queries from the topic
            arxiv_queries = [
                f"{topic} formal verification security",
                f"{topic} architecture benchmark evaluation",
                f"{topic} zero trust access control protocol",
            ]

            cutoff = datetime.now(timezone.utc) - timedelta(days=365)
            all_papers: list[dict] = []

            for q in arxiv_queries:
                try:
                    search = _arxiv.Search(
                        query=q,
                        max_results=20,
                        sort_by=_arxiv.SortCriterion.Relevance,
                    )
                    client = _arxiv.Client()
                    for paper in client.results(search):
                        if paper.published and paper.published < cutoff:
                            continue
                        all_papers.append({
                            "title": paper.title,
                            "url": paper.entry_id,
                            "summary": paper.summary[:1500],
                            "published": paper.published.isoformat() if paper.published else "",
                            "authors": ", ".join(a.name for a in paper.authors[:3]),
                        })
                        if len(all_papers) >= 5:
                            break
                except Exception as e:
                    logger.warning("[archeologist/research] ArXiv query failed: %s", e)

                if len(all_papers) >= 5:
                    break

            state["arxiv_results"] = all_papers
            logger.info("[archeologist/research] ArXiv: %d papers found (last 12 months)", len(all_papers))

            if all_papers:
                try:
                    titles = ", ".join(p["title"][:50] for p in all_papers[:3])
                    await self.client.bus_announce(
                        content=f"ArXiv papers found: {len(all_papers)} — {titles}",
                        domains=["research", "product"],
                        from_agent=self.from_agent,
                    )
                except Exception:
                    pass

        except ImportError as e:
            logger.warning("[archeologist/research] arxiv import failed: %s — skipping academic search", e)
        except Exception as e:
            logger.warning("[archeologist/research] ArXiv search failed: %s", e, exc_info=True)

        await emit_telemetry(
            self.hub_url, state.get("project_id"), self.from_agent,
            "arxiv_search", "completed", f"{len(state['arxiv_results'])} papers",
        )
        return state

    # -- Node: Synthesize Research (LLM) -------------------------------------

    async def synthesize_research(self, state: ArcheologistState) -> ArcheologistState:
        """LLM synthesizes all search results into a structured report."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "synthesize_research", "started",
        )
        if await self._check_and_interrupt(state, "synthesize_research"):
            return state
        topic = state["topic"]
        logger.info("[archeologist/research] Synthesizing report for: %s", topic[:100])

        # Format search results for the prompt
        parts = []
        for i, sr in enumerate(state["search_results"]):
            section = f"### Search {i + 1}: {sr['query']}\n"
            if sr.get("answer"):
                section += f"**Summary:** {sr['answer'][:500]}\n\n"
            for r in sr.get("results", []):
                section += f"- **{r['title']}** ({r['url']})\n  {r['content'][:500]}\n\n"
            parts.append(section)

        # Append ArXiv papers if found
        arxiv_papers = state.get("arxiv_results", [])
        if arxiv_papers:
            parts.append("\n---\n\n## Academic Papers (ArXiv — last 12 months)\n")
            for i, p in enumerate(arxiv_papers, 1):
                parts.append(
                    f"### Paper {i}: {p['title']}\n"
                    f"**Authors:** {p.get('authors', 'Unknown')}\n"
                    f"**Published:** {p.get('published', '')[:10]}\n"
                    f"**URL:** {p['url']}\n"
                    f"{p['summary'][:1000]}\n"
                )
            logger.info("[archeologist/research] Added %d ArXiv papers to synthesis input", len(arxiv_papers))

        search_text = "\n".join(parts)
        # Truncate to fit context window
        if len(search_text) > 80000:
            search_text = search_text[:80000] + "\n\n[... truncated for context window ...]"

        prompt = SYNTHESIZE_PROMPT.format(topic=topic, search_results=search_text)

        # Enable thinking for deeper synthesis
        llm = self.llm
        if hasattr(llm, 'bind'):
            llm = llm.bind(extra_body={"chat_template_kwargs": {"enable_thinking": True}})

        try:
            response = await traced_llm_call(
                llm, [
                    SystemMessage(content=(
                        "You are a thorough market research analyst using semi-formal "
                        "certificate format. Follow the structure exactly. Every claim "
                        "must trace to a specific source."
                    )),
                    HumanMessage(content=prompt),
                ],
                hub_url=self.hub_url, client=self.client,
                project_id=state.get("project_id"),
                agent=self.from_agent, node="synthesize_research",
            )
            state["synthesis"] = response.content

            # Validate CoT reasoning was used (if configured)
            reasoning = getattr(response, "reasoning_content", None)
            if not reasoning:
                reasoning = (response.response_metadata or {}).get("reasoning_content", "")
            if reasoning:
                logger.info(
                    "[archeologist/research] CoT reasoning: %d chars, synthesis: %d chars",
                    len(reasoning), len(state["synthesis"]),
                )
            else:
                logger.warning(
                    "[archeologist/research] No reasoning_content detected — "
                    "CoT may not be enabled or reasoning parser not active"
                )
            logger.info("[archeologist/research] Synthesis complete: %d chars", len(state["synthesis"]))
        except Exception as e:
            logger.error("[archeologist/research] Synthesis failed: %s", e)
            # Emergency fallback — return raw results as markdown
            state["synthesis"] = f"# {topic} — Research Results (Raw)\n\n{search_text}"

        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "synthesize_research", "completed",
            f"{len(state['synthesis'])} chars",
        )
        return state

    # ========================================================================
    # ARCHAEOLOGY PATH NODES
    # ========================================================================

    # -- Node: Clone & Index (Pure Python) -----------------------------------

    async def clone_and_index(self, state: ArcheologistState) -> ArcheologistState:
        """Fetch repo structure and read key files via GitHub API. No LLM."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "clone_and_index", "started",
        )
        if await self._check_and_interrupt(state, "clone_and_index"):
            return state
        repo_url = state["repository_url"]
        logger.info("[archeologist] Indexing repository: %s", repo_url)

        if not self.github:
            logger.error("[archeologist] GitHubProvider not configured — cannot index repo")
            state["synthesis"] = "Pipeline blocked: GitHubProvider not configured for archaeology"
            await emit_telemetry(
                self.hub_url, state.get("project_id"),
                self.from_agent, "clone_and_index", "failed", "no GitHubProvider",
            )
            return state

        try:
            owner, repo = GitHubProvider.parse_repo_url(repo_url)
        except ValueError as e:
            logger.error("[archeologist] Invalid repo URL: %s", e)
            state["synthesis"] = f"Invalid repository URL: {repo_url}"
            await emit_telemetry(
                self.hub_url, state.get("project_id"),
                self.from_agent, "clone_and_index", "failed", str(e),
            )
            return state

        # Get repo info
        try:
            info = await self.github.get_repo_info(owner, repo)
            state["repo_info"] = info
            branch = info.get("default_branch", "main")
            logger.info(
                "[archeologist] Repo: %s | Lang: %s | Branch: %s",
                info.get("name"), info.get("language"), branch,
            )
        except Exception as e:
            logger.error("[archeologist] Failed to get repo info: %s", e)
            state["repo_info"] = {"name": f"{owner}/{repo}", "default_branch": "main"}
            branch = "main"

        # Get file tree
        try:
            tree = await self.github.get_tree(owner, repo, branch)
            state["file_tree"] = tree
            logger.info("[archeologist] File tree: %d entries", len(tree))
        except Exception as e:
            logger.warning("[archeologist] Failed to get file tree: %s", e)
            state["file_tree"] = []

        # Read key files + pattern-matched source files
        try:
            key_files = await self.github.read_key_files(owner, repo, branch, state["file_tree"])
            state["key_files"] = key_files
            logger.info("[archeologist] Read %d key files (%d chars total)",
                        len(key_files), sum(len(v) for v in key_files.values()))
        except Exception as e:
            logger.warning("[archeologist] Failed to read key files: %s", e)
            state["key_files"] = {}

        # Announce progress
        try:
            await self.client.bus_announce(
                content=(
                    f"Repository indexed: {owner}/{repo} — "
                    f"{len(state['file_tree'])} files, "
                    f"{len(state['key_files'])} key files read"
                ),
                domains=["archaeology", "research"],
                from_agent=self.from_agent,
            )
        except Exception:
            pass

        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "clone_and_index", "completed",
            f"{len(state['file_tree'])} files, {len(state['key_files'])} key files",
        )
        return state

    # -- Node: Analyze Architecture (LLM) ------------------------------------

    async def analyze_architecture(self, state: ArcheologistState) -> ArcheologistState:
        """LLM analyzes the codebase structure and architecture."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "analyze_architecture", "started",
        )
        if await self._check_and_interrupt(state, "analyze_architecture"):
            return state
        repo_name = state.get("repo_info", {}).get("name", "unknown")
        logger.info("[archeologist] Analyzing architecture of %s", repo_name)

        # Format file tree (compact)
        tree_lines = []
        for item in state.get("file_tree", [])[:200]:
            prefix = "  " if item["type"] == "blob" else "+ "
            size = f" ({item['size']}B)" if item.get("size") else ""
            tree_lines.append(f"{prefix}{item['path']}{size}")
        file_tree_str = "\n".join(tree_lines) if tree_lines else "(empty)"

        # Format key files content (truncate each to fit context)
        key_files_parts = []
        for path, content in state.get("key_files", {}).items():
            key_files_parts.append(f"--- {path} ---\n{content[:4000]}")
        key_files_str = "\n\n".join(key_files_parts) if key_files_parts else "(none)"

        # Dependencies (extracted from key files)
        dep_files = ["package.json", "requirements.txt", "Pipfile", "pyproject.toml",
                      "go.mod", "Cargo.toml", "pom.xml", "build.gradle"]
        dep_parts = []
        for df in dep_files:
            if df in state.get("key_files", {}):
                dep_parts.append(f"--- {df} ---\n{state['key_files'][df][:3000]}")
        deps_str = "\n\n".join(dep_parts) if dep_parts else "(no dependency files found)"

        # Recent commits (fetch from GitHub)
        commits_str = "(unavailable)"
        try:
            owner, repo = GitHubProvider.parse_repo_url(state["repository_url"])
            branch = state.get("repo_info", {}).get("default_branch", "main")
            commits = await self.github.get_recent_commits(owner, repo, branch, 15)
            if commits:
                commits_str = "\n".join(
                    f"- {c['sha']} {c['message']} ({c['author']}, {c['date'][:10]})"
                    for c in commits
                )
        except Exception as e:
            logger.debug("[archeologist] Failed to get commits: %s", e)

        prompt = ANALYZE_ARCHITECTURE_PROMPT.format(
            repo_name=repo_name,
            project_goal=state.get("project_goal", ""),
            file_tree=file_tree_str[:15000],
            key_files_content=key_files_str[:40000],
            dependencies=deps_str[:8000],
            recent_commits=commits_str[:3000],
        )

        try:
            response = await traced_llm_call(
                self.llm, [
                    SystemMessage(content="You are a senior software architect analyzing a codebase."),
                    HumanMessage(content=prompt),
                ],
                hub_url=self.hub_url, client=self.client,
                project_id=state.get("project_id"),
                agent=self.from_agent, node="analyze_architecture",
            )
            state["architecture_analysis"] = response.content.strip()
            logger.info(
                "[archeologist] Architecture analysis: %d chars",
                len(state["architecture_analysis"]),
            )
        except Exception as e:
            logger.error("[archeologist] Architecture analysis LLM failed: %s", e)
            state["architecture_analysis"] = f"Analysis failed: {e}"

        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "analyze_architecture", "completed",
            f"{len(state.get('architecture_analysis', ''))} chars",
        )
        return state

    # -- Node: Identify Gaps (LLM) -------------------------------------------

    async def identify_gaps(self, state: ArcheologistState) -> ArcheologistState:
        """LLM compares current state against goal, cross-refs NCMS knowledge."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "identify_gaps", "started",
        )
        if await self._check_and_interrupt(state, "identify_gaps"):
            return state
        repo_name = state.get("repo_info", {}).get("name", "unknown")
        logger.info("[archeologist] Identifying gaps for %s", repo_name)

        # Search NCMS memory for relevant governance knowledge
        ncms_knowledge = "(no knowledge available)"
        try:
            desc = state.get("repo_info", {}).get("description", "")
            goal = state.get("project_goal", "")
            query = f"{desc} {goal} architecture security patterns best practices"
            results = await self.client.recall_memory(
                query=query[:3000], domain=None, limit=10,
            )
            if results:
                parts = []
                for i, r in enumerate(results):
                    content = r.get("content", "") if isinstance(r, dict) else str(r)
                    parts.append(f"[{i + 1}] {content[:1000]}")
                ncms_knowledge = "\n\n".join(parts)
                logger.info("[archeologist] Retrieved %d NCMS memories for gap analysis", len(results))
        except Exception as e:
            logger.debug("[archeologist] NCMS recall failed: %s", e)

        # Get open issues for context
        issues_str = "(unavailable)"
        try:
            owner, repo = GitHubProvider.parse_repo_url(state["repository_url"])
            issues = await self.github.get_issues(owner, repo, "open", 15)
            if issues:
                issues_str = "\n".join(
                    f"- #{i['number']}: {i['title']} [{', '.join(i['labels'][:3])}]"
                    for i in issues
                )
        except Exception as e:
            logger.debug("[archeologist] Failed to get issues: %s", e)

        prompt = IDENTIFY_GAPS_PROMPT.format(
            repo_name=repo_name,
            project_goal=state.get("project_goal", ""),
            architecture_analysis=state.get("architecture_analysis", "")[:30000],
            ncms_knowledge=ncms_knowledge[:15000],
            open_issues=issues_str[:5000],
        )

        try:
            response = await traced_llm_call(
                self.llm, [
                    SystemMessage(content="You are an expert code reviewer with governance knowledge."),
                    HumanMessage(content=prompt),
                ],
                hub_url=self.hub_url, client=self.client,
                project_id=state.get("project_id"),
                agent=self.from_agent, node="identify_gaps",
            )
            state["gap_analysis"] = response.content.strip()
            logger.info("[archeologist] Gap analysis: %d chars", len(state["gap_analysis"]))
        except Exception as e:
            logger.error("[archeologist] Gap analysis LLM failed: %s", e)
            state["gap_analysis"] = f"Gap analysis failed: {e}"

        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "identify_gaps", "completed",
            f"{len(state.get('gap_analysis', ''))} chars",
        )
        return state

    # -- Node: Web Research (Pure Python) ------------------------------------

    async def web_research(self, state: ArcheologistState) -> ArcheologistState:
        """Targeted Tavily searches grounded in codebase understanding. No LLM."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "web_research", "started",
        )
        if await self._check_and_interrupt(state, "web_research"):
            return state

        if not self.tavily_api_key:
            logger.warning("[archeologist] TAVILY_API_KEY not set — skipping web research")
            state["web_research"] = []
            await emit_telemetry(
                self.hub_url, state.get("project_id"),
                self.from_agent, "web_research", "completed", "skipped (no API key)",
            )
            return state

        # Build targeted queries from architecture analysis
        repo_info = state.get("repo_info", {})
        lang = repo_info.get("language", "")
        goal = state.get("project_goal", "")

        # Generate 3 targeted queries (grounded in what we found in the code)
        queries = [
            f"{lang} {goal} best practices architecture patterns 2025 2026",
            f"{lang} migration modernization guide security compliance",
            f"{goal} implementation examples case studies production",
        ]

        results = []
        async with httpx.AsyncClient(timeout=30.0) as http:
            tasks = []
            for q in queries:
                tasks.append(self._tavily_search(http, q))
            search_results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, sr in enumerate(search_results):
                if isinstance(sr, Exception):
                    logger.warning("[archeologist] Search %d failed: %s", i + 1, sr)
                    results.append({"query": queries[i], "results": [], "error": str(sr)})
                else:
                    results.append(sr)

        state["web_research"] = results
        total = sum(len(r.get("results", [])) for r in results)
        logger.info("[archeologist] Web research: %d queries, %d total results", len(queries), total)

        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "web_research", "completed",
            f"{len(queries)} queries, {total} results",
        )
        return state

    async def _tavily_search(self, http: httpx.AsyncClient, query: str) -> dict[str, Any]:
        """Single Tavily search (used by archaeology web_research node)."""
        resp = await http.post(
            "https://api.tavily.com/search",
            json={
                "query": query,
                "search_depth": "basic",
                "max_results": self.max_search_results,
                "include_answer": "basic",
            },
            headers={"Authorization": f"Bearer {self.tavily_api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
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

    # -- Node: Synthesize Report (LLM) --------------------------------------

    async def synthesize_report(self, state: ArcheologistState) -> ArcheologistState:
        """LLM synthesizes all findings into a structured archaeology report."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "synthesize_report", "started",
        )
        if await self._check_and_interrupt(state, "synthesize_report"):
            return state
        repo_name = state.get("repo_info", {}).get("name", "unknown")
        logger.info("[archeologist] Synthesizing report for %s", repo_name)

        # Format web research results
        web_parts = []
        for sr in state.get("web_research", []):
            web_parts.append(f"Query: {sr.get('query', '')}")
            if sr.get("answer"):
                web_parts.append(f"Summary: {sr['answer']}")
            for r in sr.get("results", []):
                web_parts.append(f"  - {r.get('title', '')} ({r.get('url', '')})")
                web_parts.append(f"    {r.get('content', '')[:500]}")
        web_str = "\n".join(web_parts) if web_parts else "(no web research available)"

        prompt = SYNTHESIZE_REPORT_PROMPT.format(
            repo_name=repo_name,
            project_goal=state.get("project_goal", ""),
            architecture_analysis=state.get("architecture_analysis", "")[:30000],
            gap_analysis=state.get("gap_analysis", "")[:20000],
            web_research=web_str[:15000],
        )

        try:
            response = await traced_llm_call(
                self.llm, [
                    SystemMessage(content="You are a technical lead producing a comprehensive report."),
                    HumanMessage(content=prompt),
                ],
                hub_url=self.hub_url, client=self.client,
                project_id=state.get("project_id"),
                agent=self.from_agent, node="synthesize_report",
            )
            state["synthesis"] = response.content.strip()
            logger.info("[archeologist] Report synthesized: %d chars", len(state["synthesis"]))
        except Exception as e:
            logger.error("[archeologist] Synthesis LLM failed: %s", e)
            # Fallback: stitch together raw analysis
            state["synthesis"] = (
                f"# Archaeology Report: {repo_name}\n\n"
                f"## Architecture\n{state.get('architecture_analysis', 'N/A')}\n\n"
                f"## Gap Analysis\n{state.get('gap_analysis', 'N/A')}\n"
            )

        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "synthesize_report", "completed",
            f"{len(state.get('synthesis', ''))} chars",
        )
        return state


# -- NAT Registration --------------------------------------------------------


class ArcheologistAgentConfig(FunctionBaseConfig, name="archeologist_agent"):
    """Configuration for the LangGraph archeologist agent (research + archaeology)."""

    llm_name: LLMRef = Field(..., description="LLM for analysis and synthesis")
    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub URL for document publishing and bus announcements",
    )
    from_agent: str = Field(
        default="archeologist",
        description="Agent ID for bus announcements and document attribution",
    )
    max_search_results: int = Field(
        default=5,
        description="Max results per Tavily search query",
    )
    trigger_next_agent: bool = Field(
        default=True,
        description="Auto-trigger product_owner after pipeline completes",
    )


@register_function(config_type=ArcheologistAgentConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def archeologist_agent_fn(
    config: ArcheologistAgentConfig, builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """Build the combined LangGraph pipeline and register as a NAT function."""
    logger.info("[archeologist] Initializing LangGraph archeologist agent")

    llm = await builder.get_llm(config.llm_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)
    tavily_api_key = os.environ.get("TAVILY_API_KEY", "")
    github_token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    client = NCMSHttpClient(hub_url=config.hub_url)
    github = GitHubProvider(token=github_token) if github_token else None

    if not github_token:
        logger.warning("[archeologist] GITHUB_PERSONAL_ACCESS_TOKEN not set — archaeology path unavailable")
    if not tavily_api_key:
        logger.warning("[archeologist] TAVILY_API_KEY not set — web searches will be skipped")

    agent = ArcheologistAgent(
        llm=llm,
        hub_url=config.hub_url,
        from_agent=config.from_agent,
        tavily_api_key=tavily_api_key,
        client=client,
        github=github,
        max_search_results=config.max_search_results,
        trigger_next_agent=config.trigger_next_agent,
    )
    graph = await agent.build_graph()
    logger.info("[archeologist] LangGraph pipeline ready (research + archaeology)")

    async def _run(input_message: str) -> str:
        """Run the combined pipeline (auto-routes research vs archaeology).

        Input format (archaeology):
            Analyze repository: https://github.com/owner/repo
            Goal: <project goal>
            (project_id: PRJ-XXXXXXXX)

        Input format (research):
            <research topic>
            (project_id: PRJ-XXXXXXXX)
        """
        logger.info("[archeologist] === Starting pipeline ===")
        logger.info("[archeologist] Input: %s", input_message[:200])

        repo_url = extract_repo_url(input_message)
        source_type = "archaeology" if repo_url else "research"
        goal = extract_goal(input_message) or extract_topic(input_message)
        project_id = extract_project_id(input_message)

        if source_type == "archaeology" and not repo_url:
            return "Error: No repository URL found in input. Expected format: 'Analyze repository: <url>'"

        logger.info("[archeologist] Route: %s", source_type)

        result = await graph.ainvoke({
            # Routing
            "source_type": source_type,
            # Shared
            "topic": input_message,
            "synthesis": "",
            "document_id": None,
            "messages": [HumanMessage(content=input_message)],
            "project_id": project_id,
            "interrupted": False,
            # Research path
            "search_queries": [],
            "search_results": [],
            "arxiv_results": [],
            # Archaeology path
            "repository_url": repo_url or "",
            "project_goal": goal,
            "repo_info": {},
            "file_tree": [],
            "key_files": {},
            "architecture_analysis": "",
            "gap_analysis": "",
            "web_research": [],
        }, config={"recursion_limit": 30})

        synthesis = result.get("synthesis", "Pipeline produced no output.")
        doc_id = result.get("document_id")

        logger.info("[archeologist] === Pipeline complete ===")
        logger.info("[archeologist] Route: %s | Report: %d chars | Doc ID: %s",
                    source_type, len(synthesis), doc_id)

        return synthesis

    try:
        yield FunctionInfo.from_fn(
            _run,
            description=(
                "Combined archeologist agent. Routes automatically based on input: "
                "(1) If a GitHub URL is detected, runs the archaeology pipeline — "
                "analyzes the repository architecture, identifies gaps, performs "
                "targeted web research, and produces a grounded report. "
                "(2) Otherwise, runs the research pipeline — plans search queries, "
                "runs parallel web + ArXiv searches, and synthesizes a structured "
                "market research report. Both paths publish to the document store "
                "and trigger the Product Owner for PRD generation."
            ),
        )
    finally:
        if github:
            await github.close()
        await client.close()
        logger.info("[archeologist] Cleaned up HTTP clients")
