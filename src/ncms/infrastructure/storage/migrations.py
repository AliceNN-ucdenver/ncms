"""SQLite schema DDL for NCMS.

Single-pass schema creation — no incremental migrations.
All tables created in their final form.
"""

SCHEMA_VERSION = 12

CREATE_SCHEMA_SQL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- Core memory storage
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    structured TEXT,
    type TEXT NOT NULL DEFAULT 'fact',
    importance REAL NOT NULL DEFAULT 5.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- Bitemporal field (schema v10): when the source event happened.
    -- Distinct from created_at (ingest time). Used by temporal query
    -- scoring to match queries like "3 weeks ago" against when the
    -- underlying event occurred, not when NCMS observed it.
    observed_at TEXT,
    content_hash TEXT,
    source_agent TEXT,
    project TEXT,
    domains TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_memories_domains ON memories(domains);
CREATE INDEX IF NOT EXISTS idx_memories_agent ON memories(source_agent);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
CREATE INDEX IF NOT EXISTS idx_memories_content_hash ON memories(content_hash);
CREATE INDEX IF NOT EXISTS idx_memories_observed_at ON memories(observed_at);

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

-- Explicit relationships between entities (NOT co-occurrence — those
-- live in association_strengths, computed by dream cycles)
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

-- Knowledge snapshots (agent sleep/wake surrogates)
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

-- Consolidation state (key-value)
CREATE TABLE IF NOT EXISTS consolidation_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- HTMG typed nodes (atomic, entity_state, episode, abstract)
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
    created_at TEXT NOT NULL,
    observed_at TEXT,
    ingested_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mnodes_memory ON memory_nodes(memory_id);
CREATE INDEX IF NOT EXISTS idx_mnodes_type ON memory_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_mnodes_parent ON memory_nodes(parent_id);

-- P1-temporal-experiment: per-memory content date range.
-- Populated at ingest when temporal_range_filter_enabled is on and
-- GLiNER extracts at least one resolvable temporal span.
-- Queried by RetrievalPipeline to hard-filter candidates whose
-- content range doesn't overlap the query range.
CREATE TABLE IF NOT EXISTS memory_content_ranges (
    memory_id   TEXT PRIMARY KEY,
    range_start TEXT NOT NULL,      -- ISO 8601, UTC
    range_end   TEXT NOT NULL,      -- ISO 8601, UTC, exclusive
    span_count  INTEGER NOT NULL,   -- how many spans contributed
    source      TEXT NOT NULL,      -- 'gliner' | 'metadata' | 'mixed'
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mcr_range
    ON memory_content_ranges(range_start, range_end);

-- HTMG typed directed edges
CREATE TABLE IF NOT EXISTS graph_edges (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    metadata TEXT NOT NULL DEFAULT '{}',
    -- Schema v12 (TLG integration): structural retirement set.
    -- JSON array of entity IDs whose state this edge retires.
    -- Populated by ReconciliationService when emitting SUPERSEDES
    -- edges.  '[]' by default so TLG-unaware paths stay correct
    -- before Phase 1 wires the extractor.
    retires_entities TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gedges_source ON graph_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_gedges_target ON graph_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_gedges_type ON graph_edges(edge_type);

-- Schema v12 (TLG integration): persisted query-shape cache.
-- Keyed by `skeleton` — a normalized form of the incoming query —
-- so repeated query shapes skip the classify/dispatch cost.
-- Populated by the grammar dispatch pipeline (Phase 3).  Empty by
-- default; no read path fails when empty.
CREATE TABLE IF NOT EXISTS grammar_shape_cache (
    skeleton TEXT PRIMARY KEY,
    intent TEXT NOT NULL,
    slot_names TEXT,                    -- JSON array of slot names
    hit_count INTEGER NOT NULL DEFAULT 0,
    last_used TEXT
);
CREATE INDEX IF NOT EXISTS idx_gsc_hit_count
    ON grammar_shape_cache(hit_count DESC);

-- Schema v12 (TLG integration): L2 transition-marker inventory.
-- One row per (transition_type, marker_head) pair with a running
-- observation count.  Populated by induction (Phase 2) and read by
-- grammar dispatch (Phase 3).
CREATE TABLE IF NOT EXISTS grammar_transition_markers (
    transition_type TEXT NOT NULL,      -- supersedes | refines | conflicts_with | ...
    marker_head TEXT NOT NULL,          -- verb/phrase head (e.g. "became", "replaced by")
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (transition_type, marker_head)
);

-- Ephemeral cache (short-lived entries below atomic admission threshold)
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

-- Dream cycle: search logging for PMI computation
CREATE TABLE IF NOT EXISTS search_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    query_entities TEXT NOT NULL DEFAULT '[]',
    returned_ids TEXT NOT NULL DEFAULT '[]',
    timestamp TEXT NOT NULL,
    agent_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_search_log_ts ON search_log(timestamp);

-- Dream cycle: learned entity-pair association strengths (PMI-based).
-- This IS the spreading activation graph — loaded into NetworkX on startup.
-- Updated by dream cycles, not per-memory ingestion.
CREATE TABLE IF NOT EXISTS association_strengths (
    entity_id_1 TEXT NOT NULL,
    entity_id_2 TEXT NOT NULL,
    strength REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (entity_id_1, entity_id_2)
);

-- Dashboard event persistence
CREATE TABLE IF NOT EXISTS dashboard_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    timestamp TEXT NOT NULL,
    type TEXT NOT NULL,
    agent_id TEXT,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_devents_ts ON dashboard_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_devents_type ON dashboard_events(type);
CREATE INDEX IF NOT EXISTS idx_devents_agent ON dashboard_events(agent_id);

-- Projects
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    target TEXT DEFAULT '',
    source_type TEXT DEFAULT 'research',
    repository_url TEXT,
    scope TEXT DEFAULT '[]',
    status TEXT DEFAULT 'active',
    phase TEXT DEFAULT 'pending',
    quality_score REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);
CREATE INDEX IF NOT EXISTS idx_projects_created ON projects(created_at);

-- Documents (versioned artifacts)
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    from_agent TEXT,
    doc_type TEXT,
    version INTEGER DEFAULT 1,
    parent_doc_id TEXT,
    format TEXT DEFAULT 'markdown',
    size_bytes INTEGER,
    content_hash TEXT,
    entities TEXT DEFAULT '[]',
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id);
CREATE INDEX IF NOT EXISTS idx_documents_type ON documents(doc_type);
CREATE INDEX IF NOT EXISTS idx_documents_agent ON documents(from_agent);
CREATE INDEX IF NOT EXISTS idx_documents_parent ON documents(parent_doc_id);

-- Document derivation links
CREATE TABLE IF NOT EXISTS document_links (
    id TEXT PRIMARY KEY,
    source_doc_id TEXT NOT NULL,
    target_doc_id TEXT NOT NULL,
    link_type TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_doc_id) REFERENCES documents(id),
    FOREIGN KEY (target_doc_id) REFERENCES documents(id)
);
CREATE INDEX IF NOT EXISTS idx_doclinks_source ON document_links(source_doc_id);
CREATE INDEX IF NOT EXISTS idx_doclinks_target ON document_links(target_doc_id);
CREATE INDEX IF NOT EXISTS idx_doclinks_type ON document_links(link_type);

-- Review scores
CREATE TABLE IF NOT EXISTS review_scores (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    project_id TEXT,
    reviewer_agent TEXT NOT NULL,
    review_round INTEGER DEFAULT 1,
    score INTEGER,
    severity TEXT,
    covered TEXT,
    missing TEXT,
    changes TEXT,
    review_doc_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_doc ON review_scores(document_id);
CREATE INDEX IF NOT EXISTS idx_reviews_project ON review_scores(project_id);
CREATE INDEX IF NOT EXISTS idx_reviews_score ON review_scores(score);

-- Pipeline events (audit trail)
CREATE TABLE IF NOT EXISTS pipeline_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    node TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT DEFAULT '',
    event_subtype TEXT DEFAULT '',
    timestamp TEXT NOT NULL,
    prev_hash TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_pipeline_project ON pipeline_events(project_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_ts ON pipeline_events(timestamp);

-- Approval decisions
CREATE TABLE IF NOT EXISTS approval_decisions (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    document_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    approver TEXT NOT NULL,
    comment TEXT,
    policies_active TEXT DEFAULT '{}',
    timestamp TEXT NOT NULL,
    prev_hash TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (document_id) REFERENCES documents(id)
);
CREATE INDEX IF NOT EXISTS idx_approvals_project ON approval_decisions(project_id);

-- Guardrail violations
CREATE TABLE IF NOT EXISTS guardrail_violations (
    id TEXT PRIMARY KEY,
    document_id TEXT,
    project_id TEXT,
    policy_type TEXT NOT NULL,
    rule TEXT NOT NULL,
    message TEXT,
    escalation TEXT NOT NULL,
    overridden INTEGER DEFAULT 0,
    override_reason TEXT,
    timestamp TEXT NOT NULL,
    prev_hash TEXT,
    FOREIGN KEY (document_id) REFERENCES documents(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_guardrails_project ON guardrail_violations(project_id);

-- Grounding log (review citations to memories)
CREATE TABLE IF NOT EXISTS grounding_log (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    review_score_id TEXT,
    memory_id TEXT NOT NULL,
    retrieval_score REAL,
    entity_query TEXT,
    domain TEXT,
    timestamp TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(id),
    FOREIGN KEY (review_score_id) REFERENCES review_scores(id)
);
CREATE INDEX IF NOT EXISTS idx_grounding_doc ON grounding_log(document_id);

-- LLM call tracking
CREATE TABLE IF NOT EXISTS llm_calls (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    agent TEXT NOT NULL,
    node TEXT NOT NULL,
    prompt_hash TEXT,
    prompt_size INTEGER,
    response_size INTEGER,
    reasoning_size INTEGER DEFAULT 0,
    model TEXT,
    thinking_enabled INTEGER DEFAULT 0,
    duration_ms INTEGER,
    trace_id TEXT,
    timestamp TEXT NOT NULL,
    prev_hash TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_llm_project ON llm_calls(project_id);
CREATE INDEX IF NOT EXISTS idx_llm_agent ON llm_calls(agent);

-- Agent config snapshots
CREATE TABLE IF NOT EXISTS agent_config_snapshots (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    agent TEXT NOT NULL,
    config_hash TEXT,
    prompt_version TEXT,
    model_name TEXT,
    thinking_enabled INTEGER DEFAULT 0,
    max_tokens INTEGER,
    timestamp TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

-- Bus conversation audit trail
CREATE TABLE IF NOT EXISTS bus_conversations (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    ask_id TEXT NOT NULL,
    from_agent TEXT NOT NULL,
    to_agent TEXT,
    question_preview TEXT,
    answer_preview TEXT,
    confidence REAL,
    duration_ms INTEGER,
    timestamp TEXT NOT NULL,
    prev_hash TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_bus_project ON bus_conversations(project_id);
CREATE INDEX IF NOT EXISTS idx_bus_askid ON bus_conversations(ask_id);

-- Pending approvals (guardrail gate)
CREATE TABLE IF NOT EXISTS pending_approvals (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    agent TEXT NOT NULL,
    node TEXT NOT NULL,
    violations TEXT NOT NULL DEFAULT '[]',
    context TEXT DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    decided_by TEXT,
    comment TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_approvals(status);
CREATE INDEX IF NOT EXISTS idx_pending_project ON pending_approvals(project_id);

-- Users (authentication)
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    role TEXT DEFAULT 'reviewer',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username);
"""

# Backward compat alias used by older code paths
CREATE_TABLES = CREATE_SCHEMA_SQL


async def create_schema(db: object) -> None:
    """Create all NCMS tables in their final form."""
    import aiosqlite

    assert isinstance(db, aiosqlite.Connection)

    await db.executescript(CREATE_SCHEMA_SQL)
    await db.execute(
        "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
        (SCHEMA_VERSION,),
    )

    # Seed default admin user
    from datetime import UTC, datetime

    import bcrypt as _bcrypt

    _hash = _bcrypt.hashpw(b"ncms", _bcrypt.gensalt()).decode()
    _now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT OR IGNORE INTO users"
        " (id, username, password_hash,"
        " display_name, role, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("usr-admin-001", "shawn", _hash, "Shawn", "admin", _now),
    )
    await db.commit()


async def run_migrations(db: object) -> None:
    """Initialize schema — creates fresh or validates existing."""
    import aiosqlite

    assert isinstance(db, aiosqlite.Connection)

    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='schema_version'"
    )
    has_version_table = await cursor.fetchone() is not None

    if not has_version_table:
        await create_schema(db)
        return

    cursor = await db.execute("SELECT MAX(version) FROM schema_version")
    row = await cursor.fetchone()
    current_version = row[0] if row and row[0] else 0

    if current_version == SCHEMA_VERSION:
        return

    if current_version < SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current_version} is outdated "
            f"(expected {SCHEMA_VERSION}). Delete the database file and restart."
        )
