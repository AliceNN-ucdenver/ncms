"""Unit tests for the confidence model + ``grammar ∨ BM25`` composition.

Pins the core safety property (Proposition 1): only HIGH/MEDIUM
grammar answers may be prepended onto the BM25 ranking.  LOW,
ABSTAIN, and NONE always fall through to BM25 unchanged.
"""

from __future__ import annotations

from ncms.domain.tlg import (
    CONFIDENT_LEVELS,
    Confidence,
    LGIntent,
    LGTrace,
    compose,
    is_confident,
)

# ---------------------------------------------------------------------------
# Confidence predicate
# ---------------------------------------------------------------------------


class TestConfidence:
    def test_high_is_confident(self) -> None:
        assert is_confident(Confidence.HIGH)

    def test_medium_is_confident(self) -> None:
        assert is_confident(Confidence.MEDIUM)

    def test_low_is_not_confident(self) -> None:
        assert not is_confident(Confidence.LOW)

    def test_abstain_is_not_confident(self) -> None:
        assert not is_confident(Confidence.ABSTAIN)

    def test_none_is_not_confident(self) -> None:
        assert not is_confident(Confidence.NONE)

    def test_python_none_is_not_confident(self) -> None:
        assert not is_confident(None)

    def test_string_values_also_work(self) -> None:
        assert is_confident("high")
        assert is_confident("medium")
        assert not is_confident("low")

    def test_unknown_string_returns_false(self) -> None:
        assert not is_confident("elephant")

    def test_confident_levels_exactly_high_and_medium(self) -> None:
        assert frozenset({Confidence.HIGH, Confidence.MEDIUM}) == CONFIDENT_LEVELS


# ---------------------------------------------------------------------------
# LGTrace.has_confident_answer
# ---------------------------------------------------------------------------


def _trace(
    grammar_answer: str | None,
    confidence: Confidence,
    zone_context: list[str] | None = None,
) -> LGTrace:
    return LGTrace(
        query="q",
        intent=LGIntent(kind="current", subject="s"),
        grammar_answer=grammar_answer,
        zone_context=zone_context or [],
        confidence=confidence,
    )


class TestTracePredicate:
    def test_high_with_answer_is_confident(self) -> None:
        assert _trace("m1", Confidence.HIGH).has_confident_answer()

    def test_medium_with_answer_is_confident(self) -> None:
        assert _trace("m1", Confidence.MEDIUM).has_confident_answer()

    def test_high_without_answer_is_not_confident(self) -> None:
        assert not _trace(None, Confidence.HIGH).has_confident_answer()

    def test_low_is_never_confident(self) -> None:
        assert not _trace("m1", Confidence.LOW).has_confident_answer()

    def test_abstain_is_never_confident(self) -> None:
        assert not _trace("m1", Confidence.ABSTAIN).has_confident_answer()

    def test_none_is_never_confident(self) -> None:
        assert not _trace("m1", Confidence.NONE).has_confident_answer()


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


class TestCompose:
    def test_confident_trace_prepends_grammar_answer(self) -> None:
        trace = _trace("grammar-hit", Confidence.HIGH)
        result = compose(["a", "b", "c"], trace)
        assert result == ["grammar-hit", "a", "b", "c"]

    def test_non_confident_trace_returns_bm25_unchanged(self) -> None:
        trace = _trace("grammar-hit", Confidence.LOW)
        result = compose(["a", "b", "c"], trace)
        assert result == ["a", "b", "c"]

    def test_abstain_returns_bm25_unchanged(self) -> None:
        trace = _trace("grammar-hit", Confidence.ABSTAIN)
        result = compose(["a", "b", "c"], trace)
        assert result == ["a", "b", "c"]

    def test_zone_context_inserted_after_grammar_answer(self) -> None:
        trace = _trace(
            "grammar-hit",
            Confidence.HIGH,
            zone_context=["sib-1", "sib-2"],
        )
        result = compose(["grammar-hit", "a", "b", "sib-1", "c"], trace)
        # Expected order: grammar-hit, then zone_context, then remaining
        # BM25 in order, with all de-duped.
        assert result == ["grammar-hit", "sib-1", "sib-2", "a", "b", "c"]

    def test_dedup_when_grammar_answer_already_in_bm25(self) -> None:
        trace = _trace("grammar-hit", Confidence.HIGH)
        result = compose(["a", "grammar-hit", "b"], trace)
        # grammar-hit lands at rank 1; original position dropped.
        assert result == ["grammar-hit", "a", "b"]

    def test_empty_bm25_yields_just_the_grammar_answer(self) -> None:
        trace = _trace(
            "grammar-hit",
            Confidence.HIGH,
            zone_context=["sib-1"],
        )
        result = compose([], trace)
        assert result == ["grammar-hit", "sib-1"]

    def test_confident_trace_without_answer_falls_back(self) -> None:
        # has_confident_answer() returns False when grammar_answer is
        # None even if confidence is HIGH — protects against trace
        # construction bugs.
        trace = _trace(None, Confidence.HIGH)
        result = compose(["a", "b"], trace)
        assert result == ["a", "b"]

    def test_iterable_input_is_accepted(self) -> None:
        trace = _trace("hit", Confidence.HIGH)
        # Pass a generator — compose should materialise it.
        result = compose((x for x in ["a", "b"]), trace)
        assert result == ["hit", "a", "b"]
