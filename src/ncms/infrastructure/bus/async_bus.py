"""AsyncIO in-process Knowledge Bus implementation.

Zero-dependency event bus using Python asyncio primitives.
Supports provider registration, domain-routed asks, announcement fanout,
subscription filtering, and per-agent inbox queues.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from ncms.domain.exceptions import AgentNotRegisteredError, BusTimeoutError
from ncms.domain.models import (
    AgentInfo,
    KnowledgeAnnounce,
    KnowledgeAsk,
    KnowledgeResponse,
    SubscriptionFilter,
)

logger = logging.getLogger(__name__)

AskHandler = Callable[[KnowledgeAsk], Awaitable[KnowledgeResponse | None]]


class AsyncKnowledgeBus:
    """In-process asyncio-based Knowledge Bus."""

    def __init__(self, ask_timeout_ms: int = 5000):
        self._ask_timeout_ms = ask_timeout_ms

        # Agent registry
        self._agents: dict[str, AgentInfo] = {}

        # Domain -> list of agent_ids
        self._domain_providers: dict[str, list[str]] = {}

        # Ask handlers: agent_id -> callback
        self._ask_handlers: dict[str, AskHandler] = {}

        # Response inbox: agent_id -> list of responses
        self._response_inbox: dict[str, list[KnowledgeResponse]] = {}

        # Announcement inbox: agent_id -> list of announcements
        self._announcement_inbox: dict[str, list[KnowledgeAnnounce]] = {}

        # Subscriptions: agent_id -> filter
        self._subscriptions: dict[str, SubscriptionFilter] = {}

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self._agents.clear()
        self._domain_providers.clear()
        self._ask_handlers.clear()
        self._response_inbox.clear()
        self._announcement_inbox.clear()
        self._subscriptions.clear()

    # ── Registration ─────────────────────────────────────────────────────

    async def register_provider(self, agent_id: str, domains: list[str]) -> None:
        self._agents[agent_id] = AgentInfo(
            agent_id=agent_id,
            domains=domains,
            status="online",
        )
        self._response_inbox.setdefault(agent_id, [])
        self._announcement_inbox.setdefault(agent_id, [])

        for domain in domains:
            providers = self._domain_providers.setdefault(domain, [])
            if agent_id not in providers:
                providers.append(agent_id)

        logger.info("Agent %s registered for domains: %s", agent_id, domains)

    async def deregister_provider(self, agent_id: str) -> None:
        info = self._agents.pop(agent_id, None)
        if info:
            for domain in info.domains:
                providers = self._domain_providers.get(domain, [])
                if agent_id in providers:
                    providers.remove(agent_id)
            self._ask_handlers.pop(agent_id, None)
            logger.info("Agent %s deregistered", agent_id)

    async def update_availability(self, agent_id: str, status: str) -> None:
        if agent_id in self._agents:
            self._agents[agent_id].status = status  # type: ignore[assignment]
            self._agents[agent_id].last_seen = datetime.now(timezone.utc)

    def is_agent_online(self, agent_id: str) -> bool:
        info = self._agents.get(agent_id)
        return info is not None and info.status == "online"

    def get_providers_for_domain(self, domain: str) -> list[str]:
        """Get online provider agent_ids for a domain, including wildcard matches."""
        providers: set[str] = set()

        # Exact match
        for aid in self._domain_providers.get(domain, []):
            if self.is_agent_online(aid):
                providers.add(aid)

        # Prefix match: "api" matches "api:user-service"
        for registered_domain, agent_ids in self._domain_providers.items():
            if registered_domain.startswith(domain + ":") or domain.startswith(
                registered_domain + ":"
            ):
                for aid in agent_ids:
                    if self.is_agent_online(aid):
                        providers.add(aid)

        return list(providers)

    def get_all_agents(self) -> list[AgentInfo]:
        return list(self._agents.values())

    # ── Ask Handling ─────────────────────────────────────────────────────

    def set_ask_handler(self, agent_id: str, handler: AskHandler) -> None:  # type: ignore[override]
        self._ask_handlers[agent_id] = handler

    async def ask(self, ask: KnowledgeAsk) -> str:
        """Route an ask to matching providers. Returns the ask_id."""
        # Find matching providers
        target_agents: set[str] = set()
        for domain in ask.domains:
            target_agents.update(self.get_providers_for_domain(domain))

        # Remove the asking agent from targets
        target_agents.discard(ask.from_agent)

        if not target_agents:
            logger.debug("No live providers for domains %s", ask.domains)
            return ask.ask_id

        # Fire ask to all matching handlers concurrently
        tasks = []
        for agent_id in target_agents:
            handler = self._ask_handlers.get(agent_id)
            if handler:
                tasks.append(self._invoke_handler(handler, ask, agent_id))

        if tasks:
            timeout_s = ask.ttl_ms / 1000.0
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=timeout_s,
                )
                # Collect valid responses into the asking agent's inbox
                for result in results:
                    if isinstance(result, KnowledgeResponse):
                        self._response_inbox.setdefault(ask.from_agent, []).append(result)
            except asyncio.TimeoutError:
                logger.warning("Ask %s timed out after %dms", ask.ask_id, ask.ttl_ms)

        return ask.ask_id

    async def _invoke_handler(
        self, handler: AskHandler, ask: KnowledgeAsk, agent_id: str
    ) -> KnowledgeResponse | None:
        try:
            return await handler(ask)
        except Exception:
            logger.exception("Ask handler for %s failed", agent_id)
            return None

    async def respond(self, response: KnowledgeResponse) -> None:
        """Manually add a response to an agent's inbox (for surrogate responses)."""
        # Find which agent asked
        # Responses go to whoever's inbox matches the ask
        for agent_id, inbox in self._response_inbox.items():
            self._response_inbox.setdefault(agent_id, []).append(response)
            break

    # ── Announcements ────────────────────────────────────────────────────

    async def announce(self, announcement: KnowledgeAnnounce) -> None:
        """Broadcast announcement to all subscribed agents."""
        for agent_id, sub_filter in self._subscriptions.items():
            if agent_id == announcement.from_agent:
                continue  # Don't announce to self

            if self._matches_filter(announcement, sub_filter):
                self._announcement_inbox.setdefault(agent_id, []).append(announcement)
                logger.debug(
                    "Announcement %s delivered to %s", announcement.announce_id, agent_id
                )

    async def subscribe(
        self,
        agent_id: str,
        domains: list[str],
        filter_policy: SubscriptionFilter | None = None,
    ) -> None:
        self._subscriptions[agent_id] = filter_policy or SubscriptionFilter(domains=domains)

    # ── Inbox ────────────────────────────────────────────────────────────

    async def get_inbox(self, agent_id: str) -> list[KnowledgeResponse]:
        return list(self._response_inbox.get(agent_id, []))

    async def get_announcements(self, agent_id: str) -> list[KnowledgeAnnounce]:
        return list(self._announcement_inbox.get(agent_id, []))

    async def drain_inbox(self, agent_id: str) -> list[KnowledgeResponse]:
        responses = self._response_inbox.get(agent_id, [])
        self._response_inbox[agent_id] = []
        return responses

    async def drain_announcements(self, agent_id: str) -> list[KnowledgeAnnounce]:
        announcements = self._announcement_inbox.get(agent_id, [])
        self._announcement_inbox[agent_id] = []
        return announcements

    # ── Internal ─────────────────────────────────────────────────────────

    @staticmethod
    def _matches_filter(
        announcement: KnowledgeAnnounce,
        sub_filter: SubscriptionFilter,
    ) -> bool:
        if sub_filter.domains:
            # Check if any announcement domain matches any subscription domain
            for ann_domain in announcement.domains:
                for sub_domain in sub_filter.domains:
                    if ann_domain == sub_domain or ann_domain.startswith(sub_domain + ":"):
                        return True
            return False
        return True  # No filter = receive everything
