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


_PARENT_NAVIGABLE_DOC = """
# ADR-002: Pick MySQL

## Status
Accepted on 2026-04-15 by the auth-service squad.  Committee
review concluded that the operational maturity of MySQL on our
existing replication tooling outweighed the schema-flexibility
advantages of Postgres for this particular use case.

## Decision
We chose MySQL 8 for the auth-service backing store.  The
service handles login state, refresh tokens, and session
metadata with a row-volume on the order of 50M rows and a
read-heavy traffic pattern.  MySQL's mature replication tooling
and our team's existing operational expertise tipped the scale.

## Trade-offs
- Mature semi-synchronous replication out of the box
- Operational familiarity across the SRE team
- Less rich JSON support than Postgres (acceptable: token
  payloads are flat key/value)
- Slower DDL on large tables; we have a maintenance window
  policy that absorbs this

## Consequences
- Capacity planning needs to account for binlog disk growth.
- Observability dashboards remain on the existing MySQL
  exporters; no new tooling work required.
"""


_CHILD_NAVIGABLE_DOC = """
# ADR-002 amendment: read-replica policy

## Status
Proposed on 2026-04-22 as a follow-on to ADR-002.  The
auth-service's read latency at the 99th percentile started
drifting upward in two regions; this amendment proposes adding
cross-region read replicas to absorb the long-tail load
without changing the primary store choice.

## Discussion
Add two cross-region read replicas (eu-west-1 and us-east-1)
to the auth-service backing store.  Replication lag on the
existing semi-sync setup is sub-second under normal traffic;
under the synthetic loadgen it stayed below 5 seconds even at
3x peak.

## Trade-offs
- Higher infra cost (~12% of the auth-service's monthly bill)
- Lower read latency p99 — projected 40ms reduction across
  the two affected regions based on the loadgen runs
- Replication-lag handling required for token rotation reads;
  see the proposed read-after-write fallback design

## Consequences
- New runbook entry for replica failover.
- Observability needs replication-lag SLO panels; existing
  exporters expose the metric, no new tooling.
"""


async def test_child_document_inherits_parent_primary_subject(
    svc: MemoryService,
    doc_service: DocumentService,
    store: SQLiteStore,
) -> None:
    """A.10 headline: parent_doc_id inherits via the real section-service flow.

    No synthetic fixtures.  Both parent and child go through the
    real ``store_memory`` → content_classification → section_service
    pipeline:

    1. Parent: ``store_memory(content=parent_doc_md, subjects=[X])``
       — section_service publishes Document A with subjects baked
       into A's profile memory's ``structured["subjects"]``, and
       stamps ``structured["doc_id"]`` so the inheritance lookup
       can find the profile.
    2. Child: ``store_memory(content=child_doc_md, parent_doc_id=A.id)``
       with no subjects / SLM signal — the bake step's parent-doc
       inheritance fires: looks up A's profile by ``doc_id``, reads
       its primary subject, returns it tagged ``source="document"``.

    The child profile memory's ``structured["subjects"][0]`` is
    the inherited subject with ``source="document"``.
    """
    pinned = Subject(
        id="decision:adr-002",
        type="decision",
        primary=True,
        aliases=("ADR-002",),
        source="caller",
    )
    parent_mem = await svc.store_memory(
        content=_PARENT_NAVIGABLE_DOC,
        memory_type="document_profile",
        subjects=[pinned],
    )
    parent_payload = (parent_mem.structured or {}).get("subjects") or []
    assert parent_payload, "parent profile memory missing subjects"
    parent_doc_id = (parent_mem.structured or {}).get("doc_id")
    assert parent_doc_id, "parent profile memory must carry doc_id"

    # Child published as a navigable doc with parent_doc_id pointing
    # at the parent.  No caller subjects, no SLM signal.
    child_mem = await svc.store_memory(
        content=_CHILD_NAVIGABLE_DOC,
        memory_type="document_profile",
        parent_doc_id=parent_doc_id,
    )
    child_payload = (child_mem.structured or {}).get("subjects") or []
    assert len(child_payload) == 1, (
        f"expected 1 inherited subject, got {child_payload!r}"
    )
    assert child_payload[0]["id"] == "decision:adr-002"
    assert child_payload[0]["primary"] is True
    assert child_payload[0]["source"] == "document"


async def test_parent_doc_inheritance_no_op_when_caller_overrides(
    svc: MemoryService,
    doc_service: DocumentService,
    store: SQLiteStore,
) -> None:
    """A.10: caller-provided subjects= takes precedence over inheritance.

    Inheritance is the LOWEST precedence tier (after caller >
    legacy-string > SLM auto-suggest).  Real-flow setup: both
    parent + child go through the navigable pipeline; child also
    pins ``subjects=[…]`` explicitly.  Caller wins.
    """
    parent_mem = await svc.store_memory(
        content=_PARENT_NAVIGABLE_DOC,
        memory_type="document_profile",
        subjects=[
            Subject(
                id="decision:adr-002",
                type="decision",
                primary=True,
                source="caller",
            ),
        ],
    )
    parent_doc_id = (parent_mem.structured or {}).get("doc_id")
    assert parent_doc_id

    child_mem = await svc.store_memory(
        content=_CHILD_NAVIGABLE_DOC,
        memory_type="document_profile",
        parent_doc_id=parent_doc_id,
        subjects=[
            Subject(
                id="service:auth-api",
                type="service",
                primary=True,
                source="caller",
            ),
        ],
    )
    payload = child_mem.structured["subjects"]
    assert payload[0]["id"] == "service:auth-api"
    assert payload[0]["source"] == "caller"  # NOT "document"


async def test_parent_doc_inheritance_silent_no_op_on_missing_parent(
    svc: MemoryService,
) -> None:
    """A.10: an unknown parent_doc_id is a silent no-op (no raise).

    The lookup helper returns None when the document doesn't
    exist or has no profile memory.  The bake step then proceeds
    with empty subjects.  Failing-noisily on a missing parent
    would break callers that pass an opportunistic parent_doc_id.
    """
    child_mem = await svc.store_memory(
        content=_CHILD_NAVIGABLE_DOC,
        memory_type="document_profile",
        parent_doc_id="doc-does-not-exist",
    )
    payload = (child_mem.structured or {}).get("subjects") or []
    # No inheritance possible → empty list.
    assert payload == []


# ---------------------------------------------------------------------------
# A.10 — HTTP doc-publish path inheritance (codex round-on-uncommitted-tree)
# ---------------------------------------------------------------------------


async def test_http_publish_document_path_inherits_parent_subject(
    svc: MemoryService,
    doc_service: DocumentService,
    store: SQLiteStore,
) -> None:
    """A.10 / HTTP: child published via publish_document inherits.

    Reproduces the call sequence at ``interfaces/http/api.py``
    (the document-publish handler):

    1. ``doc_svc.publish_document(parent=None, ...)`` creates the
       parent Document.
    2. ``section_svc.ingest_navigable(structured={"source_doc_id": parent.id})``
       creates the parent's profile memory (carrying caller subjects).
    3. ``doc_svc.publish_document(parent_doc_id=parent.id, ...)`` creates
       the child Document.
    4. ``section_svc.ingest_navigable(structured={"source_doc_id": child.id},
       parent_doc_id=parent.id)`` creates the child's profile memory.
       The bake step's parent-doc inheritance must fire here so the
       child's structured["subjects"] carries the parent's primary.

    The fix codex round-1 caught: step 4 must thread
    ``parent_doc_id``.  This test pins that contract.
    """
    from ncms.domain.content_classifier import (
        ContentClass,
        classify_content,
        extract_sections,
    )

    pinned = Subject(
        id="decision:adr-002",
        type="decision",
        primary=True,
        aliases=("ADR-002",),
        source="caller",
    )

    # ── Parent ─────────────────────────────────────────────────────
    parent_doc = await doc_service.publish_document(
        title="ADR-002 parent",
        content=_PARENT_NAVIGABLE_DOC,
        from_agent="test",
        doc_type="markdown",
    )
    parent_classif = classify_content(_PARENT_NAVIGABLE_DOC, "document")
    parent_sections = extract_sections(_PARENT_NAVIGABLE_DOC, parent_classif)
    assert parent_classif.content_class == ContentClass.NAVIGABLE
    assert len(parent_sections) >= 2
    await svc._section_svc.ingest_navigable(  # noqa: SLF001
        content=_PARENT_NAVIGABLE_DOC,
        classification=parent_classif,
        sections=parent_sections,
        memory_type="document_profile",
        importance=7.0,
        tags=["document", parent_doc.id],
        structured={"source_doc_id": parent_doc.id},
        source="test",
        agent_id="test",
        domains=["software_dev"],
        subjects=[pinned],
    )

    # ── Child published with parent_doc_id ─────────────────────────
    child_doc = await doc_service.publish_document(
        title="ADR-002 amendment",
        content=_CHILD_NAVIGABLE_DOC,
        from_agent="test",
        doc_type="markdown",
        parent_doc_id=parent_doc.id,
    )
    child_classif = classify_content(_CHILD_NAVIGABLE_DOC, "document")
    child_sections = extract_sections(_CHILD_NAVIGABLE_DOC, child_classif)
    assert child_classif.content_class == ContentClass.NAVIGABLE
    assert len(child_sections) >= 2
    await svc._section_svc.ingest_navigable(  # noqa: SLF001
        content=_CHILD_NAVIGABLE_DOC,
        classification=child_classif,
        sections=child_sections,
        memory_type="document_profile",
        importance=7.0,
        tags=["document", child_doc.id],
        structured={"source_doc_id": child_doc.id},
        source="test",
        agent_id="test",
        domains=["software_dev"],
        # Phase A claim A.10: parent_doc_id MUST flow through the
        # HTTP path, otherwise the child's profile memory inherits
        # nothing.
        parent_doc_id=parent_doc.id,
    )

    # ── Verify child profile memory inherited the parent's primary ─
    child_profile = await store.find_memory_by_doc_id(child_doc.id)
    assert child_profile is not None, "child profile memory not found"
    payload = (child_profile.structured or {}).get("subjects") or []
    assert len(payload) == 1, f"expected 1 inherited subject, got {payload!r}"
    assert payload[0]["id"] == "decision:adr-002"
    assert payload[0]["source"] == "document"
