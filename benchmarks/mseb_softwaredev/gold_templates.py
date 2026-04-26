"""Gold query templates for MSEB-SoftwareDev (ADR / RFC / post-mortem).

ADRs are the canonical TLG content — state changes are declared in
prose with explicit vocabulary ("Decision", "Supersedes", "Status",
"Deprecated by", "Alternatives Considered").  Templates exploit
that vocabulary directly: each query contains distinguishing words
from the gold section, so BM25 has a fair first shot AND TLG's
retirement / intent-classification heads have something useful to
fire on.

Distinguishing section vocabulary:

| source section        | distinguishing vocab             |
|-----------------------|----------------------------------|
| Context / Background  | "context", "background", "problem"|
| Decision              | "decision", "chose", "adopted"   |
| Rationale / Drivers   | "rationale", "drivers", "because"|
| Alternatives          | "alternatives", "considered"      |
| Status                | "status", "accepted", "superseded"|
| Consequences          | "consequences", "trade-off"       |
| Implementation        | "implementation", "rollout"       |
| Conclusion            | "conclusion", "summary"          |
"""

from __future__ import annotations

TEMPLATES: dict[str, list[dict[str, object]]] = {
    # -----------------------------------------------------------------
    # current_state — gold = Decision section (the current state of the chain)
    # -----------------------------------------------------------------
    "current_state": [
        {
            "text_template": "What decision was adopted in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["adr_section"],
        },
    ],
    # -----------------------------------------------------------------
    # origin — gold = Context / preamble
    # -----------------------------------------------------------------
    "origin": [
        {
            "text_template": "What background or problem motivated the decision in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["adr_section"],
        },
    ],
    # -----------------------------------------------------------------
    # ordinal_first — gold = first section (title/preamble, labeled ordinal_anchor)
    # -----------------------------------------------------------------
    "ordinal_first": [
        {
            "text_template": "What introduction opens the decision record: {title}?",
            "gold_kind": "ordinal_anchor",
        },
    ],
    # -----------------------------------------------------------------
    # ordinal_last — gold = Conclusion / Summary
    # -----------------------------------------------------------------
    "ordinal_last": [
        {
            "text_template": "What closing summary does the decision record provide for: {title}?",
            "gold_kind": "ordinal_anchor",
        },
    ],
    # -----------------------------------------------------------------
    # sequence — gold = first section
    # -----------------------------------------------------------------
    "sequence": [
        {
            "text_template": "Trace the ADR from context through decision for: {title}.",
            "gold_kind": "ordinal_anchor",
        },
    ],
    # -----------------------------------------------------------------
    # predecessor — gold = Alternatives Considered section
    # -----------------------------------------------------------------
    "predecessor": [
        {
            "text_template": (
                "What alternatives were considered before the final choice in: {title}?"
            ),
            "gold_kind": "retirement",
        },
    ],
    # -----------------------------------------------------------------
    # transitive_cause — gold = Rationale / Factors / Drivers
    # -----------------------------------------------------------------
    "transitive_cause": [
        {
            "text_template": "What rationale or decision drivers justified the choice in: {title}?",
            "gold_kind": "causal_link",
        },
    ],
    # -----------------------------------------------------------------
    # causal_chain — gold = Rationale / Factors
    # -----------------------------------------------------------------
    "causal_chain": [
        {
            "text_template": "Explain the chain of factors that led to the decision in: {title}.",
            "gold_kind": "causal_link",
        },
    ],
    # -----------------------------------------------------------------
    # concurrent — gold = Consequences (discussed alongside the decision)
    # -----------------------------------------------------------------
    "concurrent": [
        {
            "text_template": (
                "What consequences were anticipated alongside adopting the decision in: {title}?"
            ),
            "gold_kind": "declaration",
        },
    ],
    # -----------------------------------------------------------------
    # before_named — gold = Context (state before the decision)
    # -----------------------------------------------------------------
    "before_named": [
        {
            "text_template": "What problem existed before the decision was made in: {title}?",
            "gold_kind": "declaration",
        },
    ],
    # -----------------------------------------------------------------
    # retirement — gold = Alternatives / Deprecates / Supersedes section
    # -----------------------------------------------------------------
    "retirement": [
        {
            "text_template": "Which alternatives were retired by the decision in: {title}?",
            "gold_kind": "retirement",
        },
    ],
    # -----------------------------------------------------------------
    # noise — off-topic / unrelated decisions
    # -----------------------------------------------------------------
    "noise": [
        {"text_template": "What is the airspeed velocity of an unladen swallow?"},
        {"text_template": "How do I season a cast iron skillet properly?"},
        {"text_template": "What were the casualties of the Battle of Trafalgar?"},
        {"text_template": "How does quantum entanglement work?"},
        {"text_template": "What's the recipe for authentic Neapolitan pizza?"},
    ],
}


__all__ = ["TEMPLATES"]
