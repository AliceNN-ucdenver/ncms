"""A2A Protocol client for outbound requests.

When the NCMS Knowledge Bus receives a question with no local handler,
this client forwards it to registered external A2A agents.

Usage:
    client = A2AClient("http://external-agent:8080/a2a")
    result = await client.send_task("memory_recall", {"query": "auth config"})
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class A2AClient:
    """Client for communicating with external A2A agents."""

    def __init__(self, endpoint: str, agent_id: str = "ncms-hub"):
        self.endpoint = endpoint
        self.agent_id = agent_id
        self._session = None

    async def _ensure_session(self):
        if self._session is None:
            try:
                import httpx

                self._session = httpx.AsyncClient(timeout=30.0)
            except ImportError:
                import aiohttp

                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def get_agent_card(self) -> dict[str, Any] | None:
        """Discover an external agent's capabilities."""
        await self._ensure_session()
        try:
            response = await self._request(
                {
                    "jsonrpc": "2.0",
                    "id": str(uuid.uuid4()),
                    "method": "agent/card",
                    "params": {},
                }
            )
            return response.get("result")
        except Exception as e:
            logger.warning("Failed to get agent card from %s: %s", self.endpoint, e)
            return None

    async def send_task(
        self,
        skill: str,
        parameters: dict[str, Any],
        task_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Send a task to an external A2A agent.

        Args:
            skill: Skill name to invoke.
            parameters: Skill parameters.
            task_id: Optional task ID (auto-generated if not provided).

        Returns:
            Task result dict, or None on failure.
        """
        await self._ensure_session()
        task_id = task_id or str(uuid.uuid4())

        try:
            response = await self._request(
                {
                    "jsonrpc": "2.0",
                    "id": str(uuid.uuid4()),
                    "method": "tasks/send",
                    "params": {
                        "id": task_id,
                        "skill": skill,
                        "parameters": parameters,
                    },
                }
            )
            return response.get("result")
        except Exception as e:
            logger.warning("A2A task failed (%s → %s): %s", skill, self.endpoint, e)
            return None

    async def _request(self, payload: dict) -> dict:
        """Send a JSON-RPC 2.0 request."""
        assert self._session is not None, "_ensure_session() must be called first"
        if hasattr(self._session, "post"):
            # httpx
            resp = await self._session.post(  # type: ignore[union-attr]
                self.endpoint,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Agent-ID": self.agent_id,
                },
            )
            return resp.json()  # type: ignore[no-any-return]
        else:
            # aiohttp
            async with self._session.post(  # type: ignore[union-attr]
                self.endpoint,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Agent-ID": self.agent_id,
                },
            ) as resp:
                return await resp.json()  # type: ignore[no-any-return]

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session is not None:
            if hasattr(self._session, "aclose"):
                await self._session.aclose()
            else:
                await self._session.close()
            self._session = None
