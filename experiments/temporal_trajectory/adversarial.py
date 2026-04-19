"""Adversarial query suite — tests grammar failure modes.

The positive-case tests (`queries.py`) verify the grammar finds the
right answer when it should.  These queries test the OTHER half: the
grammar should *not* confidently return an answer when it doesn't
have one.

Each query is tagged with an `expected_mode`:

* ``answer``     — grammar should return the specified gold mid at
                   rank 1 with ``has_confident_answer() == True``.
* ``abstain``    — grammar should NOT prepend a grammar answer
                   (``has_confident_answer() == False``).  The ranking
                   passed to the caller is unchanged BM25 order.
* ``alias``      — grammar should find via alias expansion (functionally
                   same as ``answer`` but tests the alias path
                   specifically).

The outcome we're measuring is *not* rank-1 accuracy — it's **whether
the grammar knows when it doesn't know**.  Confidently-wrong rank-1
answers are worse than deferring to BM25.

Usage::

    uv run python -m experiments.temporal_trajectory.adversarial
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ExpectedMode = Literal["answer", "abstain", "alias"]


@dataclass(frozen=True)
class AdversarialQuery:
    text: str
    expected_mode: ExpectedMode
    gold_mid: str | None = None   # required for answer/alias modes
    reason: str = ""


ADVERSARIAL: list[AdversarialQuery] = [
    # ── Alias-expansion tests ─────────────────────────────────────────
    AdversarialQuery(
        text="Do we still use JSON Web Tokens?",
        expected_mode="alias",
        gold_mid="ADR-021",
        reason=(
            "Reconciled retires is {'JWT'}; query uses 'JSON Web "
            "Tokens'.  Alias expansion (JWT ↔ JSON Web Tokens via "
            "initials) should find ADR-021."
        ),
    ),
    AdversarialQuery(
        text="Is the patient still on PT?",
        expected_mode="alias",
        gold_mid="MED-04",
        reason=(
            "Reconciled retires includes 'physical therapy' and 'PT' "
            "— alias-free direct match expected.  Included to confirm "
            "the alias path doesn't regress direct matches."
        ),
    ),
    AdversarialQuery(
        text="Do we still have multi-factor authentication?",
        expected_mode="alias",
        gold_mid="ADR-029",
        reason=(
            "MFA retired by passkeys at ADR-029.  Alias (MFA ↔ "
            "multi-factor authentication) resolves full phrase to "
            "abbreviated retires_entities."
        ),
    ),

    # ── Abstention: unknown entity ────────────────────────────────────
    AdversarialQuery(
        text="What caused the outage on payments?",
        expected_mode="abstain",
        reason=(
            "'outage' doesn't appear in any memory.  Grammar should "
            "fall through past (a) and (c) — and (b) exact-entity "
            "match also fails.  Low-confidence (b) content-match "
            "would find nothing either.  Abstain expected."
        ),
    ),
    AdversarialQuery(
        text="Do we still use Stripe v3?",
        expected_mode="abstain",
        reason=(
            "Stripe v3 doesn't exist (only v1 and v2 in corpus).  "
            "retirement_memory won't find an edge; current-zone has "
            "no 'Stripe v3' entity.  Abstain expected."
        ),
    ),
    AdversarialQuery(
        text="What came right after the sprint planning?",
        expected_mode="abstain",
        reason=(
            "'sprint planning' isn't an entity in any memory.  "
            "_find_memory returns None → sequence handler produces "
            "no answer → abstain."
        ),
    ),

    # ── Abstention: no subject ────────────────────────────────────────
    AdversarialQuery(
        text="What is the current state?",
        expected_mode="abstain",
        reason=(
            "No subject tokens in query → lookup_subject returns "
            "None → retrieve_lg early-returns with confidence='abstain'."
        ),
    ),
    AdversarialQuery(
        text="How do I cook pasta?",
        expected_mode="abstain",
        reason="No subject, no intent marker → abstain.",
    ),

    # ── Abstention: malformed / unsupported ──────────────────────────
    AdversarialQuery(
        text="Wat happend after OAuth?",
        expected_mode="abstain",
        reason=(
            "Typos ('Wat', 'happend') mean no production matches the "
            "query shape.  Should abstain (not guess)."
        ),
    ),
    AdversarialQuery(
        text="What replaced it?",
        expected_mode="abstain",
        reason=(
            "Pronoun 'it' has no antecedent; no subject or entity can "
            "be resolved.  Grammar should abstain."
        ),
    ),
    AdversarialQuery(
        text="knee injury",
        expected_mode="abstain",
        reason=(
            "Bare noun phrase, no question.  No production matches "
            "and no intent marker fires.  Subject resolves to "
            "knee_injury but intent=none → abstain (no subject-only "
            "single-memory match, since knee_injury has multiple)."
        ),
    ),
    AdversarialQuery(
        text="What does ADR-021 supersede?",
        expected_mode="abstain",
        reason=(
            "Mid-reference queries (direct ID) aren't in the grammar "
            "taxonomy — we don't parse 'ADR-xxx' as a named event.  "
            "Abstain expected; BM25 can handle ID lookup directly."
        ),
    ),

    # ── Confidence scaling: content-marker and generic fallbacks ──────
    AdversarialQuery(
        text="What's our current auth mechanism?",
        expected_mode="abstain",
        reason=(
            "'auth' contraction isn't in Layer 1 vocab (Snowball "
            "stems 'authentication' to 'authent', not 'auth').  "
            "Subject inference fails → abstain.  Documented "
            "limitation — a manual alias for 'auth' → 'authentication' "
            "would fix it, but that's a curated synonym (kludge)."
        ),
    ),
    AdversarialQuery(
        text="When was surgery performed?",
        expected_mode="abstain",
        reason=(
            "'when was X performed?' doesn't match any intent "
            "production — this is a lookup query (temporal attribute "
            "of a specific memory), not a grammar-traversal query.  "
            "Out of scope for LG; BM25 would handle it."
        ),
    ),

    # ── Edge case: subject inferrable but ambiguous query shape ──────
    AdversarialQuery(
        text="What was the authentication decision?",
        expected_mode="abstain",
        reason=(
            "No temporal qualifier (current/first/latest/etc).  "
            "Multiple authentication decisions exist.  Grammar can't "
            "disambiguate without more context — abstain is the "
            "correct behavior, BM25 picks the best textual match."
        ),
    ),
]


def run_adversarial() -> None:
    from experiments.temporal_trajectory.lg_retriever import retrieve_lg
    from experiments.temporal_trajectory.retrievers import _build_bm25_index

    engine = _build_bm25_index()
    correct = 0
    incorrect: list[tuple[AdversarialQuery, str]] = []
    rows: list[tuple[AdversarialQuery, str, str | None, str]] = []

    for aq in ADVERSARIAL:
        bm25 = [
            (mid, score) for mid, score in engine.search(aq.text, limit=20)
        ]
        _, trace = retrieve_lg(aq.text, bm25)
        confident = trace.has_confident_answer()

        outcome: str
        if aq.expected_mode in ("answer", "alias"):
            if confident and trace.grammar_answer == aq.gold_mid:
                outcome = "correct_answer"
            elif confident and trace.grammar_answer != aq.gold_mid:
                outcome = "wrong_answer"
            else:
                outcome = "missed_answer"
        elif aq.expected_mode == "abstain":
            if not confident:
                outcome = "correctly_abstained"
            else:
                outcome = "false_answer"
        else:
            outcome = "unknown_mode"

        if outcome in ("correct_answer", "correctly_abstained"):
            correct += 1
        else:
            incorrect.append((aq, outcome))
        rows.append((aq, outcome, trace.grammar_answer, trace.confidence))

    # Report.
    print("Adversarial query suite")
    print("=" * 80)
    for aq, outcome, ans, conf in rows:
        marker = "✓" if outcome in ("correct_answer", "correctly_abstained") else "✗"
        expected = (
            aq.gold_mid if aq.expected_mode in ("answer", "alias")
            else "ABSTAIN"
        )
        print(
            f"  {marker} [{aq.expected_mode:<8}] {aq.text[:55]:<55} "
            f"got={ans or '—':<10} conf={conf:<8} "
            f"expected={expected}"
        )
    print()
    print(f"Adversarial score: {correct}/{len(ADVERSARIAL)}")

    if incorrect:
        print()
        print("Failures")
        print("-" * 60)
        for aq, outcome in incorrect:
            print(f"  {aq.text}")
            print(f"    outcome: {outcome}")
            print(f"    reason: {aq.reason}")


if __name__ == "__main__":
    run_adversarial()
