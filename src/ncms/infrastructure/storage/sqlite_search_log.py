"""SQLite ``search_log`` + ``association_strengths`` tables.

Pure functions over an :class:`aiosqlite.Connection` — extracted from
:class:`SQLiteStore` in the Phase F MI cleanup so the orchestrator
stays under the A-grade maintainability bar.

The class keeps thin delegates that call these helpers; this is a
pure structural split with no behavioural changes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiosqlite

from ncms.domain.models import SearchLogEntry
from ncms.infrastructure.storage.row_mappers import row_to_search_log


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# search_log
# ---------------------------------------------------------------------------


async def log_search(db: aiosqlite.Connection, entry: SearchLogEntry) -> None:
    """Log a search query and its returned memory IDs."""
    await db.execute(
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
    await db.commit()


async def get_recent_searches(
    db: aiosqlite.Connection,
    limit: int = 100,
    since: str | None = None,
) -> list[SearchLogEntry]:
    """Get recent search log entries, optionally filtered by timestamp."""
    if since:
        cursor = await db.execute(
            """SELECT * FROM search_log
               WHERE timestamp > ?
               ORDER BY timestamp DESC LIMIT ?""",
            (since, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM search_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
    rows = await cursor.fetchall()
    return [row_to_search_log(r) for r in rows]


async def get_search_access_pairs(
    db: aiosqlite.Connection,
    since: str | None = None,
) -> list[tuple[str, list[str]]]:
    """Get (query, returned_ids) pairs for PMI computation."""
    if since:
        cursor = await db.execute(
            """SELECT query, returned_ids FROM search_log
               WHERE timestamp > ?
               ORDER BY timestamp ASC""",
            (since,),
        )
    else:
        cursor = await db.execute(
            "SELECT query, returned_ids FROM search_log ORDER BY timestamp ASC"
        )
    rows = await cursor.fetchall()
    return [(row[0], json.loads(row[1])) for row in rows]


# ---------------------------------------------------------------------------
# association_strengths (Phase 8 — Dream Cycles)
# ---------------------------------------------------------------------------


async def save_association_strength(
    db: aiosqlite.Connection,
    entity_id_1: str,
    entity_id_2: str,
    strength: float,
) -> None:
    """UPSERT association strength with canonical ordering (min/max)."""
    e1, e2 = min(entity_id_1, entity_id_2), max(entity_id_1, entity_id_2)
    await db.execute(
        """INSERT OR REPLACE INTO association_strengths
           (entity_id_1, entity_id_2, strength, updated_at)
           VALUES (?, ?, ?, ?)""",
        (e1, e2, strength, _now_iso()),
    )
    await db.commit()


async def get_association_strengths(
    db: aiosqlite.Connection,
) -> dict[tuple[str, str], float]:
    """Load all association strengths into a dict with both direction lookups."""
    cursor = await db.execute(
        "SELECT entity_id_1, entity_id_2, strength FROM association_strengths"
    )
    rows = await cursor.fetchall()
    result: dict[tuple[str, str], float] = {}
    for row in rows:
        e1, e2, strength = row[0], row[1], row[2]
        result[(e1, e2)] = strength
        result[(e2, e1)] = strength
    return result


async def get_strong_associations(
    db: aiosqlite.Connection,
    min_strength: float = 0.3,
    limit: int = 50_000,
) -> list[tuple[str, str, float]]:
    """Load associations above min_strength, ordered descending."""
    cursor = await db.execute(
        "SELECT entity_id_1, entity_id_2, strength FROM association_strengths "
        "WHERE strength >= ? ORDER BY strength DESC LIMIT ?",
        (min_strength, limit),
    )
    return [(r[0], r[1], r[2]) for r in await cursor.fetchall()]
