"""Phase A — SLM auto-suggest end-to-end integration (claim A.17).

The unit-level coverage in
``tests/unit/application/test_subject_resolver.py::TestPrecedenceSLMAutoSuggest``
exercises ``resolve_subjects`` directly with fake SLM labels.
This file is the integration counterpart that the claim doc names:
ingest a Memory through the full ``store_memory`` pipeline with
an intent_slot extractor wired in, and assert the persisted
``structured["subjects"]`` carries SLM-derived subjects when the
caller didn't pin any.

Test scenarios mirror the four named tests in claim A.17:
* ``test_primary_role_span_becomes_subject``
* ``test_caller_subjects_override_slm_spans``
* ``test_no_slm_chain_no_autosuggest``
* ``test_low_confidence_span_skipped``
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from ncms.application.memory_service import MemoryService
from ncms.config import NCMSConfig
from ncms.domain.models import ExtractedLabel, Subject
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@dataclass
class _SLMReturning:
    """Stand-in intent-slot extractor returning a fixed label."""

    label: ExtractedLabel = field(
        default_factory=lambda: ExtractedLabel(
            intent="none",
            intent_confidence=0.92,
            slots={"service": "auth-service"},
            slot_confidences={"service": 0.92},
            topic="other",
            topic_confidence=0.90,
            admission="persist",
            admission_confidence=0.95,
            state_change="declaration",
            state_change_confidence=0.90,
            role_spans=[
                {
                    "char_start": 0,
                    "char_end": 12,
                    "surface": "auth-service",
                    "canonical": "auth-service",
                    "slot": "service",
                    "role": "primary",
                    "source": "test",
                },
            ],
            method="autosuggest_test_extractor",
        ),
    )

    def extract(self, text: str, *, domain: str) -> ExtractedLabel:
        return self.label


def _config() -> NCMSConfig:
    return NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
        admission_enabled=False,
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


# ---------------------------------------------------------------------------
# A.17 verifies (the four scenarios named in the claim doc)
# ---------------------------------------------------------------------------


async def test_primary_role_span_becomes_subject(
    store: SQLiteStore,
    index: TantivyEngine,
    graph: NetworkXGraph,
) -> None:
    """A.17 headline: SLM ``primary`` span auto-becomes the memory's subject.

    Caller passes neither ``subject=`` nor ``subjects=``.  The SLM
    extractor's ``role_spans`` includes one ``role="primary"``
    entry on slot ``service``.  After ingest, the persisted
    memory's ``structured["subjects"]`` must contain a single
    Subject with ``id="service:auth-service"``,
    ``source="slm_role"``, ``primary=True``.
    """
    svc = MemoryService(
        store=store,
        index=index,
        graph=graph,
        config=_config(),
        intent_slot=_SLMReturning(),
    )
    mem = await svc.store_memory(
        content="auth-service rolled to v2.3",
        domains=["software_dev"],
    )
    payload = (mem.structured or {}).get("subjects") or []
    assert len(payload) == 1
    assert payload[0]["id"] == "service:auth-service"
    assert payload[0]["source"] == "slm_role"
    assert payload[0]["primary"] is True


async def test_caller_subjects_override_slm_spans(
    store: SQLiteStore,
    index: TantivyEngine,
    graph: NetworkXGraph,
) -> None:
    """A.17 precedence: caller ``subjects=`` overrides SLM auto-suggest.

    SLM would auto-suggest ``service:auth-service`` from its
    ``primary`` role span, but the caller passes
    ``subjects=[Subject(id="service:payments-api", primary=True)]``.
    The persisted payload must contain ONLY the caller-supplied
    subject — the SLM span is ignored entirely.
    """
    svc = MemoryService(
        store=store,
        index=index,
        graph=graph,
        config=_config(),
        intent_slot=_SLMReturning(),
    )
    explicit = [
        Subject(
            id="service:payments-api",
            type="service",
            primary=True,
            source="caller",
        ),
    ]
    mem = await svc.store_memory(
        content="auth-service rolled to v2.3",
        domains=["software_dev"],
        subjects=explicit,
    )
    payload = mem.structured["subjects"]
    assert len(payload) == 1
    assert payload[0]["id"] == "service:payments-api"
    assert payload[0]["source"] == "caller"
    # Confirm the SLM span did not sneak in as a co-subject.
    assert all(s["id"] != "service:auth-service" for s in payload)


async def test_no_slm_chain_no_autosuggest(
    store: SQLiteStore,
    index: TantivyEngine,
    graph: NetworkXGraph,
) -> None:
    """A.17: when no SLM chain is wired, auto-suggest is dark.

    Build ``MemoryService`` without ``intent_slot=…``.  The
    heuristic null-output chain runs (intent_slot_label is the
    no-op fallback with empty role_spans), so no subjects can be
    auto-derived.  The persisted payload is ``[]``.
    """
    svc = MemoryService(
        store=store,
        index=index,
        graph=graph,
        config=_config(),
        # No intent_slot kwarg — chain is dark.
    )
    mem = await svc.store_memory(
        content="auth-service rolled to v2.3",
        domains=["software_dev"],
    )
    payload = (mem.structured or {}).get("subjects")
    assert payload == [], (
        "no SLM chain → empty subjects expected; "
        f"got {payload!r}"
    )


async def test_low_confidence_span_skipped(
    store: SQLiteStore,
    index: TantivyEngine,
    graph: NetworkXGraph,
) -> None:
    """A.17: extraction below ``slm_confidence_threshold`` doesn't auto-suggest.

    Same SLM mock shape as ``test_primary_role_span_becomes_subject``
    but with ``intent_confidence`` below the threshold.  The
    resolver's ``is_confident()`` gate short-circuits the
    auto-suggest, so no subjects derive.
    """
    quiet_label = ExtractedLabel(
        intent="none",
        intent_confidence=0.05,  # below default 0.3 threshold
        state_change="declaration",
        state_change_confidence=0.05,
        role_spans=[
            {
                "char_start": 0,
                "char_end": 12,
                "surface": "auth-service",
                "canonical": "auth-service",
                "slot": "service",
                "role": "primary",
                "source": "test",
            },
        ],
        method="low_confidence_test",
    )
    svc = MemoryService(
        store=store,
        index=index,
        graph=graph,
        config=_config(),
        intent_slot=_SLMReturning(label=quiet_label),
    )
    mem = await svc.store_memory(
        content="auth-service rolled to v2.3",
        domains=["software_dev"],
    )
    payload = (mem.structured or {}).get("subjects")
    assert payload == [], (
        "low-confidence SLM → empty subjects expected; "
        f"got {payload!r}"
    )
