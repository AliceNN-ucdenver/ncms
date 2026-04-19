"""Run the temporal-trajectory experiment and print a decision-table.

Usage::

    uv run python -m experiments.temporal_trajectory.run

Output: a per-query × per-strategy rank-1 / top-5 table, plus an
aggregate summary by query shape.  No LongMemEval involvement, no
NCMS pipeline — just the four retrievers from ``retrievers.py`` over
the 10-ADR corpus.
"""

from __future__ import annotations

from collections import defaultdict

from experiments.temporal_trajectory.queries import QUERIES, Query
from experiments.temporal_trajectory.retrievers import (
    _build_bm25_index,
    retrieve_path_rerank,
    run_all,
)


# Subject entities the retrievers use to scope to the authentication
# chain.  In production this would come from GLiNER on the query; for
# the experiment we hand-pass "authentication" so extraction noise
# doesn't confound the comparison.
AUTH_SUBJECTS = frozenset({
    "authentication",
    "JWT", "OAuth", "passkeys",
    "session cookies", "access tokens", "refresh tokens",
    "MFA", "multi-factor authentication", "WebAuthn",
})


def _hit(trace, gold: str) -> tuple[bool, int | None]:
    """Return (in_top_5, rank_1_or_None) for a trace."""
    in_5 = gold in trace.top_k
    rank = None
    for i, mid in enumerate(trace.full_ranking, 1):
        if mid[0] == gold:
            rank = i
            break
    return in_5, rank


def main() -> None:
    engine = _build_bm25_index()
    strategies = [
        "A_bm25", "B_bm25_date", "C_entity_scoped",
        "D_path_rerank", "E_lg_grammar",
    ]
    # Per-query trace for the raw table.
    rows: list[tuple[Query, dict]] = []
    for q in QUERIES:
        traces = run_all(q.text, AUTH_SUBJECTS, engine)
        row_data = {}
        for s in strategies:
            in_5, rank = _hit(traces[s], q.gold_mid)
            row_data[s] = (in_5, rank, traces[s].top_k)
        rows.append((q, row_data))

    # Raw per-query table + top-3 per strategy so we can see what's
    # winning rank 1 when gold isn't there.
    print()
    print("Per-query detail")
    print("=" * 92)
    for q, row in rows:
        print(f"\n[{q.shape}] {q.text}")
        print(f"  gold: {q.gold_mid}")
        for s in strategies:
            in_5, rank, top5 = row[s]
            mark = "✓" if in_5 else "✗"
            print(f"  {s:<18} gold@rank={rank}{mark:>2}  top-5: {top5}")
    print()

    # LG grammar introspection — print the syntactic proof for each query.
    print("LG (strategy E) — syntactic-proof per query")
    print("=" * 92)
    from experiments.temporal_trajectory.lg_retriever import retrieve_lg
    for q in QUERIES:
        bm25 = [(mid, score) for mid, score in engine.search(q.text, limit=20)]
        _, lg_trace = retrieve_lg(q.text, bm25)
        hit = "✓" if lg_trace.grammar_answer == q.gold_mid else (
            "—" if lg_trace.grammar_answer is None else "✗"
        )
        print(f"\n[{q.shape}] {q.text}")
        print(f"  intent: {lg_trace.intent.kind}(subject="
              f"{lg_trace.intent.subject}, entity={lg_trace.intent.entity})")
        print(f"  grammar_answer: {lg_trace.grammar_answer} {hit}  "
              f"(gold={q.gold_mid})")
        print(f"  proof: {lg_trace.proof}")
    print()

    # Aggregate by shape.
    print("Aggregate top-5 accuracy by query shape")
    print("=" * 80)
    header = f"{'Shape':<16} {'N':>3}"
    for s in strategies:
        short = s.split("_", 1)[1][:12]
        header += f"  {short:>12}"
    print(header)
    print("-" * 80)
    by_shape: dict[str, list[dict]] = defaultdict(list)
    for q, row in rows:
        by_shape[q.shape].append(row)
    for shape, rows_in_shape in by_shape.items():
        n = len(rows_in_shape)
        line = f"{shape:<16} {n:>3}"
        for s in strategies:
            hits = sum(1 for r in rows_in_shape if r[s][0])
            pct = hits / n if n else 0
            line += f"  {f'{hits}/{n} ({pct:.0%})':>12}"
        print(line)
    print()

    # Overall.
    print("Overall top-5 accuracy")
    print("=" * 80)
    total = len(rows)
    for s in strategies:
        hits = sum(1 for _, r in rows if r[s][0])
        pct = hits / total if total else 0
        print(f"  {s:<20} {hits:>2}/{total} ({pct:.0%})")
    print()

    # Rank-1 specifically.
    print("Rank-1 accuracy (gold at rank 1)")
    print("=" * 80)
    for s in strategies:
        rank1 = sum(1 for _, r in rows if r[s][1] == 1)
        pct = rank1 / total if total else 0
        print(f"  {s:<20} {rank1:>2}/{total} ({pct:.0%})")


if __name__ == "__main__":
    main()
