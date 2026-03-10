"""KnowledgeAgent base class for building knowledge-aware agents.

Provides the standard lifecycle for agents that participate in the
Knowledge Bus: register, ask, announce, sleep (snapshot), wake (restore).
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from ncms.application.bus_service import BusService
from ncms.application.memory_service import MemoryService
from ncms.application.snapshot_service import SnapshotService
from ncms.domain.models import (
    KnowledgeAnnounce,
    KnowledgeAsk,
    KnowledgePayload,
    KnowledgeResponse,
    KnowledgeSnapshot,
    ImpactAssessment,
    SnapshotEntry,
    SubscriptionFilter,
)

logger = logging.getLogger(__name__)


class KnowledgeAgent(ABC):
    """Base class for agents that participate in the Knowledge Bus.

    Subclass this and implement:
    - declare_expertise() -> domains you can answer questions about
    - declare_subscriptions() -> domains you want announcements from
    - on_ask() -> handle incoming knowledge queries
    - collect_working_knowledge() -> what to publish in snapshots
    """

    def __init__(
        self,
        agent_id: str,
        bus_service: BusService,
        memory_service: MemoryService,
        snapshot_service: SnapshotService,
    ):
        self.agent_id = agent_id
        self._bus = bus_service
        self._memory = memory_service
        self._snapshot_svc = snapshot_service
        self._running = False
        self._last_snapshot_id: str | None = None

    @abstractmethod
    def declare_expertise(self) -> list[str]:
        """Declare knowledge domains this agent can provide answers for."""
        ...

    @abstractmethod
    def declare_subscriptions(self) -> list[str]:
        """Declare knowledge domains this agent wants announcements from."""
        ...

    @abstractmethod
    async def on_ask(self, ask: KnowledgeAsk) -> KnowledgeResponse | None:
        """Handle an incoming knowledge query.

        Return a KnowledgeResponse if you can answer, or None to pass.
        This runs in a background task - your main work loop is not interrupted.
        """
        ...

    @abstractmethod
    async def collect_working_knowledge(self) -> list[SnapshotEntry]:
        """Collect current working knowledge for snapshot publication.

        Override to declare what your agent currently knows.
        Called before sleep/shutdown.
        """
        ...

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Register with the bus and start listening for asks."""
        domains = self.declare_expertise()
        await self._bus.register_provider(self.agent_id, domains)
        self._bus.set_ask_handler(self.agent_id, self._handle_ask)

        subscriptions = self.declare_subscriptions()
        if subscriptions:
            await self._bus.subscribe(self.agent_id, subscriptions)

        # Restore from previous snapshot if available
        snapshot = await self._snapshot_svc.get_snapshot(self.agent_id)
        if snapshot:
            await self.on_restore(snapshot)
            self._last_snapshot_id = snapshot.snapshot_id

        self._running = True
        logger.info("Agent %s started (domains: %s)", self.agent_id, domains)

    async def sleep(self) -> KnowledgeSnapshot:
        """Publish snapshot and go offline."""
        snapshot = await self.publish_snapshot(reason="sleep")
        await self._bus.update_availability(self.agent_id, "sleeping")
        self._running = False
        logger.info("Agent %s is sleeping (snapshot: %s)", self.agent_id, snapshot.snapshot_id)
        return snapshot

    async def wake(self) -> KnowledgeSnapshot | None:
        """Restore from snapshot and come back online."""
        snapshot = await self._snapshot_svc.get_snapshot(self.agent_id)
        if snapshot:
            await self.on_restore(snapshot)

        await self._bus.update_availability(self.agent_id, "online")
        self._bus.set_ask_handler(self.agent_id, self._handle_ask)
        self._running = True
        logger.info("Agent %s is awake", self.agent_id)
        return snapshot

    async def shutdown(self) -> None:
        """Publish final snapshot and deregister."""
        await self.publish_snapshot(reason="shutdown")
        await self._bus.deregister_provider(self.agent_id)
        self._running = False
        logger.info("Agent %s shut down", self.agent_id)

    # ── Knowledge Operations ─────────────────────────────────────────────

    async def ask_knowledge(
        self,
        question: str,
        domains: list[str],
        urgency: str = "important",
    ) -> KnowledgeResponse | None:
        """Ask the knowledge network a question (blocking)."""
        ask = KnowledgeAsk(
            from_agent=self.agent_id,
            question=question,
            domains=domains,
            urgency=urgency,  # type: ignore[arg-type]
        )
        return await self._bus.ask_sync(ask)

    async def announce_knowledge(
        self,
        event: str,
        domains: list[str],
        content: str,
        structured: dict | None = None,
        breaking: bool = False,
        severity: str = "info",
    ) -> None:
        """Broadcast knowledge to the network."""
        announcement = KnowledgeAnnounce(
            from_agent=self.agent_id,
            event=event,  # type: ignore[arg-type]
            domains=domains,
            knowledge=KnowledgePayload(
                type="interface-spec" if structured else "fact",
                content=content,
                structured=structured,
            ),
            impact=ImpactAssessment(
                breaking_change=breaking,
                affected_domains=domains,
                severity=severity,  # type: ignore[arg-type]
                description=content,
            ),
        )
        await self._bus.announce(announcement)

    async def store_knowledge(
        self,
        content: str,
        domains: list[str] | None = None,
        memory_type: str = "fact",
        **kwargs: object,
    ) -> None:
        """Store knowledge in the cognitive memory system."""
        await self._memory.store_memory(
            content=content,
            memory_type=memory_type,
            domains=domains or self.declare_expertise(),
            source_agent=self.agent_id,
            **kwargs,  # type: ignore[arg-type]
        )

    # ── Snapshot ─────────────────────────────────────────────────────────

    async def publish_snapshot(self, reason: str = "periodic") -> KnowledgeSnapshot:
        """Serialize current working knowledge and publish to Memory Core."""
        entries = await self.collect_working_knowledge()
        snapshot = await self._snapshot_svc.create_snapshot(
            agent_id=self.agent_id,
            entries=entries,
            domains=self.declare_expertise(),
        )
        self._last_snapshot_id = snapshot.snapshot_id
        return snapshot

    # ── Inbox Processing ─────────────────────────────────────────────────

    async def process_inbox(self) -> list[KnowledgeResponse]:
        """Process any queued responses and announcements."""
        responses = await self._bus.drain_inbox(self.agent_id)
        announcements = await self._bus.drain_announcements(self.agent_id)

        for ann in announcements:
            await self.on_announcement(ann)

        return responses

    # ── Hooks (Override in Subclass) ─────────────────────────────────────

    async def on_announcement(self, announcement: KnowledgeAnnounce) -> None:
        """Called when a subscribed domain receives an announcement.

        Default: stores in cognitive memory. Override to add custom logic.
        """
        await self._memory.store_memory(
            content=f"[{announcement.event}] {announcement.knowledge.content}",
            memory_type="fact",
            domains=announcement.domains,
            source_agent=announcement.from_agent,
            importance=8.0 if announcement.impact.breaking_change else 5.0,
        )

    async def on_restore(self, snapshot: KnowledgeSnapshot) -> None:
        """Called when agent wakes up and loads a previous snapshot.

        Default: no-op. Override to process restored knowledge.
        """
        pass

    # ── Internal ─────────────────────────────────────────────────────────

    async def _handle_ask(self, ask: KnowledgeAsk) -> KnowledgeResponse | None:
        """Internal handler that wraps the user's on_ask with error handling."""
        try:
            return await self.on_ask(ask)
        except Exception:
            logger.exception("on_ask failed for agent %s", self.agent_id)
            return None
