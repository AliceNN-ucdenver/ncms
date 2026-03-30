# SPDX-License-Identifier: Apache-2.0
"""NCMS memory provider configuration."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from nat.data_models.memory import MemoryBaseConfig


class NCMSMemoryConfig(MemoryBaseConfig, name="ncms_memory"):
    """Configuration for NCMS as a NAT memory backend.

    NCMS provides BM25 + SPLADE sparse neural + graph spreading activation
    retrieval — no dense vectors needed. The MemoryEditor receives raw query
    text and delegates to the NCMS Hub HTTP API.
    """

    hub_url: str = Field(
        default="http://host.docker.internal:9080",
        description="NCMS Hub API base URL",
    )
    agent_id: str = Field(
        default="nat-agent",
        description="Agent ID for Knowledge Bus registration",
    )
    domains: list[str] = Field(
        default_factory=list,
        description="Domains this agent handles (registers as handler on the bus)",
    )
    subscribe_to: list[str] = Field(
        default_factory=list,
        description="Domains to subscribe to for SSE announcements",
    )
    enable_sse: bool = Field(
        default=True,
        description="Listen for SSE announcements and auto-store as memories",
    )
    recall_limit: int = Field(
        default=10,
        description="Default limit for recall/search queries",
    )
    connect_timeout_s: float = Field(
        default=10.0,
        description="HTTP connection timeout in seconds",
    )
    request_timeout_s: float = Field(
        default=120.0,
        description="HTTP request timeout in seconds",
    )
    knowledge_paths: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Directories of knowledge files to load into NCMS Hub on startup. "
            "Each entry: {path: '/sandbox/knowledge/...', domains: ['arch']}"
        ),
    )
