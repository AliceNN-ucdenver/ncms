"""Unit tests for CTLG causal-pair extraction from cue tags.

Pure-function tests exercising :func:`extract_causal_pairs` on
hand-built TaggedToken sequences.  These cover direction
semantics (explicit vs altlex), multi-word cue spans, ENABLES
sub-case, and the low-confidence filter.
"""

from __future__ import annotations

from ncms.domain.tlg.causal_extractor import (
    extract_causal_pairs,
    pairs_to_causal_edges,
)
from ncms.domain.tlg.cue_taxonomy import TaggedToken


def _mk(pairs: list[tuple[str, str, float]]) -> list[TaggedToken]:
    """Build TaggedToken sequence from (surface, label, confidence) triples.

    Offsets are synthetic — space between tokens.
    """
    out: list[TaggedToken] = []
    pos = 0
    for surface, label, conf in pairs:
        out.append(TaggedToken(
            char_start=pos,
            char_end=pos + len(surface),
            surface=surface,
            cue_label=label,  # type: ignore[arg-type]
            confidence=conf,
        ))
        pos += len(surface) + 1
    return out


class TestExplicitCausal:
    def test_because_reverses_direction(self) -> None:
        # "X because Y" → Y caused X (cause is on the right)
        tokens = _mk([
            ("Postgres", "B-REFERENT", 0.95),
            ("because", "B-CAUSAL_EXPLICIT", 0.90),
            ("audit", "B-REFERENT", 0.92),
        ])
        pairs = extract_causal_pairs(tokens)
        assert len(pairs) == 1
        p = pairs[0]
        assert p.edge_type == "caused_by"
        assert p.effect_surface == "postgres"
        assert p.cause_surface == "audit"

    def test_due_to_reverses_direction(self) -> None:
        tokens = _mk([
            ("migration", "B-REFERENT", 0.9),
            ("due", "B-CAUSAL_EXPLICIT", 0.85),
            ("to", "I-CAUSAL_EXPLICIT", 0.85),
            ("outage", "B-REFERENT", 0.9),
        ])
        pairs = extract_causal_pairs(tokens)
        assert len(pairs) == 1
        assert pairs[0].effect_surface == "migration"
        assert pairs[0].cause_surface == "outage"


class TestAltlexCausal:
    def test_led_to_forward_direction(self) -> None:
        # "X led to Y" → X caused Y (cause is on the left)
        tokens = _mk([
            ("outage", "B-REFERENT", 0.95),
            ("led", "B-CAUSAL_ALTLEX", 0.8),
            ("to", "I-CAUSAL_ALTLEX", 0.8),
            ("rewrite", "B-REFERENT", 0.9),
        ])
        pairs = extract_causal_pairs(tokens)
        assert len(pairs) == 1
        p = pairs[0]
        assert p.edge_type == "caused_by"
        assert p.effect_surface == "rewrite"
        assert p.cause_surface == "outage"

    def test_caused_verb_forward(self) -> None:
        tokens = _mk([
            ("spike", "B-REFERENT", 0.9),
            ("caused", "B-CAUSAL_ALTLEX", 0.85),
            ("rate-limiting", "B-REFERENT", 0.9),
        ])
        pairs = extract_causal_pairs(tokens)
        assert len(pairs) == 1
        assert pairs[0].cause_surface == "spike"
        assert pairs[0].effect_surface == "rate-limiting"

    def test_motivated_forward(self) -> None:
        tokens = _mk([
            ("audit", "B-REFERENT", 0.9),
            ("motivated", "B-CAUSAL_ALTLEX", 0.88),
            ("Vault", "B-REFERENT", 0.95),
        ])
        pairs = extract_causal_pairs(tokens)
        assert len(pairs) == 1
        assert pairs[0].cause_surface == "audit"
        assert pairs[0].effect_surface == "vault"


class TestEnables:
    def test_enabled_produces_enables_edge(self) -> None:
        # "pgvector enabled Postgres" → Postgres was ENABLED by pgvector
        tokens = _mk([
            ("pgvector", "B-REFERENT", 0.9),
            ("enabled", "B-CAUSAL_ALTLEX", 0.85),
            ("Postgres", "B-REFERENT", 0.95),
        ])
        pairs = extract_causal_pairs(tokens)
        assert len(pairs) == 1
        p = pairs[0]
        assert p.edge_type == "enables"
        assert p.effect_surface == "postgres"
        assert p.cause_surface == "pgvector"


class TestConfidenceFilter:
    def test_low_confidence_cue_drops_pair(self) -> None:
        tokens = _mk([
            ("A", "B-REFERENT", 0.9),
            ("because", "B-CAUSAL_EXPLICIT", 0.3),  # low conf cue
            ("B", "B-REFERENT", 0.9),
        ])
        pairs = extract_causal_pairs(tokens, min_confidence=0.6)
        assert pairs == []

    def test_low_confidence_referent_drops_pair(self) -> None:
        tokens = _mk([
            ("A", "B-REFERENT", 0.4),  # low conf referent
            ("because", "B-CAUSAL_EXPLICIT", 0.9),
            ("B", "B-REFERENT", 0.9),
        ])
        pairs = extract_causal_pairs(tokens, min_confidence=0.6)
        assert pairs == []


class TestMissingNeighbour:
    def test_no_left_referent(self) -> None:
        tokens = _mk([
            ("because", "B-CAUSAL_EXPLICIT", 0.9),
            ("audit", "B-REFERENT", 0.9),
        ])
        assert extract_causal_pairs(tokens) == []

    def test_no_right_referent(self) -> None:
        tokens = _mk([
            ("Postgres", "B-REFERENT", 0.9),
            ("because", "B-CAUSAL_EXPLICIT", 0.9),
        ])
        assert extract_causal_pairs(tokens) == []

    def test_no_cue(self) -> None:
        tokens = _mk([
            ("Postgres", "B-REFERENT", 0.9),
            ("and", "O", 1.0),
            ("MongoDB", "B-REFERENT", 0.9),
        ])
        assert extract_causal_pairs(tokens) == []


class TestMultiPair:
    def test_two_cues_two_pairs(self) -> None:
        # "Outage led to rewrite because scale"
        # → rewrite was caused by outage (altlex)
        # → rewrite was caused by scale (explicit)
        tokens = _mk([
            ("outage", "B-REFERENT", 0.9),
            ("led", "B-CAUSAL_ALTLEX", 0.85),
            ("to", "I-CAUSAL_ALTLEX", 0.85),
            ("rewrite", "B-REFERENT", 0.9),
            ("because", "B-CAUSAL_EXPLICIT", 0.88),
            ("scale", "B-REFERENT", 0.9),
        ])
        pairs = extract_causal_pairs(tokens)
        assert len(pairs) == 2
        # altlex first (left-to-right order)
        assert pairs[0].effect_surface == "rewrite"
        assert pairs[0].cause_surface == "outage"
        # then explicit
        assert pairs[1].effect_surface == "rewrite"
        assert pairs[1].cause_surface == "scale"


class TestUnknownCueSkipped:
    def test_unknown_altlex_surface_drops_pair(self) -> None:
        # Untagged altlex phrase ("necessitated") isn't in our
        # lexicon — conservatively skip rather than guess direction.
        tokens = _mk([
            ("audit", "B-REFERENT", 0.9),
            ("necessitated", "B-CAUSAL_ALTLEX", 0.8),
            ("Vault", "B-REFERENT", 0.9),
        ])
        pairs = extract_causal_pairs(tokens)
        # Conservative policy: unknown phrase → no pair.
        assert pairs == []


class TestEdgesResolution:
    def test_pairs_to_edges_with_resolution(self) -> None:
        # Simulate an ingestion step: two pairs resolved through
        # a surface→memory-id map.
        tokens = _mk([
            ("outage", "B-REFERENT", 0.9),
            ("led", "B-CAUSAL_ALTLEX", 0.9),
            ("to", "I-CAUSAL_ALTLEX", 0.9),
            ("rewrite", "B-REFERENT", 0.9),
        ])
        pairs = extract_causal_pairs(tokens)
        lookup = {"outage": "m_out1", "rewrite": "m_rw1"}
        edges = pairs_to_causal_edges(pairs, surface_to_memory_id=lookup)
        assert len(edges) == 1
        e = edges[0]
        assert e.src == "m_rw1"      # effect
        assert e.dst == "m_out1"     # cause
        assert e.edge_type == "caused_by"
        assert e.cue_type == "CAUSAL_ALTLEX"

    def test_missing_resolution_drops_edge(self) -> None:
        tokens = _mk([
            ("outage", "B-REFERENT", 0.9),
            ("led", "B-CAUSAL_ALTLEX", 0.9),
            ("to", "I-CAUSAL_ALTLEX", 0.9),
            ("rewrite", "B-REFERENT", 0.9),
        ])
        pairs = extract_causal_pairs(tokens)
        # Only one surface in the map — can't form a complete edge.
        lookup = {"outage": "m_out1"}
        edges = pairs_to_causal_edges(pairs, surface_to_memory_id=lookup)
        assert edges == []

    def test_self_loop_skipped(self) -> None:
        # If both surfaces resolve to the same memory id (shouldn't
        # happen on a well-formed catalog, but guard defensively),
        # the resolver drops the edge.
        tokens = _mk([
            ("X", "B-REFERENT", 0.9),
            ("led", "B-CAUSAL_ALTLEX", 0.9),
            ("to", "I-CAUSAL_ALTLEX", 0.9),
            ("Y", "B-REFERENT", 0.9),
        ])
        pairs = extract_causal_pairs(tokens)
        lookup = {"x": "m_same", "y": "m_same"}
        edges = pairs_to_causal_edges(pairs, surface_to_memory_id=lookup)
        assert edges == []


class TestEmptyInput:
    def test_empty_tokens(self) -> None:
        assert extract_causal_pairs([]) == []

    def test_all_o_tokens(self) -> None:
        tokens = _mk([
            ("The", "O", 1.0),
            ("cat", "O", 1.0),
            ("sat", "O", 1.0),
        ])
        assert extract_causal_pairs(tokens) == []
