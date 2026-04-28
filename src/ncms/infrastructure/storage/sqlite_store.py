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
    Entity,
    EphemeralEntry,
    GraphEdge,
    KnowledgeSnapshot,
    Memory,
    MemoryNode,
    Relationship,
    SearchLogEntry,
)
from ncms.infrastructure.storage.migrations import run_migrations
from ncms.infrastructure.storage.row_mappers import (
    row_to_entity,
    row_to_ephemeral,
    row_to_graph_edge,
    row_to_memory,
    row_to_relationship,
    row_to_snapshot,
)


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
        """Insert-or-replace a ``Memory`` row.

        Schema v13: reads intent-slot classifier outputs from
        ``memory.structured["intent_slot"]`` (the convention the
        :class:`IngestionPipeline` follows post-P2) and persists
        them to dedicated columns so dashboard / analytics queries
        can filter without JSON parsing.  When the key is absent
        (pre-P2 ingest or feature flag off) the columns stay NULL.
        """
        intent_slot = (memory.structured or {}).get("intent_slot") or {}
        await self.db.execute(
            """INSERT OR REPLACE INTO memories
               (id, content, structured, type, importance, content_hash,
                created_at, updated_at, observed_at, source_agent,
                project, domains, tags,
                intent, intent_confidence, topic, topic_confidence,
                admission_decision, state_change, intent_slot_method)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?)""",
            (
                memory.id,
                memory.content,
                json.dumps(memory.structured) if memory.structured else None,
                memory.type,
                memory.importance,
                memory.content_hash,
                memory.created_at.isoformat(),
                memory.updated_at.isoformat(),
                memory.observed_at.isoformat() if memory.observed_at else None,
                memory.source_agent,
                memory.project,
                json.dumps(memory.domains),
                json.dumps(memory.tags),
                intent_slot.get("intent"),
                intent_slot.get("intent_confidence"),
                intent_slot.get("topic"),
                intent_slot.get("topic_confidence"),
                intent_slot.get("admission"),
                intent_slot.get("state_change"),
                intent_slot.get("method"),
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
        return row_to_memory(row)

    async def find_memory_by_doc_id(self, doc_id: str) -> Memory | None:
        """Look up the profile memory whose ``structured.doc_id`` equals ``doc_id``.

        Implements the :class:`MemoryStore` protocol.  Uses SQLite's
        JSON1 ``json_extract`` to query the ``structured`` column
        without a dedicated index — Phase A scale (one profile per
        document) makes this acceptable; a future schema bump can
        add an indexed ``doc_id`` column if profile-memory volume
        grows enough to warrant one.
        """
        cursor = await self.db.execute(
            "SELECT * FROM memories "
            "WHERE json_extract(structured, '$.doc_id') = ? "
            "LIMIT 1",
            (doc_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return row_to_memory(row)

    async def get_memory(self, memory_id: str) -> Memory | None:
        cursor = await self.db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return row_to_memory(row)

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
        return {row["id"]: row_to_memory(row) for row in rows}

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
        await self.db.execute("DELETE FROM memory_nodes WHERE memory_id = ?", (memory_id,))
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
        return [row_to_memory(r) for r in rows]

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
        self,
        memory_ids: list[str],
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
        return row_to_entity(row)

    async def find_entity_by_name(self, name: str) -> Entity | None:
        cursor = await self.db.execute(
            "SELECT * FROM entities WHERE name = ? COLLATE NOCASE LIMIT 1", (name,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return row_to_entity(row)

    async def list_entities(self, entity_type: str | None = None) -> list[Entity]:
        if entity_type:
            cursor = await self.db.execute("SELECT * FROM entities WHERE type = ?", (entity_type,))
        else:
            cursor = await self.db.execute("SELECT * FROM entities")
        rows = await cursor.fetchall()
        return [row_to_entity(r) for r in rows]

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
        return [row_to_relationship(r) for r in rows]

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

    async def get_memory_entity_names(self, memory_id: str) -> list[str]:
        """Return entity *names* (not IDs) linked to ``memory_id``.

        TLG vocabulary induction (L1) needs surface forms, not
        primary-key UUIDs, to seed the subject + entity lookup
        tables.  Callers that want the IDs (graph service, dispatch
        zone lookup) keep using ``get_memory_entities``.
        """
        cursor = await self.db.execute(
            """SELECT e.name FROM memory_entities me
               JOIN entities e ON me.entity_id = e.id
               WHERE me.memory_id = ?""",
            (memory_id,),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows if row[0]]

    async def find_memory_ids_by_entity(
        self,
        entity_identifier: str,
    ) -> list[str]:
        """Return memory IDs linked to the given entity.

        ``entity_identifier`` can be either an entity ID (primary key
        in ``entities``) or an entity name.  We first try the ID
        match, then fall back to a name lookup — both paths hit the
        ``idx_entities_name`` index so cost is O(log |entities|)
        regardless of which form the caller passes.

        Used by TLG dispatch (Phase 4 entity-memory index) to shrink
        the event-name resolution from O(|subject_nodes|) to a
        store-level index lookup.
        """
        # Exact-ID match first.  Most callers pass a name, but
        # internal code sometimes passes an ID — either is safe.
        cursor = await self.db.execute(
            """SELECT DISTINCT me.memory_id
               FROM memory_entities me
               WHERE me.entity_id = ?
               UNION
               SELECT DISTINCT me.memory_id
               FROM memory_entities me
               JOIN entities e ON me.entity_id = e.id
               WHERE LOWER(e.name) = LOWER(?)""",
            (entity_identifier, entity_identifier),
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
        self,
        memory_id: str,
    ) -> tuple[str, str] | None:
        cursor = await self.db.execute(
            "SELECT range_start, range_end FROM memory_content_ranges WHERE memory_id = ?",
            (memory_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return (row[0], row[1])

    async def get_content_ranges_batch(
        self,
        memory_ids: list[str],
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
        return row_to_snapshot(row)

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
        return [row_to_snapshot(row) for row in rows]

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
        await self.db.execute("DELETE FROM consolidation_state WHERE key = ?", (key,))
        await self.db.commit()

    # ── Memory Nodes (Phase 1 — HTMG) ──────────────────────────────────

    async def save_memory_node(self, node: MemoryNode) -> None:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        await _mn.save_memory_node(self.db, node)

    async def get_memory_node(self, node_id: str) -> MemoryNode | None:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_memory_node(self.db, node_id)

    async def get_memory_nodes_by_type(self, node_type: str) -> list[MemoryNode]:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_memory_nodes_by_type(self.db, node_type)

    async def get_memory_nodes_for_memory(self, memory_id: str) -> list[MemoryNode]:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_memory_nodes_for_memory(self.db, memory_id)

    async def get_memory_nodes_for_memories(
        self,
        memory_ids: list[str],
    ) -> dict[str, list[MemoryNode]]:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_memory_nodes_for_memories(self.db, memory_ids)

    # ── Entity State Queries (Phase 2A) ──────────────────────────────────

    async def get_current_entity_states(
        self,
        entity_id: str,
        state_key: str,
    ) -> list[MemoryNode]:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_current_entity_states(self.db, entity_id, state_key)

    async def get_entity_states_by_entity(
        self,
        entity_id: str,
    ) -> list[MemoryNode]:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_entity_states_by_entity(self.db, entity_id)

    async def update_memory_node(self, node: MemoryNode) -> None:
        """Update an existing memory node (INSERT OR REPLACE)."""
        await self.save_memory_node(node)

    # ── Temporal Queries (Phase 2B) ──────────────────────────────────────

    async def get_current_state(
        self,
        entity_id: str,
        state_key: str,
    ) -> MemoryNode | None:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_current_state(self.db, entity_id, state_key)

    async def get_state_at_time(
        self,
        entity_id: str,
        state_key: str,
        timestamp: str,
    ) -> MemoryNode | None:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_state_at_time(self.db, entity_id, state_key, timestamp)

    async def get_state_changes_since(self, timestamp: str) -> list[MemoryNode]:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_state_changes_since(self.db, timestamp)

    async def get_state_history(
        self,
        entity_id: str,
        state_key: str,
    ) -> list[MemoryNode]:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_state_history(self.db, entity_id, state_key)

    # ── Episode Queries (Phase 3) ─────────────────────────────────────

    async def get_open_episodes(self) -> list[MemoryNode]:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_open_episodes(self.db)

    async def get_episode_members(self, episode_id: str) -> list[MemoryNode]:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_episode_members(self.db, episode_id)

    async def get_episode_members_batch(
        self,
        episode_ids: list[str],
    ) -> dict[str, list[MemoryNode]]:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_episode_members_batch(self.db, episode_ids)

    async def get_episode_member_entities_batch(
        self,
        episode_ids: list[str],
    ) -> dict[str, list[str]]:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_episode_member_entities_batch(self.db, episode_ids)

    # ── Phase 5: Consolidation Queries ─────────────────────────────────

    async def get_closed_unsummarized_episodes(self) -> list[MemoryNode]:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_closed_unsummarized_episodes(self.db)

    async def get_entities_with_state_count(
        self,
        min_count: int,
    ) -> list[tuple[str, int]]:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_entities_with_state_count(self.db, min_count)

    async def get_abstract_nodes_by_type(
        self,
        abstract_type: str,
    ) -> list[MemoryNode]:
        from ncms.infrastructure.storage import sqlite_memory_nodes as _mn

        return await _mn.get_abstract_nodes_by_type(self.db, abstract_type)

    # ── Graph Edges (Phase 1 — HTMG) ────────────────────────────────────

    async def save_graph_edge(self, edge: GraphEdge) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO graph_edges
               (id, source_id, target_id, edge_type, weight, metadata,
                retires_entities, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                edge.id,
                edge.source_id,
                edge.target_id,
                edge.edge_type.value,
                edge.weight,
                json.dumps(edge.metadata),
                json.dumps(edge.retires_entities),
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
        return [row_to_graph_edge(r) for r in rows]

    async def list_graph_edges_by_type(
        self,
        edge_types: list[str],
    ) -> list[GraphEdge]:
        """All edges of the given types, ordered newest-first.

        Used by TLG L2 induction (``application/tlg/induction``) to
        scan every supersession / refinement edge.  The result is
        bounded by the number of state transitions — small in
        practice compared to the memory corpus.
        """
        if not edge_types:
            return []
        placeholders = ",".join("?" * len(edge_types))
        cursor = await self.db.execute(
            f"""SELECT * FROM graph_edges
                WHERE edge_type IN ({placeholders})
                ORDER BY created_at DESC""",
            tuple(edge_types),
        )
        rows = await cursor.fetchall()
        return [row_to_graph_edge(r) for r in rows]

    # ── TLG Grammar Transition Markers (schema v12) ──────────────────────

    async def save_transition_markers(
        self,
        markers: dict[str, frozenset[str]],
    ) -> None:
        """Replace the entire ``grammar_transition_markers`` table.

        Induction is a snapshot: the full L2 output for the current
        corpus.  We clear the table and re-insert rather than
        merging — a marker that is no longer distinctive after new
        edges land must leave the table, not linger.

        ``markers`` maps transition-type (e.g. ``"supersedes"``) to
        the frozenset of distinctive verb heads for that bucket.
        """
        await self.db.execute("DELETE FROM grammar_transition_markers")
        for transition, heads in markers.items():
            for head in heads:
                await self.db.execute(
                    """INSERT INTO grammar_transition_markers
                       (transition_type, marker_head, count)
                       VALUES (?, ?, 1)""",
                    (transition, head),
                )
        await self.db.commit()

    async def load_transition_markers(self) -> dict[str, frozenset[str]]:
        """Return the persisted marker table keyed by transition type.

        Empty dict when induction hasn't run yet — callers that need
        a seed (e.g. the reconciliation retirement extractor) should
        fall back to :data:`SEED_RETIREMENT_VERBS` in that case.
        """
        cursor = await self.db.execute(
            "SELECT transition_type, marker_head FROM grammar_transition_markers"
        )
        rows = await cursor.fetchall()
        bucketed: dict[str, set[str]] = {}
        for transition, head in rows:
            bucketed.setdefault(transition, set()).add(head)
        return {t: frozenset(heads) for t, heads in bucketed.items()}

    # ── TLG grammar-shape cache (schema v12, Phase 3d) ────────────────────

    async def save_shape_cache_entry(
        self,
        skeleton: str,
        intent: str,
        slot_names: list[str],
        hit_count: int,
        last_used: str | None,
    ) -> None:
        """Upsert one cache entry.

        Called by :class:`ncms.application.tlg.shape_cache_store.ShapeCacheStore`
        on every learn event — idempotent: conflicting rows update
        hit_count + last_used only, never intent (production remains
        the authority for intent assignment).
        """
        import json

        await self.db.execute(
            """INSERT INTO grammar_shape_cache
               (skeleton, intent, slot_names, hit_count, last_used)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(skeleton) DO UPDATE SET
                   hit_count = excluded.hit_count,
                   last_used = excluded.last_used""",
            (
                skeleton,
                intent,
                json.dumps(slot_names),
                hit_count,
                last_used,
            ),
        )
        await self.db.commit()

    async def load_shape_cache(self) -> dict[str, dict]:
        """Return the full persisted shape cache in snapshot form."""
        import json

        cursor = await self.db.execute(
            "SELECT skeleton, intent, slot_names, hit_count, last_used FROM grammar_shape_cache",
        )
        rows = await cursor.fetchall()
        out: dict[str, dict] = {}
        for skel, intent, slot_names_json, hit_count, last_used in rows:
            out[skel] = {
                "intent": intent,
                "slot_names": json.loads(slot_names_json or "[]"),
                "hit_count": int(hit_count),
                "last_used": last_used,
            }
        return out

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
        cursor = await self.db.execute("SELECT * FROM ephemeral_cache WHERE id = ?", (entry_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return row_to_ephemeral(row)

    async def expire_ephemeral(self) -> int:
        """Delete expired ephemeral entries. Returns count deleted."""
        now = _now_iso()
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM ephemeral_cache WHERE expires_at <= ?", (now,)
        )
        count_row = await cursor.fetchone()
        count = count_row[0] if count_row else 0
        if count > 0:
            await self.db.execute("DELETE FROM ephemeral_cache WHERE expires_at <= ?", (now,))
            await self.db.commit()
        return count

    # ── Search Log (Phase 8 — Dream Cycles) ────────────────────────────

    async def log_search(self, entry: SearchLogEntry) -> None:
        from ncms.infrastructure.storage import sqlite_search_log as _sl

        await _sl.log_search(self.db, entry)

    async def get_recent_searches(
        self,
        limit: int = 100,
        since: str | None = None,
    ) -> list[SearchLogEntry]:
        from ncms.infrastructure.storage import sqlite_search_log as _sl

        return await _sl.get_recent_searches(self.db, limit=limit, since=since)

    async def get_search_access_pairs(
        self,
        since: str | None = None,
    ) -> list[tuple[str, list[str]]]:
        from ncms.infrastructure.storage import sqlite_search_log as _sl

        return await _sl.get_search_access_pairs(self.db, since=since)

    # ── Association Strengths (Phase 8 — Dream Cycles) ───────────────

    async def save_association_strength(
        self,
        entity_id_1: str,
        entity_id_2: str,
        strength: float,
    ) -> None:
        from ncms.infrastructure.storage import sqlite_search_log as _sl

        await _sl.save_association_strength(self.db, entity_id_1, entity_id_2, strength)

    async def get_association_strengths(self) -> dict[tuple[str, str], float]:
        from ncms.infrastructure.storage import sqlite_search_log as _sl

        return await _sl.get_association_strengths(self.db)

    async def get_strong_associations(
        self,
        min_strength: float = 0.3,
        limit: int = 50_000,
    ) -> list[tuple[str, str, float]]:
        from ncms.infrastructure.storage import sqlite_search_log as _sl

        return await _sl.get_strong_associations(self.db, min_strength=min_strength, limit=limit)

    # ── Intent-Slot integration (Schema v13, P2) ────────────────────

    async def save_memory_slots(
        self,
        memory_id: str,
        slots: dict[str, str],
        confidences: dict[str, float] | None = None,
    ) -> None:
        """Persist per-memory slot surface forms from the classifier.

        Replaces any existing slot rows for ``memory_id``.  Empty
        ``slots`` dict → deletes all rows and returns.
        """
        await self.db.execute(
            "DELETE FROM memory_slots WHERE memory_id = ?",
            (memory_id,),
        )
        if not slots:
            await self.db.commit()
            return
        confidences = confidences or {}
        await self.db.executemany(
            """INSERT OR REPLACE INTO memory_slots
               (memory_id, slot_name, slot_value, slot_confidence)
               VALUES (?, ?, ?, ?)""",
            [
                (memory_id, name, value, confidences.get(name))
                for name, value in slots.items()
                if value
            ],
        )
        await self.db.commit()

    async def get_memory_slots(
        self,
        memory_id: str,
    ) -> dict[str, str]:
        """Return ``{slot_name: slot_value}`` for a given memory."""
        cursor = await self.db.execute(
            "SELECT slot_name, slot_value FROM memory_slots WHERE memory_id = ?",
            (memory_id,),
        )
        rows = await cursor.fetchall()
        return {row["slot_name"]: row["slot_value"] for row in rows}

    async def save_intent_slot_adapter(
        self,
        *,
        adapter_id: str,
        domain: str,
        version: str,
        adapter_path: str,
        encoder: str,
        corpus_hash: str,
        gate_passed: bool,
        gate_metrics_json: str | None,
        promoted_at: str,
        active: bool = False,
    ) -> None:
        """Insert/update a row in the adapter registry.

        Called by ``ncms adapter-promote``.  Does NOT flip the
        active bit — that's a separate op via
        :meth:`set_active_intent_slot_adapter` so promotion and
        activation can be audited separately.
        """
        await self.db.execute(
            """INSERT OR REPLACE INTO intent_slot_adapters
               (adapter_id, domain, version, adapter_path, encoder,
                corpus_hash, gate_passed, gate_metrics_json,
                promoted_at, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                adapter_id,
                domain,
                version,
                adapter_path,
                encoder,
                corpus_hash,
                1 if gate_passed else 0,
                gate_metrics_json,
                promoted_at,
                1 if active else 0,
            ),
        )
        await self.db.commit()

    async def set_active_intent_slot_adapter(
        self,
        adapter_id: str,
    ) -> None:
        """Flip the ``active`` bit to 1 for exactly one adapter.

        Clears ``active=1`` on any other adapter for the same
        domain.  A service restart is required for the ingest
        pipeline to pick up the new checkpoint path.
        """
        cursor = await self.db.execute(
            "SELECT domain FROM intent_slot_adapters WHERE adapter_id = ?",
            (adapter_id,),
        )
        row = await cursor.fetchone()
        if not row:
            raise ValueError(f"unknown adapter_id {adapter_id!r}")
        domain = row["domain"]
        await self.db.execute(
            "UPDATE intent_slot_adapters SET active = 0 WHERE domain = ?",
            (domain,),
        )
        await self.db.execute(
            "UPDATE intent_slot_adapters SET active = 1 WHERE adapter_id = ?",
            (adapter_id,),
        )
        await self.db.commit()

    async def get_active_intent_slot_adapter(
        self,
        domain: str | None = None,
    ) -> dict[str, object] | None:
        """Return the currently-active adapter row, optionally scoped.

        When ``domain`` is given returns the active adapter for that
        domain.  Otherwise returns the first active adapter across
        all domains (useful for single-domain deployments).
        """
        if domain is not None:
            cursor = await self.db.execute(
                "SELECT * FROM intent_slot_adapters WHERE domain = ? AND active = 1 LIMIT 1",
                (domain,),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM intent_slot_adapters WHERE active = 1 LIMIT 1",
            )
        row = await cursor.fetchone()
        if not row:
            return None
        return dict(row)

    async def list_intent_slot_adapters(
        self,
        domain: str | None = None,
    ) -> list[dict[str, object]]:
        """List all registered adapters, newest first."""
        if domain is not None:
            cursor = await self.db.execute(
                "SELECT * FROM intent_slot_adapters WHERE domain = ? ORDER BY promoted_at DESC",
                (domain,),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM intent_slot_adapters ORDER BY promoted_at DESC",
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def list_topics_seen(
        self,
        domain: str | None = None,
    ) -> list[dict[str, object]]:
        """Enumerate topics that have actually been persisted.

        Reads from the memories table — no coupling to the adapter
        manifest.  Dashboard uses this to render the topic-
        distribution view without knowing the trained taxonomy
        ahead of time (honours the "dynamic topics" design).
        """
        sql = (
            "SELECT topic, COUNT(*) AS n, MAX(updated_at) AS last_seen "
            "FROM memories WHERE topic IS NOT NULL"
        )
        params: list[object] = []
        if domain is not None:
            sql += " AND domains LIKE ?"
            params.append(f'%"{domain}"%')
        sql += " GROUP BY topic ORDER BY n DESC"
        cursor = await self.db.execute(sql, params)
        rows = await cursor.fetchall()
        return [
            {
                "topic": row["topic"],
                "count": row["n"],
                "last_seen": row["last_seen"],
            }
            for row in rows
        ]
