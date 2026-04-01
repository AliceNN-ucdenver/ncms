# SPDX-License-Identifier: Apache-2.0
"""LangGraph-based implementation design agent for NAT/NCMS.

Deterministic pipeline with review loop:
  read_document → ask_experts → synthesize_design → publish_design → request_review
                       ▲                                                   │
                       │                                            ┌──────┴──────┐
                       │                                            │  avg ≥ 80%? │
                       │                                            └──────┬──────┘
                       │                                       yes ──┘        └── no
                       │                                       │                  │
                       │                                    verify          revise_design
                       │                                                         │
                       └─────────────────────────────────────────────────────────┘

LLM called: synthesize (1) + revise (0-5). Review scoring is pure Python via bus_ask.
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
from .pipeline_utils import extract_project_id, extract_prd_id, extract_topic, emit_telemetry
from .design_prompts import (
    SYNTHESIZE_DESIGN_PROMPT,
    ARCHITECTURE_REVIEW_PROMPT,
    SECURITY_REVIEW_PROMPT,
    REVISE_DESIGN_PROMPT,
)

logger = logging.getLogger(__name__)


# -- State -------------------------------------------------------------------


class DesignState(TypedDict):
    """Graph state for the design pipeline with review loop."""

    topic: str  # Design subject
    source_doc_id: str | None  # PO's PRD doc ID (parsed from input)
    source_content: str  # PRD content
    manifest: dict  # Requirements manifest from PO (endpoints, security reqs, tech constraints)
    expert_input: dict[str, str]  # {"architect": "...", "security": "..."}
    design: str  # Implementation design markdown
    document_id: str | None  # Published design doc ID
    messages: list[BaseMessage]  # LangGraph compat
    # Review loop fields
    review_scores: dict[str, int]  # {"architect": 85, "security": 72}
    review_feedback: dict[str, str]  # {"architect": "COVERED:...", "security": "..."}
    iteration: int  # Current review round (0 = first pass)
    # Guardrail loop fields
    guardrail_violations: list[str]  # List of violation messages from output guardrails
    guardrail_fix_iteration: int  # Current fix iteration (0 = first check)
    project_id: str | None  # PRJ-XXXXXXXX for pipeline tracking


# -- Agent -------------------------------------------------------------------


class DesignAgent:
    """Deterministic LangGraph design pipeline with review loop.

    Graph: read_document → ask_experts → synthesize_design → publish_design
           → request_review → [pass: verify | fail: revise_design → publish_design → ...]
    LLM called: synthesize (1) + revise (0 to max_iterations).
    Review scoring is pure Python via bus_ask to architect + security.
    """

    def __init__(
        self,
        llm: BaseChatModel,
        hub_url: str,
        from_agent: str,
        client: NCMSHttpClient,
        quality_threshold: int = 80,
        max_iterations: int = 5,
    ) -> None:
        self.llm = llm
        self.hub_url = hub_url
        self.from_agent = from_agent
        self.client = client
        self.quality_threshold = quality_threshold
        self.max_iterations = max_iterations

    async def check_guardrails(self, state: DesignState) -> DesignState:
        """Check input guardrails before pipeline starts."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "check_guardrails", "started")
        from .guardrails import run_input_guardrails
        can_proceed, violations = await run_input_guardrails(self.hub_url, state["topic"], self.from_agent)
        if not can_proceed:
            logger.warning("[design_agent] Guardrails BLOCKED: %s", violations)
            state["design"] = f"Pipeline blocked by guardrails: {[str(v) for v in violations]}"
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "check_guardrails", "completed")
        return state

    async def validate_completeness(self, state: DesignState) -> DesignState:
        """Validate structural completeness. No LLM."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "validate_completeness", "started")
        from .spec_validator import validate_design_completeness
        manifest = state.get("manifest") if isinstance(state.get("manifest"), dict) else None
        result = validate_design_completeness(
            design=state["design"],
            prd=state.get("source_content", ""),
            manifest=manifest,
        )
        if not result.passed:
            logger.warning("[design_agent] Completeness: %s", result.summary())
            state["design"] += "\n\n---\n**Completeness Check:** " + "; ".join(result.issues[:5])
        else:
            logger.info("[design_agent] %s", result.summary())
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "validate_completeness", "completed", result.summary())
        return state

    # -- Node: Check Output Guardrails (Pure Python, annotation-only) --------

    async def check_output_guardrails(self, state: DesignState) -> DesignState:
        """Check output guardrails and annotate violations for human review.

        This does NOT modify or block the design. Violations are appended
        as a visible warning section that the human reviewer can act on.
        Design documents legitimately reference passwords, secrets, and
        security patterns in their specifications.
        """
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "check_output_guardrails", "started")
        from .guardrails import run_output_guardrails

        _, violations = await run_output_guardrails(
            self.hub_url, state["design"], self.from_agent,
        )
        state["guardrail_violations"] = [str(v) for v in violations]

        if not violations:
            logger.info("[design_agent] Output guardrails PASSED — no violations")
            await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "check_output_guardrails", "completed", "passed")
        else:
            logger.info(
                "[design_agent] Output guardrails found %d item(s) for human review",
                len(violations),
            )
            # Annotate the design with warnings (don't modify the actual content)
            warning_section = (
                "\n\n---\n"
                "## Guardrail Review Notes\n\n"
                "*The following items were flagged by automated guardrails for human review:*\n\n"
                + "\n".join(f"- {v}" for v in violations)
                + "\n\n*These may be false positives in a design document (e.g., password fields "
                "in interface definitions are expected). Review and address as needed before "
                "implementation.*\n"
            )
            state["design"] += warning_section

            await emit_telemetry(
                self.hub_url, state.get("project_id"), self.from_agent,
                "check_output_guardrails", "completed",
                f"{len(violations)} items flagged for review",
            )
            try:
                await self.client.bus_announce(
                    content=f"⚠️ Output guardrails: {len(violations)} item(s) flagged for human review",
                    domains=["implementation"],
                    from_agent=self.from_agent,
                )
            except Exception:
                pass

        return state

    async def build_graph(self) -> StateGraph:
        """Build and compile the design pipeline with review loop."""
        graph = StateGraph(DesignState)

        graph.add_node("check_guardrails", self.check_guardrails)
        graph.add_node("read_document", self.read_document)
        graph.add_node("ask_experts", self.ask_experts)
        graph.add_node("synthesize_design", self.synthesize_design)
        graph.add_node("validate_completeness", self.validate_completeness)
        graph.add_node("check_output_guardrails", self.check_output_guardrails)
        graph.add_node("publish_design", self.publish_design)
        graph.add_node("request_review", self.request_review)
        graph.add_node("revise_design", self.revise_design)
        graph.add_node("verify", self.verify)

        # Forward path
        graph.add_edge(START, "check_guardrails")
        graph.add_edge("check_guardrails", "read_document")
        graph.add_edge("read_document", "ask_experts")
        graph.add_edge("ask_experts", "synthesize_design")
        graph.add_edge("synthesize_design", "validate_completeness")
        graph.add_edge("validate_completeness", "check_output_guardrails")
        graph.add_edge("check_output_guardrails", "publish_design")
        graph.add_edge("publish_design", "request_review")

        # Review loop: conditional edge
        graph.add_conditional_edges("request_review", self.should_revise)

        # Revise loops back to publish (then review again)
        graph.add_edge("revise_design", "publish_design")

        # After approval: generate contracts, then exit
        graph.add_node("generate_contracts", self.generate_contracts)
        graph.add_edge("verify", "generate_contracts")
        graph.add_edge("generate_contracts", END)

        compiled = graph.compile()
        logger.info(
            "[design_agent] Graph compiled: check_guardrails → read_document → ask_experts → "
            "synthesize → validate → publish → review ⟲ (threshold: %d%%, max: %d iterations)",
            self.quality_threshold,
            self.max_iterations,
        )
        return compiled

    # -- Conditional edge ------------------------------------------------------

    async def should_revise(self, state: DesignState) -> str:
        """Decide whether to approve or revise based on review scores."""
        scores = state.get("review_scores", {})
        avg_score = sum(scores.values()) / max(len(scores), 1)
        iteration = state.get("iteration", 0)

        if avg_score >= self.quality_threshold:
            logger.info(
                "[design_agent] ✅ Review PASSED — avg: %.0f%% (round %d)",
                avg_score, iteration + 1,
            )
            return "verify"
        elif iteration >= self.max_iterations:
            logger.warning(
                "[design_agent] ⚠️ Max iterations (%d) reached — avg: %.0f%%, accepting as-is",
                self.max_iterations, avg_score,
            )
            return "verify"
        else:
            logger.info(
                "[design_agent] 🔄 Review FAILED — avg: %.0f%% < %d%% (round %d/%d) — revising",
                avg_score, self.quality_threshold, iteration + 1, self.max_iterations,
            )
            return "revise_design"

    # -- Node 1: Read Document (Pure Python) ---------------------------------

    async def read_document(self, state: DesignState) -> DesignState:
        """Parse doc_id from input and fetch the PRD. No LLM."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "read_document", "started")
        topic = state["topic"]
        logger.info("[design_agent] Reading source document for topic: %s", topic[:100])

        doc_id = extract_prd_id(topic)
        if doc_id:
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
            logger.info("[design_agent] No prd_id found in input — standalone mode")
            state["source_doc_id"] = None
            state["source_content"] = ""

        # Also fetch the requirements manifest for this project
        project_id = state.get("project_id")
        if project_id:
            try:
                import httpx as _hx
                async with _hx.AsyncClient(timeout=10.0) as http:
                    resp = await http.get(
                        f"{self.hub_url}/api/v1/documents",
                    )
                    if resp.status_code == 200:
                        all_docs = resp.json()
                        for doc in all_docs:
                            if (doc.get("project_id") == project_id
                                    and "Manifest" in doc.get("title", "")):
                                manifest_resp = await http.get(
                                    f"{self.hub_url}/api/v1/documents/{doc['document_id']}",
                                )
                                if manifest_resp.status_code == 200:
                                    import json
                                    manifest_content = manifest_resp.json().get("content", "")
                                    try:
                                        state["manifest"] = json.loads(manifest_content)
                                        logger.info(
                                            "[design_agent] Loaded manifest: %d endpoints, %d security reqs",
                                            len(state["manifest"].get("endpoints", [])),
                                            len(state["manifest"].get("security_requirements", [])),
                                        )
                                    except json.JSONDecodeError:
                                        logger.debug("[design_agent] Manifest not valid JSON")
                                break
            except Exception as e:
                logger.debug("[design_agent] Manifest fetch failed: %s", e)

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "read_document", "completed", f"doc_id={state.get('source_doc_id')}")
        return state

    # -- Node 2: Ask Experts (Pure Python) -----------------------------------

    async def ask_experts(self, state: DesignState) -> DesignState:
        """Parallel bus_ask to architecture and security experts. No LLM."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "ask_experts", "started")
        topic = state["topic"]
        source = state.get("source_content", "")
        logger.info("[design_agent] Querying experts for: %s", topic[:100])

        # Include PRD context so experts can give grounded answers
        context_summary = source[:5000] if source else "(no PRD document available)"

        try:
            await self.client.bus_announce(
                content=f"Querying architecture and security experts for: {topic[:80]}",
                domains=["implementation", "identity-service"],
                from_agent=self.from_agent,
            )
        except Exception:
            pass

        async def _ask_architect() -> str:
            try:
                question = (
                    f"What architectural patterns and ADRs apply to this implementation?\n\n"
                    f"PRD context:\n{context_summary}"
                )
                result = await self.client.bus_ask(
                    question=question,
                    domains=["architecture", "decisions"],
                    from_agent=self.from_agent,
                    timeout_ms=180000,
                )
                answer = result.get("answer", "") or result.get("content", "")
                logger.info("[design_agent] Architect response: %d chars", len(answer))
                return answer
            except Exception as e:
                logger.warning("[design_agent] Architect query failed: %s", e)
                return ""

        async def _ask_security() -> str:
            try:
                question = (
                    f"What security threats and controls apply to this implementation?\n\n"
                    f"PRD context:\n{context_summary}"
                )
                result = await self.client.bus_ask(
                    question=question,
                    domains=["security", "threats"],
                    from_agent=self.from_agent,
                    timeout_ms=180000,
                )
                answer = result.get("answer", "") or result.get("content", "")
                logger.info("[design_agent] Security response: %d chars", len(answer))
                return answer
            except Exception as e:
                logger.warning("[design_agent] Security query failed: %s", e)
                return ""

        architect_response, security_response = await asyncio.gather(
            _ask_architect(), _ask_security(),
        )

        state["expert_input"] = {
            "architect": architect_response,
            "security": security_response,
        }

        logger.info(
            "[design_agent] Expert input collected — architect: %d chars, security: %d chars",
            len(architect_response), len(security_response),
        )

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "ask_experts", "completed", f"architect={len(architect_response)} security={len(security_response)}")
        return state

    # -- Node 3: Synthesize Design (LLM) ------------------------------------

    async def synthesize_design(self, state: DesignState) -> DesignState:
        """LLM synthesizes PRD + expert input into an implementation design."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "synthesize_design", "started")
        topic = state["topic"]
        logger.info("[design_agent] Synthesizing design for: %s", topic[:100])

        prd_content = state.get("source_content", "") or ""
        if len(prd_content) > 80000:
            prd_content = prd_content[:80000] + "\n\n[... truncated for context window ...]"

        expert_input = state.get("expert_input", {})
        architect_input = expert_input.get("architect", "") or "(no architect input available)"
        security_input = expert_input.get("security", "") or "(no security input available)"
        if len(architect_input) > 15000:
            architect_input = architect_input[:15000] + "\n[... truncated ...]"
        if len(security_input) > 15000:
            security_input = security_input[:15000] + "\n[... truncated ...]"

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
        except Exception as e:
            logger.error("[design_agent] Synthesis failed: %s", e)
            state["design"] = (
                f"# {topic} — Implementation Design (Synthesis Failed)\n\n"
                f"**Error:** LLM synthesis failed: {e}\n\n"
                f"## PRD Content\n\n{prd_content[:5000]}\n\n"
                f"## Architect Input\n\n{architect_input}\n\n"
                f"## Security Input\n\n{security_input}\n"
            )

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "synthesize_design", "completed", f"{len(state['design'])} chars")
        return state

    # -- Node 4: Publish Design (Pure Python) --------------------------------

    async def publish_design(self, state: DesignState) -> DesignState:
        """Publish the design to the NCMS document store. No LLM."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "publish_design", "started")
        topic = state["topic"]
        clean_topic = extract_topic(topic)
        design = state["design"]
        iteration = state.get("iteration", 0)
        version = f"v{iteration + 1}"
        scores = state.get("review_scores", {})

        # Prepend version header to the design content
        score_line = ""
        if scores:
            score_line = (
                f"\n> **Review Status:** Architect {scores.get('architect', '?')}% | "
                f"Security {scores.get('security', '?')}% | "
                f"Round {iteration + 1}\n"
            )
        project_id = state.get("project_id")
        project_comment = f"<!-- project_id: {project_id} -->\n" if project_id else ""
        versioned_design = f"{project_comment}<!-- Version: {version} | Round: {iteration + 1} -->\n{score_line}\n{design}"

        logger.info("[design_agent] Publishing design document: %d chars %s", len(design), version)

        try:
            result = await self.client.publish_document(
                content=versioned_design,
                title=f"{clean_topic} — Implementation Design {version}",
                from_agent=self.from_agent,
                format="markdown",
            )
            doc_id = result.get("document_id", "unknown")
            state["document_id"] = doc_id
            logger.info("[design_agent] Document published: %s", doc_id)

            try:
                await self.client.bus_announce(
                    content=(
                        f"Implementation design published {version}: {topic[:60]}\n"
                        f"Document ID: {doc_id} | Size: {len(design)} chars"
                    ),
                    domains=["implementation", "identity-service"],
                    from_agent=self.from_agent,
                )
            except Exception:
                pass

        except Exception as e:
            logger.error("[design_agent] Publish failed: %s", e)
            state["document_id"] = None

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "publish_design", "completed", f"doc_id={state.get('document_id')}")
        return state

    # -- Node 5: Request Review (Pure Python) --------------------------------

    async def request_review(self, state: DesignState) -> DesignState:
        """Send design to architect + security for structured review. No LLM.

        Both reviews run in parallel. If either fails, it's retried once
        before falling back to a default score. This ensures the revision
        step always has feedback from BOTH reviewers.
        """
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "request_review", "started")
        design = state["design"]
        iteration = state.get("iteration", 0)
        logger.info("[design_agent] Requesting review (round %d)", iteration + 1)

        # Truncate design for review prompt (generous with 512K context)
        design_for_review = design[:60000] if len(design) > 60000 else design

        async def _review_single(
            prompt: str, domains: list[str], label: str,
        ) -> tuple[int, str]:
            """Run a single review with one retry on failure."""
            for attempt in range(2):  # max 2 attempts
                try:
                    result = await self.client.bus_ask(
                        question=prompt,
                        domains=domains,
                        from_agent=self.from_agent,
                        timeout_ms=240000,  # 4 minutes
                    )
                    answer = result.get("answer", "") or result.get("content", "")
                    if not answer or len(answer) < 20:
                        raise ValueError(f"Empty or too-short response: {len(answer)} chars")
                    score = self._parse_score(answer)
                    logger.info(
                        "[design_agent] %s review: %d%% (%d chars) [attempt %d]",
                        label, score, len(answer), attempt + 1,
                    )
                    return score, answer
                except Exception as e:
                    if attempt == 0:
                        logger.warning(
                            "[design_agent] %s review failed (attempt 1), retrying: %s",
                            label, e,
                        )
                        await asyncio.sleep(2)  # Brief pause before retry
                    else:
                        logger.warning(
                            "[design_agent] %s review failed after 2 attempts: %s",
                            label, e,
                        )
                        return 50, f"Review failed after 2 attempts: {e}"
            return 50, "Review failed"  # Unreachable but satisfies type checker

        # Include doc_id so experts can fetch entity metadata for enriched search
        doc_id = state.get("document_id", "")
        doc_tag = f"\n(doc_id: {doc_id})" if doc_id else ""
        arch_prompt = ARCHITECTURE_REVIEW_PROMPT.format(design_content=design_for_review) + doc_tag
        sec_prompt = SECURITY_REVIEW_PROMPT.format(design_content=design_for_review) + doc_tag

        # Run both reviews in parallel, each with internal retry
        (arch_score, arch_feedback), (sec_score, sec_feedback) = await asyncio.gather(
            _review_single(arch_prompt, ["architecture", "decisions"], "Architect"),
            _review_single(sec_prompt, ["security", "threats"], "Security"),
        )

        state["review_scores"] = {"architect": arch_score, "security": sec_score}
        state["review_feedback"] = {"architect": arch_feedback, "security": sec_feedback}

        avg_score = (arch_score + sec_score) / 2

        # Announce review results
        try:
            status = "APPROVED ✅" if avg_score >= self.quality_threshold else f"below {self.quality_threshold}%"
            await self.client.bus_announce(
                content=(
                    f"📝 Review round {iteration + 1}: "
                    f"Architect {arch_score}%, Security {sec_score}% "
                    f"(avg {avg_score:.0f}%) — {status}"
                ),
                domains=["implementation", "identity-service"],
                from_agent=self.from_agent,
            )
        except Exception:
            pass

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "request_review", "completed", f"architect={arch_score}% security={sec_score}% avg={avg_score:.0f}%")
        return state

    # -- Node 6: Revise Design (LLM) ----------------------------------------

    async def revise_design(self, state: DesignState) -> DesignState:
        """LLM revises design based on review feedback."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "revise_design", "started")
        iteration = state.get("iteration", 0) + 1
        state["iteration"] = iteration
        logger.info("[design_agent] 🔄 Revising design (round %d)", iteration)

        scores = state.get("review_scores", {})
        feedback = state.get("review_feedback", {})

        # Announce revision
        try:
            await self.client.bus_announce(
                content=f"🔄 Revising design with expert feedback (round {iteration})...",
                domains=["implementation", "identity-service"],
                from_agent=self.from_agent,
            )
        except Exception:
            pass

        original = state["design"]
        if len(original) > 80000:
            original = original[:80000] + "\n\n[... truncated ...]"

        prompt = REVISE_DESIGN_PROMPT.format(
            original_design=original,
            arch_score=scores.get("architect", 0),
            arch_feedback=feedback.get("architect", "No feedback"),
            sec_score=scores.get("security", 0),
            sec_feedback=feedback.get("security", "No feedback"),
            iteration=iteration,
        )

        try:
            response = await self.llm.ainvoke([
                SystemMessage(
                    content=(
                        "You are revising an implementation design to address "
                        "expert review feedback. Produce a complete, improved design."
                    )
                ),
                HumanMessage(content=prompt),
            ])
            state["design"] = response.content
            logger.info(
                "[design_agent] Design revised: %d chars (round %d)",
                len(state["design"]), iteration,
            )
        except Exception as e:
            logger.error("[design_agent] Revision failed: %s", e)
            # Keep existing design — don't make it worse

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "revise_design", "completed", f"round {iteration}, {len(state['design'])} chars")
        return state

    # -- Node 7: Verify (Pure Python) ----------------------------------------

    async def verify(self, state: DesignState) -> DesignState:
        """Final verification — log results and announce completion."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "verify", "started")
        topic = state["topic"]
        doc_id = state.get("document_id")
        scores = state.get("review_scores", {})
        iteration = state.get("iteration", 0)
        avg_score = sum(scores.values()) / max(len(scores), 1) if scores else 0

        if avg_score >= self.quality_threshold:
            logger.info(
                "[design_agent] ✅ Design APPROVED — avg: %.0f%% (round %d) | Doc: %s",
                avg_score, iteration + 1, doc_id,
            )
            status = f"APPROVED at {avg_score:.0f}% after {iteration + 1} round(s)"
        elif iteration >= self.max_iterations:
            logger.warning(
                "[design_agent] ⚠️ Design accepted at %.0f%% after %d rounds (below %d%%)",
                avg_score, iteration, self.quality_threshold,
            )
            status = f"Accepted at {avg_score:.0f}% after {iteration} rounds (below threshold)"
        else:
            status = "Complete"

        # Publish review report as a separate document artifact
        clean_topic = extract_topic(topic)
        feedback = state.get("review_feedback", {})
        project_id = state.get("project_id", "")
        project_tag = f"<!-- project_id: {project_id} -->\n" if project_id else ""
        review_doc = (
            f"{project_tag}# Design Review Report — {clean_topic}\n\n"
            f"**Status:** {status}\n"
            f"**Design Document:** {doc_id}\n"
            f"**Review Rounds:** {iteration + 1}\n"
            f"**Quality Threshold:** {self.quality_threshold}%\n\n"
            f"---\n\n"
            f"## Architecture Review (Score: {scores.get('architect', '?')}%)\n\n"
            f"{feedback.get('architect', 'No review available')}\n\n"
            f"---\n\n"
            f"## Security Review (Score: {scores.get('security', '?')}%)\n\n"
            f"{feedback.get('security', 'No review available')}\n\n"
            f"---\n\n"
            f"*Average Score: {avg_score:.0f}% | "
            f"Threshold: {self.quality_threshold}% | "
            f"Rounds: {iteration + 1}/{self.max_iterations}*\n"
        )

        try:
            await self.client.publish_document(
                content=review_doc,
                title=f"{clean_topic} — Design Review Report",
                from_agent=self.from_agent,
                format="markdown",
            )
            logger.info("[design_agent] Review report published")
        except Exception as e:
            logger.warning("[design_agent] Failed to publish review report: %s", e)

        # Announce completion
        try:
            await self.client.bus_announce(
                content=(
                    f"🏁 Design pipeline complete — {status}\n"
                    f"Document: {doc_id} | Size: {len(state.get('design', ''))} chars\n"
                    f"Scores: Architect {scores.get('architect', '?')}%, "
                    f"Security {scores.get('security', '?')}%"
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

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "verify", "completed", status)
        return state

    # -- Node: Generate Contracts (LLM, after approval) -------------------------

    async def generate_contracts(self, state: DesignState) -> DesignState:
        """Generate machine-parseable contracts from the approved design.

        Produces OpenAPI 3.1 YAML and Zod validation schemas as separate
        documents. Only runs after review approval. These contracts give
        the coding agent unambiguous specifications.
        """
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "generate_contracts", "started")
        design = state["design"]
        clean_topic = extract_topic(state["topic"])

        if not design or len(design) < 500:
            logger.info("[design_agent] Skipping contract generation (design too short)")
            await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "generate_contracts", "completed", "skipped")
            return state

        logger.info("[design_agent] Generating contracts from %d char design", len(design))

        try:
            prompt = (
                "Based on this implementation design, generate an OpenAPI 3.1 specification "
                "as YAML. Include every API endpoint mentioned in the design with:\n"
                "- Path, method, summary\n"
                "- Request body schema (if applicable)\n"
                "- Response schemas for success and error cases\n"
                "- Authentication requirements\n"
                "- Status codes\n\n"
                "Output ONLY valid YAML starting with 'openapi: 3.1.0'. No markdown, no explanation.\n\n"
                f"DESIGN:\n{design[:40000]}"
            )

            response = await self.llm.ainvoke([
                SystemMessage(content="Output only valid OpenAPI 3.1 YAML. No markdown fences."),
                HumanMessage(content=prompt),
            ])

            contract = response.content
            logger.info("[design_agent] OpenAPI contract generated: %d chars", len(contract))

            # Embed project_id for association
            project_id = state.get("project_id", "")
            if project_id:
                contract = f"<!-- project_id: {project_id} -->\n{contract}"

            # Publish as a separate document
            try:
                await self.client.publish_document(
                    content=contract,
                    title=f"{clean_topic} — OpenAPI Contract",
                    from_agent=self.from_agent,
                    format="yaml",
                )
                logger.info("[design_agent] OpenAPI contract published")
            except Exception as e:
                logger.warning("[design_agent] Contract publish failed: %s", e)

        except Exception as e:
            logger.warning("[design_agent] Contract generation failed: %s", e)

        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "generate_contracts", "completed")
        return state

    # -- Helpers ---------------------------------------------------------------

    @staticmethod
    def _parse_score(review_text: str) -> int:
        """Extract SCORE: N from review response. Default 50 if unparseable."""
        match = re.search(r"SCORE:\s*(\d+)", review_text)
        if match:
            score = int(match.group(1))
            return min(100, max(0, score))
        return 50  # Default if LLM doesn't follow format


# -- NAT Registration -------------------------------------------------------


class DesignAgentConfig(FunctionBaseConfig, name="design_agent"):
    """Configuration for the LangGraph design agent with review loop."""

    llm_name: LLMRef = Field(..., description="LLM to use for design synthesis")
    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub URL for document publishing and bus announcements",
    )
    from_agent: str = Field(
        default="builder",
        description="Agent ID for bus announcements and document attribution",
    )
    quality_threshold: int = Field(
        default=80,
        description="Minimum average review score (0-100) to approve the design",
    )
    max_iterations: int = Field(
        default=5,
        description="Maximum review-revise iterations before accepting as-is",
    )


@register_function(config_type=DesignAgentConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def design_agent_fn(
    config: DesignAgentConfig, builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """Build the LangGraph design pipeline and register as a NAT function."""
    logger.info("[design_agent] Initializing LangGraph design agent")

    llm = await builder.get_llm(config.llm_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)
    client = NCMSHttpClient(hub_url=config.hub_url)

    agent = DesignAgent(
        llm=llm,
        hub_url=config.hub_url,
        from_agent=config.from_agent,
        client=client,
        quality_threshold=config.quality_threshold,
        max_iterations=config.max_iterations,
    )
    graph = await agent.build_graph()
    logger.info("[design_agent] LangGraph pipeline ready")

    async def _design(input_message: str) -> str:
        """Run the full design pipeline with review loop."""
        logger.info("[design_agent] === Starting design pipeline ===")
        logger.info("[design_agent] Input: %s", input_message[:200])

        project_id = extract_project_id(input_message)
        # Builder has 10+ nodes + review loop (up to 5 iterations × 2 nodes each)
        # Default recursion limit of 15 is too low
        result = await graph.ainvoke({
            "topic": input_message,
            "source_doc_id": None,
            "source_content": "",
            "manifest": {},
            "expert_input": {},
            "design": "",
            "document_id": None,
            "messages": [HumanMessage(content=input_message)],
            "review_scores": {},
            "review_feedback": {},
            "iteration": 0,
            "guardrail_violations": [],
            "guardrail_fix_iteration": 0,
            "project_id": project_id,
        }, config={"recursion_limit": 50})

        design = result.get("design", "Design pipeline produced no output.")
        doc_id = result.get("document_id")
        scores = result.get("review_scores", {})

        logger.info("[design_agent] === Pipeline complete ===")
        logger.info(
            "[design_agent] Design: %d chars | Doc: %s | Scores: %s",
            len(design), doc_id, scores,
        )
        logger.info("[design_agent] Returning to auto_memory for persistence")

        return design

    try:
        yield FunctionInfo.from_fn(
            _design,
            description=(
                "Implementation design agent with review loop. Reads a PRD document, "
                "queries architecture and security experts, synthesizes a TypeScript "
                "implementation design, then submits for expert review. If the average "
                "score is below 80%, the design is revised with feedback and re-reviewed "
                "up to 5 times. Returns the final approved design."
            ),
        )
    finally:
        await client.close()
        logger.info("[design_agent] Cleaned up HTTP client")
