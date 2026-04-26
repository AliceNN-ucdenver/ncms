"""Unit tests for the L2 transition-marker induction.

Pins the research behaviour:

* Verb-phrase heads are extracted only from the destination content.
* The distinctiveness filter drops any head that ties or underperforms
  across buckets.
* :func:`match_intent_from_markers` resolves a query to the transition
  with the strongest marker hits; ties are broken by the priority
  ladder.
* :func:`retirement_verbs_from` flattens ``supersedes`` + ``retires``
  buckets for the reconciliation extractor.
"""

from __future__ import annotations

from ncms.domain.tlg.markers import (
    EdgeObservation,
    InducedEdgeMarkers,
    extract_verb_heads,
    induce_edge_markers,
    match_intent_from_markers,
    retirement_verbs_from,
)

# ---------------------------------------------------------------------------
# Verb-head extraction
# ---------------------------------------------------------------------------


class TestExtractVerbHeads:
    def test_retire_form_caught(self) -> None:
        assert "retire" in extract_verb_heads("Retire the legacy flow.")

    def test_moves_directional(self) -> None:
        heads = extract_verb_heads("Authentication moves from cookies to tokens.")
        assert "moves" in heads

    def test_supersedes_form(self) -> None:
        assert "supersedes" in extract_verb_heads("ADR-021 supersedes ADR-014.")

    def test_no_verb_yields_empty(self) -> None:
        assert extract_verb_heads("The weather is cloudy today.") == set()


# ---------------------------------------------------------------------------
# Induction + distinctiveness filter
# ---------------------------------------------------------------------------


class TestInduction:
    def test_distinct_heads_retained(self) -> None:
        observations = [
            EdgeObservation(
                transition="supersedes",
                dst_content="Retire legacy session cookies in favour of OAuth.",
            ),
            EdgeObservation(
                transition="supersedes",
                dst_content="Retire the long-lived JWTs.",
            ),
            EdgeObservation(
                transition="refines",
                dst_content="Add JSON Web Tokens alongside OAuth.",
            ),
        ]
        induced = induce_edge_markers(observations)
        # "retire" appears only in supersedes → distinctive there.
        assert "retire" in induced.markers["supersedes"]
        # "add" appears only in refines → distinctive there.
        assert "add" in induced.markers["refines"]

    def test_tie_drops_marker_from_both_buckets(self) -> None:
        observations = [
            EdgeObservation(
                transition="supersedes",
                dst_content="Resolved the authentication bug.",
            ),
            EdgeObservation(
                transition="refines",
                dst_content="Resolved the authentication bug.",
            ),
        ]
        induced = induce_edge_markers(observations)
        # "resolved" is tied → must drop from both.
        assert "resolved" not in induced.markers.get("supersedes", frozenset())
        assert "resolved" not in induced.markers.get("refines", frozenset())

    def test_strictly_greater_count_wins_bucket(self) -> None:
        observations = [
            EdgeObservation(
                transition="supersedes",
                dst_content="Retire legacy A.",
            ),
            EdgeObservation(
                transition="supersedes",
                dst_content="Retire legacy B.",
            ),
            EdgeObservation(
                transition="refines",
                dst_content="Retire legacy C only.",
            ),
        ]
        induced = induce_edge_markers(observations)
        # 2 > 1 → supersedes keeps it
        assert "retire" in induced.markers["supersedes"]
        # 1 !> 2 → refines drops it
        assert "retire" not in induced.markers.get("refines", frozenset())

    def test_empty_observations_produce_empty_markers(self) -> None:
        induced = induce_edge_markers([])
        assert induced.markers == {}

    def test_observation_without_content_skipped(self) -> None:
        observations = [
            EdgeObservation(transition="supersedes", dst_content=""),
            EdgeObservation(transition="", dst_content="Retire foo."),
        ]
        induced = induce_edge_markers(observations)
        assert induced.markers == {}


# ---------------------------------------------------------------------------
# Query-side marker matching
# ---------------------------------------------------------------------------


class TestMatchIntent:
    def _induced(self) -> InducedEdgeMarkers:
        return induce_edge_markers(
            [
                EdgeObservation(
                    transition="supersedes",
                    dst_content="Retire legacy A.",
                ),
                EdgeObservation(
                    transition="supersedes",
                    dst_content="Retire legacy B.",
                ),
                EdgeObservation(
                    transition="refines",
                    dst_content="Add feature flag X.",
                ),
                EdgeObservation(
                    transition="refines",
                    dst_content="Add feature flag Y.",
                ),
            ]
        )

    def test_retire_query_routes_to_supersedes(self) -> None:
        induced = self._induced()
        assert (
            match_intent_from_markers("When did we retire that service?", induced) == "supersedes"
        )

    def test_add_query_routes_to_refines(self) -> None:
        induced = self._induced()
        assert (
            match_intent_from_markers("What did we add to auth last quarter?", induced) == "refines"
        )

    def test_unrelated_query_returns_none(self) -> None:
        induced = self._induced()
        assert match_intent_from_markers("What's for lunch?", induced) is None

    def test_priority_breaks_tie(self) -> None:
        # Two buckets, same hit count — priority ladder decides.
        # Construct markers manually so both buckets match one query term.
        induced = InducedEdgeMarkers(
            markers={
                "supersedes": frozenset({"replace"}),
                "refines": frozenset({"replace"}),
            }
        )
        assert (
            match_intent_from_markers("We will replace the legacy gateway", induced) == "supersedes"
        )


# ---------------------------------------------------------------------------
# Bridge to retirement extractor
# ---------------------------------------------------------------------------


class TestRetirementVerbsFrom:
    def test_flattens_supersedes_and_retires_buckets(self) -> None:
        induced = InducedEdgeMarkers(
            markers={
                "supersedes": frozenset({"retire", "supersedes"}),
                "retires": frozenset({"deprecate"}),
                "refines": frozenset({"add", "extend"}),
            }
        )
        verbs = retirement_verbs_from(induced)
        assert verbs == frozenset({"retire", "supersedes", "deprecate"})
        # ``add`` from refines is NOT included
        assert "add" not in verbs

    def test_missing_buckets_yield_empty_set(self) -> None:
        induced = InducedEdgeMarkers(markers={})
        assert retirement_verbs_from(induced) == frozenset()
