"""SQLite implementation of MemoryStore and SnapshotStore.

Uses aiosqlite for async access and WAL mode for concurrent reads.
All SQL is parameterized to prevent injection.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from ncms.domain.models import (
    AccessRecord,
    Entity,
    KnowledgeSnapshot,
    Memory,
    Relationship,
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
               (id, content, structured, type, importance, created_at, updated_at,
                source_agent, project, domains, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                memory.id,
                memory.content,
                json.dumps(memory.structured) if memory.structured else None,
                memory.type,
                memory.importance,
                memory.created_at.isoformat(),
                memory.updated_at.isoformat(),
                memory.source_agent,
                memory.project,
                json.dumps(memory.domains),
                json.dumps(memory.tags),
            ),
        )
        await self.db.commit()

    async def get_memory(self, memory_id: str) -> Memory | None:
        cursor = await self.db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_memory(row)

    async def update_memory(self, memory: Memory) -> None:
        await self.save_memory(memory)

    async def delete_memory(self, memory_id: str) -> None:
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

    # ── Row Converters ───────────────────────────────────────────────────

    @staticmethod
    def _row_to_memory(row: aiosqlite.Row) -> Memory:
        return Memory(
            id=row["id"],
            content=row["content"],
            structured=json.loads(row["structured"]) if row["structured"] else None,
            type=row["type"],
            importance=row["importance"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
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
