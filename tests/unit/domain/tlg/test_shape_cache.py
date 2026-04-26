"""Unit tests for the in-memory query-shape cache.

Pins skeleton extraction determinism, placeholder ordering, and
lookup / learn semantics (conflict-keeps-existing).
"""

from __future__ import annotations

from ncms.domain.tlg import SubjectMemory, induce_vocabulary
from ncms.domain.tlg.shape_cache import (
    QueryShapeCache,
    extract_skeleton,
)


def _vocab():
    return induce_vocabulary(
        [
            SubjectMemory(
                subject="auth",
                entities=frozenset({"OAuth", "session cookies", "authentication"}),
            ),
            SubjectMemory(
                subject="auth",
                entities=frozenset({"JWT", "authentication"}),
            ),
        ]
    )


class TestExtractSkeleton:
    def test_entity_replaced_with_placeholder(self) -> None:
        v = _vocab()
        skel, slots = extract_skeleton("What came after OAuth?", v)
        assert "<X>" in skel
        assert slots["<X>"] == "OAuth"

    def test_multiple_entities_get_sequential_placeholders(self) -> None:
        v = _vocab()
        skel, slots = extract_skeleton(
            "What happened between OAuth and JWT?",
            v,
        )
        assert "<X>" in skel
        assert "<Y>" in skel
        assert slots["<X>"] == "OAuth"
        assert slots["<Y>"] == "JWT"

    def test_determiner_stripped(self) -> None:
        v = _vocab()
        skel1, _ = extract_skeleton("the OAuth?", v)
        skel2, _ = extract_skeleton("OAuth?", v)
        assert skel1 == skel2

    def test_stem_collapses_morphology(self) -> None:
        # Snowball stems "retire" / "retired" / "retirement" to the
        # same root; "came" / "come" are irregular verbs and don't
        # collapse (as expected for any Snowball-based approach).
        v = _vocab()
        skel1, _ = extract_skeleton("are we retiring OAuth?", v)
        skel2, _ = extract_skeleton("are we retired OAuth?", v)
        assert skel1 == skel2


class TestQueryShapeCache:
    def test_lookup_miss_on_empty_cache(self) -> None:
        cache = QueryShapeCache()
        assert cache.lookup("What came after OAuth?", _vocab()) is None

    def test_learn_then_lookup_hits(self) -> None:
        cache = QueryShapeCache()
        v = _vocab()
        cache.learn("What came after OAuth?", "sequence", v)
        hit = cache.lookup("What came after JWT?", v)
        assert hit is not None
        intent, slots = hit
        assert intent == "sequence"
        # Slot gets refilled from the actual query.
        assert slots["<X>"] == "JWT"

    def test_conflict_keeps_existing(self) -> None:
        cache = QueryShapeCache()
        v = _vocab()
        cache.learn("What came after OAuth?", "sequence", v)
        # Same skeleton, different intent — no-op.
        cache.learn("What came after OAuth?", "predecessor", v)
        hit = cache.lookup("What came after OAuth?", v)
        assert hit is not None
        assert hit[0] == "sequence"

    def test_none_intent_not_cached(self) -> None:
        cache = QueryShapeCache()
        v = _vocab()
        cache.learn("What came after OAuth?", "none", v)
        assert cache.lookup("What came after OAuth?", v) is None

    def test_snapshot_round_trip(self) -> None:
        cache = QueryShapeCache()
        v = _vocab()
        cache.learn("What came after OAuth?", "sequence", v)
        cache.learn("what came before JWT?", "predecessor", v)
        dump = cache.snapshot()
        restored = QueryShapeCache.from_snapshot(dump)
        assert len(restored) == len(cache)
        # Lookups against the restored cache find both entries.
        assert (
            restored.lookup(
                "What came after OAuth?",
                v,
            )[0]
            == "sequence"
        )
        assert (
            restored.lookup(
                "what came before JWT?",
                v,
            )[0]
            == "predecessor"
        )
