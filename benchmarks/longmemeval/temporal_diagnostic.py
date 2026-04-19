"""Temporal-reasoning diagnostic for LongMemEval.

Purpose
-------

P1 (range-filter temporal scoring) shipped but had zero measurable
impact on LongMemEval's temporal-reasoning category (0.2782 baseline,
0.2782 with temporal on).  A hand-sample of the 133 questions showed
they're almost all **ordinal** ("which came first"), **comparative**
("X or Y"), or **arithmetic** ("how many days between") — not
range-filter.  Before building P1b/P1c we need to know:

1. Which temporal pattern each question actually has.
2. Whether the ground-truth answer is even present in the retrieval
   candidate pool (and if so, at what depth).
3. Whether the answer text is present in *any* memory at all (pure
   arithmetic questions have answers like "21 days" that don't appear
   in any source memory, so Recall@K has a hard ceiling).

Output is a JSON report + markdown table to
``benchmarks/results/temporal_diagnostic/`` that drives the P1b/P1c
design decisions:

* If **answer in top-20 but not top-5** → ranking problem; ordinal
  re-rank will help.
* If **answer not in top-50** → candidate-generation problem;
  multi-anchor retrieval will help.
* If **answer not in any memory** → Recall@K ceiling; only RAG mode
  can score these.

Usage
-----

::

    uv run python -m benchmarks.longmemeval.temporal_diagnostic

Runs the 133 temporal-reasoning questions only, at top-50 retrieval,
with the production features-on bundle.  Expected runtime ~25 min.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.core.qa_metrics import recall_at_k_qa
from benchmarks.core.runner import log_run_header, run_async, setup_logging
from benchmarks.longmemeval.harness import (
    _create_ncms_instance,
    _ingest_sessions,
    _parse_lme_date,
)
from benchmarks.longmemeval.loader import LongMemQuestion, load_longmemeval_dataset

logger = logging.getLogger(__name__)


# ── Pattern taxonomy ──────────────────────────────────────────────────
#
# Patterns are ordered most-specific to least-specific.  The first
# match wins.  The taxonomy maps to what retrieval would need to do:
#
# - ARITH_BETWEEN / ARITH_ANCHORED : retrieve both anchor events; the
#   final numeric answer is computed by an LLM, not found in any
#   single memory. Recall@K has a ceiling here.
# - COMPARE_FIRST / COMPARE_LAST : retrieve both candidates; rank by
#   observed_at ordering.  Answer *is* in a memory (the earlier/later
#   one).
# - ORDINAL_FIRST / ORDINAL_LAST : single-subject ordinal; rank
#   matching memories by age.
# - RANGE_FILTER : narrow to a time range.  This is what P1a already
#   does.
# - OTHER : unclassified; fall back to default retrieval.


@dataclass(frozen=True)
class Pattern:
    id: str
    description: str
    needs_rerank: bool = False
    needs_multi_anchor: bool = False
    arithmetic_ceiling: bool = False
    covered_by_p1a: bool = False


PATTERNS: dict[str, Pattern] = {
    "ARITH_BETWEEN": Pattern(
        id="ARITH_BETWEEN",
        description='"how many {days|weeks} between X and Y"',
        needs_multi_anchor=True,
        arithmetic_ceiling=True,
    ),
    "ARITH_ANCHORED": Pattern(
        id="ARITH_ANCHORED",
        description='"how many {days|weeks} before/after/when X"',
        needs_multi_anchor=True,
        arithmetic_ceiling=True,
    ),
    "DURATION_SINCE": Pattern(
        id="DURATION_SINCE",
        description='"how long had I been X (when Y)" / "how long did I take"',
        needs_multi_anchor=True,
        arithmetic_ceiling=True,
    ),
    "AGE_OF_EVENT": Pattern(
        id="AGE_OF_EVENT",
        description='"how many {days|weeks|months} ago did I X"',
        needs_rerank=True,        # need correct event memory
        arithmetic_ceiling=True,  # answer (e.g. "3 weeks") not in memories
    ),
    "ORDER_OF_EVENTS": Pattern(
        id="ORDER_OF_EVENTS",
        description='"what is the order of X, Y, Z" (3+ events)',
        needs_multi_anchor=True,
        needs_rerank=True,
    ),
    "COMPARE_FIRST": Pattern(
        id="COMPARE_FIRST",
        description='"which X first" / "X or Y first"',
        needs_rerank=True,
        needs_multi_anchor=True,
    ),
    "COMPARE_LAST": Pattern(
        id="COMPARE_LAST",
        description='"which X last/most recent/latest"',
        needs_rerank=True,
        needs_multi_anchor=True,
    ),
    "TIME_OF_EVENT": Pattern(
        id="TIME_OF_EVENT",
        description='"when did I X" — answer is a date or time',
        # Answer typically *is* in a memory (the one describing X)
        # if that memory includes a date reference.
    ),
    "ORDINAL_FIRST": Pattern(
        id="ORDINAL_FIRST",
        description='"first/earliest/initial" (single subject)',
        needs_rerank=True,
    ),
    "ORDINAL_LAST": Pattern(
        id="ORDINAL_LAST",
        description='"last/latest/most recent/newest" (single subject)',
        needs_rerank=True,
    ),
    "RANGE_FILTER": Pattern(
        id="RANGE_FILTER",
        description='"in March" / "last week" / "this quarter"',
        covered_by_p1a=True,
    ),
    "OTHER": Pattern(
        id="OTHER",
        description="unclassified temporal question",
    ),
}


# Regex patterns, applied in order.  Each family deliberately tolerates
# phrasing variation ("how many"/"how much"/"how long", "before"/"after"/
# "since"/"when") so the taxonomy holds up on free-form conversational
# questions, not just dataset-specific templates.
_RX_ARITH_BETWEEN = re.compile(
    r"\b(how\s+(?:many|much|long))\b.*\bbetween\b.*\band\b",
    re.I | re.S,
)
# "how many/long/much X before/after/since/when Y" — anchored delta.
_RX_ARITH_ANCHORED = re.compile(
    r"\b(how\s+(?:many|much|long))\b"
    r".*\b(before|after|since|prior|later|earlier|following|"
    r"preceding|when)\b",
    re.I | re.S,
)
# "how many/how long/N days/weeks/months/years ago did I X"
_RX_AGE_OF_EVENT = re.compile(
    r"\bhow\s+(?:many|long)\b.*\b(days?|weeks?|months?|years?)\s+ago\b",
    re.I | re.S,
)
# Duration-from-start family.  Catches "how long had I been X",
# "how long did it take", "how many days did I spend on X",
# "how many weeks did I spend reading X".  Must come AFTER
# ARITH_BETWEEN so "between X and Y" wins when both match.
_RX_DURATION_SINCE = re.compile(
    r"\b(?:"
    r"how\s+long\s+(?:had|have)\s+i\s+been"
    r"|how\s+long\s+did\s+(?:i|it)\s+(?:take|use|spend)"
    r"|how\s+many\s+(?:days?|weeks?|months?|years?)\s+did\s+"
    r"(?:i|it)\s+(?:take|spend)"
    r"|how\s+many\s+(?:days?|weeks?|months?|years?)"
    r"\s+in\s+total\s+"
    r")\b",
    re.I,
)
# "what is the order of X, Y, Z" or "in what order"
_RX_ORDER_OF_EVENTS = re.compile(
    r"\b(?:what\s+is\s+the\s+order|in\s+what\s+order)\b",
    re.I,
)
# "when did I X" / "what was the date" — time-of-event lookup.
_RX_TIME_OF_EVENT = re.compile(
    r"\b(?:when\s+did\s+i|what\s+(?:was|is)\s+the\s+date\s+(?:on\s+which|that|of))\b",
    re.I,
)
_RX_WHICH = re.compile(r"\bwhich\b", re.I)
_RX_FIRST = re.compile(
    r"\b(first|earliest|initial|original)\b", re.I,
)
_RX_LAST = re.compile(
    r"\b(last|latest|most\s+recent(?:ly)?|newest)\b", re.I,
)
_RX_RANGE_MONTH = re.compile(
    r"\b(in|during|since|from)\s+"
    r"(january|february|march|april|may|june|july|"
    r"august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b",
    re.I,
)
_RX_RANGE_RELATIVE = re.compile(
    r"\b(last\s+(week|month|year|quarter)|this\s+(week|month|quarter|year)|"
    r"yesterday|today|"
    # Catches both "3 weeks ago" and "a week ago" / "two weeks ago".
    r"(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|\d+)\s+(days?|weeks?|months?|years?)\s+ago)\b",
    re.I,
)


def classify_pattern(question_text: str) -> str:
    """Classify a question into a temporal pattern bucket.

    Returns the Pattern.id string.  First match wins; patterns are
    checked most-specific first.  The order matters — an ARITH_BETWEEN
    question also matches ARITH_ANCHORED's "after"/"when", so between
    must be checked first.
    """
    q = question_text
    # Arithmetic family — most specific first
    if _RX_ARITH_BETWEEN.search(q):
        return "ARITH_BETWEEN"
    if _RX_AGE_OF_EVENT.search(q):
        return "AGE_OF_EVENT"
    if _RX_DURATION_SINCE.search(q):
        return "DURATION_SINCE"
    if _RX_ARITH_ANCHORED.search(q):
        return "ARITH_ANCHORED"
    # Order / sequence
    if _RX_ORDER_OF_EVENTS.search(q):
        return "ORDER_OF_EVENTS"
    # Comparative "which X ..." with an ordinal marker
    if _RX_WHICH.search(q):
        if _RX_FIRST.search(q):
            return "COMPARE_FIRST"
        if _RX_LAST.search(q):
            return "COMPARE_LAST"
        # "which" without an ordinal typically falls through to
        # TIME_OF_EVENT or OTHER; don't force a bucket here.
    # Time-of-event lookup
    if _RX_TIME_OF_EVENT.search(q):
        return "TIME_OF_EVENT"
    # Single-subject ordinals
    if _RX_FIRST.search(q):
        return "ORDINAL_FIRST"
    if _RX_LAST.search(q):
        return "ORDINAL_LAST"
    # Plain range filter (what P1a already handles)
    if _RX_RANGE_MONTH.search(q) or _RX_RANGE_RELATIVE.search(q):
        return "RANGE_FILTER"
    return "OTHER"


# ── Per-question evaluation ──────────────────────────────────────────


@dataclass
class QuestionResult:
    question_id: str
    question: str
    answer: str
    pattern: str
    answer_in_any_haystack: bool  # False = arithmetic-only ceiling
    answer_at_depth: int | None   # smallest k with recall=1, or None
    recall_at_5: float
    recall_at_20: float
    recall_at_50: float
    retrieved_ids: list[str] = field(default_factory=list)


def _answer_deepest_position(
    retrieved_contents: list[str],
    answer: str,
) -> int | None:
    """Return the 1-based index of the first retrieved item containing
    the answer, or None if not found.
    """
    for i in range(len(retrieved_contents)):
        if recall_at_k_qa(retrieved_contents[:i + 1], answer, k=i + 1):
            return i + 1
    return None


def _answer_in_full_haystack(
    haystack_contents: list[str],
    answer: str,
) -> bool:
    """Is the answer substring present in *any* memory in the haystack?

    False means the question is arithmetic-only — no retrieval
    improvement can score it under Recall@K, only RAG mode can.
    """
    return recall_at_k_qa(
        haystack_contents, answer, k=len(haystack_contents),
    ) > 0.0


async def evaluate_question_at_depth(
    svc: object,
    question: LongMemQuestion,
    haystack_contents: list[str],
    top_k: int = 50,
) -> QuestionResult:
    """Retrieve top-50 and record answer position + pattern."""
    from ncms.application.memory_service import MemoryService

    svc_typed: MemoryService = svc  # type: ignore[assignment]
    ref_time = _parse_lme_date(question.question_date)

    results = await svc_typed.search(
        query=question.question,
        limit=top_k,
        reference_time=ref_time,
    )
    contents = [s.memory.content for s in results]
    depth = _answer_deepest_position(contents, question.answer)

    return QuestionResult(
        question_id=question.question_id,
        question=question.question,
        answer=question.answer,
        pattern=classify_pattern(question.question),
        answer_in_any_haystack=_answer_in_full_haystack(
            haystack_contents, question.answer,
        ),
        answer_at_depth=depth,
        recall_at_5=recall_at_k_qa(contents, question.answer, k=5),
        recall_at_20=recall_at_k_qa(contents, question.answer, k=20),
        recall_at_50=recall_at_k_qa(contents, question.answer, k=50),
        retrieved_ids=[s.memory.id for s in results],
    )


# ── Aggregation and reporting ────────────────────────────────────────


def _bucket_row(results: list[QuestionResult], pattern: str) -> dict:
    """Aggregate per-pattern stats."""
    rs = [r for r in results if r.pattern == pattern]
    n = len(rs)
    if n == 0:
        return {"pattern": pattern, "count": 0}

    in_top_5 = sum(1 for r in rs if r.recall_at_5 > 0)
    in_top_20 = sum(1 for r in rs if r.recall_at_20 > 0)
    in_top_50 = sum(1 for r in rs if r.recall_at_50 > 0)
    not_in_haystack = sum(1 for r in rs if not r.answer_in_any_haystack)
    in_50_but_not_5 = in_top_50 - in_top_5
    in_20_but_not_5 = in_top_20 - in_top_5

    return {
        "pattern": pattern,
        "count": n,
        "recall_at_5": round(in_top_5 / n, 4),
        "recall_at_20": round(in_top_20 / n, 4),
        "recall_at_50": round(in_top_50 / n, 4),
        "in_top_5": in_top_5,
        "in_top_20_not_5": in_20_but_not_5,
        "in_top_50_not_5": in_50_but_not_5,
        "answer_not_in_haystack": not_in_haystack,
        "rerank_upside": in_50_but_not_5,
        "arithmetic_ceiling": not_in_haystack,
    }


def _format_markdown(
    buckets: list[dict], total_questions: int, elapsed_s: float,
) -> str:
    lines: list[str] = []
    lines.append("# Temporal-Reasoning Diagnostic")
    lines.append("")
    lines.append(
        f"**Questions analyzed:** {total_questions} "
        f"(LongMemEval temporal-reasoning category)"
    )
    lines.append(f"**Elapsed:** {elapsed_s:.1f} s")
    lines.append("**Retrieval depth:** top-50")
    lines.append("**Config:** features-on bundle (temporal_enabled=True)")
    lines.append("")
    lines.append("## Pattern Distribution & Recall")
    lines.append("")
    lines.append(
        "| Pattern | # | R@5 | R@20 | R@50 | "
        "Upside (20\\5) | Upside (50\\5) | Arith ceiling |"
    )
    lines.append(
        "|---|---:|---:|---:|---:|---:|---:|---:|"
    )
    for b in buckets:
        if b["count"] == 0:
            continue
        lines.append(
            f"| {b['pattern']} | {b['count']} "
            f"| {b['recall_at_5']:.3f} "
            f"| {b['recall_at_20']:.3f} "
            f"| {b['recall_at_50']:.3f} "
            f"| {b['in_top_20_not_5']} "
            f"| {b['in_top_50_not_5']} "
            f"| {b['arithmetic_ceiling']} |"
        )
    lines.append("")
    lines.append("**Reading the table:**")
    lines.append("")
    lines.append(
        "- **R@K** = fraction of questions where the answer text "
        "appears in the top-K retrieved memories."
    )
    lines.append(
        "- **Upside (20\\5)** = questions where the answer is in top-20 "
        "but not top-5.  These are the ones a re-rank can recover."
    )
    lines.append(
        "- **Upside (50\\5)** = same at depth 50.  Gap between the two "
        "upside columns measures how much a bigger candidate pool "
        "would help versus just better ranking."
    )
    lines.append(
        "- **Arith ceiling** = count of questions where the answer "
        "substring is present in *zero* memories in the haystack. "
        "Recall@K cannot score these at any depth; only RAG mode can."
    )
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────


async def _run(args: argparse.Namespace) -> None:
    from benchmarks.core.runner import wait_for_indexing
    from ncms.config import NCMSConfig

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Loading LongMemEval dataset...")
    sessions_by_q, questions = load_longmemeval_dataset(
        cache_dir=cache_dir, dataset_file=args.dataset,
    )
    temporal_qs = [
        q for q in questions if q.category == "temporal-reasoning"
    ]
    logger.info(
        "Loaded %d total, filtering to %d temporal-reasoning",
        len(questions), len(temporal_qs),
    )

    if args.limit:
        temporal_qs = temporal_qs[:args.limit]

    # Features-on bundle, same as the benchmark
    config = NCMSConfig(
        db_path=":memory:", actr_noise=0.0,
        splade_enabled=True,
        scoring_weight_bm25=0.6, scoring_weight_actr=0.0,
        scoring_weight_splade=0.3, scoring_weight_graph=0.3,
        admission_enabled=True,
        reconciliation_enabled=True,
        episodes_enabled=True,
        intent_classification_enabled=True,
        scoring_weight_hierarchy=0.1,
        level_first_enabled=True, topic_map_enabled=True,
        dream_query_expansion_enabled=True,
        intent_routing_enabled=True,
        reranker_enabled=True,
        temporal_enabled=True, scoring_weight_temporal=0.2,
        # P1-temporal-experiment Phase B — activates the full stack:
        # ordinal primitive, explicit-range filter, metadata-fallback
        # content_range at ingest, and the intent-routed dispatch.
        temporal_range_filter_enabled=True,
        scoring_weight_recency=0.1, recency_half_life_days=30.0,
        contradiction_detection_enabled=False,
        consolidation_knowledge_enabled=False,
        episode_consolidation_enabled=False,
        trajectory_consolidation_enabled=False,
        pattern_consolidation_enabled=False,
        synthesis_enabled=False,
    )

    # Shared SPLADE across questions to avoid reload cost
    from ncms.infrastructure.indexing.splade_engine import SpladeEngine
    shared_splade = SpladeEngine()
    logger.info("Shared SPLADE engine created")

    results: list[QuestionResult] = []
    t0 = time.perf_counter()

    for qi, q in enumerate(temporal_qs):
        q_sessions = sessions_by_q.get(q.question_id, [])
        if not q_sessions:
            continue

        shared_splade._vectors = {}
        store, _index, _graph, _splade, _cfg, svc = (
            await _create_ncms_instance(
                config=config, shared_splade=shared_splade,
            )
        )

        try:
            await _ingest_sessions(
                svc, q_sessions, haystack_dates=q.haystack_dates,
            )
            await wait_for_indexing(svc, run_logger=logger)

            # Full-haystack content for the "arithmetic ceiling" check
            haystack_contents = [
                turn.content
                for sess in q_sessions
                for turn in sess.turns
            ]

            result = await evaluate_question_at_depth(
                svc, q, haystack_contents, top_k=50,
            )
            results.append(result)
        finally:
            await store.close()

        if (qi + 1) % 10 == 0 or qi == 0:
            elapsed = time.perf_counter() - t0
            r5 = sum(1 for r in results if r.recall_at_5 > 0) / len(results)
            logger.info(
                "[%d/%d] %.0fs  running R@5=%.3f",
                qi + 1, len(temporal_qs), elapsed, r5,
            )

    elapsed = time.perf_counter() - t0
    logger.info("Diagnostic complete in %.0fs", elapsed)

    # Aggregate per-pattern
    buckets = [_bucket_row(results, p) for p in PATTERNS]
    # Sort by count descending for readability
    buckets.sort(key=lambda b: b.get("count", 0), reverse=True)

    # Write JSON (all per-question data)
    ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = output_dir / f"temporal_diagnostic_{ts}.json"
    with open(json_path, "w") as f:
        json.dump({
            "total_questions": len(results),
            "elapsed_seconds": round(elapsed, 1),
            "buckets": buckets,
            "per_question": [
                {
                    "question_id": r.question_id,
                    "question": r.question,
                    "answer": r.answer,
                    "pattern": r.pattern,
                    "answer_in_any_haystack": r.answer_in_any_haystack,
                    "answer_at_depth": r.answer_at_depth,
                    "recall_at_5": r.recall_at_5,
                    "recall_at_20": r.recall_at_20,
                    "recall_at_50": r.recall_at_50,
                }
                for r in results
            ],
        }, f, indent=2)
    logger.info("JSON: %s", json_path)

    # Symlink latest
    latest_json = output_dir / "temporal_diagnostic_latest.json"
    try:
        latest_json.unlink(missing_ok=True)
        latest_json.symlink_to(json_path.name)
    except OSError:
        pass

    # Write markdown
    md = _format_markdown(buckets, len(results), elapsed)
    md_path = output_dir / f"temporal_diagnostic_{ts}.md"
    with open(md_path, "w") as f:
        f.write(md)
    logger.info("Markdown: %s", md_path)

    latest_md = output_dir / "temporal_diagnostic_latest.md"
    try:
        latest_md.unlink(missing_ok=True)
        latest_md.symlink_to(md_path.name)
    except OSError:
        pass

    print()
    print(md)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="LongMemEval temporal-reasoning diagnostic",
    )
    parser.add_argument(
        "--cache-dir", type=str, default=None,
        help="LongMemEval cache dir (default: auto)",
    )
    parser.add_argument(
        "--output-dir", type=str,
        default="benchmarks/results/temporal_diagnostic",
    )
    parser.add_argument(
        "--dataset", type=str, default="longmemeval_oracle.json",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Cap number of questions (for quick iteration, 0 = all)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    setup_logging("temporal_diagnostic", output_dir, verbose=args.verbose)

    for name in (
        "sentence_transformers", "transformers", "torch", "httpx",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)

    log_run_header(
        "LongMemEval Temporal-Reasoning Diagnostic", logger,
    )
    run_async(_run(args), "Temporal diagnostic")


if __name__ == "__main__":
    main()
else:
    # Allow `python -m benchmarks.longmemeval.temporal_diagnostic`
    if sys.argv and sys.argv[0].endswith("__main__.py"):
        main()
