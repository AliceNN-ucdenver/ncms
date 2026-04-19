"""End-to-end LongMemEval evaluation on the mock-ingested corpus.

For each sampled question:

1. Ingest the haystack into a typed-edge corpus (mock reconciler +
   subject clustering + regex entity extraction).
2. Run the grammar against the question.
3. Compare the grammar's top answer to the question's
   ``answer_session_ids``.
4. Record grammar correct / abstained / wrong.

Baseline: same query against BM25-only (no grammar).  This isolates
grammar's contribution end-to-end.

Usage::

    uv run python -m experiments.temporal_trajectory.run_longmemeval \\
        --n 20 --types temporal-reasoning,knowledge-update

The ingest mutates global corpus state; we snapshot and restore
around each question so the harness can iterate.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


_ORACLE_PATH = Path(
    "/Users/shawnmccarthy/ncms/benchmarks/results/.cache/"
    "longmemeval/longmemeval_oracle.json"
)


def _load_oracle() -> list[dict]:
    return json.loads(_ORACLE_PATH.read_text())


def _snapshot_corpus() -> tuple[list, list]:
    from experiments.temporal_trajectory import corpus as _corpus
    return list(_corpus.ADR_CORPUS), list(_corpus.EDGES)


def _restore_corpus(mems: list, edges: list) -> None:
    import importlib

    from experiments.temporal_trajectory import corpus as _corpus
    _corpus.ADR_CORPUS = list(mems)
    _corpus.EDGES = list(edges)
    # Reload dependent modules so they pick up the restored corpus.
    from experiments.temporal_trajectory import (
        aliases,
        edge_markers,
        grammar,
        mock_reconciliation,
        query_parser,
        retirement_extractor,
        shape_cache,
        vocab_induction,
    )
    importlib.reload(vocab_induction)
    importlib.reload(edge_markers)
    importlib.reload(grammar)
    importlib.reload(aliases)
    importlib.reload(retirement_extractor)
    importlib.reload(mock_reconciliation)
    importlib.reload(query_parser)
    importlib.reload(shape_cache)


def _bm25_baseline(
    question: str, mems: list,
) -> str | None:
    """Cheap BM25-ish baseline: score each memory by the count of
    question content words that appear in its content.  Returns the
    highest-scoring memory's mid (or None if no word hits)."""
    from experiments.temporal_trajectory.longmemeval_ingest import (
        extract_question_topics,
    )
    topics = extract_question_topics(question)
    if not topics:
        return None
    topic_stems = [t.lower() for t in topics]
    best = None
    best_score = 0
    for m in mems:
        low = m.content.lower()
        score = sum(
            1 for t in topic_stems
            if re.search(rf"\b{re.escape(t)}\w*\b", low)
        )
        if score > best_score:
            best_score = score
            best = m.mid
    return best


def _run_single(
    question: dict, verbose: bool = False,
) -> dict:
    """Run one LongMemEval question through ingest + query.

    Returns a dict with keys:
        grammar_answer, grammar_confidence, grammar_correct,
        bm25_answer, bm25_correct, intent, abstained
    """
    from experiments.temporal_trajectory.longmemeval_ingest import (
        ingest_question,
    )
    corpus = ingest_question(question)

    # Import AFTER ingest — lg_retriever was reloaded so we need a
    # fresh binding to retrieve_lg on the reloaded module.
    import importlib
    from experiments.temporal_trajectory import lg_retriever as _lg
    importlib.reload(_lg)   # belt-and-suspenders
    retrieve_lg = _lg.retrieve_lg

    # Grammar.  We pass an empty bm25 list since our ingested corpus
    # doesn't have a BM25 index (not needed — grammar is standalone).
    bm25_empty: list[tuple[str, float]] = [
        (m.mid, 1.0) for m in corpus.memories
    ]
    _, trace = retrieve_lg(question["question"], bm25_empty)

    gold_set = set(question.get("answer_session_ids", []))
    grammar_mid = trace.grammar_answer if trace.has_confident_answer() else None
    grammar_correct = grammar_mid in gold_set if grammar_mid else False
    abstained = not trace.has_confident_answer()

    # BM25 baseline.
    bm25_mid = _bm25_baseline(question["question"], corpus.memories)
    bm25_correct = bm25_mid in gold_set if bm25_mid else False

    if verbose:
        print(f"\n  intent: {trace.intent.kind}  "
              f"subject: {trace.intent.subject}")
        print(f"  grammar_answer: {trace.grammar_answer}  "
              f"conf: {trace.confidence}")
        print(f"  proof: {trace.proof[:120]}")
        print(f"  bm25_answer: {bm25_mid}")
        print(f"  gold_sessions: {sorted(gold_set)}")

    return {
        "grammar_answer": grammar_mid,
        "grammar_confidence": trace.confidence,
        "grammar_correct": grammar_correct,
        "abstained": abstained,
        "bm25_answer": bm25_mid,
        "bm25_correct": bm25_correct,
        "intent": trace.intent.kind,
        "num_memories": len(corpus.memories),
        "num_subjects": len(
            {m.subject for m in corpus.memories}
        ),
        "num_edges": len(corpus.edges),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument(
        "--types",
        default="temporal-reasoning,knowledge-update,multi-session",
        help="Comma-separated LongMemEval question types to sample from.",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Deterministic stratified sample seed.",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--all", action="store_true",
        help="Run ALL questions matching --types (ignores --n).",
    )
    parser.add_argument(
        "--json-out", type=str, default=None,
        help="Dump per-question results as JSON to this path.",
    )
    args = parser.parse_args()

    oracle = _load_oracle()
    # "--types all" → all six LongMemEval question types.
    if args.types == "all":
        wanted_types = {
            "temporal-reasoning", "knowledge-update", "multi-session",
            "single-session-user", "single-session-assistant",
            "single-session-preference",
        }
    else:
        wanted_types = set(args.types.split(","))
    pool = [q for q in oracle if q.get("question_type") in wanted_types]

    # Deterministic stratified sample.
    import random
    rng = random.Random(args.seed)
    by_type: dict[str, list] = {}
    for q in pool:
        by_type.setdefault(q["question_type"], []).append(q)

    if args.all:
        sampled = pool
    else:
        per_type = max(1, args.n // len(by_type))
        sampled = []
        for t in by_type:
            rng.shuffle(by_type[t])
            sampled.extend(by_type[t][:per_type])
        sampled = sampled[:args.n]

    print(f"Sampled {len(sampled)} LongMemEval questions across types: {wanted_types}")
    print("=" * 80)

    # Snapshot original corpus so we can restore after the batch.
    orig_mems, orig_edges = _snapshot_corpus()

    results = []
    try:
        for i, q in enumerate(sampled, 1):
            header = (
                f"\n[{i}/{len(sampled)}] ({q['question_type']}) "
                f"{q['question'][:80]}"
            )
            print(header)
            result = _run_single(q, verbose=args.verbose)
            results.append((q, result))
            verdict = (
                "✓ grammar"
                if result["grammar_correct"]
                else ("~ abstain" if result["abstained"]
                      else "✗ wrong")
            )
            bm25_verdict = (
                "✓ bm25" if result["bm25_correct"] else "✗ bm25"
            )
            print(
                f"  {verdict}  |  {bm25_verdict}  |  "
                f"intent={result['intent']}  "
                f"answer={result['grammar_answer']}  "
                f"conf={result['grammar_confidence']}"
            )
    finally:
        _restore_corpus(orig_mems, orig_edges)

    # Report.
    print()
    print("=" * 80)
    total = len(results)
    grammar_correct = sum(1 for _, r in results if r["grammar_correct"])
    bm25_correct = sum(1 for _, r in results if r["bm25_correct"])
    abstained = sum(1 for _, r in results if r["abstained"])
    wrong_grammar = sum(
        1 for _, r in results
        if not r["grammar_correct"] and not r["abstained"]
    )

    print("End-to-end LongMemEval results")
    print("-" * 60)
    print(f"Total questions:       {total}")
    print(f"Grammar correct:       {grammar_correct}  "
          f"({100 * grammar_correct / total:.0f}%)")
    print(f"Grammar abstained:     {abstained}   "
          f"(→ BM25 takes over)")
    print(f"Grammar wrong (rank-1):{wrong_grammar}   "
          f"(confidently-wrong)")
    print(f"BM25-only correct:     {bm25_correct}  "
          f"({100 * bm25_correct / total:.0f}%)")
    print()

    # Grammar + BM25 fallback (the integration pattern).
    grammar_then_bm25 = sum(
        1 for _, r in results
        if r["grammar_correct"] or (r["abstained"] and r["bm25_correct"])
    )
    print(f"Grammar ∨ BM25-fallback: {grammar_then_bm25}  "
          f"({100 * grammar_then_bm25 / total:.0f}%)  "
          f"— the realistic integration mode")
    print()

    # Per-intent breakdown.
    by_intent: dict[str, list] = {}
    for _, r in results:
        by_intent.setdefault(r["intent"], []).append(r)
    print("Per-intent breakdown")
    print("-" * 60)
    for intent, rs in sorted(by_intent.items()):
        n = len(rs)
        gc = sum(1 for r in rs if r["grammar_correct"])
        ab = sum(1 for r in rs if r["abstained"])
        print(
            f"  {intent:<20} {n:>3}  grammar={gc}/{n}  "
            f"abstained={ab}/{n}"
        )

    # Per-question-type breakdown.
    by_qtype: dict[str, list] = {}
    for q, r in results:
        by_qtype.setdefault(q.get("question_type", "unknown"), []).append(r)
    print()
    print("Per-question-type breakdown")
    print("-" * 60)
    for qtype, rs in sorted(by_qtype.items()):
        n = len(rs)
        gc = sum(1 for r in rs if r["grammar_correct"])
        bm = sum(1 for r in rs if r["bm25_correct"])
        ab = sum(1 for r in rs if r["abstained"])
        wr = sum(1 for r in rs if not r["grammar_correct"] and not r["abstained"])
        combined = sum(
            1 for r in rs
            if r["grammar_correct"] or (r["abstained"] and r["bm25_correct"])
        )
        print(
            f"  {qtype:<28} {n:>4}  grammar={gc:>4}  abstain={ab:>4}  "
            f"wrong={wr:>3}  bm25={bm:>4}  combined={combined:>4}  "
            f"({100 * combined / n:.0f}%)"
        )

    # Optional JSON dump.
    if args.json_out:
        import json as _json
        payload = {
            "summary": {
                "total": total,
                "grammar_correct": grammar_correct,
                "grammar_abstained": abstained,
                "grammar_wrong": wrong_grammar,
                "bm25_correct": bm25_correct,
                "combined_correct": grammar_then_bm25,
            },
            "per_intent": {
                intent: {
                    "n": len(rs),
                    "grammar_correct": sum(1 for r in rs if r["grammar_correct"]),
                    "abstained": sum(1 for r in rs if r["abstained"]),
                }
                for intent, rs in by_intent.items()
            },
            "per_question_type": {
                qtype: {
                    "n": len(rs),
                    "grammar_correct": sum(1 for r in rs if r["grammar_correct"]),
                    "abstained": sum(1 for r in rs if r["abstained"]),
                    "bm25_correct": sum(1 for r in rs if r["bm25_correct"]),
                    "grammar_wrong": sum(
                        1 for r in rs
                        if not r["grammar_correct"] and not r["abstained"]
                    ),
                }
                for qtype, rs in by_qtype.items()
            },
            "per_question": [
                {
                    "question_id": q.get("question_id"),
                    "question_type": q.get("question_type"),
                    "question": q.get("question"),
                    **r,
                }
                for q, r in results
            ],
        }
        Path(args.json_out).write_text(_json.dumps(payload, indent=2))
        print(f"\nWrote JSON to {args.json_out}")


if __name__ == "__main__":
    main()
