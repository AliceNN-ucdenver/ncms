# SPDX-License-Identifier: Apache-2.0
"""LangGraph-based Archeologist agent for NAT/NCMS.

Deterministic pipeline: check_guardrails → clone_and_index → analyze_architecture
→ identify_gaps → web_research → synthesize_report → publish_and_trigger.

LLM called 3 times (analyze + gaps + synthesize). All other nodes are pure Python.
Starts from an existing GitHub repository and produces a grounded research report
that feeds into the existing PO → Builder pipeline.
"""

from __future__ import annotations

import asyncio
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
    emit_telemetry,
    extract_goal,
    extract_project_id,
    extract_repo_url,
    extract_topic,
)
from .archeologist_prompts import (
    ANALYZE_ARCHITECTURE_PROMPT,
    IDENTIFY_GAPS_PROMPT,
    SYNTHESIZE_REPORT_PROMPT,
)

logger = logging.getLogger(__name__)


# ── State ─────────────────────────────────────────────────────────────────────


class ArcheologyState(TypedDict):
    """Graph state for the archaeology pipeline."""

    repository_url: str  # GitHub repo URL
    project_goal: str  # What the user wants to achieve
    repo_info: dict  # Basic repo metadata
    file_tree: list[dict]  # Repo file listing
    key_files: dict[str, str]  # filename -> content
    architecture_analysis: str  # LLM output
    gap_analysis: str  # LLM output
    web_research: list[dict]  # Tavily results
    synthesis: str  # Final report
    document_id: str | None  # Published doc ID
    messages: list[BaseMessage]  # LangGraph compat
    project_id: str | None  # PRJ-XXXXXXXX


# ── Agent ─────────────────────────────────────────────────────────────────────


class ArcheologistAgent:
    """Deterministic LangGraph archaeology pipeline.

    Nodes: check_guardrails → clone_and_index → analyze_architecture
    → identify_gaps → web_research → synthesize_report → publish_and_trigger

    LLM called 3 times: analyze, gaps, synthesize.
    All other nodes are pure Python/API calls.
    """

    def __init__(
        self,
        llm: BaseChatModel,
        hub_url: str,
        from_agent: str,
        tavily_api_key: str,
        client: NCMSHttpClient,
        github: GitHubProvider,
        max_search_results: int = 3,
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

    # ── Graph builder ────────────────────────────────────────────────────

    async def build_graph(self) -> StateGraph:
        """Build and compile the deterministic archaeology pipeline."""
        graph = StateGraph(ArcheologyState)

        graph.add_node("check_guardrails", self.check_guardrails)
        graph.add_node("clone_and_index", self.clone_and_index)
        graph.add_node("analyze_architecture", self.analyze_architecture)
        graph.add_node("identify_gaps", self.identify_gaps)
        graph.add_node("web_research", self.web_research)
        graph.add_node("synthesize_report", self.synthesize_report)
        graph.add_node("publish_and_trigger", self.publish_and_trigger)

        graph.add_edge(START, "check_guardrails")
        graph.add_conditional_edges(
            "check_guardrails",
            self._after_guardrails,
            {"continue": "clone_and_index", "blocked": END},
        )
        graph.add_edge("clone_and_index", "analyze_architecture")
        graph.add_edge("analyze_architecture", "identify_gaps")
        graph.add_edge("identify_gaps", "web_research")
        graph.add_edge("web_research", "synthesize_report")
        graph.add_edge("synthesize_report", "publish_and_trigger")
        graph.add_edge("publish_and_trigger", END)

        compiled = graph.compile()
        logger.info(
            "[archeologist] Graph compiled: guardrails → clone → analyze → "
            "gaps → research → synthesize → publish"
        )
        return compiled

    def _after_guardrails(self, state: ArcheologyState) -> str:
        """Route: if guardrails blocked, skip to END."""
        if state.get("synthesis", "").startswith("Pipeline blocked"):
            return "blocked"
        return "continue"

    # ── Node 1: Check Guardrails ─────────────────────────────────────────

    async def check_guardrails(self, state: ArcheologyState) -> ArcheologyState:
        """Check input guardrails before pipeline starts."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "check_guardrails", "started",
        )
        from .guardrails import run_input_guardrails

        goal = state.get("project_goal", "")
        can_proceed, violations = await run_input_guardrails(
            self.hub_url, goal, self.from_agent,
        )
        if not can_proceed:
            logger.warning("[archeologist] Guardrails BLOCKED: %s", violations)
            state["synthesis"] = f"Pipeline blocked by guardrails: {[str(v) for v in violations]}"
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "check_guardrails", "completed",
        )
        return state

    # ── Node 2: Clone & Index (Pure Python) ──────────────────────────────

    async def clone_and_index(self, state: ArcheologyState) -> ArcheologyState:
        """Fetch repo structure and read key files via GitHub API. No LLM."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "clone_and_index", "started",
        )
        repo_url = state["repository_url"]
        logger.info("[archeologist] Indexing repository: %s", repo_url)

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

    # ── Node 3: Analyze Architecture (LLM) ───────────────────────────────

    async def analyze_architecture(self, state: ArcheologyState) -> ArcheologyState:
        """LLM analyzes the codebase structure and architecture."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "analyze_architecture", "started",
        )
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
            response = await self.llm.ainvoke([
                SystemMessage(content="You are a senior software architect analyzing a codebase."),
                HumanMessage(content=prompt),
            ])
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

    # ── Node 4: Identify Gaps (LLM) ──────────────────────────────────────

    async def identify_gaps(self, state: ArcheologyState) -> ArcheologyState:
        """LLM compares current state against goal, cross-refs NCMS knowledge."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "identify_gaps", "started",
        )
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
            response = await self.llm.ainvoke([
                SystemMessage(content="You are an expert code reviewer with governance knowledge."),
                HumanMessage(content=prompt),
            ])
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

    # ── Node 5: Web Research (Pure Python) ───────────────────────────────

    async def web_research(self, state: ArcheologyState) -> ArcheologyState:
        """Targeted Tavily searches grounded in codebase understanding. No LLM."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "web_research", "started",
        )

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
        """Single Tavily search (same pattern as research_agent)."""
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

    # ── Node 6: Synthesize Report (LLM) ──────────────────────────────────

    async def synthesize_report(self, state: ArcheologyState) -> ArcheologyState:
        """LLM synthesizes all findings into a structured archaeology report."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "synthesize_report", "started",
        )
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
            response = await self.llm.ainvoke([
                SystemMessage(content="You are a technical lead producing a comprehensive report."),
                HumanMessage(content=prompt),
            ])
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

    # ── Node 7: Publish & Trigger ────────────────────────────────────────

    async def publish_and_trigger(self, state: ArcheologyState) -> ArcheologyState:
        """Publish the archaeology report and trigger the Product Owner."""
        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "publish_and_trigger", "started",
        )
        repo_name = state.get("repo_info", {}).get("name", "unknown")
        synthesis = state.get("synthesis", "")

        if not synthesis or synthesis.startswith("Pipeline blocked"):
            logger.warning("[archeologist] No synthesis to publish")
            await emit_telemetry(
                self.hub_url, state.get("project_id"),
                self.from_agent, "publish_and_trigger", "completed", "skipped",
            )
            return state

        # Embed project_id in content for traceability
        project_id = state.get("project_id")
        content = synthesis
        if project_id:
            content = f"<!-- project_id: {project_id} -->\n{content}"

        # Publish
        try:
            result = await self.client.publish_document(
                content=content,
                title=f"Archaeology: {repo_name}",
                from_agent=self.from_agent,
            )
            doc_id = result.get("document_id", "unknown")
            state["document_id"] = doc_id
            logger.info("[archeologist] Report published: %s (%d chars)", doc_id, len(content))

            # Announce
            try:
                await self.client.bus_announce(
                    content=(
                        f"Archaeology report published for {repo_name}\n"
                        f"Document ID: {doc_id} | Size: {len(content)} chars"
                    ),
                    domains=["archaeology", "research", "product"],
                    from_agent=self.from_agent,
                )
            except Exception:
                pass

        except Exception as e:
            logger.error("[archeologist] Failed to publish report: %s", e)
            await emit_telemetry(
                self.hub_url, state.get("project_id"),
                self.from_agent, "publish_and_trigger", "failed", str(e),
            )
            return state

        # Fire-and-forget: trigger Product Owner
        if self.trigger_next_agent and state.get("document_id"):
            doc_id = state["document_id"]
            clean_goal = state.get("project_goal", repo_name)

            async def _trigger() -> None:
                try:
                    msg = build_prd_trigger(
                        clean_goal,
                        research_id=doc_id,
                        project_id=project_id,
                    )
                    await self.client.bus_announce(
                        content=msg,
                        domains=["trigger-product_owner", "archaeology", "product"],
                        from_agent=self.from_agent,
                    )
                    logger.info(
                        "[archeologist] Triggered product_owner: %s", msg[:120],
                    )
                except Exception as e:
                    logger.warning("[archeologist] Failed to trigger PO: %s", e)

            asyncio.create_task(_trigger())

        await emit_telemetry(
            self.hub_url, state.get("project_id"),
            self.from_agent, "publish_and_trigger", "completed",
            f"doc_id={state.get('document_id')}",
        )
        return state


# ── NAT Registration ──────────────────────────────────────────────────────────


class ArcheologistAgentConfig(FunctionBaseConfig, name="archeologist_agent"):
    """Configuration for the LangGraph archeologist agent."""

    llm_name: LLMRef = Field(..., description="LLM for architecture analysis and synthesis")
    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub URL for document publishing and bus announcements",
    )
    from_agent: str = Field(
        default="archeologist",
        description="Agent ID for bus announcements and document attribution",
    )
    max_search_results: int = Field(
        default=3,
        description="Max results per Tavily search query",
    )
    trigger_next_agent: bool = Field(
        default=True,
        description="Auto-trigger product_owner after archaeology completes",
    )


@register_function(config_type=ArcheologistAgentConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def archeologist_agent_fn(
    config: ArcheologistAgentConfig, builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """Build the LangGraph archaeology pipeline and register as a NAT function."""
    logger.info("[archeologist] Initializing LangGraph archeologist agent")

    llm = await builder.get_llm(config.llm_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)
    tavily_api_key = os.environ.get("TAVILY_API_KEY", "")
    github_token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    client = NCMSHttpClient(hub_url=config.hub_url)
    github = GitHubProvider(token=github_token)

    if not github_token:
        logger.warning("[archeologist] GITHUB_PERSONAL_ACCESS_TOKEN not set — API calls may be rate-limited")
    if not tavily_api_key:
        logger.warning("[archeologist] TAVILY_API_KEY not set — web research will be skipped")

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
    logger.info("[archeologist] LangGraph pipeline ready")

    async def _archaeology(input_message: str) -> str:
        """Run the full archaeology pipeline and return the report.

        Input format:
            Analyze repository: https://github.com/owner/repo
            Goal: <project goal>
            (project_id: PRJ-XXXXXXXX)
        """
        logger.info("[archeologist] === Starting archaeology pipeline ===")
        logger.info("[archeologist] Input: %s", input_message[:200])

        repo_url = extract_repo_url(input_message) or ""
        goal = extract_goal(input_message) or extract_topic(input_message)
        project_id = extract_project_id(input_message)

        if not repo_url:
            return "Error: No repository URL found in input. Expected format: 'Analyze repository: <url>'"

        result = await graph.ainvoke({
            "repository_url": repo_url,
            "project_goal": goal,
            "repo_info": {},
            "file_tree": [],
            "key_files": {},
            "architecture_analysis": "",
            "gap_analysis": "",
            "web_research": [],
            "synthesis": "",
            "document_id": None,
            "messages": [HumanMessage(content=input_message)],
            "project_id": project_id,
        }, config={"recursion_limit": 30})

        synthesis = result.get("synthesis", "Archaeology pipeline produced no output.")
        doc_id = result.get("document_id")

        logger.info("[archeologist] === Pipeline complete ===")
        logger.info("[archeologist] Report: %d chars | Doc ID: %s", len(synthesis), doc_id)

        return synthesis

    try:
        yield FunctionInfo.from_fn(
            _archaeology,
            description=(
                "Codebase archaeology agent. Analyzes an existing GitHub repository, "
                "maps its architecture, identifies gaps against a stated goal, "
                "performs targeted web research, and produces a grounded archaeology "
                "report. Triggers the Product Owner for PRD generation."
            ),
        )
    finally:
        await github.close()
        await client.close()
        logger.info("[archeologist] Cleaned up HTTP clients")
