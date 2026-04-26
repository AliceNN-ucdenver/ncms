"""SQLite ``memory_nodes`` table queries (HTMG L1/L2/L3/L4 + edges).

Pure functions over an :class:`aiosqlite.Connection` — extracted from
:class:`SQLiteStore` in the Phase F MI cleanup so the orchestrator
stays under the A-grade maintainability bar.

Covers:

* L1/L2/L3/L4 node CRUD + batch loaders
* Entity-state queries (current / by-entity / point-in-time / history)
* Episode queries (open / members / closed-unsummarized)
* Abstract-node queries

The class keeps thin delegates that call these helpers; this is a
pure structural split with no behavioural changes.
"""

from __future__ import annotations

import json

import aiosqlite

from ncms.domain.models import MemoryNode, NodeType
from ncms.infrastructure.storage.row_mappers import row_to_memory_node


async def save_memory_node(db: aiosqlite.Connection, node: MemoryNode) -> None:
    await db.execute(
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
    await db.commit()


async def get_memory_node(db: aiosqlite.Connection, node_id: str) -> MemoryNode | None:
    cursor = await db.execute("SELECT * FROM memory_nodes WHERE id = ?", (node_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    return row_to_memory_node(row)


async def get_memory_nodes_by_type(db: aiosqlite.Connection, node_type: str) -> list[MemoryNode]:
    cursor = await db.execute(
        "SELECT * FROM memory_nodes WHERE node_type = ? ORDER BY created_at DESC",
        (node_type,),
    )
    rows = await cursor.fetchall()
    return [row_to_memory_node(r) for r in rows]


async def get_memory_nodes_for_memory(db: aiosqlite.Connection, memory_id: str) -> list[MemoryNode]:
    cursor = await db.execute(
        "SELECT * FROM memory_nodes WHERE memory_id = ? ORDER BY created_at DESC",
        (memory_id,),
    )
    rows = await cursor.fetchall()
    return [row_to_memory_node(r) for r in rows]


async def get_memory_nodes_for_memories(
    db: aiosqlite.Connection,
    memory_ids: list[str],
) -> dict[str, list[MemoryNode]]:
    """Batch-load memory nodes for multiple memory IDs.  Memory IDs
    with no nodes are omitted from the result."""
    if not memory_ids:
        return {}
    placeholders = ",".join("?" for _ in memory_ids)
    cursor = await db.execute(
        f"SELECT * FROM memory_nodes WHERE memory_id IN ({placeholders})"  # noqa: S608
        " ORDER BY memory_id, created_at DESC",
        tuple(memory_ids),
    )
    rows = await cursor.fetchall()
    result: dict[str, list[MemoryNode]] = {}
    for row in rows:
        node = row_to_memory_node(row)
        result.setdefault(node.memory_id, []).append(node)
    return result


# ---------------------------------------------------------------------------
# Entity state queries (Phase 2A)
# ---------------------------------------------------------------------------


async def get_current_entity_states(
    db: aiosqlite.Connection,
    entity_id: str,
    state_key: str,
) -> list[MemoryNode]:
    """Find current entity state nodes for entity_id + state_key."""
    cursor = await db.execute(
        """SELECT * FROM memory_nodes
           WHERE node_type = ?
             AND is_current = 1
             AND json_extract(metadata, '$.entity_id') = ?
             AND json_extract(metadata, '$.state_key') = ?
           ORDER BY created_at DESC""",
        (NodeType.ENTITY_STATE.value, entity_id, state_key),
    )
    rows = await cursor.fetchall()
    return [row_to_memory_node(r) for r in rows]


async def get_entity_states_by_entity(
    db: aiosqlite.Connection,
    entity_id: str,
) -> list[MemoryNode]:
    """All entity state nodes (current + superseded) for an entity."""
    cursor = await db.execute(
        """SELECT * FROM memory_nodes
           WHERE node_type = ?
             AND json_extract(metadata, '$.entity_id') = ?
           ORDER BY created_at DESC""",
        (NodeType.ENTITY_STATE.value, entity_id),
    )
    rows = await cursor.fetchall()
    return [row_to_memory_node(r) for r in rows]


# ---------------------------------------------------------------------------
# Temporal queries (Phase 2B)
# ---------------------------------------------------------------------------


async def get_current_state(
    db: aiosqlite.Connection,
    entity_id: str,
    state_key: str,
) -> MemoryNode | None:
    """Single most-recent current state for entity+key."""
    cursor = await db.execute(
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
    return row_to_memory_node(row) if row else None


async def get_state_at_time(
    db: aiosqlite.Connection,
    entity_id: str,
    state_key: str,
    timestamp: str,
) -> MemoryNode | None:
    """Get the entity state that was valid at a specific point in time."""
    cursor = await db.execute(
        """SELECT * FROM memory_nodes
           WHERE node_type = ?
             AND json_extract(metadata, '$.entity_id') = ?
             AND json_extract(metadata, '$.state_key') = ?
             AND valid_from IS NOT NULL
             AND valid_from <= ?
             AND (valid_to IS NULL OR valid_to > ?)
           ORDER BY valid_from DESC
           LIMIT 1""",
        (NodeType.ENTITY_STATE.value, entity_id, state_key, timestamp, timestamp),
    )
    row = await cursor.fetchone()
    if row:
        return row_to_memory_node(row)
    cursor = await db.execute(
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
    return row_to_memory_node(row) if row else None


async def get_state_changes_since(
    db: aiosqlite.Connection,
    timestamp: str,
) -> list[MemoryNode]:
    cursor = await db.execute(
        """SELECT * FROM memory_nodes
           WHERE node_type = ?
             AND created_at > ?
           ORDER BY created_at ASC""",
        (NodeType.ENTITY_STATE.value, timestamp),
    )
    rows = await cursor.fetchall()
    return [row_to_memory_node(r) for r in rows]


async def get_state_history(
    db: aiosqlite.Connection,
    entity_id: str,
    state_key: str,
) -> list[MemoryNode]:
    cursor = await db.execute(
        """SELECT * FROM memory_nodes
           WHERE node_type = ?
             AND json_extract(metadata, '$.entity_id') = ?
             AND json_extract(metadata, '$.state_key') = ?
           ORDER BY created_at ASC""",
        (NodeType.ENTITY_STATE.value, entity_id, state_key),
    )
    rows = await cursor.fetchall()
    return [row_to_memory_node(r) for r in rows]


# ---------------------------------------------------------------------------
# Episode queries (Phase 3)
# ---------------------------------------------------------------------------


async def get_open_episodes(db: aiosqlite.Connection) -> list[MemoryNode]:
    cursor = await db.execute(
        """SELECT * FROM memory_nodes
           WHERE node_type = ?
             AND json_extract(metadata, '$.status') = ?
           ORDER BY created_at DESC""",
        (NodeType.EPISODE.value, "open"),
    )
    rows = await cursor.fetchall()
    return [row_to_memory_node(r) for r in rows]


async def get_episode_members(db: aiosqlite.Connection, episode_id: str) -> list[MemoryNode]:
    cursor = await db.execute(
        """SELECT * FROM memory_nodes
           WHERE parent_id = ?
           ORDER BY created_at ASC""",
        (episode_id,),
    )
    rows = await cursor.fetchall()
    return [row_to_memory_node(r) for r in rows]


async def get_episode_members_batch(
    db: aiosqlite.Connection,
    episode_ids: list[str],
) -> dict[str, list[MemoryNode]]:
    if not episode_ids:
        return {}
    placeholders = ",".join("?" for _ in episode_ids)
    cursor = await db.execute(
        f"SELECT * FROM memory_nodes WHERE parent_id IN ({placeholders}) "  # noqa: S608
        "ORDER BY created_at ASC",
        episode_ids,
    )
    rows = await cursor.fetchall()
    result: dict[str, list[MemoryNode]] = {eid: [] for eid in episode_ids}
    for row in rows:
        node = row_to_memory_node(row)
        if node.parent_id and node.parent_id in result:
            result[node.parent_id].append(node)
    return result


async def get_episode_member_entities_batch(
    db: aiosqlite.Connection,
    episode_ids: list[str],
) -> dict[str, list[str]]:
    """Get entity IDs for all members of multiple episodes (deduplicated per episode)."""
    if not episode_ids:
        return {}
    placeholders = ",".join("?" for _ in episode_ids)
    cursor = await db.execute(
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


# ---------------------------------------------------------------------------
# Phase 5: Consolidation queries
# ---------------------------------------------------------------------------


async def get_closed_unsummarized_episodes(db: aiosqlite.Connection) -> list[MemoryNode]:
    cursor = await db.execute(
        """SELECT * FROM memory_nodes
           WHERE node_type = ?
             AND json_extract(metadata, '$.status') = ?
             AND json_extract(metadata, '$.summarized') IS NULL
           ORDER BY created_at DESC""",
        (NodeType.EPISODE.value, "closed"),
    )
    rows = await cursor.fetchall()
    return [row_to_memory_node(r) for r in rows]


async def get_entities_with_state_count(
    db: aiosqlite.Connection,
    min_count: int,
) -> list[tuple[str, int]]:
    cursor = await db.execute(
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
    db: aiosqlite.Connection,
    abstract_type: str,
) -> list[MemoryNode]:
    cursor = await db.execute(
        """SELECT * FROM memory_nodes
           WHERE node_type = ?
             AND json_extract(metadata, '$.abstract_type') = ?
           ORDER BY created_at DESC""",
        (NodeType.ABSTRACT.value, abstract_type),
    )
    rows = await cursor.fetchall()
    return [row_to_memory_node(r) for r in rows]
