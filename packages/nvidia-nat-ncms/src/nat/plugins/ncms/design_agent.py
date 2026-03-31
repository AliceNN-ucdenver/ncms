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
from .pipeline_utils import extract_project_id, emit_telemetry

logger = logging.getLogger(__name__)


# -- State -------------------------------------------------------------------


class DesignState(TypedDict):
    """Graph state for the design pipeline with review loop."""

    topic: str  # Design subject
    source_doc_id: str | None  # PO's PRD doc ID (parsed from input)
    source_content: str  # PRD content
    expert_input: dict[str, str]  # {"architect": "...", "security": "..."}
    design: str  # Implementation design markdown
    document_id: str | None  # Published design doc ID
    messages: list[BaseMessage]  # LangGraph compat
    # Review loop fields
    review_scores: dict[str, int]  # {"architect": 85, "security": 72}
    review_feedback: dict[str, str]  # {"architect": "COVERED:...", "security": "..."}
    iteration: int  # Current review round (0 = first pass)
    project_id: str | None  # PRJ-XXXXXXXX for pipeline tracking


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

# -- Review Prompts (Looking Glass framework) --------------------------------

ARCHITECTURE_REVIEW_PROMPT = """\
You are an architecture reviewer evaluating an implementation design against \
documented architecture decisions and quality standards.

Your knowledge base contains ADRs (Architecture Decision Records), CALM model \
specifications, quality attribute scenarios, and C4 architecture diagrams.

IMPLEMENTATION DESIGN TO REVIEW:
{design_content}

Evaluate the design against these criteria:

1. **CALM Model Compliance**: Does the design align with documented service \
boundaries, component relationships, and containment hierarchies?

2. **ADR Compliance**: Does the design follow accepted ADRs? Check technology \
choices, communication patterns, data storage decisions, and authentication approaches. \
ADR violations are HIGH severity.

3. **Fitness Function Validation**: Does the design address measurable quality \
gates? Check complexity management, test coverage provisions, performance budgets \
(N+1 queries, pagination, async patterns), and dependency management.

4. **Quality Attribute Verification**: Does the design support availability \
(health checks, graceful shutdown), latency (hot path optimization, caching), \
throughput (connection pooling, rate limiting), and scalability (stateless design, \
externalized config)?

5. **Component Boundary Analysis**: Are coupling patterns appropriate? Is API \
clarity maintained? Is data ownership well-defined?

Respond in EXACTLY this format:
SCORE: [number 0-100]
SEVERITY: [Critical|High|Medium|Low]
COVERED: [what the design addresses correctly, referencing specific ADRs]
MISSING: [what needs to be added or changed]
CHANGES: [specific actionable changes required, numbered]
"""

SECURITY_REVIEW_PROMPT = """\
You are a security reviewer evaluating an implementation design against \
documented threat models and security standards.

Your knowledge base contains STRIDE threat models with specific threat IDs \
(THR-001, THR-002, etc.), OWASP control mappings, NIST references, and \
security control definitions.

IMPLEMENTATION DESIGN TO REVIEW:
{design_content}

Evaluate the design against these criteria:

1. **OWASP Top 10 Pattern Detection**: Check for broken access control, \
cryptographic failures, injection vulnerabilities, insecure design patterns, \
and security misconfiguration.

2. **STRIDE Threat Model Compliance**: Verify that documented threats \
(THR-001 Spoofing, THR-002 Tampering, etc.) have corresponding mitigations \
in the design. Flag unmitigated threats as HIGH severity.

3. **Security Controls Verification**: Confirm authentication, authorization, \
input validation, encryption (at rest and in transit), and audit logging are \
implemented without bypass mechanisms.

4. **Secrets Management**: Verify credentials are not hardcoded. Check for \
proper use of environment variables or vault integration.

5. **Transport Security**: Verify TLS enforcement, secure cookie settings, \
HSTS headers, and certificate validation.

Respond in EXACTLY this format:
SCORE: [number 0-100]
SEVERITY: [Critical|High|Medium|Low]
COVERED: [what the design addresses correctly, referencing specific threat IDs]
MISSING: [what needs to be added or changed]
CHANGES: [specific actionable changes required, numbered]
"""

REVISE_DESIGN_PROMPT = """\
You are revising an implementation design to address expert review feedback.
The design was scored by two domain experts. You MUST improve BOTH scores.

CURRENT DESIGN (being revised):
{original_design}

---

## Reviewer 1: ARCHITECTURE (Score: {arch_score}% — target: 80%+)

The architect evaluates CALM model compliance, ADR adherence, fitness \
functions, quality attributes, and component boundaries. Address EVERY \
item listed under MISSING and CHANGES to improve this score.

{arch_feedback}

---

## Reviewer 2: SECURITY (Score: {sec_score}% — target: 80%+)

The security expert evaluates OWASP Top 10 coverage, STRIDE threat model \
compliance, security controls, secrets management, and transport security. \
Address EVERY item listed under MISSING and CHANGES to improve this score.

{sec_feedback}

---

## Revision Instructions

1. For each MISSING item from BOTH reviewers, ADD new content to the \
appropriate section. Do NOT remove existing content to make room.
2. For each numbered CHANGES item, IMPROVE the relevant section by adding \
detail, code snippets, or configuration. Mark with: <!-- Rev {iteration}: change #N -->
3. PRESERVE everything listed under COVERED — do not remove, shorten, or \
summarize working content.
4. If the architecture score is below 80%, add ADR compliance details, \
CALM model alignment, quality attribute patterns, and fitness function gates.
5. If the security score is below 80%, add STRIDE threat mitigations with \
specific threat IDs, OWASP control implementations, and token management details.

CRITICAL: The revised design must IMPROVE compliance, not reduce content. \
Add sections, code examples, and implementation details. The output should \
be at least as detailed as the input — ideally more detailed with the \
addressed feedback incorporated as new subsections or enhanced code snippets.

Output the COMPLETE revised design. Include ALL original sections plus additions.
"""


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

    async def build_graph(self) -> StateGraph:
        """Build and compile the design pipeline with review loop."""
        graph = StateGraph(DesignState)

        graph.add_node("read_document", self.read_document)
        graph.add_node("ask_experts", self.ask_experts)
        graph.add_node("synthesize_design", self.synthesize_design)
        graph.add_node("publish_design", self.publish_design)
        graph.add_node("request_review", self.request_review)
        graph.add_node("revise_design", self.revise_design)
        graph.add_node("verify", self.verify)

        # Forward path
        graph.add_edge(START, "read_document")
        graph.add_edge("read_document", "ask_experts")
        graph.add_edge("ask_experts", "synthesize_design")
        graph.add_edge("synthesize_design", "publish_design")
        graph.add_edge("publish_design", "request_review")

        # Review loop: conditional edge
        graph.add_conditional_edges("request_review", self.should_revise)

        # Revise loops back to publish (then review again)
        graph.add_edge("revise_design", "publish_design")

        # Exit
        graph.add_edge("verify", END)

        compiled = graph.compile()
        logger.info(
            "[design_agent] Graph compiled: read_document → ask_experts → "
            "synthesize → publish → review ⟲ (threshold: %d%%, max: %d iterations)",
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
        design = state["design"]
        iteration = state.get("iteration", 0)
        version = f"v{iteration + 1}"
        scores = state.get("review_scores", {})
        suffix = f" ({version})" if iteration > 0 else ""

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
                title=f"{topic[:80]} — Implementation Design {version}",
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

        arch_prompt = ARCHITECTURE_REVIEW_PROMPT.format(design_content=design_for_review)
        sec_prompt = SECURITY_REVIEW_PROMPT.format(design_content=design_for_review)

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
        feedback = state.get("review_feedback", {})
        review_doc = (
            f"# Design Review Report — {topic[:60]}\n\n"
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
                title=f"{topic[:60]} — Design Review Report",
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
        result = await graph.ainvoke({
            "topic": input_message,
            "source_doc_id": None,
            "source_content": "",
            "expert_input": {},
            "design": "",
            "document_id": None,
            "messages": [HumanMessage(content=input_message)],
            "review_scores": {},
            "review_feedback": {},
            "iteration": 0,
            "project_id": project_id,
        })

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
