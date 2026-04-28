"""Phase A — store_memory(subjects=…) end-to-end integration.

Covers claims A.2 (payload baked into structured), A.3 (precedence
+ ValueError on conflicting primaries), A.8 (inline path writes
canonical subjects).

Multi-subject L2 emission lands in sub-PR 4 — these tests only
verify the payload bake + entity-link, not L2 nodes.
"""

from __future__ import annotations

import pytest

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.domain.models import Subject
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest.fixture
def config() -> NCMSConfig:
    # No index pool started → store_memory falls through to the
    # inline path, which is what these tests exercise.
    return NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
    )


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
# A.2 — payload baked into structured["subjects"]
# ---------------------------------------------------------------------------


async def test_subjects_persisted_after_ingest(svc: MemoryService) -> None:
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
        content="Auth-service: rolled to v2.3",
        domains=["software_dev"],
        subjects=explicit,
    )
    assert mem.structured is not None
    assert "subjects" in mem.structured
    assert len(mem.structured["subjects"]) == 1
    payload = mem.structured["subjects"][0]
    assert payload["id"] == "service:auth-api"
    assert payload["type"] == "service"
    assert payload["primary"] is True
    assert payload["source"] == "caller"


async def test_no_subjects_yields_empty_list(svc: MemoryService) -> None:
    """When no subject info is provided, the key is set to [] not omitted."""
    mem = await svc.store_memory(
        content="Random observation",
        domains=["software_dev"],
    )
    assert mem.structured is not None
    assert mem.structured["subjects"] == []


async def test_payload_survives_sqlite_round_trip(
    svc: MemoryService,
    store: SQLiteStore,
) -> None:
    """A.11 prerequisite: structured["subjects"] survives store + reload."""
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
        content="Auth-service: rolled to v2.3",
        subjects=explicit,
    )
    reloaded = await store.get_memory(mem.id)
    assert reloaded is not None
    assert reloaded.structured is not None
    assert reloaded.structured["subjects"] == mem.structured["subjects"]


# ---------------------------------------------------------------------------
# A.3 — precedence rules + ValueError
# ---------------------------------------------------------------------------


async def test_legacy_subject_string_promoted_to_list(
    svc: MemoryService,
) -> None:
    mem = await svc.store_memory(
        content="ADR-004 selected Postgres",
        subject="adr:004",
    )
    payload = mem.structured["subjects"]
    assert len(payload) == 1
    assert payload[0]["primary"] is True
    assert payload[0]["source"] == "caller"
    # Legacy string canonicalization defaults type to "subject".
    assert payload[0]["id"] == "subject:adr-004"


async def test_subjects_list_takes_precedence_over_subject_string(
    svc: MemoryService,
) -> None:
    """When both kwargs agree, subjects= wins silently (no raise).

    Caller is being redundant — passing the same canonical id
    via both shapes.  This is fine; the resolver dedupes.  Only
    *disagreement* raises (see test_subject_string_conflicts_*).
    """
    # Both reference the same canonical id.  The legacy string
    # "auth-api" + type_hint="service" canonicalizes to
    # "service:auth-api" — same as the explicit Subject.id.
    explicit = [
        Subject(
            id="service:auth-api",
            type="service",
            primary=True,
            source="caller",
        ),
    ]
    mem = await svc.store_memory(
        content="x",
        subject="auth-api",
        subjects=explicit,
    )
    payload = mem.structured["subjects"]
    assert len(payload) == 1
    assert payload[0]["id"] == "service:auth-api"


async def test_conflicting_primaries_raises(svc: MemoryService) -> None:
    """A.3 within-list conflict: multiple primary=True → ValueError."""
    explicit = [
        Subject(id="adr:004", type="decision", primary=True),
        Subject(id="adr:002", type="decision", primary=True),
    ]
    with pytest.raises(ValueError, match="primary"):
        await svc.store_memory(
            content="ADR-004 supersedes ADR-002",
            subjects=explicit,
        )


async def test_subject_string_conflicts_with_subjects_list_raises(
    svc: MemoryService,
) -> None:
    """A.3 cross-kwarg conflict: subject="x" + subjects=[Subject(id="y", primary=True)]
    where x ≠ y as canonical ids → ValueError.

    Caller is asserting two different primary timelines via two
    different shapes.  Subjects-wins-silently would hide the
    inconsistency.  Raise so the bug surfaces immediately.
    """
    explicit = [
        Subject(
            id="service:auth-api",
            type="service",
            primary=True,
            source="caller",
        ),
    ]
    with pytest.raises(ValueError, match="Conflicting primary"):
        await svc.store_memory(
            content="x",
            subject="payments-service",  # would canonicalize to "service:payments-service"
            subjects=explicit,
        )


# ---------------------------------------------------------------------------
# A.8 — inline path canonicalizes (subjects appear in the entity graph)
# ---------------------------------------------------------------------------


async def test_inline_path_canonicalizes(
    svc: MemoryService,
    store: SQLiteStore,
) -> None:
    """A.8: inline path writes canonical subjects to structured.

    "Canonical" here means: the resolved ``Subject.id`` (canonical
    id, e.g. ``service:auth-api``) lands in
    ``memory.structured["subjects"]`` — not just the raw caller
    surface (``auth-service``).  Entity-graph linking with canonical
    ids is sub-PR 4's scope (multi-subject L2 emission) and is
    deferred so the inline / async parity fitness test stays green
    while only one path is changed at a time.
    """
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
        content="Auth-service: rolled to v2.3",
        subjects=explicit,
    )
    payload = mem.structured["subjects"]
    assert len(payload) == 1
    assert payload[0]["id"] == "service:auth-api"
    # And the canonical id survives a SQLite round-trip.
    reloaded = await store.get_memory(mem.id)
    assert reloaded is not None
    assert reloaded.structured["subjects"][0]["id"] == "service:auth-api"
