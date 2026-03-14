"""SQLite schema DDL and migrations for NCMS."""

SCHEMA_VERSION = 4

# ── V1: Original schema ──────────────────────────────────────────────────

V1_TABLES = """
-- Core memory records
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    structured TEXT,
    type TEXT NOT NULL DEFAULT 'fact',
    importance REAL NOT NULL DEFAULT 5.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_agent TEXT,
    project TEXT,
    domains TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_memories_domains ON memories(domains);
CREATE INDEX IF NOT EXISTS idx_memories_agent ON memories(source_agent);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);

-- Entity registry (knowledge graph nodes)
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    attributes TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);

-- Relationships (knowledge graph edges)
CREATE TABLE IF NOT EXISTS relationships (
    id TEXT PRIMARY KEY,
    source_entity_id TEXT NOT NULL REFERENCES entities(id),
    target_entity_id TEXT NOT NULL REFERENCES entities(id),
    type TEXT NOT NULL,
    valid_at TEXT,
    invalid_at TEXT,
    source_memory_id TEXT REFERENCES memories(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rel_source ON relationships(source_entity_id);
CREATE INDEX IF NOT EXISTS idx_rel_target ON relationships(target_entity_id);

-- Memory-to-entity links
CREATE TABLE IF NOT EXISTS memory_entities (
    memory_id TEXT NOT NULL REFERENCES memories(id),
    entity_id TEXT NOT NULL REFERENCES entities(id),
    PRIMARY KEY (memory_id, entity_id)
);

-- ACT-R access history
CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL REFERENCES memories(id),
    accessed_at TEXT NOT NULL,
    accessing_agent TEXT,
    query_context TEXT
);
CREATE INDEX IF NOT EXISTS idx_access_memory ON access_log(memory_id, accessed_at);

-- Knowledge snapshots
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    domains TEXT NOT NULL DEFAULT '[]',
    entries TEXT NOT NULL DEFAULT '[]',
    is_incremental INTEGER DEFAULT 0,
    supersedes TEXT,
    ttl_hours INTEGER DEFAULT 168,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_agent ON snapshots(agent_id, timestamp DESC);

-- Consolidation state (singleton-ish key-value)
CREATE TABLE IF NOT EXISTS consolidation_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""

# ── V2: HTMG typed nodes + admission routing (Phase 1) ──────────────────

V2_TABLES = """
-- Typed HTMG nodes linked to canonical memories
CREATE TABLE IF NOT EXISTS memory_nodes (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES memories(id),
    node_type TEXT NOT NULL,
    parent_id TEXT,
    importance REAL NOT NULL DEFAULT 5.0,
    is_current INTEGER NOT NULL DEFAULT 1,
    valid_from TEXT,
    valid_to TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mnodes_memory ON memory_nodes(memory_id);
CREATE INDEX IF NOT EXISTS idx_mnodes_type ON memory_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_mnodes_parent ON memory_nodes(parent_id);

-- Typed directed edges in the HTMG
CREATE TABLE IF NOT EXISTS graph_edges (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gedges_source ON graph_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_gedges_target ON graph_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_gedges_type ON graph_edges(edge_type);

-- Ephemeral cache: short-lived entries below atomic threshold
CREATE TABLE IF NOT EXISTS ephemeral_cache (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    source_agent TEXT,
    domains TEXT NOT NULL DEFAULT '[]',
    admission_score REAL NOT NULL DEFAULT 0.0,
    ttl_seconds INTEGER NOT NULL DEFAULT 3600,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ephemeral_expires ON ephemeral_cache(expires_at);
"""

# ── V4: Dream cycle search logging + association strengths (Phase 8) ─────

V4_TABLES = """
-- Search log for tracking query → returned memory associations (dream cycle PMI)
CREATE TABLE IF NOT EXISTS search_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    query_entities TEXT NOT NULL DEFAULT '[]',
    returned_ids TEXT NOT NULL DEFAULT '[]',
    timestamp TEXT NOT NULL,
    agent_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_search_log_ts ON search_log(timestamp);

-- Learned association strengths between entity pairs (dream cycle PMI output)
CREATE TABLE IF NOT EXISTS association_strengths (
    entity_id_1 TEXT NOT NULL,
    entity_id_2 TEXT NOT NULL,
    strength REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (entity_id_1, entity_id_2)
);
"""

# Backward compat alias used by older code paths
CREATE_TABLES = V1_TABLES


async def run_migrations(db: object) -> None:
    """Run schema migrations on the given aiosqlite connection."""
    import aiosqlite

    assert isinstance(db, aiosqlite.Connection)

    # Check current version
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
    )
    cursor = await db.execute("SELECT MAX(version) FROM schema_version")
    row = await cursor.fetchone()
    current_version = row[0] if row and row[0] else 0

    if current_version < 1:
        await db.executescript(V1_TABLES)
        await db.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (1,),
        )
        await db.commit()
        current_version = 1

    if current_version < 2:
        await db.executescript(V2_TABLES)
        await db.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (2,),
        )
        await db.commit()
        current_version = 2

    if current_version < 3:
        # V3: Bitemporal columns for state reconciliation (Phase 2B)
        # Nullable columns — existing rows get NULL (safe)
        await db.execute(
            "ALTER TABLE memory_nodes ADD COLUMN observed_at TEXT"
        )
        await db.execute(
            "ALTER TABLE memory_nodes ADD COLUMN ingested_at TEXT"
        )
        await db.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (3,),
        )
        await db.commit()
        current_version = 3

    if current_version < 4:
        # V4: Dream cycle tables (Phase 8 — search logging + association learning)
        await db.executescript(V4_TABLES)
        await db.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (4,),
        )
        await db.commit()
