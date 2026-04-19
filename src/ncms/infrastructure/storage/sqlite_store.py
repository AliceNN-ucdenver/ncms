"""SQLite implementation of MemoryStore and SnapshotStore.

Uses aiosqlite for async access and WAL mode for concurrent reads.
All SQL is parameterized to prevent injection.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from ncms.domain.models import (
    AccessRecord,
    EdgeType,
    Entity,
    EphemeralEntry,
    GraphEdge,
    KnowledgeSnapshot,
    Memory,
    MemoryNode,
    NodeType,
    Relationship,
    SearchLogEntry,
    SnapshotEntry,
)
from ncms.infrastructure.storage.migrations import run_migrations


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class SQLiteStore:
    """Async SQLite storage backend for NCMS."""

    def __init__(self, db_path: str = ":memory:"):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await run_migrations(self._db)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Store not initialized. Call initialize() first."
        return self._db

    # ── Memory CRUD ──────────────────────────────────────────────────────

    async def save_memory(self, memory: Memory) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO memories
               (id, content, structured, type, importance, content_hash,
                created_at, updated_at, observed_at, source_agent,
                project, domains, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                memory.id,
                memory.content,
                json.dumps(memory.structured) if memory.structured else None,
                memory.type,
                memory.importance,
                memory.content_hash,
                memory.created_at.isoformat(),
                memory.updated_at.isoformat(),
                memory.observed_at.isoformat()
                if memory.observed_at else None,
                memory.source_agent,
                memory.project,
                json.dumps(memory.domains),
                json.dumps(memory.tags),
            ),
        )
        await self.db.commit()

    async def get_memory_by_content_hash(self, content_hash: str) -> Memory | None:
        """Look up a memory by its SHA-256 content hash (dedup gate)."""
        cursor = await self.db.execute(
            "SELECT * FROM memories WHERE content_hash = ? LIMIT 1",
            (content_hash,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_memory(row)

    async def get_memory(self, memory_id: str) -> Memory | None:
        cursor = await self.db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_memory(row)

    async def get_memories_batch(self, memory_ids: list[str]) -> dict[str, Memory]:
        """Load multiple memories in a single query."""
        if not memory_ids:
            return {}
        placeholders = ",".join("?" for _ in memory_ids)
        cursor = await self.db.execute(
            f"SELECT * FROM memories WHERE id IN ({placeholders})",  # noqa: S608
            memory_ids,
        )
        rows = await cursor.fetchall()
        return {row["id"]: self._row_to_memory(row) for row in rows}

    async def update_memory(self, memory: Memory) -> None:
        await self.save_memory(memory)

    async def delete_memory(self, memory_id: str) -> None:
        # Delete HTMG edges referencing any nodes for this memory
        await self.db.execute(
            "DELETE FROM graph_edges WHERE source_id IN "
            "(SELECT id FROM memory_nodes WHERE memory_id = ?) "
            "OR target_id IN (SELECT id FROM memory_nodes WHERE memory_id = ?)",
            (memory_id, memory_id),
        )
        # Delete HTMG nodes (FK to memories)
        await self.db.execute(
            "DELETE FROM memory_nodes WHERE memory_id = ?", (memory_id,)
        )
        await self.db.execute("DELETE FROM memory_entities WHERE memory_id = ?", (memory_id,))
        await self.db.execute("DELETE FROM access_log WHERE memory_id = ?", (memory_id,))
        await self.db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        await self.db.commit()

    async def list_memories(
        self,
        domain: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
        since: str | None = None,
        memory_type: str | None = None,
    ) -> list[Memory]:
        query = "SELECT * FROM memories WHERE 1=1"
        params: list[object] = []

        if domain:
            query += " AND domains LIKE ?"
            params.append(f'%"{domain}"%')
        if agent_id:
            query += " AND source_agent = ?"
            params.append(agent_id)
        if since:
            query += " AND created_at > ?"
            params.append(since)
        if memory_type:
            query += " AND type = ?"
            params.append(memory_type)

        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [self._row_to_memory(r) for r in rows]

    async def count_memories(self) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) FROM memories")
        row = await cursor.fetchone()
        return row[0] if row else 0

    # ── Access Log ───────────────────────────────────────────────────────

    async def log_access(self, record: AccessRecord) -> None:
        await self.db.execute(
            """INSERT INTO access_log (memory_id, accessed_at, accessing_agent, query_context)
               VALUES (?, ?, ?, ?)""",
            (
                record.memory_id,
                record.accessed_at.isoformat(),
                record.accessing_agent,
                record.query_context,
            ),
        )
        await self.db.commit()

    async def prune_access_records(self, memory_id: str, max_age_days: int) -> int:
        """Delete access records older than max_age_days for a memory.

        Returns the number of records deleted.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
        cursor = await self.db.execute(
            "DELETE FROM access_log WHERE memory_id = ? AND accessed_at < ?",
            (memory_id, cutoff),
        )
        await self.db.commit()
        return cursor.rowcount

    async def get_access_times(self, memory_id: str) -> list[float]:
        """Return ages in seconds of all accesses for a memory."""
        now = datetime.now(UTC)
        cursor = await self.db.execute(
            "SELECT accessed_at FROM access_log WHERE memory_id = ? ORDER BY accessed_at DESC",
            (memory_id,),
        )
        rows = await cursor.fetchall()
        ages = []
        for row in rows:
            accessed = datetime.fromisoformat(row[0])
            if accessed.tzinfo is None:
                accessed = accessed.replace(tzinfo=UTC)
            age = (now - accessed).total_seconds()
            if age > 0:
                ages.append(age)
        return ages

    async def get_access_times_batch(
        self, memory_ids: list[str],
    ) -> dict[str, list[float]]:
        """Return ages in seconds for multiple memories in a single query."""
        if not memory_ids:
            return {}
        now = datetime.now(UTC)
        placeholders = ",".join("?" for _ in memory_ids)
        cursor = await self.db.execute(
            "SELECT memory_id, accessed_at FROM access_log "
            f"WHERE memory_id IN ({placeholders}) "  # noqa: S608
            "ORDER BY memory_id, accessed_at DESC",
            memory_ids,
        )
        rows = await cursor.fetchall()
        result: dict[str, list[float]] = {mid: [] for mid in memory_ids}
        for row in rows:
            accessed = datetime.fromisoformat(row[1])
            if accessed.tzinfo is None:
                accessed = accessed.replace(tzinfo=UTC)
            age = (now - accessed).total_seconds()
            if age > 0:
                result[row[0]].append(age)
        return result

    # ── Entity CRUD ──────────────────────────────────────────────────────

    async def save_entity(self, entity: Entity) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO entities (id, name, type, attributes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                entity.id,
                entity.name,
                entity.type,
                json.dumps(entity.attributes),
                entity.created_at.isoformat(),
                entity.updated_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def get_entity(self, entity_id: str) -> Entity | None:
        cursor = await self.db.execute("SELECT * FROM entities WHERE id = ?", (entity_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_entity(row)

    async def find_entity_by_name(self, name: str) -> Entity | None:
        cursor = await self.db.execute(
            "SELECT * FROM entities WHERE name = ? COLLATE NOCASE LIMIT 1", (name,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_entity(row)

    async def list_entities(self, entity_type: str | None = None) -> list[Entity]:
        if entity_type:
            cursor = await self.db.execute(
                "SELECT * FROM entities WHERE type = ?", (entity_type,)
            )
        else:
            cursor = await self.db.execute("SELECT * FROM entities")
        rows = await cursor.fetchall()
        return [self._row_to_entity(r) for r in rows]

    # ── Relationship CRUD ────────────────────────────────────────────────

    async def save_relationship(self, rel: Relationship) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO relationships
               (id, source_entity_id, target_entity_id, type, valid_at, invalid_at,
                source_memory_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rel.id,
                rel.source_entity_id,
                rel.target_entity_id,
                rel.type,
                rel.valid_at.isoformat() if rel.valid_at else None,
                rel.invalid_at.isoformat() if rel.invalid_at else None,
                rel.source_memory_id,
                rel.created_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def get_relationships(self, entity_id: str) -> list[Relationship]:
        cursor = await self.db.execute(
            """SELECT * FROM relationships
               WHERE source_entity_id = ? OR target_entity_id = ?""",
            (entity_id, entity_id),
        )
        rows = await cursor.fetchall()
        return [self._row_to_relationship(r) for r in rows]

    # ── Memory-Entity Links ──────────────────────────────────────────────

    async def link_memory_entity(self, memory_id: str, entity_id: str) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO memory_entities (memory_id, entity_id) VALUES (?, ?)",
            (memory_id, entity_id),
        )
        await self.db.commit()

    async def get_memory_entities(self, memory_id: str) -> list[str]:
        cursor = await self.db.execute(
            "SELECT entity_id FROM memory_entities WHERE memory_id = ?", (memory_id,)
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    # ── Content Ranges (P1-temporal-experiment) ──────────────────────────

    async def save_content_range(
        self,
        memory_id: str,
        range_start: str,
        range_end: str,
        span_count: int,
        source: str,
    ) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO memory_content_ranges
               (memory_id, range_start, range_end, span_count, source)
               VALUES (?, ?, ?, ?, ?)""",
            (memory_id, range_start, range_end, span_count, source),
        )
        await self.db.commit()

    async def get_content_range(
        self, memory_id: str,
    ) -> tuple[str, str] | None:
        cursor = await self.db.execute(
            "SELECT range_start, range_end FROM memory_content_ranges "
            "WHERE memory_id = ?",
            (memory_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return (row[0], row[1])

    async def get_content_ranges_batch(
        self, memory_ids: list[str],
    ) -> dict[str, tuple[str, str]]:
        if not memory_ids:
            return {}
        placeholders = ",".join("?" * len(memory_ids))
        cursor = await self.db.execute(
            f"SELECT memory_id, range_start, range_end "
            f"FROM memory_content_ranges "
            f"WHERE memory_id IN ({placeholders})",
            memory_ids,
        )
        rows = await cursor.fetchall()
        return {row[0]: (row[1], row[2]) for row in rows}

    # ── Snapshots ────────────────────────────────────────────────────────

    async def save_snapshot(self, snapshot: KnowledgeSnapshot) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO snapshots
               (snapshot_id, agent_id, timestamp, domains, entries, is_incremental,
                supersedes, ttl_hours, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot.snapshot_id,
                snapshot.agent_id,
                snapshot.timestamp.isoformat(),
                json.dumps(snapshot.domains),
                json.dumps([e.model_dump(mode="json") for e in snapshot.entries]),
                1 if snapshot.is_incremental else 0,
                snapshot.supersedes,
                snapshot.ttl_hours,
                _now_iso(),
            ),
        )
        await self.db.commit()

    async def get_latest_snapshot(self, agent_id: str) -> KnowledgeSnapshot | None:
        cursor = await self.db.execute(
            """SELECT * FROM snapshots WHERE agent_id = ?
               ORDER BY timestamp DESC LIMIT 1""",
            (agent_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_snapshot(row)

    async def delete_snapshot(self, agent_id: str) -> None:
        await self.db.execute("DELETE FROM snapshots WHERE agent_id = ?", (agent_id,))
        await self.db.commit()

    async def get_snapshots_by_domain(self, domain: str) -> list[KnowledgeSnapshot]:
        """Find snapshots whose domains list contains the given domain."""
        cursor = await self.db.execute(
            """SELECT * FROM snapshots
               WHERE domains LIKE ?
               ORDER BY timestamp DESC""",
            (f'%"{domain}"%',),
        )
        rows = await cursor.fetchall()
        return [self._row_to_snapshot(row) for row in rows]

    # ── Consolidation State ──────────────────────────────────────────────

    async def get_consolidation_value(self, key: str) -> str | None:
        cursor = await self.db.execute(
            "SELECT value FROM consolidation_state WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_consolidation_value(self, key: str, value: str) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO consolidation_state (key, value, updated_at)
               VALUES (?, ?, ?)""",
            (key, value, _now_iso()),
        )
        await self.db.commit()

    async def delete_consolidation_value(self, key: str) -> None:
        await self.db.execute(
            "DELETE FROM consolidation_state WHERE key = ?", (key,)
        )
        await self.db.commit()

    # ── Memory Nodes (Phase 1 — HTMG) ──────────────────────────────────

    async def save_memory_node(self, node: MemoryNode) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO memory_nodes
               (id, memory_id, node_type, parent_id, importance, is_current,
                valid_from, valid_to, observed_at, ingested_at, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                node.id,
                node.memory_id,
                node.node_type.value,
                node.parent_id,
                node.importance,
                1 if node.is_current else 0,
                node.valid_from.isoformat() if node.valid_from else None,
                node.valid_to.isoformat() if node.valid_to else None,
                node.observed_at.isoformat() if node.observed_at else None,
                node.ingested_at.isoformat(),
                json.dumps(node.metadata),
                node.created_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def get_memory_node(self, node_id: str) -> MemoryNode | None:
        cursor = await self.db.execute(
            "SELECT * FROM memory_nodes WHERE id = ?", (node_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_memory_node(row)

    async def get_memory_nodes_by_type(self, node_type: str) -> list[MemoryNode]:
        cursor = await self.db.execute(
            "SELECT * FROM memory_nodes WHERE node_type = ? ORDER BY created_at DESC",
            (node_type,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_memory_node(r) for r in rows]

    async def get_memory_nodes_for_memory(self, memory_id: str) -> list[MemoryNode]:
        cursor = await self.db.execute(
            "SELECT * FROM memory_nodes WHERE memory_id = ? ORDER BY created_at DESC",
            (memory_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_memory_node(r) for r in rows]

    async def get_memory_nodes_for_memories(
        self, memory_ids: list[str],
    ) -> dict[str, list[MemoryNode]]:
        """Batch-load memory nodes for multiple memory IDs in a single query.

        Returns a dict mapping memory_id → list[MemoryNode].
        Memory IDs with no nodes are omitted from the result.
        """
        if not memory_ids:
            return {}
        placeholders = ",".join("?" for _ in memory_ids)
        cursor = await self.db.execute(
            f"SELECT * FROM memory_nodes WHERE memory_id IN ({placeholders})"  # noqa: S608
            " ORDER BY memory_id, created_at DESC",
            tuple(memory_ids),
        )
        rows = await cursor.fetchall()
        result: dict[str, list[MemoryNode]] = {}
        for row in rows:
            node = self._row_to_memory_node(row)
            result.setdefault(node.memory_id, []).append(node)
        return result

    # ── Entity State Queries (Phase 2A) ──────────────────────────────────

    async def get_current_entity_states(
        self, entity_id: str, state_key: str,
    ) -> list[MemoryNode]:
        """Find current entity state nodes for a given entity_id + state_key."""
        cursor = await self.db.execute(
            """SELECT * FROM memory_nodes
               WHERE node_type = ?
                 AND is_current = 1
                 AND json_extract(metadata, '$.entity_id') = ?
                 AND json_extract(metadata, '$.state_key') = ?
               ORDER BY created_at DESC""",
            (NodeType.ENTITY_STATE.value, entity_id, state_key),
        )
        rows = await cursor.fetchall()
        return [self._row_to_memory_node(r) for r in rows]

    async def get_entity_states_by_entity(
        self, entity_id: str,
    ) -> list[MemoryNode]:
        """Find all entity state nodes (current and superseded) for an entity."""
        cursor = await self.db.execute(
            """SELECT * FROM memory_nodes
               WHERE node_type = ?
                 AND json_extract(metadata, '$.entity_id') = ?
               ORDER BY created_at DESC""",
            (NodeType.ENTITY_STATE.value, entity_id),
        )
        rows = await cursor.fetchall()
        return [self._row_to_memory_node(r) for r in rows]

    async def update_memory_node(self, node: MemoryNode) -> None:
        """Update an existing memory node (INSERT OR REPLACE)."""
        await self.save_memory_node(node)

    # ── Temporal Queries (Phase 2B) ──────────────────────────────────────

    async def get_current_state(
        self, entity_id: str, state_key: str,
    ) -> MemoryNode | None:
        """Get the single current state for an entity+key (most recent if multiple)."""
        cursor = await self.db.execute(
            """SELECT * FROM memory_nodes
               WHERE node_type = ?
                 AND is_current = 1
                 AND json_extract(metadata, '$.entity_id') = ?
                 AND json_extract(metadata, '$.state_key') = ?
               ORDER BY created_at DESC
               LIMIT 1""",
            (NodeType.ENTITY_STATE.value, entity_id, state_key),
        )
        row = await cursor.fetchone()
        return self._row_to_memory_node(row) if row else None

    async def get_state_at_time(
        self, entity_id: str, state_key: str, timestamp: str,
    ) -> MemoryNode | None:
        """Get the entity state that was valid at a specific point in time.

        Finds the node where valid_from <= timestamp and
        (valid_to IS NULL OR valid_to > timestamp).
        Falls back to the most recent created_at <= timestamp if no
        valid_from is set.
        """
        # Prefer nodes with explicit valid_from
        cursor = await self.db.execute(
            """SELECT * FROM memory_nodes
               WHERE node_type = ?
                 AND json_extract(metadata, '$.entity_id') = ?
                 AND json_extract(metadata, '$.state_key') = ?
                 AND valid_from IS NOT NULL
                 AND valid_from <= ?
                 AND (valid_to IS NULL OR valid_to > ?)
               ORDER BY valid_from DESC
               LIMIT 1""",
            (
                NodeType.ENTITY_STATE.value,
                entity_id, state_key,
                timestamp, timestamp,
            ),
        )
        row = await cursor.fetchone()
        if row:
            return self._row_to_memory_node(row)

        # Fallback: use created_at
        cursor = await self.db.execute(
            """SELECT * FROM memory_nodes
               WHERE node_type = ?
                 AND json_extract(metadata, '$.entity_id') = ?
                 AND json_extract(metadata, '$.state_key') = ?
                 AND created_at <= ?
               ORDER BY created_at DESC
               LIMIT 1""",
            (NodeType.ENTITY_STATE.value, entity_id, state_key, timestamp),
        )
        row = await cursor.fetchone()
        return self._row_to_memory_node(row) if row else None

    async def get_state_changes_since(
        self, timestamp: str,
    ) -> list[MemoryNode]:
        """Get all entity state changes since a given timestamp."""
        cursor = await self.db.execute(
            """SELECT * FROM memory_nodes
               WHERE node_type = ?
                 AND created_at > ?
               ORDER BY created_at ASC""",
            (NodeType.ENTITY_STATE.value, timestamp),
        )
        rows = await cursor.fetchall()
        return [self._row_to_memory_node(r) for r in rows]

    async def get_state_history(
        self, entity_id: str, state_key: str,
    ) -> list[MemoryNode]:
        """Get full history of an entity+key (current + superseded, chronological)."""
        cursor = await self.db.execute(
            """SELECT * FROM memory_nodes
               WHERE node_type = ?
                 AND json_extract(metadata, '$.entity_id') = ?
                 AND json_extract(metadata, '$.state_key') = ?
               ORDER BY created_at ASC""",
            (NodeType.ENTITY_STATE.value, entity_id, state_key),
        )
        rows = await cursor.fetchall()
        return [self._row_to_memory_node(r) for r in rows]

    # ── Episode Queries (Phase 3) ─────────────────────────────────────

    async def get_open_episodes(self) -> list[MemoryNode]:
        """Get all open episode nodes."""
        cursor = await self.db.execute(
            """SELECT * FROM memory_nodes
               WHERE node_type = ?
                 AND json_extract(metadata, '$.status') = ?
               ORDER BY created_at DESC""",
            (NodeType.EPISODE.value, "open"),
        )
        rows = await cursor.fetchall()
        return [self._row_to_memory_node(r) for r in rows]

    async def get_episode_members(self, episode_id: str) -> list[MemoryNode]:
        """Get all fragment nodes belonging to an episode via parent_id."""
        cursor = await self.db.execute(
            """SELECT * FROM memory_nodes
               WHERE parent_id = ?
               ORDER BY created_at ASC""",
            (episode_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_memory_node(r) for r in rows]

    async def get_episode_members_batch(
        self, episode_ids: list[str],
    ) -> dict[str, list[MemoryNode]]:
        """Get fragment nodes for multiple episodes in a single query."""
        if not episode_ids:
            return {}
        placeholders = ",".join("?" for _ in episode_ids)
        cursor = await self.db.execute(
            f"SELECT * FROM memory_nodes WHERE parent_id IN ({placeholders}) "  # noqa: S608
            "ORDER BY created_at ASC",
            episode_ids,
        )
        rows = await cursor.fetchall()
        result: dict[str, list[MemoryNode]] = {eid: [] for eid in episode_ids}
        for row in rows:
            node = self._row_to_memory_node(row)
            if node.parent_id and node.parent_id in result:
                result[node.parent_id].append(node)
        return result

    async def get_episode_member_entities_batch(
        self, episode_ids: list[str],
    ) -> dict[str, list[str]]:
        """Get entity IDs for all members of multiple episodes in a single query.

        Returns {episode_id: [entity_id, ...]} with deduplication per episode.
        """
        if not episode_ids:
            return {}
        # Step 1: get all member memory_ids grouped by episode
        placeholders = ",".join("?" for _ in episode_ids)
        cursor = await self.db.execute(
            f"SELECT mn.parent_id, me.entity_id "  # noqa: S608
            f"FROM memory_nodes mn "
            f"JOIN memory_entities me ON mn.memory_id = me.memory_id "
            f"WHERE mn.parent_id IN ({placeholders})",
            episode_ids,
        )
        rows = await cursor.fetchall()
        result: dict[str, list[str]] = {eid: [] for eid in episode_ids}
        seen: dict[str, set[str]] = {eid: set() for eid in episode_ids}
        for row in rows:
            ep_id, entity_id = row[0], row[1]
            if ep_id in result and entity_id not in seen[ep_id]:
                result[ep_id].append(entity_id)
                seen[ep_id].add(entity_id)
        return result

    # ── Phase 5: Consolidation Queries ─────────────────────────────────

    async def get_closed_unsummarized_episodes(self) -> list[MemoryNode]:
        """Get closed episodes that have not been summarized yet."""
        cursor = await self.db.execute(
            """SELECT * FROM memory_nodes
               WHERE node_type = ?
                 AND json_extract(metadata, '$.status') = ?
                 AND json_extract(metadata, '$.summarized') IS NULL
               ORDER BY created_at DESC""",
            (NodeType.EPISODE.value, "closed"),
        )
        rows = await cursor.fetchall()
        return [self._row_to_memory_node(r) for r in rows]

    async def get_entities_with_state_count(
        self, min_count: int,
    ) -> list[tuple[str, int]]:
        """Get entity IDs with at least min_count state transitions."""
        cursor = await self.db.execute(
            """SELECT json_extract(metadata, '$.entity_id') AS eid,
                      COUNT(*) AS cnt
               FROM memory_nodes
               WHERE node_type = ?
                 AND json_extract(metadata, '$.entity_id') IS NOT NULL
               GROUP BY eid
               HAVING cnt >= ?
               ORDER BY cnt DESC""",
            (NodeType.ENTITY_STATE.value, min_count),
        )
        rows = await cursor.fetchall()
        return [(row[0], row[1]) for row in rows]

    async def get_abstract_nodes_by_type(
        self, abstract_type: str,
    ) -> list[MemoryNode]:
        """Get abstract memory nodes filtered by abstract_type metadata."""
        cursor = await self.db.execute(
            """SELECT * FROM memory_nodes
               WHERE node_type = ?
                 AND json_extract(metadata, '$.abstract_type') = ?
               ORDER BY created_at DESC""",
            (NodeType.ABSTRACT.value, abstract_type),
        )
        rows = await cursor.fetchall()
        return [self._row_to_memory_node(r) for r in rows]

    # ── Graph Edges (Phase 1 — HTMG) ────────────────────────────────────

    async def save_graph_edge(self, edge: GraphEdge) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO graph_edges
               (id, source_id, target_id, edge_type, weight, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                edge.id,
                edge.source_id,
                edge.target_id,
                edge.edge_type.value,
                edge.weight,
                json.dumps(edge.metadata),
                edge.created_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def get_graph_edges(
        self, source_id: str, edge_type: str | None = None
    ) -> list[GraphEdge]:
        if edge_type:
            cursor = await self.db.execute(
                """SELECT * FROM graph_edges
                   WHERE source_id = ? AND edge_type = ?
                   ORDER BY created_at DESC""",
                (source_id, edge_type),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM graph_edges WHERE source_id = ? ORDER BY created_at DESC",
                (source_id,),
            )
        rows = await cursor.fetchall()
        return [self._row_to_graph_edge(r) for r in rows]

    # ── Ephemeral Cache (Phase 1 — Admission Routing) ────────────────────

    async def save_ephemeral(self, entry: EphemeralEntry) -> None:
        expires_at = entry.expires_at or entry.created_at
        await self.db.execute(
            """INSERT OR REPLACE INTO ephemeral_cache
               (id, content, source_agent, domains, admission_score,
                ttl_seconds, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.id,
                entry.content,
                entry.source_agent,
                json.dumps(entry.domains),
                entry.admission_score,
                entry.ttl_seconds,
                entry.created_at.isoformat(),
                expires_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def get_ephemeral(self, entry_id: str) -> EphemeralEntry | None:
        cursor = await self.db.execute(
            "SELECT * FROM ephemeral_cache WHERE id = ?", (entry_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_ephemeral(row)

    async def expire_ephemeral(self) -> int:
        """Delete expired ephemeral entries. Returns count deleted."""
        now = _now_iso()
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM ephemeral_cache WHERE expires_at <= ?", (now,)
        )
        count_row = await cursor.fetchone()
        count = count_row[0] if count_row else 0
        if count > 0:
            await self.db.execute(
                "DELETE FROM ephemeral_cache WHERE expires_at <= ?", (now,)
            )
            await self.db.commit()
        return count

    # ── Search Log (Phase 8 — Dream Cycles) ────────────────────────────

    async def log_search(self, entry: SearchLogEntry) -> None:
        """Log a search query and its returned memory IDs."""
        await self.db.execute(
            """INSERT INTO search_log (query, query_entities, returned_ids, timestamp, agent_id)
               VALUES (?, ?, ?, ?, ?)""",
            (
                entry.query,
                json.dumps(entry.query_entities),
                json.dumps(entry.returned_ids),
                entry.timestamp.isoformat(),
                entry.agent_id,
            ),
        )
        await self.db.commit()

    async def get_recent_searches(
        self, limit: int = 100, since: str | None = None,
    ) -> list[SearchLogEntry]:
        """Get recent search log entries, optionally filtered by timestamp."""
        if since:
            cursor = await self.db.execute(
                """SELECT * FROM search_log
                   WHERE timestamp > ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (since, limit),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM search_log ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [self._row_to_search_log(r) for r in rows]

    async def get_search_access_pairs(
        self, since: str | None = None,
    ) -> list[tuple[str, list[str]]]:
        """Get (query, returned_ids) pairs for PMI computation."""
        if since:
            cursor = await self.db.execute(
                """SELECT query, returned_ids FROM search_log
                   WHERE timestamp > ?
                   ORDER BY timestamp ASC""",
                (since,),
            )
        else:
            cursor = await self.db.execute(
                "SELECT query, returned_ids FROM search_log ORDER BY timestamp ASC"
            )
        rows = await cursor.fetchall()
        return [(row[0], json.loads(row[1])) for row in rows]

    # ── Association Strengths (Phase 8 — Dream Cycles) ───────────────

    async def save_association_strength(
        self, entity_id_1: str, entity_id_2: str, strength: float,
    ) -> None:
        """UPSERT association strength with canonical ordering (min/max)."""
        # Canonical ordering ensures (A, B) and (B, A) map to same row
        e1, e2 = min(entity_id_1, entity_id_2), max(entity_id_1, entity_id_2)
        await self.db.execute(
            """INSERT OR REPLACE INTO association_strengths
               (entity_id_1, entity_id_2, strength, updated_at)
               VALUES (?, ?, ?, ?)""",
            (e1, e2, strength, _now_iso()),
        )
        await self.db.commit()

    async def get_association_strengths(self) -> dict[tuple[str, str], float]:
        """Load all association strengths into a dict with both direction lookups."""
        cursor = await self.db.execute(
            "SELECT entity_id_1, entity_id_2, strength FROM association_strengths"
        )
        rows = await cursor.fetchall()
        result: dict[tuple[str, str], float] = {}
        for row in rows:
            e1, e2, strength = row[0], row[1], row[2]
            # Store both directions for O(1) lookup in spreading_activation
            result[(e1, e2)] = strength
            result[(e2, e1)] = strength
        return result

    async def get_strong_associations(
        self, min_strength: float = 0.3, limit: int = 50_000,
    ) -> list[tuple[str, str, float]]:
        """Load associations above min_strength, ordered by strength descending."""
        cursor = await self.db.execute(
            "SELECT entity_id_1, entity_id_2, strength FROM association_strengths "
            "WHERE strength >= ? ORDER BY strength DESC LIMIT ?",
            (min_strength, limit),
        )
        return [(r[0], r[1], r[2]) for r in await cursor.fetchall()]

    # ── Row Converters ───────────────────────────────────────────────────

    @staticmethod
    def _row_to_memory(row: aiosqlite.Row) -> Memory:
        # aiosqlite.Row.__contains__ doesn't work on string keys; use
        # row.keys() explicitly.
        keys = set(row.keys())
        observed_raw = row["observed_at"] if "observed_at" in keys else None
        content_hash = row["content_hash"] if "content_hash" in keys else None
        return Memory(
            id=row["id"],
            content=row["content"],
            structured=json.loads(row["structured"]) if row["structured"] else None,
            type=row["type"],
            importance=row["importance"],
            content_hash=content_hash,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            observed_at=(
                datetime.fromisoformat(observed_raw)
                if observed_raw else None
            ),
            source_agent=row["source_agent"],
            project=row["project"],
            domains=json.loads(row["domains"]),
            tags=json.loads(row["tags"]),
        )

    @staticmethod
    def _row_to_entity(row: aiosqlite.Row) -> Entity:
        return Entity(
            id=row["id"],
            name=row["name"],
            type=row["type"],
            attributes=json.loads(row["attributes"]) if row["attributes"] else {},
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _row_to_relationship(row: aiosqlite.Row) -> Relationship:
        return Relationship(
            id=row["id"],
            source_entity_id=row["source_entity_id"],
            target_entity_id=row["target_entity_id"],
            type=row["type"],
            valid_at=datetime.fromisoformat(row["valid_at"]) if row["valid_at"] else None,
            invalid_at=datetime.fromisoformat(row["invalid_at"]) if row["invalid_at"] else None,
            source_memory_id=row["source_memory_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_snapshot(row: aiosqlite.Row) -> KnowledgeSnapshot:
        raw_entries = json.loads(row["entries"])
        entries = []
        for e in raw_entries:
            # Handle nested KnowledgePayload
            if isinstance(e.get("knowledge"), dict):
                entries.append(SnapshotEntry(**e))
            else:
                entries.append(SnapshotEntry(**e))

        return KnowledgeSnapshot(
            snapshot_id=row["snapshot_id"],
            agent_id=row["agent_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            domains=json.loads(row["domains"]),
            entries=entries,
            is_incremental=bool(row["is_incremental"]),
            supersedes=row["supersedes"],
            ttl_hours=row["ttl_hours"],
        )

    @staticmethod
    def _row_to_memory_node(row: aiosqlite.Row) -> MemoryNode:
        # Graceful fallback for pre-V3 rows (observed_at/ingested_at may be missing)
        row_keys = row.keys() if hasattr(row, "keys") else []
        observed_at_raw = row["observed_at"] if "observed_at" in row_keys else None
        ingested_at_raw = row["ingested_at"] if "ingested_at" in row_keys else None

        return MemoryNode(
            id=row["id"],
            memory_id=row["memory_id"],
            node_type=NodeType(row["node_type"]),
            parent_id=row["parent_id"],
            importance=row["importance"],
            is_current=bool(row["is_current"]),
            valid_from=(
                datetime.fromisoformat(row["valid_from"]) if row["valid_from"] else None
            ),
            valid_to=(
                datetime.fromisoformat(row["valid_to"]) if row["valid_to"] else None
            ),
            observed_at=(
                datetime.fromisoformat(observed_at_raw) if observed_at_raw else None
            ),
            ingested_at=(
                datetime.fromisoformat(ingested_at_raw)
                if ingested_at_raw
                else datetime.fromisoformat(row["created_at"])
            ),
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_graph_edge(row: aiosqlite.Row) -> GraphEdge:
        return GraphEdge(
            id=row["id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            edge_type=EdgeType(row["edge_type"]),
            weight=row["weight"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_ephemeral(row: aiosqlite.Row) -> EphemeralEntry:
        return EphemeralEntry(
            id=row["id"],
            content=row["content"],
            source_agent=row["source_agent"],
            domains=json.loads(row["domains"]),
            admission_score=row["admission_score"],
            ttl_seconds=row["ttl_seconds"],
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
        )

    @staticmethod
    def _row_to_search_log(row: aiosqlite.Row) -> SearchLogEntry:
        return SearchLogEntry(
            id=row["id"],
            query=row["query"],
            query_entities=json.loads(row["query_entities"]),
            returned_ids=json.loads(row["returned_ids"]),
            timestamp=datetime.fromisoformat(row["timestamp"]),
            agent_id=row["agent_id"],
        )
