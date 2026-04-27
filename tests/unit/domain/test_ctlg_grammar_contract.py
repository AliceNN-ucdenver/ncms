"""Executable CTLG grammar contract.

These tests mirror ``docs/research/ctlg-grammar-contract.md``.  They are
deliberately pure: no adapter, no store, no search.  Their job is to keep
the cue-tag → TLGQuery semantic contract stable before we train another
CTLG adapter or judge shadow-mode retrieval lift.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ncms.domain.tlg.cue_taxonomy import TaggedToken
from ncms.domain.tlg.semantic_parser import SLMQuerySignals, synthesize


@dataclass(frozen=True)
class GrammarCase:
    shape: str
    cue_tags: tuple[tuple[str, str], ...]
    expected: tuple[str, str] | None
    referent: str | None = None
    secondary: str | None = None
    scope: str | None = None
    temporal_anchor: str | None = None
    slm: SLMQuerySignals | None = None


def _tokens(pairs: tuple[tuple[str, str], ...]) -> list[TaggedToken]:
    out: list[TaggedToken] = []
    pos = 0
    for surface, label in pairs:
        out.append(
            TaggedToken(
                char_start=pos,
                char_end=pos + len(surface),
                surface=surface,
                cue_label=label,
                confidence=0.99,
            )
        )
        pos += len(surface) + 1
    return out


CONTRACT_CASES: tuple[GrammarCase, ...] = (
    GrammarCase(
        shape="current_state",
        cue_tags=(
            ("current", "B-ASK_CURRENT"),
            ("database", "B-SCOPE"),
        ),
        expected=("state", "current"),
        scope="database",
    ),
    GrammarCase(
        shape="origin",
        cue_tags=(
            ("original", "B-ORDINAL_FIRST"),
            ("framework", "B-SCOPE"),
        ),
        expected=("ordinal", "first"),
        scope="framework",
    ),
    GrammarCase(
        shape="ordinal_first",
        cue_tags=(
            ("first", "B-ORDINAL_FIRST"),
            ("decision", "B-SCOPE"),
        ),
        expected=("ordinal", "first"),
        scope="decision",
    ),
    GrammarCase(
        shape="ordinal_last",
        cue_tags=(
            ("final", "B-ORDINAL_LAST"),
            ("decision", "B-SCOPE"),
        ),
        expected=("ordinal", "last"),
        scope="decision",
    ),
    GrammarCase(
        shape="sequence",
        cue_tags=(
            ("after", "B-TEMPORAL_AFTER"),
            ("OAuth", "B-REFERENT"),
        ),
        expected=("temporal", "after_named"),
        referent="oauth",
    ),
    GrammarCase(
        shape="predecessor",
        cue_tags=(
            ("before", "B-TEMPORAL_BEFORE"),
            ("Postgres", "B-REFERENT"),
        ),
        expected=("temporal", "predecessor"),
        referent="postgres",
    ),
    GrammarCase(
        shape="before_named",
        cue_tags=(
            ("OAuth", "B-REFERENT"),
            ("before", "B-TEMPORAL_BEFORE"),
            ("JWT", "B-REFERENT"),
        ),
        expected=("temporal", "before_named"),
        referent="oauth",
        secondary="jwt",
    ),
    GrammarCase(
        shape="range",
        cue_tags=(
            ("during", "B-TEMPORAL_DURING"),
            ("Q2 2024", "B-TEMPORAL_ANCHOR"),
        ),
        expected=("temporal", "during_interval"),
        temporal_anchor="q2 2024",
    ),
    GrammarCase(
        shape="concurrent",
        cue_tags=(
            ("during", "B-TEMPORAL_DURING"),
            ("OAuth", "B-REFERENT"),
        ),
        expected=("temporal", "concurrent_with"),
        referent="oauth",
    ),
    GrammarCase(
        shape="transitive_cause",
        cue_tags=(
            ("led", "B-CAUSAL_ALTLEX"),
            ("to", "I-CAUSAL_ALTLEX"),
            ("Postgres", "B-REFERENT"),
        ),
        expected=("causal", "chain_cause_of"),
        referent="postgres",
    ),
    GrammarCase(
        shape="causal_chain",
        cue_tags=(
            ("chain", "B-CAUSAL_ALTLEX"),
            ("causing", "I-CAUSAL_ALTLEX"),
            ("outage", "B-REFERENT"),
        ),
        expected=("causal", "chain_cause_of"),
        referent="outage",
    ),
    GrammarCase(
        shape="retirement",
        cue_tags=(
            ("changed", "B-ASK_CHANGE"),
            ("Postgres", "B-REFERENT"),
        ),
        expected=("state", "retired"),
        referent="postgres",
        slm=SLMQuerySignals(state_change="retirement"),
    ),
    GrammarCase(
        shape="noise",
        cue_tags=(("hello", "O"),),
        expected=None,
    ),
)


@pytest.mark.parametrize("case", CONTRACT_CASES, ids=[case.shape for case in CONTRACT_CASES])
def test_ctlg_shape_contract(case: GrammarCase) -> None:
    q = synthesize(_tokens(case.cue_tags), slm_signals=case.slm)

    if case.expected is None:
        assert q is None
        return

    assert q is not None
    assert (q.axis, q.relation) == case.expected
    assert q.referent == case.referent
    assert q.secondary == case.secondary
    assert q.scope == case.scope
    assert q.temporal_anchor == case.temporal_anchor


def test_causal_origin_wording_is_not_ordinal_origin() -> None:
    q = synthesize(
        _tokens(
            (
                ("motivated", "B-CAUSAL_ALTLEX"),
                ("decision", "B-REFERENT"),
            )
        )
    )

    assert q is not None
    assert (q.axis, q.relation) == ("causal", "cause_of")


def test_before_named_requires_two_anchors() -> None:
    q = synthesize(
        _tokens(
            (
                ("before", "B-TEMPORAL_BEFORE"),
                ("decision", "B-REFERENT"),
            )
        )
    )

    assert q is not None
    assert (q.axis, q.relation) == ("temporal", "predecessor")
