# SPDX-License-Identifier: Apache-2.0
"""Shared utilities for pipeline LangGraph agents.

Provides typed document ID extraction, project_id propagation,
lightweight pipeline telemetry, and interrupt checking.

Document ID formats (each type has its own tag to avoid ambiguity):
  (research_id: XXXX)  — Market research reports
  (prd_id: XXXX)       — Product requirements documents
  (design_id: XXXX)    — Implementation designs
  (review_id: XXXX)    — Design review reports
  (project_id: PRJ-XX) — Project identifier
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# OpenTelemetry — optional, no hard dependency.
# Tracer is resolved lazily (not at import time) because NAT initializes
# OTel from YAML config AFTER our module is imported. Getting the tracer
# at import time returns a no-op tracer with no exporter attached.
_otel_available = False
try:
    from opentelemetry import trace as otel_trace
    _otel_available = True
except Exception:
    otel_trace = None  # type: ignore[assignment]

# LangChain/LangGraph instrumentor — captures LLM calls with full
# prompt/response content in OpenInference format for Phoenix.
# Initialized lazily on first LLM call (after NAT sets up OTel).
_langchain_instrumented = False


def _ensure_langchain_instrumented():
    """One-time setup: instrument LangChain for OpenInference tracing."""
    global _langchain_instrumented  # noqa: PLW0603
    if _langchain_instrumented:
        return
    _langchain_instrumented = True
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor
        LangChainInstrumentor().instrument()
        logger.info("[otel] LangChain instrumented for Phoenix tracing")
    except Exception as e:
        logger.debug("[otel] LangChain instrumentation not available: %s", e)


def _get_tracer():
    """Get the OTel tracer lazily — NAT must have initialized OTel first."""
    if not _otel_available:
        return None
    try:
        return otel_trace.get_tracer("ncms.agents")
    except Exception:
        return None

# ── ID Extraction ────────────────────────────────────────────────────────────

_PROJECT_ID_PATTERN = re.compile(r"\(project_id:\s*(PRJ-[a-f0-9]{8})\)")
_RESEARCH_ID_PATTERN = re.compile(r"\(research_id:\s*([^)]+)\)")
_PRD_ID_PATTERN = re.compile(r"\(prd_id:\s*([^)]+)\)")
_DESIGN_ID_PATTERN = re.compile(r"\(design_id:\s*([^)]+)\)")
_REVIEW_ID_PATTERN = re.compile(r"\(review_id:\s*([^)]+)\)")
# Legacy fallback — matches any (doc_id: XXX) if typed patterns don't match
_DOC_ID_PATTERN = re.compile(r"\(doc_id:\s*([^)]+)\)")


def extract_project_id(text: str) -> str | None:
    """Extract (project_id: PRJ-XXXXXXXX) from text."""
    match = _PROJECT_ID_PATTERN.search(text)
    return match.group(1) if match else None


def extract_research_id(text: str) -> str | None:
    """Extract (research_id: XXXX) from text."""
    match = _RESEARCH_ID_PATTERN.search(text)
    return match.group(1).strip() if match else None


def extract_prd_id(text: str) -> str | None:
    """Extract (prd_id: XXXX) from text."""
    match = _PRD_ID_PATTERN.search(text)
    return match.group(1).strip() if match else None


def extract_design_id(text: str) -> str | None:
    """Extract (design_id: XXXX) from text."""
    match = _DESIGN_ID_PATTERN.search(text)
    return match.group(1).strip() if match else None


def extract_doc_id(text: str) -> str | None:
    """Extract any document ID from text. Tries typed patterns first, falls back to (doc_id:)."""
    for pattern in [_RESEARCH_ID_PATTERN, _PRD_ID_PATTERN, _DESIGN_ID_PATTERN, _REVIEW_ID_PATTERN]:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    # Legacy fallback
    match = _DOC_ID_PATTERN.search(text)
    return match.group(1).strip() if match else None


def extract_topic(text: str) -> str:
    """Extract a clean topic from a trigger message.

    Strips nested document references and trigger prefixes iteratively
    to produce a human-readable topic for document titles.

    Examples:
        "Create implementation design for: Create a PRD for: Research auth patterns..."
        → "Authentication patterns for identity services"

        "Research auth patterns for identity services (project_id: PRJ-123)"
        → "Authentication patterns for identity services"
    """
    topic = text

    # Remove ID tags first (before stripping prefixes, so nested IDs are gone)
    topic = re.sub(
        r'\((research_id|prd_id|design_id|review_id|project_id|doc_id):\s*[^)]+\)',
        '', topic,
    )

    # Remove quoted nested titles
    topic = re.sub(r'"[^"]*"', '', topic).strip()

    # Strip known prefixes ITERATIVELY (handles nested chains)
    prefixes = [
        "Create implementation design for:",
        "Create a PRD for:",
        "Create a PRD based on this market research:",
        "Create implementation design based on PRD:",
        "Research",
    ]
    changed = True
    while changed:
        changed = False
        stripped = topic.strip().lstrip(' :-–—')
        for prefix in prefixes:
            if stripped.lower().startswith(prefix.lower()):
                stripped = stripped[len(prefix):]
                changed = True
        topic = stripped

    # Remove "for Market research document suitable for..." boilerplate
    for boilerplate in [
        "for Market research document suitable for a product owner to develop a PRD from",
        "for Production ready design document",
    ]:
        topic = topic.replace(boilerplate, "").strip()

    # Clean up whitespace and trailing punctuation
    topic = re.sub(r'\s+', ' ', topic).strip().rstrip(' :-–—,.')

    # Capitalize first letter
    if topic and topic[0].islower():
        topic = topic[0].upper() + topic[1:]

    # If nothing left, return original first 80 chars
    return topic if len(topic) > 3 else text[:80]


# ── Trigger Message Builder ──────────────────────────────────────────────────


def build_research_trigger(topic: str, project_id: str | None = None) -> str:
    """Build trigger message for the Researcher."""
    msg = f"Research {topic}"
    if project_id:
        msg += f" (project_id: {project_id})"
    return msg


def build_prd_trigger(
    topic: str, research_id: str, project_id: str | None = None,
) -> str:
    """Build trigger message for the Product Owner."""
    msg = f"Create a PRD for: {topic} (research_id: {research_id})"
    if project_id:
        msg += f" (project_id: {project_id})"
    return msg


def build_design_trigger(
    topic: str, prd_id: str, project_id: str | None = None,
) -> str:
    """Build trigger message for the Builder."""
    msg = f"Create implementation design for: {topic} (prd_id: {prd_id})"
    if project_id:
        msg += f" (project_id: {project_id})"
    return msg


# ── Archaeology Trigger ──────────────────────────────────────────────────────

_REPO_URL_PATTERN = re.compile(r"Analyze repository:\s*(\S+)")
_GOAL_PATTERN = re.compile(r"Goal:\s*(.+?)(?:\(project_id:|$)", re.DOTALL)


def extract_repo_url(text: str) -> str | None:
    """Extract repository URL from an archaeology trigger message."""
    match = _REPO_URL_PATTERN.search(text)
    return match.group(1).strip() if match else None


def extract_goal(text: str) -> str | None:
    """Extract project goal from an archaeology trigger message."""
    match = _GOAL_PATTERN.search(text)
    return match.group(1).strip() if match else None


def build_archaeology_trigger(
    repository_url: str, goal: str, project_id: str | None = None,
) -> str:
    """Build trigger message for the Archeologist."""
    msg = f"Analyze repository: {repository_url}\nGoal: {goal}"
    if project_id:
        msg += f" (project_id: {project_id})"
    return msg


# ── Document-by-Reference ───────────────────────────────────────────────────
# Agents pass doc_id references in bus messages. The receiving agent fetches
# the document from NCMS hub and caches it locally for LLM context.


async def fetch_and_cache_document(
    client: "NCMSHttpClient",  # noqa: F821 — avoid circular import
    doc_id: str,
) -> tuple[str, list[dict]]:
    """Fetch a document from the NCMS hub and cache it locally.

    Returns (content, entities) where entities are the GLiNER-extracted
    metadata from the document sidecar. The document is cached to
    /tmp/ncms-docs/{doc_id}.md for LLM context.
    """
    from pathlib import Path

    logger.info("[doc-ref] Fetching document %s from hub", doc_id)
    doc = await client.read_document(doc_id)
    content = doc.get("content", "")
    entities = doc.get("entities", [])

    # Cache locally
    cache_path = Path(f"/tmp/ncms-docs/{doc_id}.md")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(content, encoding="utf-8")

    entity_names = [e.get("name", "") for e in entities[:5]]
    logger.info(
        "[doc-ref] Cached %s to %s (%d chars, %d entities: %s)",
        doc_id, cache_path, len(content), len(entities),
        ", ".join(entity_names),
    )
    return content, entities


def build_entity_search_query(
    entities: list[dict],
    domain: str | None = None,
    max_entities: int = 12,
) -> str:
    """Build a search query from document entity metadata.

    Combines domain-specific boost terms with entity names from the
    document sidecar. This produces targeted BM25/SPLADE queries that
    match governance knowledge without sending raw document content.
    """
    domain_boost = {
        "architecture": "ADR architecture decisions CALM quality attributes",
        "security": "STRIDE threat model OWASP security controls",
    }.get(domain or "", "")

    entity_names = " ".join(e.get("name", "") for e in entities[:max_entities])
    query = f"{domain_boost} {entity_names}".strip()
    logger.info("[doc-ref] Entity search query: %s", query[:120])
    return query


# ── Pipeline Telemetry ───────────────────────────────────────────────────────


async def emit_telemetry(
    hub_url: str,
    project_id: str | None,
    agent: str,
    node: str,
    status: str,
    detail: str = "",
) -> None:
    """Emit a lightweight pipeline telemetry event to the hub.

    These events are NOT stored as memories. They flow through a dedicated
    telemetry channel for dashboard pipeline progress visualization.
    """
    if not project_id:
        return

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{hub_url}/api/v1/pipeline/events",
                json={
                    "project_id": project_id,
                    "agent": agent,
                    "node": node,
                    "status": status,
                    "detail": detail,
                },
            )
    except Exception:
        pass  # Best-effort, never blocks the pipeline


# ── Interrupt Checking ───────────────────────────────────────────────────────


async def snapshot_agent_config(
    client: Any,
    project_id: str | None,
    agent: str,
    llm: Any,
) -> None:
    """Record agent config snapshot at pipeline start (fire-and-forget)."""
    if not client or not project_id:
        return
    model_name = getattr(llm, "model_name", None) or getattr(llm, "model", None)
    # Detect thinking/max_tokens from model kwargs
    kwargs = getattr(llm, "model_kwargs", {}) or {}
    thinking = kwargs.get("thinking", {}).get("enabled", False) if isinstance(kwargs.get("thinking"), dict) else False
    max_tokens = getattr(llm, "max_tokens", None) or kwargs.get("max_tokens")
    try:
        await client.record_config_snapshot(
            project_id=project_id, agent=agent,
            model_name=str(model_name) if model_name else None,
            thinking_enabled=thinking,
            max_tokens=max_tokens,
        )
    except Exception:
        pass  # Non-fatal


async def check_interrupt(hub_url: str, agent_id: str) -> bool:
    """Check if an interrupt signal has been sent for this agent."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(
                f"{hub_url}/api/v1/pipeline/interrupt/{agent_id}",
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("interrupted", False)
    except Exception:
        pass
    return False


# ── Traced LLM Call ─────────────────────────────────────────────────────────
# Wraps ainvoke() with OpenTelemetry span + audit record.
# Phoenix captures the full LLM context; the llm_calls table stores
# queryable metadata (sizes, duration, model) with trace_id for linkage.


async def traced_llm_call(
    llm: Any,
    messages: list,
    *,
    hub_url: str,
    client: "NCMSHttpClient | None" = None,  # noqa: F821
    project_id: str | None = None,
    agent: str = "unknown",
    node: str = "unknown",
    model_name: str | None = None,
) -> Any:
    """Call llm.ainvoke() with an OTel span and audit record.

    On first call, instruments LangChain for OpenInference tracing
    so Phoenix captures full LLM prompt/response content.

    Returns the LLM response object. The span and audit record are
    best-effort — failures never block the pipeline.
    """
    # Instrument LangChain on first call (after NAT has initialized OTel)
    _ensure_langchain_instrumented()

    # Compute prompt size for audit
    prompt_text = " ".join(
        getattr(m, "content", str(m)) for m in messages
    )
    prompt_size = len(prompt_text)
    prompt_hash = hashlib.sha256(prompt_text.encode()).hexdigest()[:16]

    # Detect model name from llm object if not provided
    if not model_name:
        model_name = getattr(llm, "model_name", None) or getattr(llm, "model", None)

    span = None
    trace_id = None
    t0 = time.monotonic()

    try:
        # Create OTel span if tracer is available (lazy — NAT initializes OTel first)
        tracer = _get_tracer()
        if tracer:
            span = tracer.start_span(f"{agent}.{node}")
            span.set_attribute("ncms.agent", agent)
            span.set_attribute("ncms.node", node)
            span.set_attribute("ncms.prompt_size", prompt_size)
            if project_id:
                span.set_attribute("ncms.project_id", project_id)
            if model_name:
                span.set_attribute("ncms.model", str(model_name))
            # Get trace ID for audit linkage
            ctx = span.get_span_context()
            if ctx and ctx.trace_id:
                trace_id = format(ctx.trace_id, "032x")

        # Actual LLM call
        response = await llm.ainvoke(messages)

        duration_ms = int((time.monotonic() - t0) * 1000)
        response_text = getattr(response, "content", str(response))
        response_size = len(response_text)

        # Check for reasoning content (CoT)
        reasoning_size = 0
        reasoning = getattr(response, "additional_kwargs", {}).get("reasoning_content")
        if reasoning:
            reasoning_size = len(reasoning)

        if span:
            span.set_attribute("ncms.response_size", response_size)
            span.set_attribute("ncms.reasoning_size", reasoning_size)
            span.set_attribute("ncms.duration_ms", duration_ms)

        # Record audit entry (fire-and-forget)
        if client:
            try:
                await client.record_llm_call(
                    project_id=project_id, agent=agent, node=node,
                    prompt_size=prompt_size, response_size=response_size,
                    reasoning_size=reasoning_size, model=str(model_name) if model_name else None,
                    thinking_enabled=reasoning_size > 0,
                    duration_ms=duration_ms, prompt_hash=prompt_hash,
                )
            except Exception:
                pass

        logger.info(
            "[%s.%s] LLM call: %d→%d chars (%dms)%s",
            agent, node, prompt_size, response_size, duration_ms,
            f" +{reasoning_size} reasoning" if reasoning_size else "",
        )
        return response

    except Exception:
        if span:
            span.set_attribute("ncms.error", True)
        raise
    finally:
        if span:
            span.end()
