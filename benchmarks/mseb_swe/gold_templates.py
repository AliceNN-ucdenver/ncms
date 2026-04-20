"""Gold query templates for MSEB-SWE.

Consumed by ``benchmarks/mseb/gold_author.py`` to generate
reviewable gold.yaml candidates.  Each shape maps to one or more
templates; the author tool iterates subjects and emits one
candidate per (subject, shape) using the first template that
matches the subject's memory chain.

Template fields:

- ``text_template``       — phrasing with ``{title}`` /
  ``{first_sentence}`` placeholders filled from the gold memory.
- ``gold_kind``           — required MemoryKind on the gold
  memory (declaration / retirement / causal_link / ordinal_anchor / none).
- ``gold_source_filter``  — optional list of acceptable
  ``metadata.source`` values (``issue_body`` / ``resolving_patch``
  / ``test_patch`` / ``pr_discussion``).
- ``preference``          — optional; SWE queries are all "none".
- ``note``                — optional author-facing note.

Design principle: templates are conservative — if a subject's
chain doesn't contain the expected (kind, source) pair, the
template is skipped.  The author reviews every generated
candidate before committing to gold.yaml.
"""

from __future__ import annotations

TEMPLATES: dict[str, list[dict[str, object]]] = {
    # -----------------------------------------------------------------
    # current_state — gold = the patch (the fix that defines current behaviour)
    # -----------------------------------------------------------------
    "current_state": [
        {
            "text_template": "What is the current resolution for: {title}?",
            "gold_kind": "retirement",
            "gold_source_filter": ["resolving_patch"],
            "title_from_source": ["issue_body"],
        },
        {
            "text_template": "After the fix, how does the code now behave regarding: {title}?",
            "gold_kind": "retirement",
            "gold_source_filter": ["resolving_patch"],
            "title_from_source": ["issue_body"],
        },
    ],

    # -----------------------------------------------------------------
    # origin — gold = the issue body (where it all started)
    # -----------------------------------------------------------------
    "origin": [
        {
            "text_template": "Where was the issue \"{title}\" first reported?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["issue_body"],
        },
        {
            "text_template": "What triggered work on: {title}?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["issue_body"],
        },
    ],

    # -----------------------------------------------------------------
    # ordinal_first — gold = issue body (first in chronological chain)
    # -----------------------------------------------------------------
    "ordinal_first": [
        {
            "text_template": "What was the first report about: {title}?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["issue_body"],
        },
    ],

    # -----------------------------------------------------------------
    # ordinal_last — gold = test_patch (last artefact on the arc)
    # -----------------------------------------------------------------
    "ordinal_last": [
        {
            "text_template": "What was the last artefact added to fix the issue described as: {title}?",
            "gold_kind": "declaration",
            "gold_source_filter": ["test_patch"],
            "title_from_source": ["issue_body"],
            "note": "test_patch is added after the resolving_patch in SWE-bench chronology",
        },
    ],

    # -----------------------------------------------------------------
    # sequence — gold = issue_body (start of sequence)
    # -----------------------------------------------------------------
    "sequence": [
        {
            "text_template": "Trace the fix sequence that started with: {title}",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["issue_body"],
        },
    ],

    # -----------------------------------------------------------------
    # predecessor — gold = issue body (the pre-fix state)
    # -----------------------------------------------------------------
    "predecessor": [
        {
            "text_template": "What was the behaviour before the fix for: {title}?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["issue_body"],
        },
        {
            "text_template": "What preceded the patch resolving: {title}?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["issue_body"],
        },
    ],

    # -----------------------------------------------------------------
    # transitive_cause — gold = issue body (the cause of the downstream patch)
    # -----------------------------------------------------------------
    "transitive_cause": [
        {
            "text_template": "What caused the patch to be required that addressed: {title}?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["issue_body"],
        },
    ],

    # -----------------------------------------------------------------
    # causal_chain — gold = PR discussion (discussion connects issue → patch)
    # -----------------------------------------------------------------
    "causal_chain": [
        {
            "text_template": "Which discussion explains the root cause of: {title}?",
            "gold_kind": "causal_link",
            "gold_source_filter": ["pr_discussion"],
        },
    ],

    # -----------------------------------------------------------------
    # concurrent — gold = PR discussion (concurrent with the patch)
    # -----------------------------------------------------------------
    "concurrent": [
        {
            "text_template": "What discussion happened alongside the patch for: {title}?",
            "gold_kind": "causal_link",
            "gold_source_filter": ["pr_discussion"],
        },
    ],

    # -----------------------------------------------------------------
    # before_named — gold = issue body (before the test_patch landed)
    # -----------------------------------------------------------------
    "before_named": [
        {
            "text_template": "What was reported before the test patch was added for: {title}?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["issue_body"],
        },
    ],

    # -----------------------------------------------------------------
    # retirement — gold = resolving patch (retires the old behaviour)
    # -----------------------------------------------------------------
    "retirement": [
        {
            "text_template": "Which patch retired the old behaviour described in: {title}?",
            "gold_kind": "retirement",
            "gold_source_filter": ["resolving_patch"],
            "title_from_source": ["issue_body"],
        },
        {
            "text_template": "Which change replaced the faulty implementation in: {title}?",
            "gold_kind": "retirement",
            "gold_source_filter": ["resolving_patch"],
            "title_from_source": ["issue_body"],
        },
    ],

    # -----------------------------------------------------------------
    # noise — adversarial / off-topic (gold_mid intentionally empty)
    # -----------------------------------------------------------------
    "noise": [
        {"text_template": "What is the recommended way to configure a Redis cluster?"},
        {"text_template": "How do I implement OAuth2 device flow in JavaScript?"},
        {"text_template": "What's the syntax for creating a materialized view in PostgreSQL?"},
        {"text_template": "How do I write a Dockerfile for a Go HTTP server?"},
        {"text_template": "What's the difference between var, let, and const in TypeScript?"},
    ],
}


__all__ = ["TEMPLATES"]
