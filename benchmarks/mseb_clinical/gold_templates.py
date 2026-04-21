"""Gold query templates for MSEB-Clinical.

Same design principle as SWE (see that module's docstring): each
query must reference distinguishing vocabulary of the gold
section so it doesn't just match the memory with the most
bug-description words.

Clinical sections and their distinguishing vocabulary:

| source                   | distinguishing vocabulary                   |
|--------------------------|---------------------------------------------|
| ``abstract``             | summary; often names final diagnosis         |
| ``case presentation`` / ``case report`` | patient age/sex, presenting complaint |
| ``history``              | past medical history, medications             |
| ``investigations`` / ``workup`` | lab values, imaging, test results      |
| ``differential diagnosis`` | hypothesis list, "differential"             |
| ``initial diagnosis``    | first working diagnosis                       |
| ``management`` / ``treatment`` | drug/procedure names, "administered"    |
| ``course`` / ``outcome`` / ``follow-up`` | patient trajectory, "recovered" |
| ``final diagnosis``      | "confirmed", "final", established diagnosis   |
| ``discussion``           | retrospective reasoning, mechanism            |
| ``conclusion``           | lessons learned, takeaways                    |

Queries use the abstract's first sentence as ``{title}`` (the
disease context stays consistent across a subject's queries)
and shape-appropriate section vocabulary.
"""

from __future__ import annotations

TEMPLATES: dict[str, list[dict[str, object]]] = {
    # -----------------------------------------------------------------
    # current_state — final established diagnosis
    # -----------------------------------------------------------------
    "current_state": [
        {
            "text_template": "What final confirmed diagnosis did the workup establish for: {title}?",
            "gold_kind": "retirement",
            "gold_source_filter": ["final diagnosis"],
        },
        {
            "text_template": "What was the corrected diagnosis after full investigation in: {title}?",
            "gold_kind": "retirement",
            "gold_source_filter": ["discussion"],
        },
    ],

    # -----------------------------------------------------------------
    # origin — how the patient first presented
    # -----------------------------------------------------------------
    "origin": [
        {
            "text_template": "How did the patient first present with signs consistent with: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["case presentation", "case report", "presentation", "history"],
        },
        {
            "text_template": "What was the patient's presenting complaint in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["case presentation", "case report", "presentation", "history"],
        },
    ],

    # -----------------------------------------------------------------
    # ordinal_first — earliest recorded observation
    # -----------------------------------------------------------------
    "ordinal_first": [
        {
            "text_template": "What was the earliest documented patient observation in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["case presentation", "case report", "history"],
        },
    ],

    # -----------------------------------------------------------------
    # ordinal_last — final outcome / follow-up
    # -----------------------------------------------------------------
    "ordinal_last": [
        {
            "text_template": "What was the patient's outcome at final follow-up in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["outcome", "follow-up"],
        },
        {
            "text_template": "What conclusion did the authors draw from the case of: {title}?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["conclusion"],
        },
    ],

    # -----------------------------------------------------------------
    # sequence — the diagnostic sequence
    # -----------------------------------------------------------------
    "sequence": [
        {
            "text_template": "What initial presentation started the diagnostic workup for: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["case presentation", "case report", "history"],
        },
    ],

    # -----------------------------------------------------------------
    # predecessor — earlier working diagnosis (before the final)
    # -----------------------------------------------------------------
    "predecessor": [
        {
            "text_template": "What working diagnosis was considered before the final one in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["initial diagnosis", "differential diagnosis"],
        },
        {
            "text_template": "What diagnosis was initially suspected based on presentation in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["case presentation", "case report", "history"],
        },
    ],

    # -----------------------------------------------------------------
    # transitive_cause — which new investigation revised the diagnosis
    # -----------------------------------------------------------------
    "transitive_cause": [
        {
            "text_template": "What laboratory or imaging finding drove the diagnostic revision in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["investigations", "workup"],
        },
        {
            "text_template": "What new test result prompted reconsidering the diagnosis in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["investigations", "workup"],
        },
    ],

    # -----------------------------------------------------------------
    # causal_chain — retrospective reasoning
    # -----------------------------------------------------------------
    "causal_chain": [
        {
            "text_template": "What retrospective reasoning explains the diagnostic trajectory in: {title}?",
            "gold_kind": "causal_link",
            "gold_source_filter": ["discussion"],
        },
        {
            "text_template": "What mechanism did the authors discuss to explain the findings in: {title}?",
            "gold_kind": "causal_link",
            "gold_source_filter": ["discussion"],
        },
    ],

    # -----------------------------------------------------------------
    # concurrent — treatment alongside the workup
    # -----------------------------------------------------------------
    "concurrent": [
        {
            "text_template": "What therapy was administered concurrently with diagnostic workup in: {title}?",
            "gold_kind": "causal_link",
            "gold_source_filter": ["management", "treatment", "course"],
        },
    ],

    # -----------------------------------------------------------------
    # before_named — state prior to treatment
    # -----------------------------------------------------------------
    "before_named": [
        {
            "text_template": "What was the patient's clinical state before treatment was initiated in: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["case presentation", "case report", "history"],
        },
    ],

    # -----------------------------------------------------------------
    # retirement — the diagnosis that retired the earlier hypothesis
    # -----------------------------------------------------------------
    "retirement": [
        {
            "text_template": "Which established diagnosis retired the initial working hypothesis in: {title}?",
            "gold_kind": "retirement",
            "gold_source_filter": ["final diagnosis"],
        },
        {
            "text_template": "What confirmed diagnosis corrected the earlier misdiagnosis in: {title}?",
            "gold_kind": "retirement",
        },
    ],

    # -----------------------------------------------------------------
    # noise — off-topic clinical queries
    # -----------------------------------------------------------------
    "noise": [
        {"text_template": "What is the recommended dose of amlodipine for hypertension in adults?"},
        {"text_template": "How do you perform a standard lumbar puncture procedure?"},
        {"text_template": "What ICD-10 codes describe childhood-onset asthma?"},
        {"text_template": "What is the standard of care for type 2 diabetes screening guidelines?"},
        {"text_template": "How is serum potassium measured in a clinical chemistry lab?"},
    ],
}


__all__ = ["TEMPLATES"]
