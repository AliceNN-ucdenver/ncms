"""Row → domain-model converters for :class:`SQLiteStore`.

Extracted from ``sqlite_store.py`` so the orchestrator stays under
the B+ MI bar.  Every function takes one ``aiosqlite.Row`` and
returns one domain model instance.

Pure: no I/O, no logging, no class state — just JSON parsing +
ISO datetime parsing.
"""

from __future__ import annotations

import json
from datetime import datetime

import aiosqlite

from ncms.domain.models import (
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


def row_to_memory(row: aiosqlite.Row) -> Memory:
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
        observed_at=(datetime.fromisoformat(observed_raw) if observed_raw else None),
        source_agent=row["source_agent"],
        project=row["project"],
        domains=json.loads(row["domains"]),
        tags=json.loads(row["tags"]),
    )


def row_to_entity(row: aiosqlite.Row) -> Entity:
    return Entity(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        attributes=json.loads(row["attributes"]) if row["attributes"] else {},
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def row_to_relationship(row: aiosqlite.Row) -> Relationship:
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


def row_to_snapshot(row: aiosqlite.Row) -> KnowledgeSnapshot:
    raw_entries = json.loads(row["entries"])
    entries = [SnapshotEntry(**e) for e in raw_entries]
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


def row_to_memory_node(row: aiosqlite.Row) -> MemoryNode:
    # Graceful fallback for pre-V3 rows (observed_at/ingested_at may be missing).
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
        valid_from=(datetime.fromisoformat(row["valid_from"]) if row["valid_from"] else None),
        valid_to=(datetime.fromisoformat(row["valid_to"]) if row["valid_to"] else None),
        observed_at=(datetime.fromisoformat(observed_at_raw) if observed_at_raw else None),
        ingested_at=(
            datetime.fromisoformat(ingested_at_raw)
            if ingested_at_raw
            else datetime.fromisoformat(row["created_at"])
        ),
        metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def row_to_graph_edge(row: aiosqlite.Row) -> GraphEdge:
    # Schema v12: retires_entities column stores a JSON array.
    retires_raw: str | None = None
    try:
        retires_raw = row["retires_entities"]
    except (IndexError, KeyError):
        retires_raw = None
    retires_entities = json.loads(retires_raw) if retires_raw else []
    return GraphEdge(
        id=row["id"],
        source_id=row["source_id"],
        target_id=row["target_id"],
        edge_type=EdgeType(row["edge_type"]),
        weight=row["weight"],
        metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        retires_entities=retires_entities,
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def row_to_ephemeral(row: aiosqlite.Row) -> EphemeralEntry:
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


def row_to_search_log(row: aiosqlite.Row) -> SearchLogEntry:
    return SearchLogEntry(
        id=row["id"],
        query=row["query"],
        query_entities=json.loads(row["query_entities"]),
        returned_ids=json.loads(row["returned_ids"]),
        timestamp=datetime.fromisoformat(row["timestamp"]),
        agent_id=row["agent_id"],
    )
