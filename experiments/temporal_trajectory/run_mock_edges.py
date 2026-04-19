"""Run the full LG experiment using mock-reconciliation-generated
edges instead of hand-labeled ones.

Shows the realistic retrieval impact when NCMS's
``ReconciliationService`` (deterministic, no LLM) is the edge
producer.  Reports top-5 / rank-1 accuracy under the mock vs the
hand-labeled baseline.

Usage::

    uv run python -m experiments.temporal_trajectory.run_mock_edges
"""

from __future__ import annotations

from collections import defaultdict

from experiments.temporal_trajectory import corpus as _corpus
from experiments.temporal_trajectory.mock_reconciliation import (
    reconcile_corpus,
)
from experiments.temporal_trajectory.queries import QUERIES


def main() -> None:
    # Run with hand-labeled edges first for the baseline.
    hand_edges = list(_corpus.EDGES)

    print("=" * 70)
    print("Running LG over MOCK-RECONCILIATION edges")
    print("=" * 70)

    # Swap edges to mock output, then re-import modules that captured
    # EDGES at load time.
    mock_edges = reconcile_corpus()
    _corpus.EDGES = mock_edges
    # Force re-import of modules that cache state from EDGES.
    import importlib
    from experiments.temporal_trajectory import (
        edge_markers,
        grammar,
        query_parser,
        retrievers,
        run as run_module,
    )
    importlib.reload(edge_markers)
    importlib.reload(grammar)
    importlib.reload(query_parser)
    importlib.reload(retrievers)
    importlib.reload(run_module)

    # Run the main experiment loop with mock edges in place.
    from experiments.temporal_trajectory.retrievers import (
        _build_bm25_index, run_all,
    )
    AUTH_SUBJECTS = frozenset({
        "authentication", "JWT", "OAuth", "passkeys",
        "session cookies", "access tokens", "refresh tokens",
        "MFA", "multi-factor authentication", "WebAuthn",
    })

    engine = _build_bm25_index()
    strategies = [
        "A_bm25", "B_bm25_date", "C_entity_scoped",
        "D_path_rerank", "E_lg_grammar",
    ]
    rows = []
    for q in QUERIES:
        traces = run_all(q.text, AUTH_SUBJECTS, engine)
        row = {}
        for s in strategies:
            in_5 = q.gold_mid in traces[s].top_k
            rank = None
            for i, (mid, _) in enumerate(traces[s].full_ranking, 1):
                if mid == q.gold_mid:
                    rank = i
                    break
            row[s] = (in_5, rank, traces[s].top_k)
        rows.append((q, row))

    # Report.
    print()
    total = len(rows)
    print("Rank-1 accuracy (mock-reconciliation edges)")
    print("-" * 60)
    for s in strategies:
        r1 = sum(1 for _, r in rows if r[s][1] == 1)
        t5 = sum(1 for _, r in rows if r[s][0])
        print(f"  {s:<20} rank-1: {r1:>2}/{total}   top-5: {t5:>2}/{total}")
    print()

    # Also show which queries flipped relative to the hand-labeled run.
    print("LG (strategy E) per-query outcomes under mock edges:")
    for q, row in rows:
        in_5, rank, _ = row["E_lg_grammar"]
        mark = "✓" if rank == 1 else ("~top5" if in_5 else "✗")
        print(f"  [{q.shape:<14}] {q.text[:55]:<55} rank={rank} {mark}")

    # Restore hand-labeled edges so other runs are clean.
    _corpus.EDGES = hand_edges


if __name__ == "__main__":
    main()
