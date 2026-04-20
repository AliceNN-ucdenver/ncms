"""Gold query templates for MSEB-Clinical.

Anchors each query at a specific section of a PMC case report.
See ``benchmarks/mseb_swe/gold_templates.py`` for field docs.

Clinical narratives don't carry per-event timestamps, so the
``interval`` / ``range`` shapes are excluded by design — those
queries need real clock times (see `docs/p3-state-evolution-benchmark.md`
§3b).  All 12 supported shapes produce candidates against the
normal case-report arc:

  abstract → case presentation → investigations →
    differential diagnosis / initial diagnosis →
    management / treatment → outcome / follow-up →
    final diagnosis → discussion

Caveats documented in `benchmarks/mseb_clinical/README.md` §6:
- ~30 % of authors don't use the canonical "Differential
  Diagnosis" heading; reasoning lives in Discussion / Case
  Description.  Templates target `causal_link` memories labeled
  by the labeler's regex rather than raw heading names.
"""

from __future__ import annotations

TEMPLATES: dict[str, list[dict[str, object]]] = {
    # -----------------------------------------------------------------
    # current_state — gold = final diagnosis (what the patient ends up with)
    # -----------------------------------------------------------------
    "current_state": [
        {
            "text_template": "What is the final diagnosis for the patient described in: {title}?",
            "gold_kind": "retirement",
            "gold_source_filter": ["final diagnosis"],
        },
        {
            "text_template": "What is the current clinical picture following: {title}?",
            "gold_kind": "retirement",
        },
    ],

    # -----------------------------------------------------------------
    # origin — gold = abstract or presentation (how the patient presented)
    # -----------------------------------------------------------------
    "origin": [
        {
            "text_template": "How did the patient first present with: {title}?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["abstract"],
        },
        {
            "text_template": "What were the initial symptoms in the case described as: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["case presentation", "case report", "presentation"],
        },
    ],

    # -----------------------------------------------------------------
    # ordinal_first
    # -----------------------------------------------------------------
    "ordinal_first": [
        {
            "text_template": "What was the first recorded observation about this patient: {title}?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["abstract"],
        },
        {
            "text_template": "What was the presenting complaint in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["case presentation", "case report", "presentation", "history"],
        },
    ],

    # -----------------------------------------------------------------
    # ordinal_last — gold = conclusion or final diagnosis
    # -----------------------------------------------------------------
    "ordinal_last": [
        {
            "text_template": "What was the final outcome in the case: {title}?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["conclusion"],
        },
        {
            "text_template": "What was the last documented state of the patient in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["outcome", "follow-up"],
        },
    ],

    # -----------------------------------------------------------------
    # sequence — gold = case presentation (start of diagnostic arc)
    # -----------------------------------------------------------------
    "sequence": [
        {
            "text_template": "Trace the diagnostic sequence starting with: {title}",
            "gold_kind": "declaration",
            "gold_source_filter": ["case presentation", "case report", "history"],
        },
    ],

    # -----------------------------------------------------------------
    # predecessor — gold = initial_diagnosis / case presentation (before revision)
    # -----------------------------------------------------------------
    "predecessor": [
        {
            "text_template": "What was the initial diagnosis before revision in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["initial diagnosis"],
        },
        {
            "text_template": "What was the working diagnosis prior to the final one in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["differential diagnosis", "initial diagnosis"],
        },
        {
            "text_template": "What was suspected before the correct diagnosis was established in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["case presentation", "case report", "history"],
        },
    ],

    # -----------------------------------------------------------------
    # transitive_cause — gold = discussion (explains the revised diagnosis)
    # -----------------------------------------------------------------
    "transitive_cause": [
        {
            "text_template": "What evidence ultimately led to the correct diagnosis in: {title}?",
            "gold_kind": "causal_link",
            "gold_source_filter": ["discussion"],
        },
        {
            "text_template": "What new test or finding caused the diagnosis to be revised in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["investigations", "workup"],
        },
    ],

    # -----------------------------------------------------------------
    # causal_chain — gold = discussion (chain of reasoning)
    # -----------------------------------------------------------------
    "causal_chain": [
        {
            "text_template": "Explain the chain of reasoning from symptoms to diagnosis in: {title}",
            "gold_kind": "causal_link",
            "gold_source_filter": ["discussion"],
        },
        {
            "text_template": "What chain of evidence led to the final diagnosis in: {title}?",
            "gold_kind": "causal_link",
        },
    ],

    # -----------------------------------------------------------------
    # concurrent — gold = treatment / management alongside workup
    # -----------------------------------------------------------------
    "concurrent": [
        {
            "text_template": "What treatment was given alongside the workup for: {title}?",
            "gold_kind": "causal_link",
            "gold_source_filter": ["management", "treatment", "course"],
        },
    ],

    # -----------------------------------------------------------------
    # before_named — gold = case presentation (before a named treatment)
    # -----------------------------------------------------------------
    "before_named": [
        {
            "text_template": "What was the patient's state before treatment was initiated in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["case presentation", "case report", "history"],
        },
    ],

    # -----------------------------------------------------------------
    # retirement — gold = final_diagnosis (retires the initial one)
    # -----------------------------------------------------------------
    "retirement": [
        {
            "text_template": "Which diagnosis retired the initial hypothesis in: {title}?",
            "gold_kind": "retirement",
            "gold_source_filter": ["final diagnosis"],
        },
        {
            "text_template": "What was the corrected diagnosis in the misdiagnosis case: {title}?",
            "gold_kind": "retirement",
        },
    ],

    # -----------------------------------------------------------------
    # noise — off-topic clinical queries (different domain)
    # -----------------------------------------------------------------
    "noise": [
        {"text_template": "What is the current recommended dosage for amlodipine?"},
        {"text_template": "How do you perform a standard lumbar puncture?"},
        {"text_template": "What are the ICD-10 codes for childhood asthma?"},
        {"text_template": "What's the standard of care for type 2 diabetes screening?"},
        {"text_template": "How is serum potassium measured in the lab?"},
    ],
}


__all__ = ["TEMPLATES"]
