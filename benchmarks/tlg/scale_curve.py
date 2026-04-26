"""TLG scale-curve benchmark — dispatch latency vs corpus size.

Measures :func:`retrieve_lg` wall-clock at 100 / 1 k / 10 k / 100 k
ENTITY_STATE nodes.  Target per the p1-plan Phase 4: < 50 ms at all
sizes.

Design:

* Synthetic corpus — each size tier creates ``N`` subjects each with
  a single ENTITY_STATE node pointing at one entity.  Keeps the
  zone graph flat so we isolate the entity-index lookup cost.
* Warm the vocabulary + alias caches once, then fire 20 queries and
  report mean + p95 latency.
* Uses :memory: SQLite so disk I/O isn't in the measurement.

Usage:

    uv run python -m benchmarks.tlg.scale_curve

Writes a markdown report to
``benchmarks/results/tlg/scale_<timestamp>.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from ncms.application.tlg import VocabularyCache, retrieve_lg
from ncms.domain.models import (
    Entity,
    Memory,
    MemoryNode,
    NodeType,
)
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


async def _seed_corpus(store: SQLiteStore, n: int) -> list[str]:
    """Create ``n`` subjects, each with one ENTITY_STATE node.

    Returns the list of subject entity IDs so the benchmark can
    fire representative queries.
    """
    subjects: list[str] = []
    for i in range(n):
        subj_id = f"svc-{i:06d}"
        entity_name = f"feature-{i:06d}"
        ent = Entity(name=entity_name, type="concept")
        ent.id = entity_name
        await store.save_entity(ent)

        subj_ent = Entity(name=subj_id, type="concept")
        subj_ent.id = subj_id
        await store.save_entity(subj_ent)

        mem = Memory(
            content=f"{subj_id} currently uses {entity_name}.",
            domains=["tlg-scale"],
        )
        await store.save_memory(mem)
        await store.link_memory_entity(mem.id, entity_name)
        await store.link_memory_entity(mem.id, subj_id)

        node = MemoryNode(
            memory_id=mem.id,
            node_type=NodeType.ENTITY_STATE,
            metadata={
                "entity_id": subj_id,
                "state_key": "feature",
                "state_value": entity_name,
            },
        )
        await store.save_memory_node(node)
        subjects.append(subj_id)
    return subjects


async def _run_one_tier(n: int, query_count: int) -> dict:
    """Measure dispatch latency at corpus size ``n``.

    Returns a result dict with mean + p50 + p95 + max latencies (ms)
    and the query set used.
    """
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    try:
        t_seed_start = time.perf_counter()
        subjects = await _seed_corpus(store, n)
        seed_ms = (time.perf_counter() - t_seed_start) * 1000

        cache = VocabularyCache()
        # Warm: force caches + parser context build.
        warm_trace = await retrieve_lg(
            "What is the current feature?",
            store=store,
            vocabulary_cache=cache,
        )
        _ = warm_trace.intent.kind  # touch to silence lint

        # Sample subjects for queries (pick every Nth so we don't
        # stress the first few entries).
        sample_indices = [(i * n) // query_count for i in range(query_count)]
        queries: list[tuple[str, str]] = []
        for idx in sample_indices:
            subject = subjects[idx]
            entity = f"feature-{idx:06d}"
            queries.append(
                (
                    f"What is the current {entity} for {subject}?",
                    subject,
                )
            )

        timings_ms: list[float] = []
        for query, _subject in queries:
            t0 = time.perf_counter()
            trace = await retrieve_lg(
                query,
                store=store,
                vocabulary_cache=cache,
            )
            timings_ms.append((time.perf_counter() - t0) * 1000)
            # Sanity check — query should resolve.
            assert trace.intent.kind in {"current", "still"}, (
                f"unexpected intent {trace.intent.kind!r} at N={n}"
            )

        timings_ms.sort()
        return {
            "n": n,
            "seed_ms": round(seed_ms, 1),
            "mean_ms": round(statistics.mean(timings_ms), 2),
            "p50_ms": round(timings_ms[len(timings_ms) // 2], 2),
            "p95_ms": round(
                timings_ms[int(len(timings_ms) * 0.95)],
                2,
            ),
            "max_ms": round(max(timings_ms), 2),
            "n_queries": len(timings_ms),
        }
    finally:
        await store.close()


def _format_report(results: list[dict]) -> str:
    lines = [
        "# TLG scale curve",
        "",
        f"Timestamp: {datetime.now(UTC).isoformat()}",
        "",
        "Target: < 50 ms per dispatch at all corpus sizes (p1-plan §4 Phase 4).",
        "",
        "| N (nodes) | seed ms | mean ms | p50 ms | p95 ms | max ms | <50ms |",
        "|----------:|--------:|--------:|-------:|-------:|-------:|:-----:|",
    ]
    for row in results:
        target = "yes" if row["p95_ms"] < 50 else "no"
        lines.append(
            f"| {row['n']:>9} "
            f"| {row['seed_ms']:>9.1f} "
            f"| {row['mean_ms']:>9.2f} "
            f"| {row['p50_ms']:>8.2f} "
            f"| {row['p95_ms']:>8.2f} "
            f"| {row['max_ms']:>8.2f} "
            f"| {target:^18} |"
        )
    return "\n".join(lines) + "\n"


async def _main(sizes: list[int], query_count: int, output_dir: Path) -> None:
    results = []
    for n in sizes:
        print(f"[scale] N={n}...", flush=True)
        row = await _run_one_tier(n, query_count)
        print(
            f"    seed={row['seed_ms']}ms  mean={row['mean_ms']}ms  p95={row['p95_ms']}ms",
            flush=True,
        )
        results.append(row)

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = output_dir / f"scale_{stamp}.json"
    md_path = output_dir / f"scale_{stamp}.md"
    latest_json = output_dir / "scale_latest.json"
    latest_md = output_dir / "scale_latest.md"

    json_path.write_text(json.dumps(results, indent=2))
    md_path.write_text(_format_report(results))
    for link, target in [(latest_json, json_path), (latest_md, md_path)]:
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(target.name)
    print(f"[scale] wrote {md_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TLG dispatch latency vs corpus size",
    )
    parser.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=[100, 1_000, 10_000, 100_000],
        help="Corpus sizes to measure (nodes).",
    )
    parser.add_argument(
        "--queries",
        type=int,
        default=20,
        help="Number of queries to fire per tier.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("benchmarks/results/tlg"),
        help="Directory to write scale_<timestamp>.{md,json}.",
    )
    args = parser.parse_args()
    asyncio.run(_main(args.sizes, args.queries, args.output_dir))


if __name__ == "__main__":
    main()
