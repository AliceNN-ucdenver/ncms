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
from .pipeline_utils import extract_project_id, extract_prd_id, extract_topic, emit_telemetry, check_interrupt, traced_llm_call, snapshot_agent_config
from .design_prompts import (
    SYNTHESIZE_DESIGN_PROMPT,
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
    interrupted: bool


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

    async def _check_and_interrupt(self, state: DesignState, node: str) -> bool:
        """Check for interrupt signal. Returns True if interrupted."""
        if state.get("interrupted"):
            return True
        if await check_interrupt(self.hub_url, self.from_agent):
            state["interrupted"] = True
            logger.info("[design_agent] Interrupted at node %s", node)
            await emit_telemetry(
                self.hub_url, state.get("project_id"),
                self.from_agent, node, "interrupted",
            )
            return True
        return False

    async def check_guardrails(self, state: DesignState) -> DesignState:
        """Check input guardrails before pipeline starts.

        If violations have escalation=block or reject, pauses pipeline
        and waits for human approval via the hub approval gate.
        """
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "check_guardrails", "started")
        await snapshot_agent_config(self.client, state.get("project_id"), self.from_agent, self.llm)
        from .guardrails import request_approval, run_input_guardrails, wait_for_approval
        can_proceed, violations = await run_input_guardrails(self.hub_url, state["topic"], self.from_agent)
        if not can_proceed:
            logger.warning("[design_agent] Guardrails BLOCKED: %s", violations)
            # Request human approval
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
                    context={"topic": state["topic"]},
                )
                if approval_id:
                    decision, comment = await wait_for_approval(
                        self.hub_url, approval_id, self.from_agent,
                    )
                    if decision == "approved":
                        logger.info("[design_agent] Human approved guardrail override")
                        can_proceed = True
                    else:
                        logger.info("[design_agent] Human denied (%s): %s", decision, comment)
                        state["design"] = f"Pipeline denied by human: {decision}"
                        state["interrupted"] = True
                        await emit_telemetry(
                            self.hub_url, state.get("project_id"), self.from_agent,
                            "check_guardrails", "denied", f"Human denied: {comment or decision}",
                        )
                        return state
                else:
                    state["design"] = f"Pipeline blocked by guardrails: {[str(v) for v in violations]}"
            else:
                state["design"] = f"Pipeline blocked by guardrails: {[str(v) for v in violations]}"
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "check_guardrails", "completed")
        return state

    async def validate_completeness(self, state: DesignState) -> DesignState:
        """Validate structural completeness. No LLM."""
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "validate_completeness", "started")
        if await self._check_and_interrupt(state, "validate_completeness"):
            return state
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
        """Check output guardrails before publishing.

        For block/reject violations: pauses pipeline and waits for human
        approval via the hub approval gate. Warn violations are annotated.
        """
        await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "check_output_guardrails", "started")
        if await self._check_and_interrupt(state, "check_output_guardrails"):
            return state
        from .guardrails import request_approval, run_output_guardrails, wait_for_approval

        _, violations = await run_output_guardrails(
            self.hub_url, state["design"], self.from_agent,
        )
        state["guardrail_violations"] = [str(v) for v in violations]

        if not violations:
            logger.info("[design_agent] Output guardrails PASSED — no violations")
            await emit_telemetry(self.hub_url, state.get("project_id"), self.from_agent, "check_output_guardrails", "completed", "passed")
            return state

        blocking = [v for v in violations if v.escalation in ("block", "reject")]
        warns = [v for v in violations if v.escalation == "warn"]

        # Always annotate warn-level items
        if warns:
            warning_section = (
                "\n\n---\n"
                "## Guardrail Review Notes\n\n"
                "*The following items were flagged by automated guardrails for human review:*\n\n"
                + "\n".join(f"- {v}" for v in warns)
                + "\n\n*These may be false positives in a design document.*\n"
            )
            state["design"] += warning_section

        # Block/reject violations require human approval
        if blocking:
            logger.info(
                "[design_agent] Output guardrails: %d blocking violation(s), requesting approval",
                len(blocking),
            )
            await emit_telemetry(
                self.hub_url, state.get("project_id"), self.from_agent,
                "check_output_guardrails", "awaiting_approval",
                f"{len(blocking)} violation(s) require human approval",
            )
            approval_id = await request_approval(
                self.hub_url, self.from_agent, "check_output_guardrails",
                state.get("project_id"), blocking,
                context={"topic": state.get("topic", ""), "design_preview": state["design"][:500]},
            )
            if approval_id:
                decision, comment = await wait_for_approval(
                    self.hub_url, approval_id, self.from_agent,
                )
                if decision == "approved":
                    logger.info("[design_agent] Human approved output guardrail override")
                else:
                    logger.info("[design_agent] Human denied output: %s", decision)
                    state["design"] = f"Pipeline denied by human at output guardrails: {decision}"
                    state["interrupted"] = True
                    await emit_telemetry(
                        self.hub_url, state.get("project_id"), self.from_agent,
                        "check_output_guardrails", "denied",
                        f"Human denied: {comment or decision}",
                    )
                    return state
            # If approval creation failed, continue with annotations (graceful degradation)

        await emit_telemetry(
            self.hub_url, state.get("project_id"), self.from_agent,
            "check_output_guardrails", "completed",
            f"{len(violations)} items flagged ({len(blocking)} blocking, {len(warns)} warn)",
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

    def _after_input_guardrails(self, state: DesignState) -> str:
        """Route: if input guardrails denied/blocked, end the graph."""
        if state.get("interrupted"):
            return END
        return "continue"

    def _after_output_guardrails(self, state: DesignState) -> str:
        """Route: if output guardrails denied, end the graph."""
        if state.get("interrupted"):
            return END
        return "continue"

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

        # Forward path — guardrail gates route to END on denial
        graph.add_edge(START, "check_guardrails")
        graph.add_conditional_edges(
            "check_guardrails",
            self._after_input_guardrails,
            {"continue": "read_document", END: END},
        )
        graph.add_edge("read_document", "ask_experts")
        graph.add_edge("ask_experts", "synthesize_design")
        graph.add_edge("synthesize_design", "validate_completeness")
        graph.add_edge("validate_completeness", "check_output_guardrails")
        graph.add_conditional_edges(
            "check_output_guardrails",
            self._after_output_guardrails,
            {"continue": "publish_design", END: END},
        )
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
        if await self._check_and_interrupt(state, "read_document"):
            return state
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
        if await self._check_and_interrupt(state, "ask_experts"):
            return state
        topic = state["topic"]
        logger.info("[design_agent] Querying experts for: %s", topic[:100])

        # Document-by-reference: pass doc_id + entity keywords to experts
        source_doc_id = state.get("source_doc_id")
        project_id = state.get("project_id", "")
        entity_keywords = ""
        if source_doc_id:
            try:
                doc_meta = await self.client.read_document(source_doc_id)
                doc_entities = doc_meta.get("entities", [])
                if doc_entities:
                    entity_keywords = ", ".join(e["name"] for e in doc_entities[:10])
                    logger.info(
                        "[design_agent] Expert query: doc_id=%s entities=%s (ref-only)",
                        source_doc_id, entity_keywords[:80],
                    )
            except Exception as e:
                logger.debug("[design_agent] Failed to fetch entity metadata: %s", e)

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
                    f"What architectural patterns and ADRs apply to this implementation?\n"
                    f"(doc_id: {source_doc_id}) (project_id: {project_id})\n"
                    f"Key entities: {entity_keywords}"
                )
                result = await self.client.bus_ask(
                    question=question,
                    domains=["architecture", "decisions"],
                    from_agent=self.from_agent,
                    timeout_ms=300000,
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
                    f"What security threats and controls apply to this implementation?\n"
                    f"(doc_id: {source_doc_id}) (project_id: {project_id})\n"
                    f"Key entities: {entity_keywords}"
                )
                result = await self.client.bus_ask(
                    question=question,
                    domains=["security", "threats"],
                    from_agent=self.from_agent,
                    timeout_ms=300000,
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
        if await self._check_and_interrupt(state, "synthesize_design"):
            return state
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
            response = await traced_llm_call(
                self.llm, [
                    SystemMessage(
                        content=(
                            "You are an expert implementation architect. "
                            "Write detailed, actionable TypeScript implementation designs "
                            "with concrete code snippets."
                        )
                    ),
                    HumanMessage(content=prompt),
                ],
                hub_url=self.hub_url, client=self.client,
                project_id=state.get("project_id"),
                agent=self.from_agent, node="synthesize_design",
            )
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
        if await self._check_and_interrupt(state, "publish_design"):
            return state
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
            # Determine parent_doc_id for version chain
            prev_doc_id = state.get("document_id") if state.get("iteration", 0) > 0 else None

            result = await self.client.publish_document(
                content=versioned_design,
                title=f"{clean_topic} — Implementation Design {version}",
                from_agent=self.from_agent,
                doc_type="design",
                parent_doc_id=prev_doc_id,
                format="markdown",
            )
            doc_id = result.get("document_id", "unknown")
            state["document_id"] = doc_id
            logger.info("[design_agent] Design published: %s (type=design, parent=%s)", doc_id, prev_doc_id)

            # Create traceability link: Design → PRD
            source_doc_id = state.get("source_doc_id")
            if source_doc_id and doc_id != "unknown" and not prev_doc_id:
                try:
                    await self.client.create_document_link(
                        source_doc_id=doc_id,
                        target_doc_id=source_doc_id,
                        link_type="derived_from",
                    )
                    logger.info("[design_agent] Link: %s derived_from %s", doc_id, source_doc_id)
                except Exception as e:
                    logger.debug("[design_agent] Link creation failed: %s", e)

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
        if await self._check_and_interrupt(state, "request_review"):
            return state
        iteration = state.get("iteration", 0)
        doc_id = state.get("document_id", "")
        project_id = state.get("project_id", "")
        logger.info(
            "[design_agent] Review request: doc_id=%s project_id=%s round=%d (ref-only, no content in bus message)",
            doc_id, project_id, iteration + 1,
        )

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
                        timeout_ms=420000,  # 7 minutes (CoT experts need 4-5 min)
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

        # Document-by-reference: pass doc_id only, expert fetches content
        arch_prompt = (
            f"Review design document (doc_id: {doc_id}) (project_id: {project_id})\n"
            f"Use ARCHITECTURE review criteria."
        )
        sec_prompt = (
            f"Review design document (doc_id: {doc_id}) (project_id: {project_id})\n"
            f"Use SECURITY review criteria."
        )

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
        if await self._check_and_interrupt(state, "revise_design"):
            return state
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
            response = await traced_llm_call(
                self.llm, [
                    SystemMessage(
                        content=(
                            "You are revising an implementation design to address "
                            "expert review feedback. Produce a complete, improved design."
                        )
                    ),
                    HumanMessage(content=prompt),
                ],
                hub_url=self.hub_url, client=self.client,
                project_id=state.get("project_id"),
                agent=self.from_agent, node="revise_design",
            )
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
        if await self._check_and_interrupt(state, "verify"):
            return state
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
            review_result = await self.client.publish_document(
                content=review_doc,
                title=f"{clean_topic} — Design Review Report",
                from_agent=self.from_agent,
                doc_type="review",
                format="markdown",
            )
            review_doc_id = review_result.get("document_id")
            logger.info("[design_agent] Review report published: %s (type=review)", review_doc_id)

            # Create link: review → design
            if review_doc_id and doc_id:
                try:
                    await self.client.create_document_link(
                        source_doc_id=review_doc_id,
                        target_doc_id=doc_id,
                        link_type="reviews",
                        metadata={"avg_score": avg_score, "round": iteration + 1},
                    )
                except Exception:
                    pass

            # Persist review scores
            for agent_name in ["architect", "security"]:
                agent_score = scores.get(agent_name)
                if agent_score is not None:
                    try:
                        await self.client.save_review_score(
                            document_id=doc_id,
                            project_id=project_id,
                            reviewer_agent=agent_name,
                            review_round=iteration + 1,
                            score=agent_score,
                        )
                    except Exception:
                        pass

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
        if await self._check_and_interrupt(state, "generate_contracts"):
            return state
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

            response = await traced_llm_call(
                self.llm, [
                    SystemMessage(content="Output only valid OpenAPI 3.1 YAML. No markdown fences."),
                    HumanMessage(content=prompt),
                ],
                hub_url=self.hub_url, client=self.client,
                project_id=state.get("project_id"),
                agent=self.from_agent, node="generate_contracts",
            )

            contract = response.content
            logger.info("[design_agent] OpenAPI contract generated: %d chars", len(contract))

            # Embed project_id for association
            project_id = state.get("project_id", "")
            if project_id:
                contract = f"<!-- project_id: {project_id} -->\n{contract}"

            # Publish as a separate document
            try:
                contract_result = await self.client.publish_document(
                    content=contract,
                    title=f"{clean_topic} — OpenAPI Contract",
                    from_agent=self.from_agent,
                    doc_type="contract",
                    format="yaml",
                )
                contract_doc_id = contract_result.get("document_id")
                logger.info("[design_agent] Contract published: %s (type=contract)", contract_doc_id)

                # Link: contract → design
                design_doc_id = state.get("document_id")
                if contract_doc_id and design_doc_id:
                    try:
                        await self.client.create_document_link(
                            source_doc_id=contract_doc_id,
                            target_doc_id=design_doc_id,
                            link_type="derived_from",
                        )
                    except Exception:
                        pass
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
            "interrupted": False,
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
