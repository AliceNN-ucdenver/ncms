"""Phase A — MSEB backend uses the new subjects= API (A.13).

The MSEB harness contract takes a per-memory ``subject`` string;
the NCMS backend used to pass it through ``store_memory(subject=)``
(legacy single-subject path).  Phase A wraps it in a
:class:`Subject` and passes ``subjects=[Subject(...)]`` so the
SubjectRegistry registers the alias and downstream graph
consumers see a stable canonical id.

These tests exercise the MSEB backend's ``ingest()`` directly
(without spinning up the full harness) and confirm:

1. The persisted memory carries ``structured["subjects"]`` with
   the MSEB subject as the canonical id.
2. The ``subject_map`` derived from canonical ids has zero alias
   splits — every CorpusMemory with ``subject="X"`` resolves to
   the same canonical id ``"X"`` (no per-row drift from the
   registry minting different ids on each call).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def feature_set():
    """Minimal FeatureSet: temporal off, SLM off (heuristic chain)."""
    from benchmarks.mseb.harness import FeatureSet

    return FeatureSet(temporal=False, slm=False)


@pytest.fixture
async def backend(feature_set):
    """A configured NcmsBackend with an isolated in-memory DB.

    SPLADE is force-enabled by ``NcmsBackend.setup()``; we override
    via ``ncms_config_overrides`` so the test doesn't load the
    SPLADE model (which would make this run for minutes).  The
    canonicalization claim doesn't depend on dense retrieval.
    """
    from benchmarks.mseb.backends.ncms_backend import NcmsBackend

    b = NcmsBackend(
        feature_set=feature_set,
        ncms_config_overrides={
            "splade_enabled": False,
            "scoring_weight_splade": 0.0,
        },
    )
    await b.setup()
    yield b
    # NcmsBackend doesn't expose teardown — drain the index pool
    # and close the store directly.
    if b._svc is not None:
        await b._svc.stop_index_pool()
        await b._svc.store.close()


def _make_corpus_memory(*, mid: str, subject: str, content: str):
    """Build a minimal CorpusMemory for ingest()."""
    from benchmarks.mseb.schema import CorpusMemory

    return CorpusMemory(
        mid=mid,
        content=content,
        subject=subject,
        observed_at="2026-04-27T00:00:00Z",
        metadata={"source_agent": "mseb-test", "domains": ["software_dev"]},
    )


async def test_mseb_subject_baked_into_structured(backend) -> None:
    """The persisted memory's structured["subjects"] carries the MSEB subject."""
    memories = [
        _make_corpus_memory(
            mid="m1",
            subject="auth-service",
            content="auth-service rolled to v2.3",
        ),
    ]
    await backend.ingest(memories)

    # Read back the persisted memory.
    svc = backend._svc
    assert svc is not None
    persisted = await svc.store.list_memories(limit=10)
    rows = [m for m in persisted if "mid:m1" in m.tags]
    assert len(rows) == 1
    structured = rows[0].structured or {}
    subjects = structured.get("subjects") or []
    assert len(subjects) == 1
    assert subjects[0]["id"] == "auth-service"
    assert subjects[0]["primary"] is True
    assert subjects[0]["source"] == "caller"


async def test_mseb_no_alias_split_on_repeated_subject(backend) -> None:
    """Multiple memories with the same MSEB subject share one canonical id.

    A.13 verify: ``subject_map derived from canonical ids has zero
    alias splits``.  When the same surface (``"auth-service"``) is
    asserted across rows, every row's structured["subjects"][0]["id"]
    must equal ``"auth-service"`` — the SubjectRegistry's
    INSERT-OR-IGNORE keeps the canonical id stable.
    """
    memories = [
        _make_corpus_memory(
            mid=f"m{i}",
            subject="auth-service",
            content=f"observation {i} about auth-service",
        )
        for i in range(5)
    ]
    await backend.ingest(memories)

    svc = backend._svc
    assert svc is not None
    persisted = await svc.store.list_memories(limit=20)
    rows = [m for m in persisted if any(t.startswith("mid:m") for t in m.tags)]
    assert len(rows) == 5
    canonical_ids = {
        ((m.structured or {}).get("subjects") or [{}])[0].get("id")
        for m in rows
    }
    assert canonical_ids == {"auth-service"}


async def test_mseb_distinct_subjects_distinct_canonical_ids(
    backend,
) -> None:
    """Different subjects produce different canonical ids."""
    memories = [
        _make_corpus_memory(
            mid="m1",
            subject="auth-service",
            content="auth observation",
        ),
        _make_corpus_memory(
            mid="m2",
            subject="payments-service",
            content="payments observation",
        ),
    ]
    await backend.ingest(memories)

    svc = backend._svc
    assert svc is not None
    persisted = await svc.store.list_memories(limit=10)
    rows = sorted(
        (m for m in persisted if any(t.startswith("mid:m") for t in m.tags)),
        key=lambda m: m.tags,
    )
    assert len(rows) == 2
    ids = [
        ((r.structured or {}).get("subjects") or [{}])[0].get("id")
        for r in rows
    ]
    assert sorted(ids) == ["auth-service", "payments-service"]
