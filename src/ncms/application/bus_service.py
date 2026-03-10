"""Bus Service - high-level Knowledge Bus operations with surrogate dispatch.

Wraps the low-level AsyncKnowledgeBus with application logic:
surrogate response fallback, blocking ask, and domain listing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ncms.application.snapshot_service import SnapshotService
from ncms.domain.models import (
    AgentInfo,
    KnowledgeAnnounce,
    KnowledgeAsk,
    KnowledgeResponse,
    SubscriptionFilter,
)
from ncms.infrastructure.bus.async_bus import AsyncKnowledgeBus

logger = logging.getLogger(__name__)


class BusService:
    """Application-level Knowledge Bus with surrogate response support."""

    def __init__(
        self,
        bus: AsyncKnowledgeBus,
        snapshot_service: SnapshotService,
        surrogate_enabled: bool = True,
        event_log: object | None = None,
    ):
        self._bus = bus
        self._snapshot = snapshot_service
        self._surrogate_enabled = surrogate_enabled
        self._event_log = event_log

    @property
    def bus(self) -> AsyncKnowledgeBus:
        return self._bus

    # ── Registration ─────────────────────────────────────────────────────

    async def register_provider(self, agent_id: str, domains: list[str]) -> None:
        await self._bus.register_provider(agent_id, domains)

    async def deregister_provider(self, agent_id: str) -> None:
        await self._bus.deregister_provider(agent_id)

    async def update_availability(self, agent_id: str, status: str) -> None:
        await self._bus.update_availability(agent_id, status)

    def set_ask_handler(
        self,
        agent_id: str,
        handler: Callable[[KnowledgeAsk], Awaitable[KnowledgeResponse | None]],
    ) -> None:
        self._bus.set_ask_handler(agent_id, handler)

    # ── Ask ──────────────────────────────────────────────────────────────

    async def ask(self, ask: KnowledgeAsk) -> str:
        """Route ask to live agents. Returns ask_id immediately."""
        return await self._bus.ask(ask)

    async def ask_sync(
        self,
        ask: KnowledgeAsk,
        timeout_ms: int | None = None,
    ) -> KnowledgeResponse | None:
        """Blocking ask that waits for a response.

        Tries live agents first, then falls back to surrogate response
        from snapshots if no live agent responds.
        """
        await self._bus.ask(ask)

        # Wait briefly for response
        timeout = (timeout_ms or ask.ttl_ms) / 1000.0
        deadline = asyncio.get_event_loop().time() + timeout

        while asyncio.get_event_loop().time() < deadline:
            inbox = await self._bus.get_inbox(ask.from_agent)
            for response in inbox:
                if response.ask_id == ask.ask_id:
                    await self._bus.drain_inbox(ask.from_agent)
                    return response
            await asyncio.sleep(0.05)

        # Drain to get any late responses
        responses = await self._bus.drain_inbox(ask.from_agent)
        for response in responses:
            if response.ask_id == ask.ask_id:
                return response

        # Fallback: surrogate response from snapshots
        if self._surrogate_enabled:
            return await self._try_surrogate(ask)

        return None

    async def _try_surrogate(self, ask: KnowledgeAsk) -> KnowledgeResponse | None:
        """Try to generate a surrogate response from agent snapshots."""
        # Find all agents that were registered for these domains
        all_agents = self._bus.get_all_agents()
        tried_agents: set[str] = set()

        for domain in ask.domains:
            for agent in all_agents:
                if agent.agent_id in tried_agents:
                    continue
                if agent.status != "online" and any(
                    d == domain or d.startswith(domain + ":") or domain.startswith(d + ":")
                    for d in agent.domains
                ):
                    response = await self._snapshot.surrogate_respond(
                        agent.agent_id, ask.question, ask.domains
                    )
                    if response:
                        response.ask_id = ask.ask_id
                        logger.info(
                            "Surrogate response for ask %s from %s snapshot",
                            ask.ask_id,
                            agent.agent_id,
                        )
                        if self._event_log:
                            self._event_log.bus_surrogate(
                                ask_id=ask.ask_id,
                                from_agent=agent.agent_id,
                                confidence=response.confidence,
                                snapshot_age_seconds=response.snapshot_age_seconds,
                                answer=response.knowledge.content,
                            )
                        return response
                    tried_agents.add(agent.agent_id)

        # Also check snapshots for agents that have fully deregistered
        # (their snapshots persist in storage even after deregistration)
        for _domain in ask.domains:
            # We can't enumerate all snapshots by domain easily, so skip for now
            pass

        return None

    # ── Announce ─────────────────────────────────────────────────────────

    async def announce(self, announcement: KnowledgeAnnounce) -> None:
        await self._bus.announce(announcement)

    # ── Subscribe ────────────────────────────────────────────────────────

    async def subscribe(
        self,
        agent_id: str,
        domains: list[str],
        filter_policy: SubscriptionFilter | None = None,
    ) -> None:
        await self._bus.subscribe(agent_id, domains, filter_policy)

    # ── Inbox ────────────────────────────────────────────────────────────

    async def get_inbox(self, agent_id: str) -> list[KnowledgeResponse]:
        return await self._bus.get_inbox(agent_id)

    async def get_announcements(self, agent_id: str) -> list[KnowledgeAnnounce]:
        return await self._bus.get_announcements(agent_id)

    async def drain_inbox(self, agent_id: str) -> list[KnowledgeResponse]:
        return await self._bus.drain_inbox(agent_id)

    async def drain_announcements(self, agent_id: str) -> list[KnowledgeAnnounce]:
        return await self._bus.drain_announcements(agent_id)

    # ── Domain Info ──────────────────────────────────────────────────────

    def list_domains(self) -> dict[str, list[str]]:
        """Return domain -> list of provider agent_ids."""
        domains: dict[str, list[str]] = {}
        for agent in self._bus.get_all_agents():
            for domain in agent.domains:
                domains.setdefault(domain, []).append(agent.agent_id)
        return domains

    def get_all_agents(self) -> list[AgentInfo]:
        return self._bus.get_all_agents()

    def is_agent_online(self, agent_id: str) -> bool:
        return self._bus.is_agent_online(agent_id)
