"""Lint service — read-only diagnostics for memory store quality issues.

Detects orphans, duplicates, junk entities, broken edges, dangling
memory-entity links, and stale episodes without modifying any data.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ncms.domain.models import NodeType
from ncms.domain.protocols import GraphEngine, MemoryStore

logger = logging.getLogger(__name__)


@dataclass
class LintIssue:
    severity: str  # "error", "warning", "info"
    category: str  # see LintService check methods
    message: str
    entity_id: str | None = None
    memory_id: str | None = None


@dataclass
class LintReport:
    issues: list[LintIssue] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_ms: float = 0.0


class LintService:
    """Read-only diagnostic scanner for the NCMS memory store.

    All checks are non-destructive — they only read from the store
    and graph, never write or mutate.
    """

    def __init__(self, store: MemoryStore, graph: GraphEngine) -> None:
        self._store = store
        self._graph = graph

    async def run_full_lint(self) -> LintReport:
        """Run all lint checks and return a consolidated report."""
        t0 = time.monotonic()

        all_issues: list[LintIssue] = []
        for check in (
            self.check_junk_entities,
            self.check_duplicate_content,
            self.check_dangling_memory_entity_links,
            self.check_stale_episodes,
            self.check_orphan_memory_nodes,
        ):
            try:
                issues = await check()
                all_issues.extend(issues)
            except Exception:
                logger.exception("Lint check %s failed", check.__name__)
                all_issues.append(
                    LintIssue(
                        severity="error",
                        category="internal",
                        message=f"Check {check.__name__} raised an exception",
                    )
                )

        elapsed = (time.monotonic() - t0) * 1000
        counts: dict[str, int] = Counter(i.category for i in all_issues)
        return LintReport(
            issues=all_issues,
            stats=dict(counts),
            checked_at=datetime.now(UTC),
            duration_ms=round(elapsed, 1),
        )

    # ── Individual checks ────────────────────────────────────────────────

    async def check_junk_entities(self) -> list[LintIssue]:
        """Find entities with no linked memories or names shorter than 2 chars."""
        issues: list[LintIssue] = []
        entities = await self._store.list_entities()

        for entity in entities:
            # Short name check
            if len(entity.name.strip()) < 2:
                issues.append(
                    LintIssue(
                        severity="warning",
                        category="junk_entity",
                        message=(
                            f"Entity '{entity.name}' (type={entity.type}) "
                            f"has name shorter than 2 characters"
                        ),
                        entity_id=entity.id,
                    )
                )

            # No linked memories check (via graph engine reverse lookup)
            linked = self._graph.get_memory_ids_for_entity(entity.id)
            if not linked:
                issues.append(
                    LintIssue(
                        severity="info",
                        category="junk_entity",
                        message=(
                            f"Entity '{entity.name}' (type={entity.type}) "
                            f"has no linked memories in graph"
                        ),
                        entity_id=entity.id,
                    )
                )

        return issues

    async def check_duplicate_content(self) -> list[LintIssue]:
        """Find memories that share the same content_hash (exact duplicates).

        The admission pipeline should deduplicate, so duplicates indicate
        a bug or bypass of the normal ingest path.
        """
        issues: list[LintIssue] = []
        # Fetch all memories; use a large limit
        memories = await self._store.list_memories(limit=100_000)

        hash_groups: dict[str, list[str]] = {}
        for mem in memories:
            if mem.content_hash:
                hash_groups.setdefault(mem.content_hash, []).append(mem.id)

        for content_hash, ids in hash_groups.items():
            if len(ids) > 1:
                issues.append(
                    LintIssue(
                        severity="warning",
                        category="duplicate",
                        message=(
                            f"{len(ids)} memories share content_hash "
                            f"{content_hash[:16]}...: {', '.join(ids[:5])}"
                        ),
                    )
                )

        return issues

    async def check_dangling_memory_entity_links(self) -> list[LintIssue]:
        """Find memory-entity links where one side is missing.

        Checks:
        - memory_entities rows referencing a memory_id that no longer exists
        - memory_entities rows referencing an entity_id that no longer exists
        """
        issues: list[LintIssue] = []

        # Build lookup sets
        memories = await self._store.list_memories(limit=100_000)
        memory_ids = {m.id for m in memories}

        entities = await self._store.list_entities()
        entity_ids = {e.id for e in entities}

        # For each memory, check its entity links
        for mem in memories:
            linked_entity_ids = await self._store.get_memory_entities(mem.id)
            for eid in linked_entity_ids:
                if eid not in entity_ids:
                    issues.append(
                        LintIssue(
                            severity="error",
                            category="dangling_ref",
                            message=(
                                f"Memory {mem.id[:16]}... links to "
                                f"non-existent entity {eid[:16]}..."
                            ),
                            memory_id=mem.id,
                            entity_id=eid,
                        )
                    )

        # For each entity, check that its linked memories exist
        for entity in entities:
            linked_memory_ids = self._graph.get_memory_ids_for_entity(entity.id)
            for mid in linked_memory_ids:
                if mid not in memory_ids:
                    issues.append(
                        LintIssue(
                            severity="error",
                            category="dangling_ref",
                            message=(
                                f"Entity '{entity.name}' ({entity.id[:16]}...) "
                                f"links to non-existent memory {mid[:16]}..."
                            ),
                            entity_id=entity.id,
                            memory_id=mid,
                        )
                    )

        return issues

    async def check_stale_episodes(
        self,
        stale_hours: int = 72,
    ) -> list[LintIssue]:
        """Find open episodes with no recent member activity.

        An episode open for longer than ``stale_hours`` with no new
        members is likely abandoned and should be closed.
        """
        issues: list[LintIssue] = []
        cutoff = datetime.now(UTC) - timedelta(hours=stale_hours)

        open_episodes = await self._store.get_open_episodes()
        for ep in open_episodes:
            members = await self._store.get_episode_members(ep.id)
            if not members:
                # Open episode with zero members
                issues.append(
                    LintIssue(
                        severity="warning",
                        category="stale_episode",
                        message=(
                            f"Open episode {ep.id[:16]}... "
                            f"('{ep.metadata.get('episode_title', '')}') "
                            f"has no members"
                        ),
                    )
                )
                continue

            # Find the most recent member timestamp
            latest = max(
                (m.created_at for m in members if m.created_at),
                default=ep.created_at,
            )
            if latest and latest < cutoff:
                hours_ago = int((datetime.now(UTC) - latest).total_seconds() / 3600)
                issues.append(
                    LintIssue(
                        severity="info",
                        category="stale_episode",
                        message=(
                            f"Open episode {ep.id[:16]}... "
                            f"('{ep.metadata.get('episode_title', '')}') "
                            f"last activity {hours_ago}h ago "
                            f"({len(members)} members)"
                        ),
                    )
                )

        return issues

    async def check_orphan_memory_nodes(self) -> list[LintIssue]:
        """Find memory_nodes whose parent memory_id no longer exists."""
        issues: list[LintIssue] = []

        memories = await self._store.list_memories(limit=100_000)
        memory_ids = {m.id for m in memories}

        for node_type in (NodeType.ATOMIC, NodeType.ENTITY_STATE):
            nodes = await self._store.get_memory_nodes_by_type(node_type.value)
            for node in nodes:
                if node.memory_id not in memory_ids:
                    issues.append(
                        LintIssue(
                            severity="error",
                            category="orphan",
                            message=(
                                f"{node_type.value} node {node.id[:16]}... "
                                f"references non-existent memory "
                                f"{node.memory_id[:16]}..."
                            ),
                            memory_id=node.memory_id,
                        )
                    )

        return issues
