"""CTLG compositional synthesizer — cue tags → TLGQuery logical form.

Pure-function rule engine that composes :class:`TaggedToken` sequences
produced by the CTLG 6th head (``shape_cue_head``) into a structured
:class:`TLGQuery` that the TLG dispatcher consumes directly.  When no
rule matches, :func:`synthesize` returns ``None`` and the caller falls
back to LLM adjudication.

This is the grammar-guided piece of the pivot: the classification
``shape_intent`` enum in v6/v7.x is replaced by compositional
rule-matching over typed cue spans.  Rules are explicit, deterministic,
unit-testable, and explainable — every match emits a named rule id in
the TLGQuery trace.

See:

* :doc:`../../../docs/research/ctlg-design.md` §2.2 — TLGQuery shape
* :doc:`../../../docs/research/ctlg-grammar.md` §3.3 — target → trajectory
* :doc:`../../../docs/research/ctlg-cue-guidelines.md` — cue vocabulary
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ncms.domain.tlg.cue_taxonomy import (
    TaggedToken,
    group_bio_spans,
)

# ---------------------------------------------------------------------------
# TLGQuery — structured query logical form
# ---------------------------------------------------------------------------


#: Five query axes.  Temporal/causal/ordinal/modal/state per
#: ctlg-design.md §2.2.
TLGAxis = Literal["temporal", "causal", "ordinal", "modal", "state"]

#: Concrete relations within each axis.  Some cross-axis (e.g.
#: ``cause_of`` is causal; ``first`` is ordinal).  The axis in the
#: TLGQuery disambiguates.
TLGRelation = Literal[
    # temporal
    "state_at", "before_named", "after_named", "between",
    "concurrent_with", "during_interval",
    # causal
    "cause_of", "effect_of", "chain_cause_of", "trigger_of",
    "contributing_factor",
    # ordinal
    "first", "last", "nth",
    # modal
    "would_be_current_if", "could_have_been",
    # state
    "current", "retired", "declared",
]


@dataclass(frozen=True)
class TLGQuery:
    """Structured query produced by the CTLG semantic parser.

    Replaces the flat ``shape_intent: Literal[12 + none]`` enum with
    a compositional logical form that the TLG dispatcher consumes
    directly.  Every field is optional; the dispatcher reads only
    the fields relevant to the target relation.

    Attributes
    ----------
    axis
        Query axis — temporal / causal / ordinal / modal / state.
    relation
        Concrete relation within the axis.
    referent
        Named catalog entity anchor (e.g. ``"postgres"``).
    subject
        Subject whose state evolves (e.g. ``"auth-service"``).
    scope
        Catalog slot the query is asking about (``"database"`` / ...).
    depth
        Chain depth — 1 for direct, ≥2 for transitive.
    scenario
        Counterfactual branch id — ``None`` for actual history.
    temporal_anchor
        Extracted date / named period ("2023", "Q2", "the CRDB era").
    confidence
        Synthesizer's own confidence in the TLGQuery in [0, 1].
        Distinct from per-head confidences in :class:`ExtractedLabel`.
    matched_rule
        Name of the synthesizer rule that fired (for tracing).
    """

    axis: TLGAxis
    relation: TLGRelation
    referent: str | None = None
    subject: str | None = None
    scope: str | None = None
    depth: int = 1
    scenario: str | None = None
    temporal_anchor: str | None = None
    confidence: float = 0.0
    matched_rule: str = ""


# ---------------------------------------------------------------------------
# Cue index helpers
# ---------------------------------------------------------------------------


@dataclass
class _CueIndex:
    """Spans grouped by cue family — shorthand for rule bodies."""

    causal_explicit: list[list[TaggedToken]] = field(default_factory=list)
    causal_altlex: list[list[TaggedToken]] = field(default_factory=list)
    temporal_before: list[list[TaggedToken]] = field(default_factory=list)
    temporal_after: list[list[TaggedToken]] = field(default_factory=list)
    temporal_during: list[list[TaggedToken]] = field(default_factory=list)
    temporal_since: list[list[TaggedToken]] = field(default_factory=list)
    temporal_anchor: list[list[TaggedToken]] = field(default_factory=list)
    ordinal_first: list[list[TaggedToken]] = field(default_factory=list)
    ordinal_last: list[list[TaggedToken]] = field(default_factory=list)
    ordinal_nth: list[list[TaggedToken]] = field(default_factory=list)
    modal_hypothetical: list[list[TaggedToken]] = field(default_factory=list)
    ask_change: list[list[TaggedToken]] = field(default_factory=list)
    ask_current: list[list[TaggedToken]] = field(default_factory=list)
    referent: list[list[TaggedToken]] = field(default_factory=list)
    subject: list[list[TaggedToken]] = field(default_factory=list)
    scope: list[list[TaggedToken]] = field(default_factory=list)

    @property
    def has_causal(self) -> bool:
        return bool(self.causal_explicit or self.causal_altlex)

    @property
    def has_temporal(self) -> bool:
        return bool(
            self.temporal_before or self.temporal_after
            or self.temporal_during or self.temporal_since
        )

    @property
    def has_ordinal(self) -> bool:
        return bool(
            self.ordinal_first or self.ordinal_last or self.ordinal_nth
        )


_CUE_TYPE_TO_ATTR: dict[str, str] = {
    "CAUSAL_EXPLICIT": "causal_explicit",
    "CAUSAL_ALTLEX": "causal_altlex",
    "TEMPORAL_BEFORE": "temporal_before",
    "TEMPORAL_AFTER": "temporal_after",
    "TEMPORAL_DURING": "temporal_during",
    "TEMPORAL_SINCE": "temporal_since",
    "TEMPORAL_ANCHOR": "temporal_anchor",
    "ORDINAL_FIRST": "ordinal_first",
    "ORDINAL_LAST": "ordinal_last",
    "ORDINAL_NTH": "ordinal_nth",
    "MODAL_HYPOTHETICAL": "modal_hypothetical",
    "ASK_CHANGE": "ask_change",
    "ASK_CURRENT": "ask_current",
    "REFERENT": "referent",
    "SUBJECT": "subject",
    "SCOPE": "scope",
}


def _index_cues(tagged: list[TaggedToken] | tuple[TaggedToken, ...]) -> _CueIndex:
    """Group BIO spans into a cue-family index."""
    index = _CueIndex()
    spans = group_bio_spans(list(tagged))
    for cue_type, tokens in spans:
        attr = _CUE_TYPE_TO_ATTR.get(cue_type)
        if attr is None:
            continue
        getattr(index, attr).append(tokens)
    return index


def _span_canonical(tokens: list[TaggedToken]) -> str:
    """Best-effort canonical lowercase form of a span's surface."""
    return " ".join(t.surface for t in tokens).lower().strip()


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------


def synthesize(
    tagged: list[TaggedToken] | tuple[TaggedToken, ...],
) -> TLGQuery | None:
    """Compose cue tags into a :class:`TLGQuery`.

    Returns ``None`` when no rule matches — the caller should fall
    back to LLM adjudication.  The return's ``matched_rule`` names
    which rule fired for explainability and testing.

    Rule ordering is specificity-first: modal cues trump temporal,
    causal trumps ordinal, explicit cues trump implicit.  When two
    rules could fire, the more specific one wins deterministically.
    """
    tagged_list = list(tagged)
    if not tagged_list:
        return None

    cues = _index_cues(tagged_list)

    # Canonical referent / subject / scope extraction — used by many
    # rules below.  First-occurrence wins for each.
    referent_canon = (
        _span_canonical(cues.referent[0]) if cues.referent else None
    )
    subject_canon = (
        _span_canonical(cues.subject[0]) if cues.subject else None
    )
    scope_canon = (
        _span_canonical(cues.scope[0]) if cues.scope else None
    )
    temporal_anchor_canon = (
        _span_canonical(cues.temporal_anchor[0])
        if cues.temporal_anchor else None
    )

    # ── Rule 1: MODAL_HYPOTHETICAL → counterfactual (highest specificity) ──
    if cues.modal_hypothetical:
        # scenario defaults to "skip_most_recent_supersession" when
        # the referent is the thing being preserved; the dispatcher
        # resolves the actual edge-skip at walk time.
        scenario = (
            f"preserve_{referent_canon}" if referent_canon
            else "skip_most_recent_supersession"
        )
        return TLGQuery(
            axis="modal",
            relation="would_be_current_if",
            referent=referent_canon,
            subject=subject_canon,
            scope=scope_canon,
            scenario=scenario,
            confidence=0.85,
            matched_rule="modal_counterfactual",
        )

    # ── Rule 2: CAUSAL + REFERENT → cause_of / chain_cause_of ──────────────
    if cues.has_causal:
        # Depth: multi-word CAUSAL_ALTLEX spans ("the chain of
        # factors", "what led to") suggest a chain, so prefer
        # ``chain_cause_of`` with depth=2+ when the altlex span is
        # ≥2 tokens.  Explicit single-word causals stay depth=1.
        is_chain = any(
            len(span) >= 2 for span in cues.causal_altlex
        ) or any(
            "chain" in _span_canonical(span)
            for span in cues.causal_altlex
        )
        if is_chain:
            return TLGQuery(
                axis="causal",
                relation="chain_cause_of",
                referent=referent_canon,
                subject=subject_canon,
                depth=2,
                confidence=0.82,
                matched_rule="causal_chain",
            )
        return TLGQuery(
            axis="causal",
            relation="cause_of",
            referent=referent_canon,
            subject=subject_canon,
            depth=1,
            confidence=0.88,
            matched_rule="causal_direct",
        )

    # ── Rule 3: TEMPORAL_BEFORE + REFERENT → before_named ─────────────────
    if cues.temporal_before and cues.referent:
        return TLGQuery(
            axis="temporal",
            relation="before_named",
            referent=referent_canon,
            subject=subject_canon,
            scope=scope_canon,
            temporal_anchor=temporal_anchor_canon,
            confidence=0.9,
            matched_rule="temporal_before_named",
        )

    # ── Rule 4: TEMPORAL_AFTER + REFERENT → after_named ───────────────────
    if cues.temporal_after and cues.referent:
        return TLGQuery(
            axis="temporal",
            relation="after_named",
            referent=referent_canon,
            subject=subject_canon,
            scope=scope_canon,
            temporal_anchor=temporal_anchor_canon,
            confidence=0.88,
            matched_rule="temporal_after_named",
        )

    # ── Rule 5: TEMPORAL_DURING + TEMPORAL_ANCHOR → during_interval ──────
    if cues.temporal_during or (
        cues.temporal_anchor and not cues.has_ordinal and not cues.temporal_since
    ):
        return TLGQuery(
            axis="temporal",
            relation="during_interval",
            subject=subject_canon,
            scope=scope_canon,
            temporal_anchor=temporal_anchor_canon,
            confidence=0.82,
            matched_rule="temporal_during",
        )

    # ── Rule 5b: TEMPORAL_SINCE → state_at (current state with anchor) ───
    # "Since Q2 we've used X" / "as of the last release, what's our Y" —
    # semantically equivalent to "current state given a start anchor".
    # If there's also a REFERENT or SCOPE, prefer that context.
    if cues.temporal_since:
        return TLGQuery(
            axis="temporal",
            relation="state_at",
            subject=subject_canon,
            scope=scope_canon,
            referent=referent_canon,
            temporal_anchor=temporal_anchor_canon,
            confidence=0.78,
            matched_rule="temporal_since",
        )

    # ── Rule 6: ASK_CURRENT + SCOPE → current state ──────────────────────
    if cues.ask_current or (cues.ordinal_last and cues.scope):
        return TLGQuery(
            axis="state",
            relation="current",
            subject=subject_canon,
            scope=scope_canon,
            referent=referent_canon,
            confidence=0.9,
            matched_rule="state_current",
        )

    # ── Rule 7: ORDINAL_FIRST → first / origin ───────────────────────────
    if cues.ordinal_first:
        return TLGQuery(
            axis="ordinal",
            relation="first",
            subject=subject_canon,
            scope=scope_canon,
            referent=referent_canon,
            confidence=0.85,
            matched_rule="ordinal_first",
        )

    # ── Rule 8: ORDINAL_LAST without ASK_CURRENT → last ──────────────────
    if cues.ordinal_last:
        return TLGQuery(
            axis="ordinal",
            relation="last",
            subject=subject_canon,
            scope=scope_canon,
            referent=referent_canon,
            confidence=0.85,
            matched_rule="ordinal_last",
        )

    # ── Rule 9: ORDINAL_NTH → nth (depth=N) ──────────────────────────────
    if cues.ordinal_nth:
        # Depth is stored in scenario field for now — a future rule
        # might parse "2nd"/"third" into a concrete depth int.
        return TLGQuery(
            axis="ordinal",
            relation="nth",
            subject=subject_canon,
            scope=scope_canon,
            referent=referent_canon,
            scenario=_span_canonical(cues.ordinal_nth[0]),
            confidence=0.75,
            matched_rule="ordinal_nth",
        )

    # ── Rule 10: ASK_CHANGE → generic state transition query ─────────────
    if cues.ask_change:
        return TLGQuery(
            axis="state",
            relation="retired" if cues.temporal_before else "declared",
            subject=subject_canon,
            scope=scope_canon,
            referent=referent_canon,
            confidence=0.7,
            matched_rule="ask_change",
        )

    # ── Rule 11: REFERENT + SCOPE alone → state_at (looking up state) ────
    # A query like "What database was Postgres for auth-service?" —
    # minimal cues, default to a state lookup.
    if cues.referent and cues.scope:
        return TLGQuery(
            axis="state",
            relation="state_at",
            subject=subject_canon,
            scope=scope_canon,
            referent=referent_canon,
            confidence=0.6,
            matched_rule="state_bare_referent",
        )

    # Fallback — no rule matched.  Caller falls to LLM.
    return None


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------


__all__ = [
    "TLGAxis",
    "TLGQuery",
    "TLGRelation",
    "synthesize",
]
