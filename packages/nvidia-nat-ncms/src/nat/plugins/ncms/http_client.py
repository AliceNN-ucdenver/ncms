# SPDX-License-Identifier: Apache-2.0
"""Async HTTP client wrapping the NCMS Hub API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class NCMSHttpClient:
    """Thin async wrapper around the NCMS Hub REST API.

    All methods translate to a single HTTP call and return the parsed JSON body.
    Retry logic is intentionally omitted here — the caller (editor / SSE listener)
    handles retries at the operation level, matching the pattern in ``bus_agent.py``.
    """

    def __init__(
        self,
        hub_url: str,
        connect_timeout: float = 10.0,
        request_timeout: float = 120.0,
    ) -> None:
        self._base = hub_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=httpx.Timeout(request_timeout, connect=connect_timeout),
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ── Memory operations ─────────────────────────────────────────────────

    async def store_memory(
        self,
        content: str,
        *,
        type: str = "fact",
        domains: list[str] | None = None,
        tags: list[str] | None = None,
        importance: float = 5.0,
        source_agent: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"content": content, "type": type, "importance": importance}
        if domains:
            body["domains"] = domains
        if tags:
            body["tags"] = tags
        if source_agent:
            body["source_agent"] = source_agent
        resp = await self._client.post("/api/v1/memories", json=body)
        resp.raise_for_status()
        return resp.json()

    async def recall_memory(
        self,
        query: str,
        *,
        domain: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"q": query, "limit": limit}
        if domain:
            params["domain"] = domain
        resp = await self._client.get("/api/v1/memories/recall", params=params)
        if resp.status_code != 200:
            logger.debug("recall_memory returned %s, falling back to search", resp.status_code)
            return await self.search_memory(query, domain=domain, limit=limit)
        data = resp.json()
        return data if isinstance(data, list) else data.get("results", [])

    async def search_memory(
        self,
        query: str,
        *,
        domain: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"q": query, "limit": limit}
        if domain:
            params["domain"] = domain
        resp = await self._client.get("/api/v1/memories/search", params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        resp = await self._client.delete(f"/api/v1/memories/{memory_id}")
        resp.raise_for_status()
        return resp.json()

    async def load_knowledge(
        self,
        file_path: str,
        domains: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"file_path": file_path}
        if domains:
            body["domains"] = domains
        resp = await self._client.post("/api/v1/knowledge/load", json=body)
        resp.raise_for_status()
        return resp.json()

    # ── Knowledge Bus operations ──────────────────────────────────────────

    async def bus_register(
        self,
        agent_id: str,
        domains: list[str],
        subscribe_to: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"agent_id": agent_id, "domains": domains}
        if subscribe_to:
            body["subscribe_to"] = subscribe_to
        resp = await self._client.post("/api/v1/bus/register", json=body)
        resp.raise_for_status()
        return resp.json()

    async def bus_deregister(self, agent_id: str) -> dict[str, Any]:
        resp = await self._client.post(
            "/api/v1/bus/deregister", json={"agent_id": agent_id}
        )
        resp.raise_for_status()
        return resp.json()

    async def bus_ask(
        self,
        question: str,
        domains: list[str],
        *,
        from_agent: str = "nat-agent",
        timeout_ms: int = 60000,
    ) -> dict[str, Any]:
        body = {
            "question": question,
            "domains": domains,
            "from_agent": from_agent,
            "timeout_ms": timeout_ms,
        }
        resp = await self._client.post("/api/v1/bus/ask", json=body)
        resp.raise_for_status()
        return resp.json()

    async def bus_announce(
        self,
        content: str,
        domains: list[str],
        *,
        from_agent: str = "nat-agent",
        event: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "content": content,
            "domains": domains,
            "from_agent": from_agent,
        }
        if event:
            body["event"] = event
        resp = await self._client.post("/api/v1/bus/announce", json=body)
        resp.raise_for_status()
        return resp.json()

    async def bus_respond(
        self,
        ask_id: str,
        content: str,
        *,
        from_agent: str = "nat-agent",
        confidence: float = 0.5,
    ) -> dict[str, Any]:
        body = {
            "ask_id": ask_id,
            "content": content,
            "from_agent": from_agent,
            "confidence": confidence,
        }
        resp = await self._client.post("/api/v1/bus/respond", json=body)
        resp.raise_for_status()
        return resp.json()

    # ── Document operations ──────────────────────────────────────────────

    async def publish_document(
        self,
        content: str,
        title: str,
        *,
        from_agent: str | None = None,
        plan_id: str | None = None,
        format: str = "markdown",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"title": title, "content": content, "format": format}
        if from_agent:
            body["from_agent"] = from_agent
        if plan_id:
            body["plan_id"] = plan_id
        resp = await self._client.post("/api/v1/documents", json=body)
        resp.raise_for_status()
        return resp.json()

    async def read_document(self, document_id: str) -> dict[str, Any]:
        """Fetch a document by ID.  Returns {document_id, title, content, ...}."""
        resp = await self._client.get(f"/api/v1/documents/{document_id}")
        resp.raise_for_status()
        return resp.json()

    # ── Agent trigger (auto-chain) ───────────────────────────────────────

    async def trigger_agent(
        self, agent_id: str, message: str, *, timeout: float = 600.0,
    ) -> dict[str, Any]:
        """Send a message to another agent's /generate endpoint via hub proxy.

        Used by the verify node to auto-chain: researcher → PO → builder.
        The long timeout (10 min) accommodates full LangGraph pipelines.
        """
        resp = await self._client.post(
            f"/api/v1/agent/{agent_id}/chat",
            json={"input_message": message},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Health ────────────────────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        resp = await self._client.get("/api/v1/health")
        resp.raise_for_status()
        return resp.json()
