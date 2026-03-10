"""SQLite schema DDL and migrations for NCMS."""

SCHEMA_VERSION = 1

CREATE_TABLES = """
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

    if current_version < SCHEMA_VERSION:
        await db.executescript(CREATE_TABLES)
        await db.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
        await db.commit()
