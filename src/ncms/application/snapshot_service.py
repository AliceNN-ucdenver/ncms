"""Snapshot Service - manages agent sleep/wake/surrogate response cycle.

Handles Knowledge Snapshots: the "last will" pattern where agents
publish their working knowledge before going offline.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from ncms.domain.models import (
    KnowledgeProvenance,
    KnowledgeResponse,
    KnowledgeSnapshot,
    SnapshotEntry,
)
from ncms.domain.protocols import MemoryStore

logger = logging.getLogger(__name__)

_STRIP_RE = re.compile(r"[^a-z0-9]+")


def _normalize_terms(text: str) -> set[str]:
    """Normalize text into keyword terms for matching.

    Strips punctuation, path prefixes (e.g. /users -> users),
    and filters out short stop-words.
    """
    words = text.lower().split()
    terms: set[str] = set()
    for w in words:
        # Strip path prefixes and surrounding punctuation
        cleaned = _STRIP_RE.sub("", w)
        if len(cleaned) >= 2:
            terms.add(cleaned)
    return terms


class SnapshotService:
    """Manages Knowledge Snapshots for surrogate response."""

    def __init__(self, store: MemoryStore, max_entries: int = 50, ttl_hours: int = 168):
        self._store = store
        self._max_entries = max_entries
        self._ttl_hours = ttl_hours

    async def create_snapshot(
        self,
        agent_id: str,
        entries: list[SnapshotEntry],
        domains: list[str] | None = None,
    ) -> KnowledgeSnapshot:
        """Create and persist a knowledge snapshot for an agent."""
        # Get previous snapshot ID for supersedes chain
        previous = await self._store.get_latest_snapshot(agent_id)

        snapshot = KnowledgeSnapshot(
            agent_id=agent_id,
            domains=domains or list({e.domain for e in entries}),
            entries=entries[: self._max_entries],
            supersedes=previous.snapshot_id if previous else None,
            ttl_hours=self._ttl_hours,
        )

        await self._store.save_snapshot(snapshot)
        logger.info(
            "Snapshot created for %s: %d entries, domains=%s",
            agent_id,
            len(snapshot.entries),
            snapshot.domains,
        )
        return snapshot

    async def get_snapshot(self, agent_id: str) -> KnowledgeSnapshot | None:
        """Retrieve the latest snapshot for an agent, checking TTL."""
        snapshot = await self._store.get_latest_snapshot(agent_id)
        if not snapshot:
            return None

        # Check TTL
        now = datetime.now(UTC)
        ts = snapshot.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        age_hours = (now - ts).total_seconds() / 3600
        if age_hours > snapshot.ttl_hours:
            logger.info("Snapshot for %s expired (%.1fh old)", agent_id, age_hours)
            return None

        return snapshot

    async def surrogate_respond(
        self,
        agent_id: str,
        question: str,
        domains: list[str] | None = None,
    ) -> KnowledgeResponse | None:
        """Generate a surrogate response from an agent's snapshot.

        Returns a KnowledgeResponse with source_mode="warm" if a matching
        snapshot entry is found, or None if no snapshot exists.
        """
        snapshot = await self.get_snapshot(agent_id)
        if not snapshot:
            return None

        # Find best matching entry via keyword matching
        best_entry: SnapshotEntry | None = None
        best_score = 0.0

        query_terms = _normalize_terms(question)

        for entry in snapshot.entries:
            # Domain filter
            if domains and entry.domain not in domains:
                match = False
                for d in domains:
                    if entry.domain.startswith(d) or d.startswith(entry.domain):
                        match = True
                        break
                if not match:
                    continue

            # Keyword overlap scoring with normalized terms
            entry_terms = _normalize_terms(entry.knowledge.content)
            overlap = len(query_terms & entry_terms)
            score = overlap / max(len(query_terms), 1)

            if score > best_score:
                best_score = score
                best_entry = entry

        if not best_entry or best_score == 0:
            return None

        now = datetime.now(UTC)
        ts = snapshot.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        age_seconds = int((now - ts).total_seconds())

        return KnowledgeResponse(
            ask_id="",  # Will be set by caller
            from_agent=agent_id,
            confidence=best_entry.confidence * 0.8,  # Discount for surrogate
            knowledge=best_entry.knowledge,
            provenance=KnowledgeProvenance(
                source="memory-store",
                last_verified=best_entry.last_verified,
                trust_level="observed",
            ),
            freshness=snapshot.timestamp,
            source_mode="warm",
            snapshot_age_seconds=age_seconds,
            original_agent=agent_id,
            staleness_warning=f"From {agent_id} snapshot {age_seconds // 3600}h ago"
            if age_seconds > 3600
            else None,
        )

    async def get_snapshots_by_domain(self, domain: str) -> list[KnowledgeSnapshot]:
        """Find snapshots whose domains include the given domain."""
        return await self._store.get_snapshots_by_domain(domain)

    async def delete_snapshot(self, agent_id: str) -> None:
        await self._store.delete_snapshot(agent_id)
