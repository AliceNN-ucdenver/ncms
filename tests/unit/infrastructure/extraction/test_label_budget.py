"""Unit tests for ``extract_with_label_budget``.

Phase B.1 — split GLiNER calls along the entity/temporal axis when
total label count exceeds the latency threshold.  Rationale in
``docs/p1-experiment-diary.md`` (Phase A ablation entry).

These tests exercise the *routing* of the utility — single-call vs
split-call decisions — without exercising GLiNER itself (which is
covered by tests/unit/infrastructure/extraction/test_gliner_extractor.py).
We monkeypatch ``extract_entities_gliner`` so we can assert how many
times it was called and with which label lists.
"""

from __future__ import annotations

import pytest

from ncms.domain.entity_extraction import TEMPORAL_LABELS
from ncms.infrastructure.extraction import gliner_extractor
from ncms.infrastructure.extraction.gliner_extractor import (
    LABEL_BUDGET_PER_CALL,
    extract_with_label_budget,
)


@pytest.fixture
def spy_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Monkeypatch extract_entities_gliner, return a list of the label
    lists it was called with (order preserved)."""
    calls: list[list[str]] = []

    def fake_extract(
        text: str, *, labels=None, model_name=None,
        threshold=None, cache_dir=None,
    ) -> list[dict[str, object]]:
        calls.append(list(labels or []))
        # Emit one dummy entity per label so callers can dedupe by type.
        return [
            {"name": f"ent_{label}", "type": label,
             "char_start": 0, "char_end": 1}
            for label in (labels or [])
        ]

    monkeypatch.setattr(
        gliner_extractor, "extract_entities_gliner", fake_extract,
    )
    return calls


class TestUnderBudget:

    def test_small_label_list_single_call(self, spy_calls) -> None:
        labels = ["person", "location", "date"]  # 3 labels
        out = extract_with_label_budget("hi", labels)
        assert len(spy_calls) == 1
        assert spy_calls[0] == labels
        assert {e["type"] for e in out} == set(labels)

    def test_exactly_at_budget_single_call(self, spy_calls) -> None:
        labels = [f"l{i}" for i in range(LABEL_BUDGET_PER_CALL)]
        extract_with_label_budget("hi", labels)
        assert len(spy_calls) == 1


class TestOverBudget:

    def test_mixed_entity_and_temporal_splits(self, spy_calls) -> None:
        # 10 entity + 7 temporal = 17 labels, over budget.
        entity_labels = [f"e{i}" for i in range(10)]
        labels = entity_labels + list(TEMPORAL_LABELS)
        out = extract_with_label_budget("hi", labels)
        assert len(spy_calls) == 2
        # First call: entity labels, second call: temporal labels.
        # Order within each call is preserved from the input.
        call_sets = [set(c) for c in spy_calls]
        assert set(entity_labels) in call_sets
        assert set(TEMPORAL_LABELS) in call_sets
        # Output has both kinds.
        out_labels = {e["type"] for e in out}
        assert any(lbl in out_labels for lbl in entity_labels)
        assert any(lbl in out_labels for lbl in TEMPORAL_LABELS)

    def test_all_entity_over_budget_single_call(self, spy_calls) -> None:
        """Degenerate: 12 entity labels, 0 temporal → single combined
        call, splitting would be pointless."""
        labels = [f"e{i}" for i in range(12)]
        extract_with_label_budget("hi", labels)
        assert len(spy_calls) == 1

    def test_all_temporal_over_budget_single_call(self, spy_calls) -> None:
        """Degenerate: everything is temporal (hypothetical — we only
        have 7 real temporal labels, but the branch must handle it)."""
        labels = [f"temporal{i}" for i in range(12)]
        # None of these are in TEMPORAL_LABELS, so they all go to the
        # "entity" side — single call.
        extract_with_label_budget("hi", labels)
        assert len(spy_calls) == 1

    def test_case_insensitive_temporal_classification(
        self, spy_calls,
    ) -> None:
        """TEMPORAL_LABELS classification ignores case on both sides."""
        labels = [f"e{i}" for i in range(10)] + [
            t.upper() for t in TEMPORAL_LABELS[:3]
        ]
        extract_with_label_budget("hi", labels)
        # 13 total → over budget → should split
        assert len(spy_calls) == 2


class TestCustomBudget:

    def test_explicit_budget_override(self, spy_calls) -> None:
        labels = [f"e{i}" for i in range(6)] + ["date", "duration"]
        # 8 total labels, default budget 10 → single call.
        extract_with_label_budget("hi", labels)
        assert len(spy_calls) == 1
        spy_calls.clear()
        # Same labels, budget lowered to 5 → over → split.
        extract_with_label_budget("hi", labels, max_labels_per_call=5)
        assert len(spy_calls) == 2
