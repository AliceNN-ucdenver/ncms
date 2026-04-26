"""HTTP Bus Transport — KnowledgeBusTransport over HTTP + SSE.

Wraps AsyncKnowledgeBus, adding remote agent support via Server-Sent Events.
Remote agents connect via SSE to receive questions and announcements, and
POST responses back. All networking is encapsulated in this transport —
BusService and api.py don't need to know whether agents are local or remote.

Implements KnowledgeBusTransport so it can be used as a drop-in replacement
for AsyncKnowledgeBus in any composition root.

Usage:
    inner = AsyncKnowledgeBus(ask_timeout_ms=60000, event_log=event_log)
    transport = HttpBusTransport(inner=inner, event_log=event_log)
    bus_svc = BusService(bus=transport, ...)
    routes += transport.starlette_routes()
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from ncms.domain.models import (
    KnowledgeAnnounce,
    KnowledgeAsk,
    KnowledgePayload,
    KnowledgeResponse,
    SubscriptionFilter,
)
from ncms.infrastructure.bus.async_bus import AsyncKnowledgeBus
from ncms.infrastructure.bus.remote_handler import RemoteAskHandler

logger = logging.getLogger(__name__)


class HttpBusTransport:
    """KnowledgeBusTransport over HTTP + SSE.

    Wraps AsyncKnowledgeBus for core bus logic (domain routing, inbox
    management, handler invocation) and adds SSE transport for remote
    agents. All remote agent state is encapsulated here.
    """

    def __init__(
        self,
        inner: AsyncKnowledgeBus,
        event_log: object | None = None,
    ):
        self._inner = inner
        self._event_log = event_log
        self._remote_agents: dict[str, _RemoteAgent] = {}

    # ── KnowledgeBusTransport protocol — delegation ───────────────────

    async def start(self) -> None:
        await self._inner.start()

    async def stop(self) -> None:
        # Cancel all remote agent futures
        for agent in self._remote_agents.values():
            agent.handler.cancel_all()
        self._remote_agents.clear()
        await self._inner.stop()

    async def register_provider(
        self,
        agent_id: str,
        domains: list[str],
    ) -> None:
        await self._inner.register_provider(agent_id, domains)

    async def deregister_provider(self, agent_id: str) -> None:
        # Clean up remote agent state if present
        remote = self._remote_agents.pop(agent_id, None)
        if remote:
            remote.handler.cancel_all()
        await self._inner.deregister_provider(agent_id)

    async def update_availability(
        self,
        agent_id: str,
        status: str,
    ) -> None:
        await self._inner.update_availability(agent_id, status)

    async def ask(self, ask: KnowledgeAsk) -> str:
        return await self._inner.ask(ask)

    async def respond(self, response: KnowledgeResponse) -> None:
        await self._inner.respond(response)

    async def announce(self, announcement: KnowledgeAnnounce) -> None:
        await self._inner.announce(announcement)
        # Push to remote agents' SSE streams
        self._push_announcement(announcement)

    async def subscribe(
        self,
        agent_id: str,
        domains: list[str],
        filter_policy: SubscriptionFilter | None = None,
    ) -> None:
        await self._inner.subscribe(
            agent_id,
            domains,
            filter_policy=filter_policy,
        )

    async def get_inbox(self, agent_id: str) -> list[KnowledgeResponse]:
        return await self._inner.get_inbox(agent_id)

    async def get_announcements(
        self,
        agent_id: str,
    ) -> list[KnowledgeAnnounce]:
        return await self._inner.get_announcements(agent_id)

    async def drain_inbox(
        self,
        agent_id: str,
    ) -> list[KnowledgeResponse]:
        return await self._inner.drain_inbox(agent_id)

    async def drain_announcements(
        self,
        agent_id: str,
    ) -> list[KnowledgeAnnounce]:
        return await self._inner.drain_announcements(agent_id)

    def get_providers_for_domain(self, domain: str) -> list[str]:
        return self._inner.get_providers_for_domain(domain)

    def is_agent_online(self, agent_id: str) -> bool:
        return self._inner.is_agent_online(agent_id)

    def get_all_agents(self) -> list:
        return self._inner.get_all_agents()

    def set_ask_handler(
        self,
        agent_id: str,
        handler: Callable[[KnowledgeAsk], Awaitable[KnowledgeResponse | None]],
    ) -> None:
        self._inner.set_ask_handler(agent_id, handler)

    def get_subscriptions(self) -> dict[str, SubscriptionFilter]:
        return self._inner.get_subscriptions()

    # ── Remote agent management ───────────────────────────────────────

    async def register_remote_agent(
        self,
        agent_id: str,
        domains: list[str],
        subscribe_to: list[str] | None = None,
    ) -> None:
        """Register a remote agent with SSE transport.

        Creates SSE queue, sets up RemoteAskHandler on the inner bus,
        and subscribes to announcement domains.
        """
        # Register on inner bus
        await self._inner.register_provider(agent_id, domains)

        # Subscribe for announcements
        if subscribe_to:
            await self._inner.subscribe(agent_id, subscribe_to)

        # Create SSE infrastructure
        sse_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=500,
        )
        handler = RemoteAskHandler(agent_id=agent_id, sse_queue=sse_queue)
        self._remote_agents[agent_id] = _RemoteAgent(
            handler=handler,
            sse_queue=sse_queue,
            subscribed_domains=set(subscribe_to or domains),
        )

        # Wire handler into inner bus
        self._inner.set_ask_handler(agent_id, handler)

        logger.info(
            "Remote agent %s registered (domains=%s, subscribe=%s)",
            agent_id,
            domains,
            subscribe_to,
        )

    async def deregister_remote_agent(self, agent_id: str) -> None:
        """Deregister a remote agent and clean up SSE state."""
        remote = self._remote_agents.pop(agent_id, None)
        if remote:
            remote.handler.cancel_all()
        await self._inner.deregister_provider(agent_id)
        logger.info("Remote agent %s deregistered", agent_id)

    def resolve_response(
        self,
        agent_id: str,
        ask_id: str,
        response: KnowledgeResponse,
    ) -> bool:
        """Resolve a pending ask Future from a remote agent's POST.

        Returns True if resolved, False if ask_id expired or unknown.
        """
        remote = self._remote_agents.get(agent_id)
        if remote is None:
            return False
        return remote.handler.resolve(ask_id, response)

    def on_agent_disconnected(self, agent_id: str) -> None:
        """Handle SSE stream disconnect — clean up and mark offline."""
        remote = self._remote_agents.pop(agent_id, None)
        if remote:
            remote.handler.cancel_all()

    # ── SSE announcement fan-out ──────────────────────────────────────

    def _push_announcement(
        self,
        announcement: KnowledgeAnnounce,
    ) -> list[str]:
        """Push announcement to matching remote agents' SSE streams."""
        announce_data = {
            "announce_id": announcement.announce_id,
            "from_agent": announcement.from_agent,
            "event": announcement.event,
            "domains": announcement.domains,
            "content": announcement.knowledge.content,
        }
        pushed_to: list[str] = []
        for agent_id, remote in self._remote_agents.items():
            if agent_id == announcement.from_agent:
                continue  # Don't echo back to sender
            # Check domain subscription match
            if remote.subscribed_domains and not any(
                d in remote.subscribed_domains or d == "*" for d in announcement.domains
            ):
                continue
            if remote.handler.push_announcement(announce_data):
                pushed_to.append(agent_id)
        return pushed_to

    # ── Starlette routes ──────────────────────────────────────────────

    def starlette_routes(
        self,
        prefix: str = "/api/v1/bus",
    ) -> list[Route]:
        """Return Starlette routes for remote agent HTTP + SSE.

        Mount these alongside the existing API routes:
            routes += transport.starlette_routes()
        """
        return [
            Route(
                f"{prefix}/register",
                self._handle_register,
                methods=["POST"],
            ),
            Route(
                f"{prefix}/deregister",
                self._handle_deregister,
                methods=["POST"],
            ),
            Route(
                f"{prefix}/subscribe",
                self._handle_subscribe_sse,
                methods=["GET"],
            ),
            Route(
                f"{prefix}/respond",
                self._handle_respond,
                methods=["POST"],
            ),
        ]

    # ── Route handlers (private) ──────────────────────────────────────

    async def _handle_register(self, request: Request) -> JSONResponse:
        """POST /bus/register — register a remote agent."""
        body = await request.json()
        agent_id = body.get("agent_id", "")
        domains = body.get("domains", [])
        subscribe_to = body.get("subscribe_to", [])

        if not agent_id or not domains:
            return JSONResponse(
                {"error": "agent_id and domains are required"},
                status_code=400,
            )

        await self.register_remote_agent(
            agent_id,
            domains,
            subscribe_to=subscribe_to,
        )
        return JSONResponse(
            {
                "registered": True,
                "agent_id": agent_id,
                "domains": domains,
                "subscribe_to": subscribe_to,
            }
        )

    async def _handle_deregister(self, request: Request) -> JSONResponse:
        """POST /bus/deregister — deregister a remote agent."""
        body = await request.json()
        agent_id = body.get("agent_id", "")
        if not agent_id:
            return JSONResponse(
                {"error": "agent_id is required"},
                status_code=400,
            )

        await self.deregister_remote_agent(agent_id)
        return JSONResponse(
            {
                "deregistered": True,
                "agent_id": agent_id,
            }
        )

    async def _handle_subscribe_sse(
        self,
        request: Request,
    ) -> StreamingResponse | JSONResponse:
        """GET /bus/subscribe?agent_id=X — SSE stream for a remote agent."""
        agent_id = request.query_params.get("agent_id", "")
        if not agent_id:
            return JSONResponse(
                {"error": "agent_id query param is required"},
                status_code=400,
            )

        remote = self._remote_agents.get(agent_id)
        if remote is None:
            return JSONResponse(
                {"error": f"Agent {agent_id} not registered"},
                status_code=404,
            )

        sse_queue = remote.sse_queue

        async def generate():
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(
                            sse_queue.get(),
                            timeout=30.0,
                        )
                        event_type = event.get("type", "message")
                        payload = json.dumps(event, default=str)
                        yield f"event: {event_type}\ndata: {payload}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                logger.info("SSE stream closed for agent %s", agent_id)
                await self.update_availability(agent_id, "offline")
                self.on_agent_disconnected(agent_id)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def _handle_respond(self, request: Request) -> JSONResponse:
        """POST /bus/respond — receive a response from a remote agent."""
        body = await request.json()
        ask_id = body.get("ask_id", "")
        from_agent = body.get("from_agent", "")
        content = body.get("content", "")

        if not ask_id or not from_agent:
            return JSONResponse(
                {"error": "ask_id and from_agent are required"},
                status_code=400,
            )

        response = KnowledgeResponse(
            ask_id=ask_id,
            from_agent=from_agent,
            knowledge=KnowledgePayload(content=content),
            confidence=body.get("confidence", 0.5),
            source_mode="live",
        )

        resolved = self.resolve_response(from_agent, ask_id, response)
        if not resolved:
            return JSONResponse(
                {
                    "resolved": False,
                    "reason": "ask_id not found or already timed out",
                },
                status_code=410,
            )

        return JSONResponse({"resolved": True, "ask_id": ask_id})


class _RemoteAgent:
    """Internal state for a single remote agent."""

    __slots__ = ("handler", "sse_queue", "subscribed_domains")

    def __init__(
        self,
        handler: RemoteAskHandler,
        sse_queue: asyncio.Queue[dict[str, Any]],
        subscribed_domains: set[str],
    ):
        self.handler = handler
        self.sse_queue = sse_queue
        self.subscribed_domains = subscribed_domains
