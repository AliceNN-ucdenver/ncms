"""Gold query templates for MSEB-Convo.

Two axes exercised simultaneously:

1. **Intent shape** (11 of 14 — LMEval has session-level dates
   but not intra-session clocks, so ``interval`` / ``range`` are
   excluded).
2. **Preference sub-type** (positive / avoidance / habitual /
   difficult) — the P2 ``intent_head`` axis that only Convo tests.

Design rationale:

- Gold memories for preference queries are user turns that
  carry the labeler's ``metadata.preference`` tag.  The labeler
  is rule-based, so ``gold_preference`` in a template directly
  filters labeled turns.
- State-evolution shapes (origin, predecessor, retirement, …)
  point at user declarations — the labeler tags these as
  ``declaration`` / ``retirement`` / ``ordinal_anchor``.
- Noise queries are off-topic / adversarial.

Known corpus limitation (see
``benchmarks/mseb_convo/README.md`` §9): LMEval has ~250
positive + ~105 habitual preference turns but fewer than 2 each
for avoidance and difficult.  The gold tool emits whatever
candidates the corpus supports; the author-side mitigation is
either (a) SDG injection or (b) accepting thinner per-cell n
for those two sub-types.
"""

from __future__ import annotations

TEMPLATES: dict[str, list[dict[str, object]]] = {
    # =================================================================
    # Preference × current_state — the headline Convo queries
    # =================================================================
    "current_state": [
        {
            "text_template": "What does the user currently prefer?",
            "gold_kind": "declaration",
            "gold_preference": "positive",
            "preference": "positive",
        },
        {
            "text_template": "What does the user avoid or dislike?",
            "gold_kind": "declaration",
            "gold_preference": "avoidance",
            "preference": "avoidance",
        },
        {
            "text_template": "What is the user's habitual routine?",
            "gold_kind": "declaration",
            "gold_preference": "habitual",
            "preference": "habitual",
        },
        {
            "text_template": "What does the user struggle with?",
            "gold_kind": "declaration",
            "gold_preference": "difficult",
            "preference": "difficult",
        },
        # Fallback (non-preference) current-state queries — grounded
        # on any first-person declaration.
        {
            "text_template": (
                "What's the user's current state per their latest statement: {first_sentence}?"
            ),
            "gold_kind": "declaration",
        },
    ],
    # =================================================================
    # origin / ordinal_first — gold = very first turn in the chain
    # =================================================================
    "origin": [
        {
            "text_template": "What did the user first tell the assistant?",
            "gold_kind": "ordinal_anchor",
        },
    ],
    "ordinal_first": [
        {
            "text_template": "What was the user's first message in this conversation?",
            "gold_kind": "ordinal_anchor",
        },
    ],
    # =================================================================
    # ordinal_last — gold = a late-chain declaration (the most recent)
    # =================================================================
    "ordinal_last": [
        {
            "text_template": "What's the most recent thing the user declared about themselves?",
            "gold_kind": "declaration",
            # No source filter; the author_tool picks the first chain
            # memory matching the filter — in subject-chronological
            # order that's not literally "last", so the author should
            # review these candidates carefully.  Note stamped below.
            "note": (
                "Review: generator emits EARLIEST matching declaration; "
                "flip to LATEST in build.py review"
            ),
        },
    ],
    # =================================================================
    # sequence — gold = ordinal anchor (start of user's story)
    # =================================================================
    "sequence": [
        {
            "text_template": "Trace the user's history from their first message onward.",
            "gold_kind": "ordinal_anchor",
        },
    ],
    # =================================================================
    # predecessor — gold = earlier declaration (before a later retirement)
    # =================================================================
    "predecessor": [
        {
            "text_template": "What did the user previously prefer before their latest change?",
            "gold_kind": "declaration",
            "gold_preference": "positive",
            "preference": "positive",
            "note": (
                "Needs a subject with both an early positive declaration AND a later retirement"
            ),
        },
    ],
    # =================================================================
    # transitive_cause — gold = causal_link turn ("because X, so Y")
    # =================================================================
    "transitive_cause": [
        {
            "text_template": "What triggered the user's stated preference or situation?",
            "gold_kind": "causal_link",
        },
    ],
    # =================================================================
    # causal_chain — gold = causal_link turn connecting state changes
    # =================================================================
    "causal_chain": [
        {
            "text_template": "Explain the reasoning the user gave for their current preference.",
            "gold_kind": "causal_link",
        },
        {
            "text_template": "Why does the user struggle with this?",
            "gold_kind": "causal_link",
            "preference": "difficult",
        },
    ],
    # =================================================================
    # concurrent — gold = habitual declaration alongside another state
    # =================================================================
    "concurrent": [
        {
            "text_template": "What habit does the user practice alongside their main activity?",
            "gold_kind": "declaration",
            "gold_preference": "habitual",
            "preference": "habitual",
        },
    ],
    # =================================================================
    # before_named — gold = an earlier declaration
    # =================================================================
    "before_named": [
        {
            "text_template": "What did the user prefer before their most recent update?",
            "gold_kind": "declaration",
            "gold_preference": "positive",
            "preference": "positive",
        },
    ],
    # =================================================================
    # retirement — gold = user's "I've switched / I used to" turn
    # =================================================================
    "retirement": [
        {
            "text_template": "What preference has the user changed or retired?",
            "gold_kind": "retirement",
            "note": "Corpus-thin: LMEval users rarely say 'I used to...' explicitly",
        },
    ],
    # =================================================================
    # noise — off-topic adversarial queries
    # =================================================================
    "noise": [
        {"text_template": "What is the capital of Madagascar?"},
        {"text_template": "How many moons does Jupiter have?"},
        {"text_template": "What's the chemical formula for caffeine?"},
        {"text_template": "How do you make sourdough starter from scratch?"},
        {"text_template": "Who won the 2022 World Cup?"},
    ],
}


__all__ = ["TEMPLATES"]
