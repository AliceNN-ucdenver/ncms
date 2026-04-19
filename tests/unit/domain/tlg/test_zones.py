"""Unit tests for zone computation.

Pins the production-rule semantics:

* A memory with no incoming admissible edge is a zone root.
* ``refines`` extends the zone.
* ``supersedes`` closes the zone and opens a new one.
* ``retires`` closes the zone without opening a new one.
* ``current_zone`` = the zone with no closer (newest terminal
  wins on multiple ungrounded chains).
* ``origin_memory`` = root of the earliest zone by
  ``observed_at``.
* ``retirement_memory`` matches via stem equality, alias expansion,
  and agentive-noun prefix tolerance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ncms.domain.tlg.zones import (
    Zone,
    ZoneEdge,
    compute_zones,
    current_zone,
    origin_memory,
    retirement_memory,
)


@dataclass
class _Mem:
    id: str
    content: str
    observed_at: datetime | None


def _mem(mid: str, *, day: int = 1, content: str = "") -> _Mem:
    return _Mem(
        id=mid,
        content=content,
        observed_at=datetime(2026, 1, day, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------


class TestComputeZones:
    def test_single_memory_one_zone(self) -> None:
        m = _mem("m1")
        zones = compute_zones("s", [m], edges=[])
        assert len(zones) == 1
        assert zones[0].start_mid == "m1"
        assert zones[0].terminal_mid == "m1"
        assert zones[0].ended_transition is None

    def test_refines_extends_zone(self) -> None:
        a, b, c = _mem("a", day=1), _mem("b", day=2), _mem("c", day=3)
        edges = [
            ZoneEdge(src="a", dst="b", transition="refines"),
            ZoneEdge(src="b", dst="c", transition="refines"),
        ]
        zones = compute_zones("s", [a, b, c], edges)
        assert len(zones) == 1
        assert zones[0].memory_ids == ("a", "b", "c")
        assert zones[0].terminal_mid == "c"

    def test_supersedes_closes_and_reopens(self) -> None:
        a, b, c = _mem("a", day=1), _mem("b", day=2), _mem("c", day=3)
        edges = [
            ZoneEdge(src="a", dst="b", transition="refines"),
            ZoneEdge(src="b", dst="c", transition="supersedes"),
        ]
        zones = compute_zones("s", [a, b, c], edges)
        assert len(zones) == 2
        assert zones[0].memory_ids == ("a", "b")
        assert zones[0].ended_transition == "supersedes"
        assert zones[0].ended_by == "c"
        assert zones[1].start_mid == "c"
        assert zones[1].ended_transition is None

    def test_retires_closes_without_reopen(self) -> None:
        a, b = _mem("a"), _mem("b")
        edges = [ZoneEdge(src="a", dst="b", transition="retires")]
        zones = compute_zones("s", [a, b], edges)
        # Only one zone: the chain starting at a.  b is the closer.
        assert len(zones) == 1
        assert zones[0].ended_transition == "retires"
        assert zones[0].ended_by == "b"

    def test_non_subject_edges_filtered(self) -> None:
        # edge involves a memory not in the subject set — dropped.
        a = _mem("a")
        edges = [ZoneEdge(src="a", dst="other", transition="refines")]
        zones = compute_zones("s", [a], edges)
        assert len(zones) == 1
        assert zones[0].memory_ids == ("a",)

    def test_inadmissible_transition_ignored(self) -> None:
        a, b = _mem("a"), _mem("b")
        edges = [ZoneEdge(src="a", dst="b", transition="random_thing")]
        zones = compute_zones("s", [a, b], edges)
        # Both are zone roots (no admissible in-edges).
        assert len(zones) == 2


# ---------------------------------------------------------------------------


class TestCurrentZone:
    def test_single_ungrounded_zone(self) -> None:
        a = _mem("a")
        zones = compute_zones("s", [a], edges=[])
        idx = {"a": a}
        assert current_zone(zones, idx) is zones[0]

    def test_after_supersession_current_is_new(self) -> None:
        a, b = _mem("a", day=1), _mem("b", day=2)
        edges = [ZoneEdge(src="a", dst="b", transition="supersedes")]
        zones = compute_zones("s", [a, b], edges)
        idx = {m.id: m for m in [a, b]}
        current = current_zone(zones, idx)
        assert current is not None
        assert current.terminal_mid == "b"

    def test_empty_zones_return_none(self) -> None:
        assert current_zone([], {}) is None

    def test_multiple_ungrounded_picks_newest_terminal(self) -> None:
        # Two parallel chains, no edges between them.
        older = _mem("older", day=1)
        newer = _mem("newer", day=5)
        zones = compute_zones("s", [older, newer], edges=[])
        idx = {m.id: m for m in [older, newer]}
        current = current_zone(zones, idx)
        assert current is not None
        assert current.terminal_mid == "newer"


class TestOriginMemory:
    def test_earliest_zone_root_wins(self) -> None:
        early, mid, late = (
            _mem("early", day=1), _mem("mid", day=3), _mem("late", day=5),
        )
        edges = [
            ZoneEdge(src="early", dst="mid", transition="supersedes"),
            ZoneEdge(src="mid", dst="late", transition="supersedes"),
        ]
        zones = compute_zones("s", [early, mid, late], edges)
        idx = {m.id: m for m in [early, mid, late]}
        assert origin_memory(zones, idx) == "early"

    def test_empty_input_none(self) -> None:
        assert origin_memory([], {}) is None


# ---------------------------------------------------------------------------


class TestRetirementMemory:
    def _edges(
        self, retires: list[tuple[str, str, list[str]]],
    ) -> list[ZoneEdge]:
        return [
            ZoneEdge(
                src=src,
                dst=dst,
                transition="supersedes",
                retires_entities=frozenset(retired),
            )
            for src, dst, retired in retires
        ]

    def test_stem_equality_direct_match(self) -> None:
        edges = self._edges([("a", "b", ["session cookies"])])
        result = retirement_memory(
            "session cookie", edges, subject_memory_ids={"a", "b"},
        )
        # Stemmer collapses "cookies"/"cookie".
        assert result == "b"

    def test_alias_expansion(self) -> None:
        edges = self._edges([("a", "b", ["JSON Web Tokens"])])
        aliases = {
            "JWT": frozenset({"JSON Web Tokens"}),
            "JSON Web Tokens": frozenset({"JWT"}),
        }
        assert retirement_memory(
            "JWT", edges, subject_memory_ids={"a", "b"}, aliases=aliases,
        ) == "b"

    def test_prefix_tolerance(self) -> None:
        edges = self._edges([("a", "b", ["blocker"])])
        # "blocked" stem == "block"; "blocker" stem == "blocker".
        # Prefix match triggers at 4+ char stems.
        assert retirement_memory(
            "blocked", edges, subject_memory_ids={"a", "b"},
        ) == "b"

    def test_no_match_returns_none(self) -> None:
        edges = self._edges([("a", "b", ["something-else"])])
        assert retirement_memory(
            "xyz", edges, subject_memory_ids={"a", "b"},
        ) is None

    def test_non_subject_edges_skipped(self) -> None:
        # dst is out-of-subject → edge ignored.
        edges = self._edges([("a", "OTHER", ["session cookies"])])
        assert retirement_memory(
            "session cookies", edges, subject_memory_ids={"a"},
        ) is None

    def test_non_retirement_transition_ignored(self) -> None:
        edges = [
            ZoneEdge(
                src="a", dst="b",
                transition="refines",
                retires_entities=frozenset({"session cookies"}),
            ),
        ]
        assert retirement_memory(
            "session cookies", edges, subject_memory_ids={"a", "b"},
        ) is None
