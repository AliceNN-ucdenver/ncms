"""Measure query-shape cache effectiveness as it warms.

Simulates a production workload: stream queries through the
grammar, track when productions fire vs when the shape cache
serves the intent.  Output: hit-rate curve over cumulative queries.

Measures three things:

1. **Cold-cache baseline** — every query falls through to productions.
2. **Warm-cache throughput** — duplicated queries hit cache.
3. **Skeleton coverage** — how many distinct skeletons emerge per
   100 unique queries (proxy for the structural diversity of
   natural-language queries about temporal memory).

Run::

    uv run python -m experiments.temporal_trajectory.run_cache_warming \\
        --n 500

The cache's value grows with workload scale; a production
deployment that sees many queries of similar shape sees the
amortized production cost drop toward zero.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


_ORACLE = Path(
    "/Users/shawnmccarthy/ncms/benchmarks/results/.cache/"
    "longmemeval/longmemeval_oracle.json"
)


def _load_queries(n: int) -> list[str]:
    """Load N LongMemEval questions — diverse real-world query shapes."""
    data = json.loads(_ORACLE.read_text())
    return [q["question"] for q in data[:n]]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=500)
    args = parser.parse_args()

    queries = _load_queries(args.n)
    # Duplicate the pool to simulate repeat queries — in production
    # the same shapes recur even if surface text differs.
    doubled = queries + queries

    # Fresh cache.
    from experiments.temporal_trajectory.shape_cache import QueryShapeCache
    from experiments.temporal_trajectory import shape_cache
    shape_cache.GLOBAL_CACHE = QueryShapeCache()   # reset
    from experiments.temporal_trajectory.query_parser import analyze_query

    # ── Cold run (fill cache) ─────────────────────────────────────
    cold_times: list[float] = []
    t_start = time.perf_counter()
    for q in queries:
        t = time.perf_counter()
        analyze_query(q)
        cold_times.append((time.perf_counter() - t) * 1000)
    cold_total = (time.perf_counter() - t_start) * 1000
    cache_size_after_cold = len(shape_cache.GLOBAL_CACHE)

    # ── Warm run (same queries) ────────────────────────────────────
    warm_times: list[float] = []
    t_start = time.perf_counter()
    for q in queries:
        t = time.perf_counter()
        analyze_query(q)
        warm_times.append((time.perf_counter() - t) * 1000)
    warm_total = (time.perf_counter() - t_start) * 1000

    # Report.
    print("Query-shape cache warming curve")
    print("=" * 70)
    print(f"Total queries:            {len(queries)}")
    print(f"Distinct skeletons:       {cache_size_after_cold}")
    print(f"  (≈ {100 * cache_size_after_cold / len(queries):.1f}% unique)")
    print()
    print(f"Cold run total:           {cold_total:.1f} ms")
    print(f"Warm run total:           {warm_total:.1f} ms")
    print(f"Speedup:                  {cold_total / max(0.001, warm_total):.2f}×")
    print()
    print(f"Cold p50 per query:       {sorted(cold_times)[len(cold_times)//2]:.2f} ms")
    print(f"Cold p95 per query:       {sorted(cold_times)[int(len(cold_times)*0.95)]:.2f} ms")
    print(f"Warm p50 per query:       {sorted(warm_times)[len(warm_times)//2]:.2f} ms")
    print(f"Warm p95 per query:       {sorted(warm_times)[int(len(warm_times)*0.95)]:.2f} ms")
    print()

    # Rolling cache-miss rate as cache warms.
    print("Cache miss rate as it warms (cold pass)")
    print("-" * 50)
    cache_size_by_query = []
    # Reset cache, re-run, track size after each.
    shape_cache.GLOBAL_CACHE = QueryShapeCache()
    for i, q in enumerate(queries, 1):
        size_before = len(shape_cache.GLOBAL_CACHE)
        analyze_query(q)
        size_after = len(shape_cache.GLOBAL_CACHE)
        cache_size_by_query.append((i, size_after))
    # Buckets of 50.
    for bucket in range(50, len(queries) + 1, 50):
        prev = next(
            (s for i, s in cache_size_by_query if i == bucket - 50),
            0,
        )
        cur = next((s for i, s in cache_size_by_query if i == bucket), None)
        if cur is None:
            continue
        new_in_bucket = cur - prev
        miss_rate = 100 * new_in_bucket / 50
        print(
            f"  queries {bucket-50+1:>3}..{bucket:<3}: "
            f"cache +{new_in_bucket:>3} new shapes  "
            f"(miss rate {miss_rate:5.1f}%)"
        )

    print()
    print(f"Final cache size: {len(shape_cache.GLOBAL_CACHE)} skeletons")

    # Summary per intent.
    print()
    print("Skeletons by intent (top 10)")
    print("-" * 50)
    by_intent: dict[str, list] = {}
    for shape in shape_cache.GLOBAL_CACHE._cache.values():
        by_intent.setdefault(shape.intent, []).append(shape)
    for intent in sorted(by_intent, key=lambda x: -len(by_intent[x])):
        shapes = by_intent[intent]
        shapes.sort(key=lambda s: -s.hit_count)
        top = shapes[0]
        print(
            f"  {intent:<20} skeletons={len(shapes):>3}  "
            f"top={top.hit_count}× shape={top.skeleton[:50]}"
        )


if __name__ == "__main__":
    main()
