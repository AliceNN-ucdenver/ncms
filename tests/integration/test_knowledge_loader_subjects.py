"""Phase A — KnowledgeLoader subject coverage (claim A.12).

Verifies that the ``ncms.application.knowledge_loader.KnowledgeLoader``
ingest path:

1. Always produces ``structured["subjects"]`` on every memory it
   stores (even when no subject info is provided — the bake step
   in ``store_memory`` ensures the key is present, defaulting to
   ``[]``).
2. Threads a caller-provided ``subjects=[Subject(...)]`` through
   to every chunk, so importing an ADR with a known timeline
   anchor doesn't silently lose the subject.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ncms.application.knowledge_loader import KnowledgeLoader
from ncms.application.memory_service import MemoryService
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
def svc(store, index, config) -> MemoryService:
    return MemoryService(
        store=store,
        index=index,
        graph=NetworkXGraph(),
        config=config,
    )


@pytest.fixture
def adr_file(tmp_path: Path) -> Path:
    """A minimal ADR-shaped markdown file with two paragraphs."""
    f = tmp_path / "adr-004-pick-postgres.md"
    f.write_text(
        "# ADR-004: Pick Postgres\n\n"
        "Status: accepted\n\n"
        "We pick Postgres over MySQL for the new auth-service.\n\n"
        "Trade-off: heavier ops cost; better RLS support.\n",
    )
    return f


# ---------------------------------------------------------------------------
# A.12 — every loaded memory carries the subjects payload
# ---------------------------------------------------------------------------


async def test_loader_bakes_subjects_key_with_no_caller_input(
    svc: MemoryService,
    store: SQLiteStore,
    adr_file: Path,
) -> None:
    """No subjects= passed → key still present (empty or SLM-derived)."""
    loader = KnowledgeLoader(svc)
    stats = await loader.load_file(adr_file)
    assert stats.memories_created >= 1

    # All memories from this load have structured["subjects"].
    memories = await store.list_memories(limit=20)
    loaded = [m for m in memories if any(t.startswith("source:") for t in m.tags)]
    assert loaded, "expected at least one loaded memory"
    for m in loaded:
        assert m.structured is not None
        assert "subjects" in m.structured
        assert isinstance(m.structured["subjects"], list)


async def test_loader_canonicalizes_subjects(
    svc: MemoryService,
    store: SQLiteStore,
    adr_file: Path,
) -> None:
    """A.12 headline: caller-supplied subjects= reach every chunk."""
    loader = KnowledgeLoader(svc)
    explicit = [
        Subject(
            id="decision:adr-004",
            type="decision",
            primary=True,
            aliases=("ADR-004", "adr-004"),
            source="caller",
        ),
    ]
    stats = await loader.load_file(adr_file, subjects=explicit)
    assert stats.memories_created >= 1

    memories = await store.list_memories(limit=20)
    loaded = [m for m in memories if any(t.startswith("source:") for t in m.tags)]
    assert loaded, "expected at least one loaded memory"

    # Every chunk's subjects payload pins the same canonical id.
    for m in loaded:
        payload = m.structured["subjects"]
        assert len(payload) == 1, f"expected 1 subject, got {payload!r}"
        assert payload[0]["id"] == "decision:adr-004"
        assert payload[0]["primary"] is True
        assert payload[0]["source"] == "caller"
