"""AsyncIO in-process Knowledge Bus implementation.

Zero-dependency event bus using Python asyncio primitives.
Supports provider registration, domain-routed asks, announcement fanout,
subscription filtering, and per-agent inbox queues.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from ncms.domain.models import (
    AgentInfo,
    KnowledgeAnnounce,
    KnowledgeAsk,
    KnowledgeResponse,
    SubscriptionFilter,
)

logger = logging.getLogger(__name__)

AskHandler = Callable[[KnowledgeAsk], Awaitable[KnowledgeResponse | None]]

# Reserved broadcast domain — all agents subscribe to this automatically.
# Announcements with no domains get routed here so they reach everyone.
BROADCAST_DOMAIN = "*"


class AsyncKnowledgeBus:
    """In-process asyncio-based Knowledge Bus."""

    def __init__(
        self,
        ask_timeout_ms: int = 5000,
        event_log: object | None = None,
    ):
        self._ask_timeout_ms = ask_timeout_ms
        # Optional EventLog for dashboard observability (duck-typed to avoid import)
        self._event_log = event_log

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

        # Auto-subscribe to broadcast domain so agent receives system-wide announces
        if agent_id not in self._subscriptions:
            self._subscriptions[agent_id] = SubscriptionFilter(domains=[BROADCAST_DOMAIN])
        elif BROADCAST_DOMAIN not in (self._subscriptions[agent_id].domains or []):
            existing = list(self._subscriptions[agent_id].domains or [])
            existing.append(BROADCAST_DOMAIN)
            self._subscriptions[agent_id] = SubscriptionFilter(
                domains=existing,
                severity_min=self._subscriptions[agent_id].severity_min,
                tags=self._subscriptions[agent_id].tags,
            )

        logger.info("Agent %s registered for domains: %s", agent_id, domains)
        if self._event_log:
            self._event_log.agent_registered(agent_id, domains)

    async def deregister_provider(self, agent_id: str) -> None:
        info = self._agents.pop(agent_id, None)
        if info:
            for domain in info.domains:
                providers = self._domain_providers.get(domain, [])
                if agent_id in providers:
                    providers.remove(agent_id)
            self._ask_handlers.pop(agent_id, None)
            logger.info("Agent %s deregistered", agent_id)
            if self._event_log:
                self._event_log.agent_deregistered(agent_id)

    async def update_availability(self, agent_id: str, status: str) -> None:
        if agent_id in self._agents:
            self._agents[agent_id].status = status  # type: ignore[assignment]
            self._agents[agent_id].last_seen = datetime.now(UTC)
            if self._event_log:
                self._event_log.agent_status(agent_id, status)

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

        if self._event_log:
            self._event_log.bus_ask(
                ask_id=ask.ask_id,
                from_agent=ask.from_agent,
                question=ask.question,
                domains=ask.domains,
                targets=list(target_agents),
            )

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
                        self._response_inbox.setdefault(ask.from_agent, []).append(
                            result
                        )
                        if self._event_log:
                            self._event_log.bus_response(
                                ask_id=result.ask_id,
                                from_agent=result.from_agent,
                                source_mode=result.source_mode,
                                confidence=result.confidence,
                                answer=result.knowledge.content,
                            )
            except TimeoutError:
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
        for agent_id, _inbox in self._response_inbox.items():
            self._response_inbox.setdefault(agent_id, []).append(response)
            break

    # ── Announcements ────────────────────────────────────────────────────

    async def announce(self, announcement: KnowledgeAnnounce) -> None:
        """Broadcast announcement to all subscribed agents."""
        recipients: list[str] = []
        for agent_id, sub_filter in self._subscriptions.items():
            if agent_id == announcement.from_agent:
                continue  # Don't announce to self

            if self._matches_filter(announcement, sub_filter):
                self._announcement_inbox.setdefault(agent_id, []).append(announcement)
                recipients.append(agent_id)
                logger.debug(
                    "Announcement %s delivered to %s", announcement.announce_id, agent_id
                )

        if self._event_log:
            severity = "info"
            if announcement.impact:
                severity = announcement.impact.severity
            self._event_log.bus_announce(
                announce_id=announcement.announce_id,
                from_agent=announcement.from_agent,
                event=announcement.event,
                domains=announcement.domains,
                severity=severity,
                recipients=recipients,
                content=announcement.knowledge.content,
            )

    async def subscribe(
        self,
        agent_id: str,
        domains: list[str],
        filter_policy: SubscriptionFilter | None = None,
    ) -> None:
        policy = filter_policy or SubscriptionFilter(domains=domains)
        # Ensure broadcast domain is always included
        if policy.domains is not None and BROADCAST_DOMAIN not in policy.domains:
            policy = SubscriptionFilter(
                domains=[*policy.domains, BROADCAST_DOMAIN],
                severity_min=policy.severity_min,
                tags=policy.tags,
            )
        self._subscriptions[agent_id] = policy

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
        if not sub_filter.domains:
            return True  # No filter = receive everything

        # Effective announcement domains — empty means broadcast via "*"
        ann_domains = announcement.domains if announcement.domains else [BROADCAST_DOMAIN]

        for ann_domain in ann_domains:
            for sub_domain in sub_filter.domains:
                # Broadcast "*" only matches broadcast — not every domain
                if ann_domain == BROADCAST_DOMAIN and sub_domain == BROADCAST_DOMAIN:
                    return True
                # Skip wildcard for regular domain matching
                if ann_domain == BROADCAST_DOMAIN or sub_domain == BROADCAST_DOMAIN:
                    continue
                # Regular domain + prefix matching
                if ann_domain == sub_domain or ann_domain.startswith(sub_domain + ":"):
                    return True
        return False
