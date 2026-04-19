"""Unit tests for ``RetrievalPipeline.apply_ordinal_ordering``.

Phase B.2 primitive — reorder top-K by ``observed_at`` when the query
has classified ordinal intent and at least one subject entity was
extracted.

Two modes:
  * single-subject — sort all subject-linked memories by date
  * multi-subject — one representative per subject, ordered
    chronologically, then remaining subject-linked, then rest

Failure modes tested:
  * no subjects → no-op
  * no subject-linked candidates in top-K → no-op
  * unknown ordinal → no-op
  * empty input → empty output
  * input list must not be mutated
"""

from __future__ import annotations

from datetime import UTC, datetime

from ncms.application.retrieval.pipeline import RetrievalPipeline
from ncms.config import NCMSConfig
from ncms.domain.models import Memory, ScoredMemory


class _FakeGraph:
    """Minimal graph stub: maps entity_id → set[memory_id]."""

    def __init__(self, mapping: dict[str, set[str]]) -> None:
        self._mapping = mapping

    def get_memory_ids_for_entity(self, entity_id: str) -> set[str]:
        return self._mapping.get(entity_id, set()).copy()


def _sm(id_: str, when: datetime, activation: float = 0.5) -> ScoredMemory:
    mem = Memory(content=f"memory {id_}", observed_at=when)
    mem.id = id_
    return ScoredMemory(memory=mem, total_activation=activation)


def _pipeline(graph_mapping: dict[str, set[str]]) -> RetrievalPipeline:
    from unittest.mock import MagicMock

    config = NCMSConfig(db_path=":memory:")
    return RetrievalPipeline(
        store=MagicMock(),
        index=MagicMock(),
        graph=_FakeGraph(graph_mapping),
        config=config,
        splade=None,
        reranker=None,
    )


class TestSingleSubject:

    def test_first_sorts_subject_linked_ascending(self) -> None:
        pool = [
            _sm("x3", datetime(2024, 3, 1, tzinfo=UTC), 0.9),
            _sm("x1", datetime(2024, 1, 1, tzinfo=UTC), 0.8),
            _sm("other", datetime(2023, 1, 1, tzinfo=UTC), 0.7),
            _sm("x2", datetime(2024, 2, 1, tzinfo=UTC), 0.6),
        ]
        pipe = _pipeline({"xray": {"x1", "x2", "x3"}})
        out = pipe.apply_ordinal_ordering(
            pool, subject_entity_ids=["xray"],
            ordinal="first", multi_subject=False,
        )
        assert [s.memory.id for s in out] == ["x1", "x2", "x3", "other"]

    def test_last_sorts_subject_linked_descending(self) -> None:
        pool = [
            _sm("x1", datetime(2024, 1, 1, tzinfo=UTC), 0.9),
            _sm("other", datetime(2024, 6, 1, tzinfo=UTC), 0.8),
            _sm("x3", datetime(2024, 3, 1, tzinfo=UTC), 0.7),
            _sm("x2", datetime(2024, 2, 1, tzinfo=UTC), 0.6),
        ]
        pipe = _pipeline({"xray": {"x1", "x2", "x3"}})
        out = pipe.apply_ordinal_ordering(
            pool, subject_entity_ids=["xray"],
            ordinal="last", multi_subject=False,
        )
        assert [s.memory.id for s in out] == ["x3", "x2", "x1", "other"]


class TestMultiSubject:

    def test_compare_two_subjects_representatives_chronological(
        self,
    ) -> None:
        """Both MoMA and Met represented, earliest each, ordered by date."""
        pool = [
            _sm("moma-a", datetime(2024, 6, 1, tzinfo=UTC), 0.9),
            _sm("moma-b", datetime(2024, 3, 1, tzinfo=UTC), 0.8),  # earliest MoMA
            _sm("met-a", datetime(2024, 4, 1, tzinfo=UTC), 0.7),   # earliest Met
            _sm("met-b", datetime(2024, 8, 1, tzinfo=UTC), 0.6),
            _sm("other", datetime(2024, 5, 1, tzinfo=UTC), 0.5),
        ]
        pipe = _pipeline({
            "moma": {"moma-a", "moma-b"},
            "met":  {"met-a", "met-b"},
        })
        out = pipe.apply_ordinal_ordering(
            pool, subject_entity_ids=["moma", "met"],
            ordinal="first", multi_subject=True,
        )
        ids = [s.memory.id for s in out]
        # Reps: moma-b (Mar) then met-a (Apr), both chronologically.
        assert ids[:2] == ["moma-b", "met-a"]
        # Remaining subject-linked memories after reps.
        assert set(ids[2:4]) == {"moma-a", "met-b"}
        # "other" at the tail.
        assert ids[-1] == "other"

    def test_compare_three_subjects_order(self) -> None:
        pool = [
            _sm("a1", datetime(2024, 1, 1, tzinfo=UTC), 0.9),
            _sm("b1", datetime(2024, 3, 1, tzinfo=UTC), 0.8),
            _sm("c1", datetime(2024, 2, 1, tzinfo=UTC), 0.7),
        ]
        pipe = _pipeline({
            "a": {"a1"}, "b": {"b1"}, "c": {"c1"},
        })
        out = pipe.apply_ordinal_ordering(
            pool, subject_entity_ids=["a", "b", "c"],
            ordinal="first", multi_subject=True,
        )
        # Reps chronological regardless of subject order.
        assert [s.memory.id for s in out] == ["a1", "c1", "b1"]

    def test_compare_subject_missing_from_pool(self) -> None:
        """Subject with no memories in the pool — other subjects still rep'd."""
        pool = [
            _sm("a1", datetime(2024, 1, 1, tzinfo=UTC), 0.9),
            _sm("a2", datetime(2024, 2, 1, tzinfo=UTC), 0.8),
        ]
        pipe = _pipeline({"a": {"a1", "a2"}, "b": set()})
        out = pipe.apply_ordinal_ordering(
            pool, subject_entity_ids=["a", "b"],
            ordinal="first", multi_subject=True,
        )
        # Only "a" has a rep.
        assert [s.memory.id for s in out] == ["a1", "a2"]


class TestDegradeRules:

    def test_empty_pool_empty_output(self) -> None:
        pipe = _pipeline({"a": {"x"}})
        assert pipe.apply_ordinal_ordering(
            [], subject_entity_ids=["a"],
            ordinal="first", multi_subject=False,
        ) == []

    def test_no_subjects_is_noop(self) -> None:
        pool = [
            _sm("a", datetime(2024, 1, 1, tzinfo=UTC), 0.9),
            _sm("b", datetime(2024, 2, 1, tzinfo=UTC), 0.8),
        ]
        pipe = _pipeline({})
        out = pipe.apply_ordinal_ordering(
            pool, subject_entity_ids=[],
            ordinal="first", multi_subject=False,
        )
        assert [s.memory.id for s in out] == ["a", "b"]

    def test_no_subject_linked_in_head_is_noop(self) -> None:
        pool = [
            _sm("a", datetime(2024, 1, 1, tzinfo=UTC), 0.9),
            _sm("b", datetime(2024, 2, 1, tzinfo=UTC), 0.8),
        ]
        # Graph has "x" pointing to memories NOT in the pool.
        pipe = _pipeline({"x": {"outside-1", "outside-2"}})
        out = pipe.apply_ordinal_ordering(
            pool, subject_entity_ids=["x"],
            ordinal="first", multi_subject=False,
        )
        assert [s.memory.id for s in out] == ["a", "b"]

    def test_unknown_ordinal_is_noop(self) -> None:
        pool = [
            _sm("a", datetime(2024, 1, 1, tzinfo=UTC), 0.9),
        ]
        pipe = _pipeline({"x": {"a"}})
        out = pipe.apply_ordinal_ordering(
            pool, subject_entity_ids=["x"],
            ordinal="middle", multi_subject=False,
        )
        assert [s.memory.id for s in out] == ["a"]


class TestTextFallback:
    """GLiNER non-determinism means subject entities don't always link
    to every memory that mentions them.  For single-subject queries,
    text-fallback recovers these cases.  Not enabled for multi-subject
    because cross-subject bleed on text match is worse than missing a
    few memories.
    """

    def test_single_subject_text_fallback_recovers_unlinked(self) -> None:
        """3 of 5 ADRs graph-linked to 'authentication'; the other 2
        contain the word but weren't linked at ingest.  Text-fallback
        includes all 5."""
        def _adr(id_: str, content: str, when: datetime) -> ScoredMemory:
            mem = Memory(content=content, observed_at=when)
            mem.id = id_
            return ScoredMemory(memory=mem, total_activation=0.5)

        pool = [
            _adr("ADR-021", "Authentication supersedes JWT",
                 datetime(2025, 3, 1, tzinfo=UTC)),
            _adr("ADR-014", "Authentication adds JWT",
                 datetime(2024, 2, 1, tzinfo=UTC)),
            _adr("ADR-007", "Authentication refactored OAuth",
                 datetime(2023, 6, 1, tzinfo=UTC)),
            _adr("ADR-029", "Authentication latest passkeys",
                 datetime(2026, 1, 1, tzinfo=UTC)),  # NOT graph-linked
            _adr("ADR-001", "Initial authentication cookies",
                 datetime(2023, 1, 1, tzinfo=UTC)),  # NOT graph-linked
        ]
        pipe = _pipeline({"auth": {"ADR-007", "ADR-014", "ADR-021"}})
        out = pipe.apply_ordinal_ordering(
            pool, subject_entity_ids=["auth"],
            subject_names=["authentication"],
            ordinal="last", multi_subject=False,
        )
        # All 5 are subject-linked via text fallback; desc by date:
        assert [s.memory.id for s in out] == [
            "ADR-029", "ADR-021", "ADR-014", "ADR-007", "ADR-001",
        ]

    def test_multi_subject_ignores_text_fallback(self) -> None:
        """subject_names passed but multi_subject=True → ignored."""
        def _mk(id_: str, content: str, when: datetime) -> ScoredMemory:
            mem = Memory(content=content, observed_at=when)
            mem.id = id_
            return ScoredMemory(memory=mem, total_activation=0.5)

        pool = [
            _mk("x1", "I visited the museum", datetime(2024, 1, 1, tzinfo=UTC)),
            _mk("x2", "I visited the gallery", datetime(2024, 2, 1, tzinfo=UTC)),
        ]
        # Graph has neither linked; text fallback would match both if used.
        pipe = _pipeline({"a": set(), "b": set()})
        out = pipe.apply_ordinal_ordering(
            pool, subject_entity_ids=["a", "b"],
            subject_names=["museum", "gallery"],
            ordinal="first", multi_subject=True,
        )
        # Should be a no-op (no graph links, text-fallback disabled).
        assert [s.memory.id for s in out] == ["x1", "x2"]


class TestInputIntegrity:

    def test_input_list_not_mutated(self) -> None:
        pool = [
            _sm("x2", datetime(2024, 2, 1, tzinfo=UTC), 0.9),
            _sm("x1", datetime(2024, 1, 1, tzinfo=UTC), 0.8),
        ]
        before = [s.memory.id for s in pool]
        pipe = _pipeline({"x": {"x1", "x2"}})
        pipe.apply_ordinal_ordering(
            pool, subject_entity_ids=["x"],
            ordinal="first", multi_subject=False,
        )
        assert [s.memory.id for s in pool] == before

    def test_rerank_k_bounds_head(self) -> None:
        pool = [
            _sm("xA", datetime(2024, 1, 1, tzinfo=UTC), 1.0),
            _sm("xB", datetime(2023, 1, 1, tzinfo=UTC), 0.9),
            _sm("tail-subject", datetime(2022, 1, 1, tzinfo=UTC), 0.5),
            _sm("tail-other",   datetime(2021, 1, 1, tzinfo=UTC), 0.4),
        ]
        pipe = _pipeline({
            "x": {"xA", "xB", "tail-subject"},
        })
        out = pipe.apply_ordinal_ordering(
            pool, subject_entity_ids=["x"],
            ordinal="first", multi_subject=False,
            rerank_k=2,
        )
        # Top-2 reordered, tail preserved.
        assert [s.memory.id for s in out] == [
            "xB", "xA",      # head sorted ascending by date
            "tail-subject",  # original index 2
            "tail-other",    # original index 3
        ]
