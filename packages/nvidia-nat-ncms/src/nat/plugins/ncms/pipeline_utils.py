# SPDX-License-Identifier: Apache-2.0
"""Shared utilities for pipeline LangGraph agents.

Provides project_id extraction, lightweight pipeline telemetry,
and interrupt checking used by all LangGraph agents.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Regex to extract project_id (PRJ-XXXXXXXX) from input messages
_PROJECT_ID_PATTERN = re.compile(r"(PRJ-[a-f0-9]{8})")

# Regex to extract doc_id from input messages
_DOC_ID_PATTERN = re.compile(r"\(doc_id:\s*([^)]+)\)")


def extract_project_id(text: str) -> str | None:
    """Extract PRJ-XXXXXXXX from text. Returns None if not found."""
    match = _PROJECT_ID_PATTERN.search(text)
    return match.group(1) if match else None


def extract_doc_id(text: str) -> str | None:
    """Extract doc_id from (doc_id: XXXX) in text. Returns None if not found."""
    match = _DOC_ID_PATTERN.search(text)
    return match.group(1).strip() if match else None


async def emit_telemetry(
    hub_url: str,
    project_id: str | None,
    agent: str,
    node: str,
    status: str,
    detail: str = "",
) -> None:
    """Emit a lightweight pipeline telemetry event to the hub.

    These events are NOT stored as memories. They go through a dedicated
    telemetry channel for dashboard pipeline progress visualization.

    Args:
        hub_url: NCMS Hub URL (e.g., http://host.docker.internal:9080)
        project_id: The project this event belongs to (PRJ-XXXXXXXX)
        agent: Agent ID (e.g., "researcher", "builder")
        node: LangGraph node name (e.g., "plan_queries", "synthesize")
        status: One of "started", "completed", "failed"
        detail: Optional detail message (e.g., "25 results found")
    """
    if not project_id:
        return  # No project context, skip telemetry

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
        pass  # Telemetry is best-effort, never blocks the pipeline


async def check_interrupt(hub_url: str, agent_id: str) -> bool:
    """Check if an interrupt signal has been sent for this agent.

    Returns True if the agent should stop its current pipeline.
    The interrupt is consumed (cleared) on read.
    """
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


def build_trigger_message(
    message: str,
    project_id: str | None = None,
    doc_id: str | None = None,
) -> str:
    """Build a trigger message with project_id and doc_id embedded.

    Args:
        message: The base trigger message
        project_id: Optional project ID to embed
        doc_id: Optional document ID to embed

    Returns:
        Message with (project_id: PRJ-XXX) and (doc_id: YYY) appended
    """
    parts = [message]
    if doc_id and f"doc_id: {doc_id}" not in message:
        parts.append(f"(doc_id: {doc_id})")
    if project_id and project_id not in message:
        parts.append(f"(project_id: {project_id})")
    return " ".join(parts)
