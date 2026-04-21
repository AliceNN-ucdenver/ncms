"""Gold query templates for MSEB-SWE.

Design principle (forced by the first mini-ablation's 0/30 scores
on three shapes): **every query must contain a distinguishing
feature of the gold memory that does NOT appear in its chain
siblings.**

SWE memory types and their distinguishing features:

| source            | distinguishing feature               | how we extract |
|-------------------|--------------------------------------|----------------|
| ``issue_body``    | prose bug description with code snippets | `{symbol}` (first backticked identifier) |
| ``resolving_patch`` | `diff --git a/<path>` with non-test file | `{patch_file}` |
| ``test_patch``    | patch touching `tests/…` path         | `{test_file}` |
| ``pr_discussion`` | conversational confirmation / debate  | prose cues ("discussion", "confirmed") |

Templates declare ``requires_entities`` — if the subject chain
doesn't expose that entity, the candidate is skipped rather than
emitted with an empty placeholder.  The
``benchmarks/mseb/gold_validator.py`` pass then rejects any
remaining candidate where the gold doesn't win a local lexical
race against its chain siblings.

Result: every gold query is structurally winnable, and no query
is trivially solvable just by matching bug-description words
(which would dominate the issue body and short-circuit the test
for TLG mechanisms).
"""

from __future__ import annotations

TEMPLATES: dict[str, list[dict[str, object]]] = {
    # -----------------------------------------------------------------
    # current_state — gold = patch.  Query mentions the patched file
    # path (unique to patch) + the symbol from the issue body, so it
    # references the state-change artefact without lexically matching
    # the issue body verbatim.
    # -----------------------------------------------------------------
    "current_state": [
        {
            "text_template": "What is the current implementation in {patch_file} after the fix?",
            "gold_kind": "retirement",
            "gold_source_filter": ["resolving_patch"],
            "requires_entities": ["patch_file"],
        },
        {
            "text_template": "Show me the diff that currently resolves the {symbol} issue.",
            "gold_kind": "retirement",
            "gold_source_filter": ["resolving_patch"],
            "requires_entities": ["symbol"],
        },
    ],

    # -----------------------------------------------------------------
    # origin — gold = issue body.  Query explicitly asks for the
    # *report* / *description* (bug-report vocabulary), so query
    # naturally matches the issue body over the patch.
    # -----------------------------------------------------------------
    "origin": [
        {
            "text_template": "Where was the {symbol} bug first reported by a user?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["issue_body"],
            "requires_entities": ["symbol"],
        },
        {
            "text_template": "What issue report triggered the work on {symbol}?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["issue_body"],
            "requires_entities": ["symbol"],
        },
    ],

    # -----------------------------------------------------------------
    # ordinal_first — gold = issue body (chronologically first).
    # -----------------------------------------------------------------
    "ordinal_first": [
        {
            "text_template": "What was the first user-reported observation about {symbol}?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["issue_body"],
            "requires_entities": ["symbol"],
        },
    ],

    # -----------------------------------------------------------------
    # ordinal_last — gold = test_patch (chronologically last in SWE).
    # Query mentions *testing* / *regression* vocabulary, which
    # appears only in the test_patch content.
    # -----------------------------------------------------------------
    "ordinal_last": [
        {
            "text_template": "Which regression tests were added to cover the {symbol} fix?",
            "gold_kind": "declaration",
            "gold_source_filter": ["test_patch"],
            "requires_entities": ["symbol"],
        },
        {
            "text_template": "What test cases landed in {test_file} for this fix?",
            "gold_kind": "declaration",
            "gold_source_filter": ["test_patch"],
            "requires_entities": ["test_file"],
        },
    ],

    # -----------------------------------------------------------------
    # sequence — gold = issue body (start of the ingest sequence).
    # -----------------------------------------------------------------
    "sequence": [
        {
            "text_template": "What was the opening report that started the {symbol} investigation?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["issue_body"],
            "requires_entities": ["symbol"],
        },
    ],

    # -----------------------------------------------------------------
    # predecessor — gold = issue body (the pre-fix state).  Query
    # emphasises the pre-fix / buggy behaviour so it aligns with the
    # issue-body description, not the patch that resolves it.
    # -----------------------------------------------------------------
    "predecessor": [
        {
            "text_template": "What buggy behaviour of {symbol} was described before the patch to {patch_file}?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["issue_body"],
            "requires_entities": ["symbol", "patch_file"],
        },
        {
            "text_template": "What failing scenario did users describe before {symbol} was fixed?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["issue_body"],
            "requires_entities": ["symbol"],
        },
    ],

    # -----------------------------------------------------------------
    # transitive_cause — gold = issue body (the root cause of
    # downstream patch + test_patch).
    # -----------------------------------------------------------------
    "transitive_cause": [
        {
            "text_template": "What root cause made both the patch in {patch_file} and its test case necessary?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["issue_body"],
            "requires_entities": ["patch_file"],
        },
    ],

    # -----------------------------------------------------------------
    # causal_chain — gold = PR discussion (root-cause reasoning lives here).
    # -----------------------------------------------------------------
    "causal_chain": [
        {
            "text_template": "Which PR discussion traces the root cause of the {symbol} issue?",
            "gold_kind": "causal_link",
            "gold_source_filter": ["pr_discussion"],
            "requires_entities": ["symbol"],
        },
        {
            "text_template": "Where did reviewers discuss why {symbol} was broken?",
            "gold_kind": "causal_link",
            "gold_source_filter": ["pr_discussion"],
            "requires_entities": ["symbol"],
        },
    ],

    # -----------------------------------------------------------------
    # concurrent — gold = PR discussion (happens alongside the patch).
    # -----------------------------------------------------------------
    "concurrent": [
        {
            "text_template": "What review discussion happened concurrently with the patch to {patch_file}?",
            "gold_kind": "causal_link",
            "gold_source_filter": ["pr_discussion"],
            "requires_entities": ["patch_file"],
        },
    ],

    # -----------------------------------------------------------------
    # before_named — gold = issue body (state before test_patch was added).
    # -----------------------------------------------------------------
    "before_named": [
        {
            "text_template": "What was reported before the tests in {test_file} were added?",
            "gold_kind": "ordinal_anchor",
            "gold_source_filter": ["issue_body"],
            "requires_entities": ["test_file"],
        },
    ],

    # -----------------------------------------------------------------
    # retirement — gold = patch (retires buggy behaviour).  Query
    # refers to *code change* / *patch* vocabulary that lives in the
    # diff content.
    # -----------------------------------------------------------------
    "retirement": [
        {
            "text_template": "Which code change in {patch_file} retired the buggy {symbol} behaviour?",
            "gold_kind": "retirement",
            "gold_source_filter": ["resolving_patch"],
            "requires_entities": ["symbol", "patch_file"],
        },
        {
            "text_template": "Which diff removed the faulty implementation of {symbol}?",
            "gold_kind": "retirement",
            "gold_source_filter": ["resolving_patch"],
            "requires_entities": ["symbol"],
        },
    ],

    # -----------------------------------------------------------------
    # noise — adversarial off-topic queries.  Intentionally uses
    # vocabulary that should not match any SWE memory.
    # -----------------------------------------------------------------
    "noise": [
        {"text_template": "What is the recommended way to configure a Redis cluster for HA?"},
        {"text_template": "How do I implement OAuth2 device flow in a mobile app?"},
        {"text_template": "What's the syntax for creating a materialized view in PostgreSQL?"},
        {"text_template": "How do I write a Dockerfile for a Go HTTP server?"},
        {"text_template": "What's the best way to do A/B testing of a pricing page?"},
    ],
}


__all__ = ["TEMPLATES"]
