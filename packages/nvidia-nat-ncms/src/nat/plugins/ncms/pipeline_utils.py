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

import logging
import re

import httpx

logger = logging.getLogger(__name__)

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

    Strips nested document references and trigger prefixes to produce
    a human-readable topic for document titles.
    """
    # Remove known prefixes
    topic = text
    for prefix in [
        "Research ", "Create a PRD based on this market research: ",
        "Create implementation design based on PRD: ",
    ]:
        if topic.startswith(prefix):
            topic = topic[len(prefix):]

    # Remove quoted nested titles
    topic = re.sub(r'"[^"]*"', '', topic).strip()

    # Remove ID tags
    topic = re.sub(r'\((research_id|prd_id|design_id|review_id|project_id|doc_id):\s*[^)]+\)', '', topic)

    # Clean up whitespace and trailing punctuation
    topic = re.sub(r'\s+', ' ', topic).strip().rstrip(' :-–—')

    # If nothing left, return original first 80 chars
    return topic if topic else text[:80]


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
