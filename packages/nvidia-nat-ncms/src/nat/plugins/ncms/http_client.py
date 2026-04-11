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
        request_timeout: float = 300.0,
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
        body: dict[str, Any] = {"q": query, "limit": limit}
        if domain:
            body["domain"] = domain
        # POST for large queries (avoids 431), GET for short ones
        if len(query) > 2000:
            resp = await self._client.post("/api/v1/memories/recall", json=body)
        else:
            resp = await self._client.get("/api/v1/memories/recall", params=body)
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
        body: dict[str, Any] = {"q": query, "limit": limit}
        if domain:
            body["domain"] = domain
        if len(query) > 2000:
            resp = await self._client.post("/api/v1/memories/search", json=body)
        else:
            resp = await self._client.get("/api/v1/memories/search", params=body)
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
        doc_type: str | None = None,
        parent_doc_id: str | None = None,
        format: str = "markdown",
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"title": title, "content": content, "format": format}
        if from_agent:
            body["from_agent"] = from_agent
        if plan_id:
            body["plan_id"] = plan_id
        if doc_type:
            body["doc_type"] = doc_type
        if parent_doc_id:
            body["parent_doc_id"] = parent_doc_id
        if metadata:
            body["metadata"] = metadata
        resp = await self._client.post("/api/v1/documents", json=body)
        resp.raise_for_status()
        return resp.json()

    async def read_document(self, document_id: str) -> dict[str, Any]:
        """Fetch a document by ID.  Returns {document_id, title, content, ...}."""
        resp = await self._client.get(f"/api/v1/documents/{document_id}")
        resp.raise_for_status()
        return resp.json()

    async def create_document_link(
        self,
        source_doc_id: str,
        target_doc_id: str,
        link_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a typed link between two documents."""
        body: dict[str, Any] = {
            "source_doc_id": source_doc_id,
            "target_doc_id": target_doc_id,
            "link_type": link_type,
        }
        if metadata:
            body["metadata"] = metadata
        resp = await self._client.post("/api/v1/documents/links", json=body)
        resp.raise_for_status()
        return resp.json()

    async def save_review_score(
        self,
        document_id: str,
        project_id: str | None,
        reviewer_agent: str,
        review_round: int,
        score: int | None = None,
        severity: str | None = None,
        covered: str | None = None,
        missing: str | None = None,
        changes: str | None = None,
    ) -> dict[str, Any]:
        """Save a structured review score."""
        body: dict[str, Any] = {
            "document_id": document_id,
            "project_id": project_id,
            "reviewer_agent": reviewer_agent,
            "review_round": review_round,
        }
        if score is not None:
            body["score"] = score
        if severity:
            body["severity"] = severity
        if covered:
            body["covered"] = covered
        if missing:
            body["missing"] = missing
        if changes:
            body["changes"] = changes
        resp = await self._client.post("/api/v1/reviews", json=body)
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

    # ── Audit Records ────────────────────────────────────────────────────

    async def record_llm_call(
        self, project_id: str | None, agent: str, node: str,
        prompt_size: int | None = None, response_size: int | None = None,
        reasoning_size: int = 0, model: str | None = None,
        thinking_enabled: bool = False, duration_ms: int | None = None,
        prompt_hash: str | None = None,
    ) -> None:
        """Report an LLM call for the audit trail (fire-and-forget)."""
        try:
            await self._client.post("/api/v1/audit/llm-call", json={
                "project_id": project_id, "agent": agent, "node": node,
                "prompt_size": prompt_size, "response_size": response_size,
                "reasoning_size": reasoning_size, "model": model,
                "thinking_enabled": thinking_enabled, "duration_ms": duration_ms,
                "prompt_hash": prompt_hash,
            })
        except Exception:
            pass  # Non-fatal — audit is best-effort

    async def record_config_snapshot(
        self, project_id: str | None, agent: str,
        model_name: str | None = None, thinking_enabled: bool = False,
        max_tokens: int | None = None, prompt_version: str | None = None,
        config_hash: str | None = None,
    ) -> None:
        """Report agent config at pipeline start (fire-and-forget)."""
        try:
            await self._client.post("/api/v1/audit/config-snapshot", json={
                "project_id": project_id, "agent": agent,
                "model_name": model_name, "thinking_enabled": thinking_enabled,
                "max_tokens": max_tokens, "prompt_version": prompt_version,
                "config_hash": config_hash,
            })
        except Exception:
            pass

    async def record_grounding(
        self, document_id: str, memory_id: str,
        retrieval_score: float | None = None,
        entity_query: str | None = None,
        domain: str | None = None,
    ) -> None:
        """Report memory grounding for a review citation (fire-and-forget)."""
        try:
            await self._client.post("/api/v1/audit/grounding", json={
                "document_id": document_id, "memory_id": memory_id,
                "retrieval_score": retrieval_score,
                "entity_query": entity_query, "domain": domain,
            })
        except Exception:
            pass

    # ── Health ────────────────────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        resp = await self._client.get("/api/v1/health")
        resp.raise_for_status()
        return resp.json()
