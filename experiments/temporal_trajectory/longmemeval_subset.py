"""LongMemEval taxonomy-coverage validation.

LongMemEval's questions are conversational and their answers are
extracted facts across multiple sessions — not single memories.  A
full end-to-end test against our grammar would require
reconstructing a typed-edge corpus from each question's haystack,
which is integration-time work, not grammar-taxonomy work.

What this module tests instead: **can the grammar's production list
classify real LongMemEval queries into an appropriate intent?**  If
yes, the intent taxonomy covers real query shapes and integration
can fill in the typed-edge graph.  If no, we have a taxonomy gap
that would show up in production regardless of edge quality.

Each curated query is tagged with:

* ``expected_intents`` — set of grammar intents that would be a
  reasonable classification.  Multiple entries = the query is
  ambiguous (e.g., "Which event did I attend first?" could be
  ``before_named`` or ``ordinal_first``).
* ``acceptable_abstain`` — whether abstention is also an acceptable
  outcome (for queries outside our taxonomy entirely, e.g., those
  requiring cross-session fact aggregation that grammar can't
  handle — then abstention → BM25 is the right behavior).

Scoring: a query passes if ``analyze_query(text).intent`` is in
``expected_intents`` OR (``acceptable_abstain`` AND intent is
``"none"``).

The 15-query subset is stratified across LongMemEval question types
to stress each grammar family.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LMECurated:
    text: str
    lme_type: str              # LongMemEval original question type
    expected_intents: frozenset[str]   # acceptable grammar classifications
    acceptable_abstain: bool = False
    note: str = ""


CURATED: list[LMECurated] = [
    # ── temporal-reasoning ────────────────────────────────────────────
    LMECurated(
        text="What was the first issue I had with my new car after its first service?",
        lme_type="temporal-reasoning",
        expected_intents=frozenset({"origin", "sequence"}),
        note=(
            "'first issue after first service' — either origin of a "
            "car-issue sub-chain (if modeled as subject) or sequence "
            "with named event 'first service'."
        ),
    ),
    LMECurated(
        text="Which event did I attend first, the workshop or the webinar?",
        lme_type="temporal-reasoning",
        expected_intents=frozenset({"before_named", "interval"}),
        note=(
            "Two-event ordering — direct map to ``before_named``.  "
            "Grammar returns whichever came first with a yes/no verdict."
        ),
    ),
    LMECurated(
        text="Which vehicle did I take care of first in February, the bike or the car?",
        lme_type="temporal-reasoning",
        expected_intents=frozenset({"before_named", "range"}),
        note=(
            "Ordering question with range qualifier ('in February').  "
            "Primary fit = before_named; range handling would need a "
            "secondary pass."
        ),
    ),
    LMECurated(
        text="What did I do before starting my new job?",
        lme_type="temporal-reasoning",
        expected_intents=frozenset({"predecessor"}),
        note="Predecessor intent — direct chain-predecessor of 'new job'.",
    ),

    # ── knowledge-update (current-state queries) ──────────────────────
    LMECurated(
        text="What is my personal best time in the charity 5K run?",
        lme_type="knowledge-update",
        expected_intents=frozenset({"current", "still"}),
        acceptable_abstain=True,
        note=(
            "'Personal best' is a domain-specific currency marker "
            "(performance tracking).  Adding 'best'/'record' to "
            "current markers would risk false positives ('best plan', "
            "'best friend').  Accepted taxonomy gap — abstention → "
            "BM25 handles this class of query.  Integration-time "
            "fix: a domain-specific marker pack per subject type."
        ),
    ),
    LMECurated(
        text="How many Korean restaurants have I tried in my city?",
        lme_type="knowledge-update",
        expected_intents=frozenset({"current", "none"}),
        acceptable_abstain=True,
        note=(
            "Count-aggregation query — out of our memory-return "
            "taxonomy.  BM25 fallback is appropriate; grammar "
            "abstaining is the correct behavior."
        ),
    ),
    LMECurated(
        text="Where did Rachel move to after her recent relocation?",
        lme_type="knowledge-update",
        expected_intents=frozenset({"sequence", "current"}),
        note=(
            "'After her relocation' is a sequence-intent signal; "
            "alternatively current state of Rachel's location."
        ),
    ),
    LMECurated(
        text="Do I still live in Seattle?",
        lme_type="knowledge-update",
        expected_intents=frozenset({"still"}),
        note=(
            "Classic still-intent query — retirement lookup on "
            "'Seattle' within the location/residence subject."
        ),
    ),

    # ── multi-session (often aggregation — mostly abstain) ────────────
    LMECurated(
        text="What hobbies have I mentioned across our conversations?",
        lme_type="multi-session",
        expected_intents=frozenset({"none"}),
        acceptable_abstain=True,
        note=(
            "Cross-session aggregation — no grammar primitive. "
            "Abstention → BM25 + SPLADE aggregation is correct."
        ),
    ),
    LMECurated(
        text="When did I first mention my interest in photography?",
        lme_type="multi-session",
        expected_intents=frozenset({"origin"}),
        note=(
            "'When did I first X' — origin-intent, finds earliest "
            "mention of photography."
        ),
    ),

    # ── single-session-user ───────────────────────────────────────────
    LMECurated(
        text="What was the original plan for my trip?",
        lme_type="single-session-user",
        expected_intents=frozenset({"origin"}),
        note=(
            "'original plan' — origin-intent, earliest plan-memory "
            "in the trip subject."
        ),
    ),
    LMECurated(
        text="Am I still going to the conference next month?",
        lme_type="single-session-user",
        expected_intents=frozenset({"still"}),
        note="still-intent on the 'conference' plan.",
    ),

    # ── retirement / cause-of ────────────────────────────────────────
    LMECurated(
        text="Why did I cancel my gym membership?",
        lme_type="multi-session",
        expected_intents=frozenset({"cause_of"}),
        note=(
            "'Why did I X' — cause_of, finds memory explaining "
            "the cancellation."
        ),
    ),
    LMECurated(
        text="What eventually led to me switching jobs?",
        lme_type="temporal-reasoning",
        expected_intents=frozenset({"transitive_cause"}),
        note=(
            "'Eventually led to' → transitive_cause, full predecessor "
            "walk in the career-subject chain."
        ),
    ),

    # ── concurrent ────────────────────────────────────────────────────
    LMECurated(
        text="What else was happening while I was traveling in Europe?",
        lme_type="multi-session",
        expected_intents=frozenset({"concurrent"}),
        note=(
            "'While I was X' — concurrent-intent, cross-subject "
            "memories temporally aligned with Europe trip."
        ),
    ),
]


def run_coverage() -> None:
    from experiments.temporal_trajectory.query_parser import analyze_query

    correct = 0
    misses: list[tuple[LMECurated, str]] = []
    rows: list[tuple[LMECurated, str, bool]] = []

    for q in CURATED:
        qs = analyze_query(q.text)
        detected = qs.intent
        ok = (
            detected in q.expected_intents
            or (q.acceptable_abstain and detected == "none")
        )
        rows.append((q, detected, ok))
        if ok:
            correct += 1
        else:
            misses.append((q, detected))

    # Report.
    print("LongMemEval taxonomy-coverage subset")
    print("=" * 85)
    for q, detected, ok in rows:
        marker = "✓" if ok else "✗"
        exp = "/".join(sorted(q.expected_intents))
        if q.acceptable_abstain:
            exp += " (|none)"
        print(
            f"  {marker} [{q.lme_type:<25}] "
            f"{q.text[:50]:<50} "
            f"got={detected:<15} "
            f"exp={exp}"
        )
    print()
    print(f"Coverage: {correct}/{len(CURATED)}")

    if misses:
        print()
        print("Misses")
        print("-" * 60)
        for q, detected in misses:
            print(f"  query: {q.text}")
            print(f"    expected: {q.expected_intents}  got: {detected!r}")
            print(f"    note: {q.note}")


if __name__ == "__main__":
    run_coverage()
