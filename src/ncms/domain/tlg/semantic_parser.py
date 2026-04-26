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
    "state_at",
    "before_named",
    "after_named",
    "between",
    "concurrent_with",
    "during_interval",
    "predecessor",
    # causal
    "cause_of",
    "effect_of",
    "chain_cause_of",
    "trigger_of",
    "contributing_factor",
    # ordinal
    "first",
    "last",
    "nth",
    # modal
    "would_be_current_if",
    "could_have_been",
    # state
    "current",
    "retired",
    "declared",
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
            self.temporal_before
            or self.temporal_after
            or self.temporal_during
            or self.temporal_since
        )

    @property
    def has_ordinal(self) -> bool:
        return bool(self.ordinal_first or self.ordinal_last or self.ordinal_nth)


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


@dataclass(frozen=True)
class _RuleCtx:
    cues: object  # _CueIndex
    referent: str | None
    subject: str | None
    scope: str | None
    temporal_anchor: str | None


def _rule_modal(c: _RuleCtx) -> TLGQuery | None:
    if not c.cues.modal_hypothetical:  # type: ignore[attr-defined]
        return None
    scenario = f"preserve_{c.referent}" if c.referent else "skip_most_recent_supersession"
    return TLGQuery(
        axis="modal",
        relation="would_be_current_if",
        referent=c.referent,
        subject=c.subject,
        scope=c.scope,
        scenario=scenario,
        confidence=0.85,
        matched_rule="modal_counterfactual",
    )


def _rule_causal(c: _RuleCtx) -> TLGQuery | None:
    if not c.cues.has_causal:  # type: ignore[attr-defined]
        return None
    altlex = c.cues.causal_altlex  # type: ignore[attr-defined]
    is_chain = any(len(span) >= 2 for span in altlex) or any(
        "chain" in _span_canonical(span) for span in altlex
    )
    if is_chain:
        return TLGQuery(
            axis="causal",
            relation="chain_cause_of",
            referent=c.referent,
            subject=c.subject,
            depth=2,
            confidence=0.82,
            matched_rule="causal_chain",
        )
    return TLGQuery(
        axis="causal",
        relation="cause_of",
        referent=c.referent,
        subject=c.subject,
        depth=1,
        confidence=0.88,
        matched_rule="causal_direct",
    )


def _rule_temporal_before(c: _RuleCtx) -> TLGQuery | None:
    if not (c.cues.temporal_before and c.cues.referent):  # type: ignore[attr-defined]
        return None
    return TLGQuery(
        axis="temporal",
        relation="before_named",
        referent=c.referent,
        subject=c.subject,
        scope=c.scope,
        temporal_anchor=c.temporal_anchor,
        confidence=0.9,
        matched_rule="temporal_before_named",
    )


def _rule_temporal_after(c: _RuleCtx) -> TLGQuery | None:
    if not (c.cues.temporal_after and c.cues.referent):  # type: ignore[attr-defined]
        return None
    return TLGQuery(
        axis="temporal",
        relation="after_named",
        referent=c.referent,
        subject=c.subject,
        scope=c.scope,
        temporal_anchor=c.temporal_anchor,
        confidence=0.88,
        matched_rule="temporal_after_named",
    )


def _rule_temporal_during(c: _RuleCtx) -> TLGQuery | None:
    cues = c.cues
    bare_anchor = (
        cues.temporal_anchor  # type: ignore[attr-defined]
        and not cues.has_ordinal  # type: ignore[attr-defined]
        and not cues.temporal_since  # type: ignore[attr-defined]
    )
    if not (cues.temporal_during or bare_anchor):  # type: ignore[attr-defined]
        return None
    return TLGQuery(
        axis="temporal",
        relation="during_interval",
        subject=c.subject,
        scope=c.scope,
        temporal_anchor=c.temporal_anchor,
        confidence=0.82,
        matched_rule="temporal_during",
    )


def _rule_temporal_since(c: _RuleCtx) -> TLGQuery | None:
    if not c.cues.temporal_since:  # type: ignore[attr-defined]
        return None
    return TLGQuery(
        axis="temporal",
        relation="state_at",
        subject=c.subject,
        scope=c.scope,
        referent=c.referent,
        temporal_anchor=c.temporal_anchor,
        confidence=0.78,
        matched_rule="temporal_since",
    )


def _rule_state_current(c: _RuleCtx) -> TLGQuery | None:
    cues = c.cues
    if not (cues.ask_current or (cues.ordinal_last and cues.scope)):  # type: ignore[attr-defined]
        return None
    return TLGQuery(
        axis="state",
        relation="current",
        subject=c.subject,
        scope=c.scope,
        referent=c.referent,
        confidence=0.9,
        matched_rule="state_current",
    )


def _rule_ordinal_first(c: _RuleCtx) -> TLGQuery | None:
    if not c.cues.ordinal_first:  # type: ignore[attr-defined]
        return None
    return TLGQuery(
        axis="ordinal",
        relation="first",
        subject=c.subject,
        scope=c.scope,
        referent=c.referent,
        confidence=0.85,
        matched_rule="ordinal_first",
    )


def _rule_ordinal_last(c: _RuleCtx) -> TLGQuery | None:
    if not c.cues.ordinal_last:  # type: ignore[attr-defined]
        return None
    return TLGQuery(
        axis="ordinal",
        relation="last",
        subject=c.subject,
        scope=c.scope,
        referent=c.referent,
        confidence=0.85,
        matched_rule="ordinal_last",
    )


def _rule_ordinal_nth(c: _RuleCtx) -> TLGQuery | None:
    if not c.cues.ordinal_nth:  # type: ignore[attr-defined]
        return None
    return TLGQuery(
        axis="ordinal",
        relation="nth",
        subject=c.subject,
        scope=c.scope,
        referent=c.referent,
        scenario=_span_canonical(c.cues.ordinal_nth[0]),  # type: ignore[attr-defined]
        confidence=0.75,
        matched_rule="ordinal_nth",
    )


def _rule_ask_change(c: _RuleCtx) -> TLGQuery | None:
    if not c.cues.ask_change:  # type: ignore[attr-defined]
        return None
    return TLGQuery(
        axis="state",
        relation="retired" if c.cues.temporal_before else "declared",  # type: ignore[attr-defined]
        subject=c.subject,
        scope=c.scope,
        referent=c.referent,
        confidence=0.7,
        matched_rule="ask_change",
    )


def _rule_state_bare_referent(c: _RuleCtx) -> TLGQuery | None:
    if not (c.cues.referent and c.cues.scope):  # type: ignore[attr-defined]
        return None
    return TLGQuery(
        axis="state",
        relation="state_at",
        subject=c.subject,
        scope=c.scope,
        referent=c.referent,
        confidence=0.6,
        matched_rule="state_bare_referent",
    )


# Specificity-first ordering: modal trumps temporal, causal trumps
# ordinal, explicit cues trump implicit.  First match wins.
_RULES: tuple = (
    _rule_modal,
    _rule_causal,
    _rule_temporal_before,
    _rule_temporal_after,
    _rule_temporal_during,
    _rule_temporal_since,
    _rule_state_current,
    _rule_ordinal_first,
    _rule_ordinal_last,
    _rule_ordinal_nth,
    _rule_ask_change,
    _rule_state_bare_referent,
)


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
    ctx = _RuleCtx(
        cues=cues,
        referent=_span_canonical(cues.referent[0]) if cues.referent else None,
        subject=_span_canonical(cues.subject[0]) if cues.subject else None,
        scope=_span_canonical(cues.scope[0]) if cues.scope else None,
        temporal_anchor=(
            _span_canonical(cues.temporal_anchor[0]) if cues.temporal_anchor else None
        ),
    )
    for rule in _RULES:
        result = rule(ctx)
        if result is not None:
            return result
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
