"""Unit tests for content-derived marker induction.

Pins the purity + support filter and the seed-strip behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass

from ncms.domain.tlg import (
    InducedContentMarkers,
    induce_content_markers,
)


@dataclass
class _Mem:
    id: str
    content: str
    observed_at: None = None


class TestInduction:
    def test_word_appearing_only_in_terminals_is_current_candidate(
        self,
    ) -> None:
        memories = [
            _Mem(id="t1", content="Auth now uses OAuth launched."),
            _Mem(id="t2", content="Payments now launched."),
            _Mem(id="r1", content="Auth introduced session cookies."),
            _Mem(id="r2", content="Payments introduced the ledger."),
        ]
        induced = induce_content_markers(
            memories,
            terminal_ids={"t1", "t2"},
            root_ids={"r1", "r2"},
            seed_current=frozenset(),
            seed_origin=frozenset(),
        )
        # "launched" appears in both terminal memories, neither root
        # — support >= 2, opposing count 0, passes the gate.
        assert "launched" in induced.current_candidates
        # "introduced" appears only in roots — origin candidate.
        assert "introduced" in induced.origin_candidates

    def test_support_gate_drops_low_count(self) -> None:
        memories = [
            _Mem(id="t1", content="Rare verb fired."),
            _Mem(id="r1", content="Other verb."),
        ]
        induced = induce_content_markers(
            memories,
            terminal_ids={"t1"},
            root_ids={"r1"},
            min_support=2,
        )
        # Only one terminal — support=1 — nothing survives.
        assert induced.current_candidates == frozenset()
        assert induced.origin_candidates == frozenset()

    def test_purity_gate_drops_ambiguous(self) -> None:
        # A verb that fires equally often in terminals and roots must
        # fail the purity_ratio=2.0 gate (1:1 not >= 2:1).
        memories = [
            _Mem(id="t1", content="Rolled out feature X."),
            _Mem(id="t2", content="Rolled out feature Y."),
            _Mem(id="r1", content="Rolled out feature Z."),
            _Mem(id="r2", content="Rolled out feature W."),
        ]
        induced = induce_content_markers(
            memories,
            terminal_ids={"t1", "t2"},
            root_ids={"r1", "r2"},
            min_support=2,
            purity_ratio=2.0,
        )
        # Equal counts — no candidates.
        assert "rolled" not in induced.current_candidates
        assert "rolled" not in induced.origin_candidates

    def test_seed_strip(self) -> None:
        memories = [
            _Mem(id="t1", content="System is now launched."),
            _Mem(id="t2", content="System is now launched."),
            _Mem(id="r1", content="Nothing relevant here."),
        ]
        induced = induce_content_markers(
            memories,
            terminal_ids={"t1", "t2"},
            root_ids={"r1"},
            seed_current=frozenset({"launched"}),
            seed_origin=frozenset(),
        )
        # "launched" already in seed — stripped from candidates.
        assert "launched" not in induced.current_candidates

    def test_empty_input_empty_output(self) -> None:
        induced = induce_content_markers(
            [],
            terminal_ids=set(),
            root_ids=set(),
        )
        assert induced == InducedContentMarkers()
