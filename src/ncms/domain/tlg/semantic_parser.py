"""CTLG compositional synthesizer — cue tags → TLGQuery logical form.

Pure-function rule engine that composes :class:`TaggedToken` sequences
produced by the dedicated CTLG cue tagger into a structured
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

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

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
    secondary
        Optional second anchor for binary temporal relations such as
        before/after/between comparisons.
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
    secondary: str | None = None
    subject: str | None = None
    scope: str | None = None
    depth: int = 1
    scenario: str | None = None
    temporal_anchor: str | None = None
    confidence: float = 0.0
    matched_rule: str = ""


@dataclass(frozen=True)
class SLMQuerySignals:
    """Query-side output from the 5-head SLM used to ground CTLG.

    The CTLG cue tagger owns relation detection.  The five-head SLM
    contributes typed entity grounding (``slots`` / ``role_spans``),
    topic, intent, and state-change hints.  Keeping this as a small
    domain dataclass avoids importing the Pydantic ``ExtractedLabel``
    boundary into the grammar module.
    """

    intent: str | None = None
    topic: str | None = None
    state_change: str | None = None
    slots: Mapping[str, str] = field(default_factory=dict)
    role_spans: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_label(cls, label: object | None) -> SLMQuerySignals | None:
        if label is None:
            return None
        role_spans = tuple(
            span
            for span in (getattr(label, "role_spans", None) or ())
            if isinstance(span, Mapping)
        )
        return cls(
            intent=getattr(label, "intent", None),
            topic=getattr(label, "topic", None),
            state_change=getattr(label, "state_change", None),
            slots=dict(getattr(label, "slots", None) or {}),
            role_spans=role_spans,
        )


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
    surfaces: tuple[str, ...]
    referent: str | None
    secondary: str | None
    subject: str | None
    scope: str | None
    temporal_anchor: str | None
    slm: SLMQuerySignals | None = None

    @property
    def surface_text(self) -> str:
        return " ".join(self.surfaces).lower()


def _slm_grounding(signals: SLMQuerySignals | None) -> tuple[str | None, str | None, str | None]:
    """Return ``(referent, secondary, scope)`` from query-side SLM signals."""
    if signals is None:
        return None, None, None

    primary: list[tuple[str | None, str]] = []
    alternatives: list[str] = []
    for span in signals.role_spans:
        canonical = str(span.get("canonical") or span.get("surface") or "").strip().lower()
        if not canonical:
            continue
        slot = str(span.get("slot") or "").strip().lower() or None
        role = str(span.get("role") or "").strip().lower()
        if role == "alternative":
            alternatives.append(canonical)
        elif role == "primary":
            primary.append((slot, canonical))

    slots = {str(k): str(v).strip().lower() for k, v in signals.slots.items() if v}
    for slot, value in slots.items():
        if slot == "alternative":
            if value:
                alternatives.append(value)
        elif value and value not in {v for _, v in primary}:
            primary.append((slot, value))

    referent = primary[0][1] if primary else None
    secondary = alternatives[0] if alternatives else (primary[1][1] if len(primary) > 1 else None)
    scope = next((slot for slot, _ in primary if slot and slot != "object"), None)
    return referent, secondary, scope


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
            secondary=c.secondary,
            subject=c.subject,
            depth=2,
            confidence=0.82,
            matched_rule="causal_chain",
        )
    return TLGQuery(
        axis="causal",
        relation="cause_of",
        referent=c.referent,
        secondary=c.secondary,
        subject=c.subject,
        depth=1,
        confidence=0.88,
        matched_rule="causal_direct",
    )


def _rule_temporal_before(c: _RuleCtx) -> TLGQuery | None:
    if not (c.cues.temporal_before and c.referent):  # type: ignore[attr-defined]
        return None
    relation: TLGRelation = "before_named" if c.secondary else "predecessor"
    return TLGQuery(
        axis="temporal",
        relation=relation,
        referent=c.referent,
        secondary=c.secondary,
        subject=c.subject,
        scope=c.scope,
        temporal_anchor=c.temporal_anchor,
        confidence=0.9,
        matched_rule="temporal_before_named" if c.secondary else "temporal_predecessor",
    )


def _rule_trace_from_start(c: _RuleCtx) -> TLGQuery | None:
    if not c.cues.temporal_before:  # type: ignore[attr-defined]
        return None
    text = c.surface_text
    if not ("trace" in text and "from" in text and ("through" in text or "to" in text)):
        return None
    return TLGQuery(
        axis="ordinal",
        relation="first",
        subject=c.subject,
        scope=c.scope,
        referent=c.referent,
        secondary=c.secondary,
        confidence=0.78,
        matched_rule="trace_from_start",
    )


def _rule_alternatives_section(c: _RuleCtx) -> TLGQuery | None:
    if c.referent:
        return None
    text = c.surface_text
    asks_alternatives = (
        "alternative" in text
        or "alternatives" in text
        or "considered" in text
        or "current choice" in text
    )
    if not asks_alternatives:
        return None
    if not (
        c.cues.temporal_before  # type: ignore[attr-defined]
        or "retired" in text
        or "retire" in text
        or "replaced" in text
        or "deprecated" in text
    ):
        return None
    return TLGQuery(
        axis="state",
        relation="retired",
        subject=c.subject,
        scope=c.scope or "alternatives",
        referent=c.referent,
        secondary=c.secondary,
        confidence=0.76,
        matched_rule="alternatives_section",
    )


def _rule_temporal_after(c: _RuleCtx) -> TLGQuery | None:
    if not (c.cues.temporal_after and c.referent):  # type: ignore[attr-defined]
        return None
    return TLGQuery(
        axis="temporal",
        relation="after_named",
        referent=c.referent,
        secondary=c.secondary,
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
    if c.referent and not c.temporal_anchor:
        return TLGQuery(
            axis="temporal",
            relation="concurrent_with",
            referent=c.referent,
            secondary=c.secondary,
            subject=c.subject,
            scope=c.scope,
            confidence=0.84,
            matched_rule="temporal_concurrent",
        )
    return TLGQuery(
        axis="temporal",
        relation="during_interval",
        referent=c.referent,
        secondary=c.secondary,
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
        secondary=c.secondary,
        temporal_anchor=c.temporal_anchor,
        confidence=0.78,
        matched_rule="temporal_since",
    )


_BARE_ASK_CURRENT_SURFACES = frozenset(
    {"what", "which", "who", "where", "when", "why", "how", "most"}
)


def _has_meaningful_ask_current(cues: object) -> bool:
    return any(
        _span_canonical(span) not in _BARE_ASK_CURRENT_SURFACES
        for span in cues.ask_current  # type: ignore[attr-defined]
    )


def _rule_state_current(c: _RuleCtx) -> TLGQuery | None:
    cues = c.cues
    if not cues.ask_current:  # type: ignore[attr-defined]
        return None
    if not (c.subject or c.scope or c.referent or _has_meaningful_ask_current(cues)):
        return None
    return TLGQuery(
        axis="state",
        relation="current",
        subject=c.subject,
        scope=c.scope,
        referent=c.referent,
        secondary=c.secondary,
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
        secondary=c.secondary,
        temporal_anchor=c.temporal_anchor,
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
        secondary=c.secondary,
        temporal_anchor=c.temporal_anchor,
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
        secondary=c.secondary,
        temporal_anchor=c.temporal_anchor,
        scenario=_span_canonical(c.cues.ordinal_nth[0]),  # type: ignore[attr-defined]
        confidence=0.75,
        matched_rule="ordinal_nth",
    )


def _rule_ask_change(c: _RuleCtx) -> TLGQuery | None:
    if not c.cues.ask_change:  # type: ignore[attr-defined]
        return None
    state_change = c.slm.state_change if c.slm is not None else None
    asks_retirement = any(
        surface in {"retired", "retire", "removed", "replaced", "deprecated"}
        or "retired" in surface
        or "removed" in surface
        or "replaced" in surface
        or "deprecated" in surface
        for surface in (_span_canonical(span) for span in c.cues.ask_change)  # type: ignore[attr-defined]
    )
    return TLGQuery(
        axis="state",
        relation=(
            "retired"
            if c.cues.temporal_before  # type: ignore[attr-defined]
            or state_change == "retirement"
            or asks_retirement
            else "declared"
        ),
        subject=c.subject,
        scope=c.scope,
        referent=c.referent,
        secondary=c.secondary,
        temporal_anchor=c.temporal_anchor,
        confidence=0.7,
        matched_rule="ask_change",
    )


def _rule_state_bare_referent(c: _RuleCtx) -> TLGQuery | None:
    if not (c.referent and c.scope):
        return None
    return TLGQuery(
        axis="state",
        relation="state_at",
        subject=c.subject,
        scope=c.scope,
        referent=c.referent,
        secondary=c.secondary,
        confidence=0.6,
        matched_rule="state_bare_referent",
    )


# Target-first ordering: modal/counterfactual and explicit state-change
# questions are most specific; ASK_CURRENT controls current-state queries even
# when phrased with "latest"; ordinal target cues beat causal relative clauses
# such as "earliest concern that led to...".
_RULES: tuple = (
    _rule_modal,
    _rule_trace_from_start,
    _rule_alternatives_section,
    _rule_ask_change,
    _rule_state_current,
    _rule_ordinal_first,
    _rule_ordinal_last,
    _rule_ordinal_nth,
    _rule_causal,
    _rule_temporal_before,
    _rule_temporal_after,
    _rule_temporal_during,
    _rule_temporal_since,
    _rule_state_bare_referent,
)


def synthesize(
    tagged: list[TaggedToken] | tuple[TaggedToken, ...],
    *,
    slm_signals: SLMQuerySignals | None = None,
) -> TLGQuery | None:
    """Compose cue tags into a :class:`TLGQuery`.

    Returns ``None`` when no rule matches — the caller should fall
    back to LLM adjudication.  The return's ``matched_rule`` names
    which rule fired for explainability and testing.

    Rule ordering is target-first: modal and explicit state-change
    forms win first; ASK_CURRENT beats ordinal wording like "latest";
    ordinal target cues beat causal relative clauses.  When two rules
    could fire, the query target wins deterministically.
    """
    tagged_list = list(tagged)
    if not tagged_list:
        return None
    cues = _index_cues(tagged_list)
    slm_referent, slm_secondary, slm_scope = _slm_grounding(slm_signals)
    cue_referents = [_span_canonical(span) for span in cues.referent]
    ctx = _RuleCtx(
        cues=cues,
        surfaces=tuple(tok.surface for tok in tagged_list),
        referent=cue_referents[0] if cue_referents else slm_referent,
        secondary=(
            cue_referents[1]
            if len(cue_referents) > 1
            else slm_secondary
        ),
        subject=_span_canonical(cues.subject[0]) if cues.subject else None,
        scope=_span_canonical(cues.scope[0]) if cues.scope else slm_scope,
        temporal_anchor=(
            _span_canonical(cues.temporal_anchor[0]) if cues.temporal_anchor else None
        ),
        slm=slm_signals,
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
    "SLMQuerySignals",
    "synthesize",
]
