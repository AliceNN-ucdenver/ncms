"""Bus Service - high-level Knowledge Bus operations with surrogate dispatch.

Wraps the low-level AsyncKnowledgeBus with application logic:
surrogate response fallback, blocking ask, and domain listing.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from ncms.domain.protocols import KnowledgeBusTransport
from ncms.infrastructure.observability.event_log import DashboardEvent, NullEventLog

logger = logging.getLogger(__name__)


class BusService:
    """Application-level Knowledge Bus with surrogate response support."""

    def __init__(
        self,
        bus: KnowledgeBusTransport,
        snapshot_service: SnapshotService,
        surrogate_enabled: bool = True,
        event_log: object | None = None,
    ):
        self._bus = bus
        self._snapshot = snapshot_service
        self._surrogate_enabled = surrogate_enabled
        self._event_log = event_log or NullEventLog()

    @property
    def bus(self) -> KnowledgeBusTransport:
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
        for domain in ask.domains:
            snapshots = await self._snapshot.get_snapshots_by_domain(domain)
            for snapshot in snapshots:
                if snapshot.agent_id in tried_agents:
                    continue
                response = await self._snapshot.surrogate_respond(
                    snapshot.agent_id, ask.question, ask.domains
                )
                if response:
                    response.ask_id = ask.ask_id
                    logger.info(
                        "Surrogate response for ask %s from deregistered %s snapshot",
                        ask.ask_id,
                        snapshot.agent_id,
                    )
                    self._event_log.bus_surrogate(
                        ask_id=ask.ask_id,
                        from_agent=snapshot.agent_id,
                        confidence=response.confidence,
                        snapshot_age_seconds=response.snapshot_age_seconds,
                        answer=response.knowledge.content,
                    )
                    return response
                tried_agents.add(snapshot.agent_id)

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

    def get_subscriptions(self) -> dict[str, SubscriptionFilter]:
        """Return current subscription filters per agent (for dashboard)."""
        if hasattr(self._bus, "get_subscriptions"):
            return self._bus.get_subscriptions()
        return {}

    # ── Phase 6: Heartbeat & Auto-Snapshot ───────────────────────────────

    async def start_heartbeat_monitor(
        self,
        interval_seconds: int = 30,
        timeout_seconds: int = 90,
        auto_snapshot: bool = False,
        snapshot_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Start background heartbeat monitoring for all registered agents.

        Checks agent `last_seen` timestamps periodically. If an agent
        hasn't been seen within timeout_seconds, marks it offline and
        optionally triggers a snapshot publish.

        Args:
            interval_seconds: How often to check heartbeats.
            timeout_seconds: Mark offline after this silence.
            auto_snapshot: Auto-publish snapshot on disconnect.
            snapshot_callback: Async callback(agent_id) for snapshot trigger.
        """
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(
                interval_seconds, timeout_seconds,
                auto_snapshot, snapshot_callback,
            ),
            name="bus-heartbeat-monitor",
        )
        logger.info(
            "[heartbeat] Monitor started: interval=%ds timeout=%ds auto_snapshot=%s",
            interval_seconds, timeout_seconds, auto_snapshot,
        )

    async def stop_heartbeat_monitor(self) -> None:
        """Stop the heartbeat monitor."""
        task = getattr(self, "_heartbeat_task", None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            logger.info("[heartbeat] Monitor stopped")

    async def heartbeat(self, agent_id: str) -> None:
        """Record a heartbeat from an agent (updates last_seen)."""
        await self._bus.update_availability(agent_id, "online")
        logger.debug("[heartbeat] Received from %s", agent_id)

    async def _heartbeat_loop(
        self,
        interval: int,
        timeout: int,
        auto_snapshot: bool,
        snapshot_callback: Callable[[str], Awaitable[None]] | None,
    ) -> None:
        """Background loop checking agent heartbeats."""
        from datetime import UTC, datetime

        while True:
            try:
                await asyncio.sleep(interval)
                now = datetime.now(UTC)

                for agent in self._bus.get_all_agents():
                    if agent.status != "online":
                        continue

                    age_seconds = (now - agent.last_seen).total_seconds()
                    if age_seconds > timeout:
                        logger.warning(
                            "[heartbeat] Agent %s timed out "
                            "(last_seen %.0fs ago, timeout=%ds) — marking offline",
                            agent.agent_id, age_seconds, timeout,
                        )

                        # Emit event before status change
                        self._event_log.emit(DashboardEvent(
                            type="agent.heartbeat_timeout",
                            agent_id=agent.agent_id,
                            data={
                                "last_seen_seconds_ago": round(age_seconds),
                                "timeout_seconds": timeout,
                                "auto_snapshot": auto_snapshot,
                            },
                        ))

                        # Mark offline
                        await self._bus.update_availability(
                            agent.agent_id, "offline",
                        )

                        # Auto-snapshot
                        if auto_snapshot and snapshot_callback:
                            try:
                                await snapshot_callback(agent.agent_id)
                                logger.info(
                                    "[heartbeat] Auto-snapshot published for %s",
                                    agent.agent_id,
                                )
                                self._event_log.emit(DashboardEvent(
                                    type="agent.auto_snapshot",
                                    agent_id=agent.agent_id,
                                    data={"reason": "heartbeat_timeout"},
                                ))
                            except Exception as exc:
                                logger.error(
                                    "[heartbeat] Auto-snapshot failed for %s: %s",
                                    agent.agent_id, exc,
                                )

                        # Try surrogate mode
                        if self._surrogate_enabled:
                            logger.info(
                                "[heartbeat] Surrogate mode activated for %s",
                                agent.agent_id,
                            )

            except asyncio.CancelledError:
                break
            except Exception:
                logger.error(
                    "[heartbeat] Unexpected error in monitor",
                    exc_info=True,
                )
