"""Phase A — document publish writes canonical subjects (claim A.10).

A.10 is a coverage claim.  Two paths produce documents in NCMS:

1. ``DocumentService.publish_document(...)`` — writes only to
   the ``documents`` table.  Per the claim doc: "the profile
   Memory carries the subject; the Document row does not."  This
   path does not create a Memory and is therefore NOT in scope
   for the subject payload.
2. ``MemoryService.store_memory(content=big_doc)`` with
   ``content_classification_enabled=True`` — the content
   classification gate routes long, structured content to
   ``SectionService.ingest_navigable``, which builds a profile
   memory and persists doc + sections in the document store.
   The profile memory DOES go through ``store_memory`` (with
   classification disabled to prevent recursion), so the Phase
   A bake fires and ``structured["subjects"]`` lands.

This file covers path 2.  Path 1 has nothing to assert about
subjects because no Memory is produced.
"""

from __future__ import annotations

import pytest

from ncms.application.document_service import DocumentService
from ncms.application.memory_service import MemoryService
from ncms.application.section_service import SectionService
from ncms.config import NCMSConfig
from ncms.domain.models import Subject
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.document_store import SQLiteDocumentStore
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest.fixture
def config() -> NCMSConfig:
    return NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
        content_classification_enabled=True,
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
def doc_service(store) -> DocumentService:
    # DocumentService needs an SQLiteDocumentStore over the same
    # connection so the documents/document_links tables (created
    # by SQLiteStore.initialize) are visible.
    doc_store = SQLiteDocumentStore(db=store.db)
    return DocumentService(store=doc_store)


@pytest.fixture
def svc(store, index, doc_service, config) -> MemoryService:
    section_svc = SectionService(
        memory_service=None,  # set below to avoid the chicken/egg
        document_service=doc_service,
        config=config,
    )
    ms = MemoryService(
        store=store,
        index=index,
        graph=NetworkXGraph(),
        config=config,
        section_service=section_svc,
        document_service=doc_service,
    )
    section_svc._memory_service = ms  # noqa: SLF001 — wire after construction
    return ms


# ---------------------------------------------------------------------------
# A.10 — document profile memory carries subjects payload
# ---------------------------------------------------------------------------


_NAVIGABLE_DOC = """
# ADR-004: Pick Postgres

## Status
Accepted

## Decision
We pick Postgres over MySQL for the new auth-service.

## Trade-offs
- Heavier ops cost
- Better RLS support
- More mature backup tooling

## Consequences
The auth-service now requires a Postgres-compatible host.
"""


async def test_document_profile_carries_subjects_payload(
    svc: MemoryService,
    store: SQLiteStore,
) -> None:
    """A.10 minimum: doc profile memory has structured["subjects"] key.

    When a long, structured doc routes through the content
    classification gate, the profile memory created by
    ``SectionService.ingest_navigable`` goes through
    ``store_memory`` and therefore gets the bake step.  The
    payload is empty (no caller-provided subjects, SLM dark) but
    the key is present.
    """
    mem = await svc.store_memory(
        content=_NAVIGABLE_DOC,
        memory_type="document_profile",
        domains=["software_dev"],
    )
    assert mem.structured is not None
    assert "subjects" in mem.structured
    assert isinstance(mem.structured["subjects"], list)


async def test_document_profile_canonicalizes_caller_subjects(
    svc: MemoryService,
    store: SQLiteStore,
) -> None:
    """A.10 headline: caller-supplied subjects reach the profile memory."""
    explicit = [
        Subject(
            id="decision:adr-004",
            type="decision",
            primary=True,
            aliases=("ADR-004",),
            source="caller",
        ),
    ]
    mem = await svc.store_memory(
        content=_NAVIGABLE_DOC,
        memory_type="document_profile",
        domains=["software_dev"],
        subjects=explicit,
    )
    payload = mem.structured["subjects"]
    assert len(payload) == 1
    assert payload[0]["id"] == "decision:adr-004"
    assert payload[0]["primary"] is True
    assert payload[0]["source"] == "caller"


async def test_document_publish_does_not_create_memory_with_subjects(
    doc_service: DocumentService,
    store: SQLiteStore,
) -> None:
    """A.10 negative: ``publish_document`` writes ONLY a Document row.

    Per the claim doc: "the profile Memory carries the subject;
    the Document row does not."  ``DocumentService.publish_document``
    creates the row in the ``documents`` table and does NOT
    create a Memory.  This test pins that contract.
    """
    doc = await doc_service.publish_document(
        title="ADR-004",
        content="We pick Postgres.",
        from_agent="test",
    )
    assert doc.id is not None

    # No memory row was created with this content's hash.
    memories = await store.list_memories(limit=10)
    assert all("ADR-004" not in (m.content or "") for m in memories), (
        "publish_document unexpectedly created a memory row"
    )
