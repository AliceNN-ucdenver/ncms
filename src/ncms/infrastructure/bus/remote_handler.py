"""Remote ask handler — bridges in-process Knowledge Bus to HTTP agents via SSE.

When a remote agent registers via HTTP, the hub creates a RemoteAskHandler
and sets it as the ask handler on the AsyncKnowledgeBus. When a question
routes to this agent, the handler pushes the question to the agent's SSE
stream and waits for the agent to POST /api/v1/bus/respond with the answer.

This is the *only* new abstraction needed to make the existing bus work
across network boundaries. The bus itself is unchanged — it calls handlers
as Python callables, unaware whether they're local or remote.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ncms.domain.models import KnowledgeAsk, KnowledgeResponse

logger = logging.getLogger(__name__)


class RemoteAskHandler:
    """Bridges AsyncKnowledgeBus ask to a remote agent via SSE + HTTP response.

    Created when a remote agent registers via POST /api/v1/bus/register.
    The bus calls this handler like any local handler — it conforms to the
    ``AskHandler = Callable[[KnowledgeAsk], Awaitable[KnowledgeResponse | None]]``
    signature.
    """

    def __init__(self, agent_id: str, sse_queue: asyncio.Queue[dict[str, Any]]):
        self._agent_id = agent_id
        self._sse_queue = sse_queue
        self._pending: dict[str, asyncio.Future[KnowledgeResponse]] = {}

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def __call__(self, ask: KnowledgeAsk) -> KnowledgeResponse | None:
        """Called by AsyncKnowledgeBus when a question routes to this agent.

        1. Push question to the agent's SSE stream.
        2. Wait for the agent to POST /bus/respond with matching ask_id.
        3. Return the response (or None on timeout).
        """
        # Push question to SSE stream
        event_data = {
            "type": "bus.ask.routed",
            "ask_id": ask.ask_id,
            "from_agent": ask.from_agent,
            "question": ask.question,
            "domains": ask.domains,
            "urgency": ask.urgency,
            "ttl_ms": ask.ttl_ms,
        }
        try:
            self._sse_queue.put_nowait(event_data)
        except asyncio.QueueFull:
            logger.warning(
                "SSE queue full for agent %s, dropping ask %s",
                self._agent_id, ask.ask_id,
            )
            return None

        # Create a future and wait for POST /bus/respond to resolve it
        loop = asyncio.get_running_loop()
        future: asyncio.Future[KnowledgeResponse] = loop.create_future()
        self._pending[ask.ask_id] = future

        try:
            return await asyncio.wait_for(future, timeout=ask.ttl_ms / 1000.0)
        except TimeoutError:
            logger.debug(
                "Remote ask %s to agent %s timed out after %dms",
                ask.ask_id, self._agent_id, ask.ttl_ms,
            )
            return None  # Bus will try surrogate fallback
        finally:
            self._pending.pop(ask.ask_id, None)

    def resolve(self, ask_id: str, response: KnowledgeResponse) -> bool:
        """Called when POST /api/v1/bus/respond arrives.

        Returns True if the ask_id was found and resolved, False otherwise.
        """
        future = self._pending.get(ask_id)
        if future is None or future.done():
            logger.debug("Late/unknown response for ask %s (agent %s)", ask_id, self._agent_id)
            return False
        future.set_result(response)
        return True

    def push_announcement(self, announcement_data: dict[str, Any]) -> bool:
        """Push an announcement event to this agent's SSE stream.

        Returns True if queued successfully, False if queue is full.
        """
        event = {
            "type": "bus.announce",
            **announcement_data,
        }
        try:
            self._sse_queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            logger.warning(
                "SSE queue full for agent %s, dropping announcement",
                self._agent_id,
            )
            return False

    def cancel_all(self) -> None:
        """Cancel all pending futures (called on agent disconnect)."""
        for ask_id, future in self._pending.items():
            if not future.done():
                future.cancel()
                logger.debug("Cancelled pending ask %s for disconnected agent %s",
                             ask_id, self._agent_id)
        self._pending.clear()
