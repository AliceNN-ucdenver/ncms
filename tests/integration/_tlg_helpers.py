"""Shared helpers for the TLG integration tests.

The dispatcher changed its primary signal in v8.1 from the flat
``shape_intent`` string to a structured :class:`TLGQuery`.  Tests
that used to pass ``slm_shape_intent="current_state"`` now pass
``tlg_query=tlg_query_for("current_state")``.  This helper is the
compatibility shim that maps the retired shape-intent label space
onto the structured form so test bodies stay readable.

Production code does NOT import this — the production synthesizer
produces TLGQuery instances directly from cue_tags.
"""

from __future__ import annotations

from ncms.domain.tlg.semantic_parser import TLGQuery

#: Legacy shape_intent → TLGQuery mapping for tests.  Each entry
#: produces a minimal TLGQuery with no referent / subject — tests
#: that depend on referent extraction pass ``referent=...`` to
#: :func:`tlg_query_for` directly.
_SHAPE_TO_TLG: dict[str, TLGQuery] = {
    "current_state": TLGQuery(axis="state", relation="current"),
    "origin": TLGQuery(axis="ordinal", relation="first"),
    "retirement": TLGQuery(axis="state", relation="retired"),
    "sequence": TLGQuery(axis="temporal", relation="after_named"),
    "predecessor": TLGQuery(axis="temporal", relation="predecessor"),
    "before_named": TLGQuery(axis="temporal", relation="before_named"),
    "interval": TLGQuery(axis="temporal", relation="during_interval"),
    "transitive_cause": TLGQuery(axis="causal", relation="chain_cause_of"),
    "concurrent": TLGQuery(axis="temporal", relation="concurrent_with"),
    "cause_of": TLGQuery(axis="causal", relation="cause_of"),
}


def tlg_query_for(
    shape_intent: str,
    *,
    referent: str | None = None,
    subject: str | None = None,
) -> TLGQuery:
    """Build a TLGQuery from a legacy shape_intent label.

    Optional ``referent`` / ``subject`` fill the corresponding
    fields — test bodies that depended on the dispatcher
    extracting these from the query text should provide them
    explicitly for determinism.
    """
    base = _SHAPE_TO_TLG[shape_intent]
    if referent is None and subject is None:
        return base
    import dataclasses

    return dataclasses.replace(
        base,
        referent=referent or base.referent,
        subject=subject or base.subject,
    )


__all__ = ["tlg_query_for"]
