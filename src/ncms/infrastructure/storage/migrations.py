"""SQLite schema DDL and migrations for NCMS."""

SCHEMA_VERSION = 8

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

# ── V5: Dashboard event persistence for time-travel replay ───────────────

V5_TABLES = """
-- Persistent dashboard events for historical replay / time-travel debugging
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
"""

V6_TABLES = """
-- ═══════════════════════════════════════════════════════════════════════
-- Phase 2.5: Document Intelligence Persistence
-- Projects, documents, reviews, traceability, and audit tables.
-- ═══════════════════════════════════════════════════════════════════════

-- Projects: persistent, queryable, survives restarts
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

-- Documents: persistent, versioned, entity-enriched
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

-- Document relationships: cross-document traceability
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

-- Review scores: structured, queryable
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

-- Pipeline state: persistent workflow tracking
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

-- ═══════════════════════════════════════════════════════════════════════
-- Audit tables
-- ═══════════════════════════════════════════════════════════════════════

-- Human approval decisions
CREATE TABLE IF NOT EXISTS approval_decisions (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    document_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    approver TEXT NOT NULL,
    comment TEXT,
    policies_active TEXT DEFAULT '{}',
    timestamp TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (document_id) REFERENCES documents(id)
);
CREATE INDEX IF NOT EXISTS idx_approvals_project ON approval_decisions(project_id);

-- Guardrail violations linked to documents
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
    FOREIGN KEY (document_id) REFERENCES documents(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_guardrails_project ON guardrail_violations(project_id);

-- Knowledge grounding: links review citations to NCMS memories
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

-- LLM call metadata + Phoenix trace link
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
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_llm_project ON llm_calls(project_id);
CREATE INDEX IF NOT EXISTS idx_llm_agent ON llm_calls(agent);

-- Agent config snapshot at pipeline start
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

-- Bus conversation log (ask/respond pairs)
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
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_bus_project ON bus_conversations(project_id);
CREATE INDEX IF NOT EXISTS idx_bus_askid ON bus_conversations(ask_id);
"""

# ═════════════════════════════════════════════════════════════════════════
# V7: Guardrail Approval Gate (human-in-the-loop at guardrail checks)
# ═════════════════════════════════════════════════════════════════════════

V7_TABLES = """
-- Pending approvals: agent creates when guardrails flag block/reject violations.
-- Human approves or denies via dashboard. Agent polls for decision.
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
"""

# ═════════════════════════════════════════════════════════════════════════
# V8: Authentication + Tamper-evident hash chains
# ═════════════════════════════════════════════════════════════════════════

V8_TABLES = """
-- Users: local auth with bcrypt-hashed passwords
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

V8_HASH_CHAIN_COLUMNS = [
    "ALTER TABLE pipeline_events ADD COLUMN prev_hash TEXT",
    "ALTER TABLE approval_decisions ADD COLUMN prev_hash TEXT",
    "ALTER TABLE guardrail_violations ADD COLUMN prev_hash TEXT",
    "ALTER TABLE llm_calls ADD COLUMN prev_hash TEXT",
    "ALTER TABLE bus_conversations ADD COLUMN prev_hash TEXT",
]

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
        current_version = 4

    if current_version < 5:
        # V5: Dashboard event persistence for time-travel replay
        await db.executescript(V5_TABLES)
        await db.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (5,),
        )
        await db.commit()
        current_version = 5

    if current_version < 6:  # noqa: PLR1702
        # V6: Document Intelligence Persistence (Phase 2.5)
        # 11 tables: projects, documents, document_links, review_scores,
        # pipeline_events, approval_decisions, guardrail_violations,
        # grounding_log, llm_calls, agent_config_snapshots, bus_conversations
        await db.executescript(V6_TABLES)
        await db.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (6,),
        )
        await db.commit()
        current_version = 6

    if current_version < 7:
        # V7: Guardrail Approval Gate (human-in-the-loop)
        await db.executescript(V7_TABLES)
        await db.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (7,),
        )
        await db.commit()
        current_version = 7

    if current_version < 8:
        # V8: Authentication + tamper-evident hash chains
        await db.executescript(V8_TABLES)
        # Add prev_hash columns to existing audit tables (nullable — safe for existing rows)
        for alter_sql in V8_HASH_CHAIN_COLUMNS:
            try:
                await db.execute(alter_sql)
            except Exception:
                pass  # Column may already exist from a partial migration
        # Seed default admin user: shawn / ncms (bcrypt hashed)
        import bcrypt as _bcrypt
        from datetime import UTC, datetime
        _hash = _bcrypt.hashpw(b"ncms", _bcrypt.gensalt()).decode()
        _now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT OR IGNORE INTO users (id, username, password_hash, display_name, role, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("usr-admin-001", "shawn", _hash, "Shawn", "admin", _now),
        )
        await db.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (8,),
        )
        await db.commit()
