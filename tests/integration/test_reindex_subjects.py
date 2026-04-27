"""Phase A — reindex preserves canonical subjects (claim A.11).

Phase A scope decision: we target a fresh DB.  Memories that were
stored *under* the new shape carry ``structured["subjects"]`` and
that payload must survive the reindex pass — re-running the
indexers (BM25, SPLADE, GLiNER) over already-persisted memories
should not strip or modify the structured field.

We do NOT test back-fill on legacy memories — that's explicitly
out of scope per the revised A.11 (caller imports fresh).
"""

from __future__ import annotations

import pytest

from ncms.application.memory_service import MemoryService
from ncms.application.reindex_service import ReindexService
from ncms.config import NCMSConfig
from ncms.domain.models import Subject
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest.fixture
def config() -> NCMSConfig:
    return NCMSConfig(db_path=":memory:", actr_noise=0.0)


@pytest.fixture
async def store():
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def index() -> TantivyEngine:
    e = TantivyEngine()
    e.initialize()
    return e


@pytest.fixture
def graph() -> NetworkXGraph:
    return NetworkXGraph()


@pytest.fixture
def svc(store, index, graph, config) -> MemoryService:
    return MemoryService(store=store, index=index, graph=graph, config=config)


# ---------------------------------------------------------------------------
# A.11 — reindex preserves the payload (no strip, no mutation)
# ---------------------------------------------------------------------------


async def test_reindex_preserves_subject_payload(
    svc: MemoryService,
    store: SQLiteStore,
    index: TantivyEngine,
    graph: NetworkXGraph,
    config: NCMSConfig,
) -> None:
    """Store with subjects → reindex → read back → payload identical."""
    explicit = [
        Subject(
            id="service:auth-api",
            type="service",
            primary=True,
            aliases=("auth-service",),
            source="caller",
        ),
    ]
    mem = await svc.store_memory(
        content="auth-service is now on v2.3",
        subjects=explicit,
    )
    pre_payload = (mem.structured or {}).get("subjects")
    assert pre_payload, "fixture invariant: subjects baked at store time"

    # Run a full reindex.  ReindexService rebuilds BM25 + SPLADE +
    # GLiNER + entity edges from the persisted memories.  It must
    # NOT touch the structured field.
    reindexer = ReindexService(
        store=store,
        tantivy=index,
        splade=None,
        graph=graph,
        config=config,
    )
    await reindexer.rebuild_all()

    # Read the memory back from the store and confirm the payload
    # is byte-equivalent to what was written.
    reloaded = await store.get_memory(mem.id)
    assert reloaded is not None
    post_payload = (reloaded.structured or {}).get("subjects")
    assert post_payload == pre_payload, (
        "reindex stripped or mutated structured['subjects']\n"
        f"pre:  {pre_payload!r}\npost: {post_payload!r}"
    )


async def test_reindex_does_not_introduce_subjects_for_legacy_memories(
    svc: MemoryService,
    store: SQLiteStore,
    index: TantivyEngine,
    graph: NetworkXGraph,
    config: NCMSConfig,
) -> None:
    """A.11 negative: reindex doesn't back-fill subjects on legacy rows.

    Phase A is fresh-DB-only.  If a memory was somehow persisted
    without the bake step running (manual SQL insert, downgrade
    artifact), reindex must NOT invent a subjects payload.
    """
    # Manually persist a memory with structured that lacks "subjects".
    import uuid
    from datetime import UTC, datetime

    mem_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    await store.db.execute(
        "INSERT INTO memories "
        "(id, content, structured, type, importance, created_at, updated_at, "
        " content_hash, source_agent, project, domains, tags) "
        "VALUES (?, ?, ?, 'fact', 5.0, ?, ?, NULL, NULL, NULL, '[]', '[]')",
        (
            mem_id,
            "legacy memory without bake",
            '{"intent_slot": null}',  # no "subjects" key
            now,
            now,
        ),
    )
    await store.db.commit()

    reindexer = ReindexService(
        store=store,
        tantivy=index,
        splade=None,
        graph=graph,
        config=config,
    )
    await reindexer.rebuild_all()

    reloaded = await store.get_memory(mem_id)
    assert reloaded is not None
    structured = reloaded.structured or {}
    # The legacy memory MUST NOT have "subjects" auto-added.
    assert "subjects" not in structured, (
        f"reindex back-filled subjects on a legacy row: {structured!r}"
    )
