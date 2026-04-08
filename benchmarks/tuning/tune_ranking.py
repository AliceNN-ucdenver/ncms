"""Ranking weight grid search — finds optimal retrieval weights on BEIR datasets.

Reuses the ablation harness (ingest_corpus + run_config_queries) to evaluate a
focused grid of weight combinations around the best-performing region from the
Phase 6 ablation study.

Baseline (scifact, SPLADE v3 + sentence-transformers SparseEncoder):
  BM25+SPLADE+Graph  nDCG@10=0.7206  (best, tuned 2026-03-14)
  Full (+ ACT-R)     nDCG@10=0.6903  (ACT-R hurts on cold corpora)

Grid focuses on:
  - BM25: [0.5, 0.6, 0.7, 0.8]
  - ACT-R: [0.0, 0.1, 0.2] (0.4 hurt in baseline)
  - SPLADE: [0.2, 0.3, 0.4]
  - Graph: [0.0, 0.1, 0.2, 0.3]
  - Hierarchy: [0.0, 0.1, 0.2]
  - ACT-R threshold: [-3.0, -2.0]

Results written to benchmarks/tuning/ranking_grid_results.json + ranking_grid_report.md
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

TUNING_DIR = Path(__file__).parent

logger = logging.getLogger(__name__)


# ── Grid Definition ──────────────────────────────────────────────────────

GRID = {
    "bm25": [0.6, 0.7, 0.8],
    "actr": [0.0, 0.1],
    "splade": [0.2, 0.3, 0.4],
    "graph": [0.0, 0.2, 0.3],
    "hierarchy": [0.0, 0.1],
    "actr_threshold": [-2.0],
}


def _generate_configs() -> list[dict]:
    """Generate all grid search configurations."""
    keys = list(GRID.keys())
    configs = []
    for combo in itertools.product(*[GRID[k] for k in keys]):
        cfg = dict(zip(keys, combo, strict=False))
        # Skip configs with ACT-R weight > 0 but threshold disabled
        if cfg["actr"] == 0.0 and cfg["actr_threshold"] == -3.0:
            # Only need one threshold value when ACT-R is off
            continue
        configs.append(cfg)
    return configs


# ── Query Runner ──────────────────────────────────────────────────────────

async def _run_grid_queries(
    store: object,
    index: object,
    graph: object,
    splade_engine: object,
    weights: dict,
    queries: dict[str, str],
    mem_to_doc: dict[str, str],
    domain: str,
) -> dict[str, list[str]]:
    """Run all queries with a specific weight configuration.

    Custom version of harness.run_config_queries that supports hierarchy
    weight and intent classification parameters.
    """
    from ncms.application.memory_service import MemoryService
    from ncms.config import NCMSConfig

    has_actr = weights["actr"] > 0
    has_hierarchy = weights["hierarchy"] > 0

    config = NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
        splade_enabled=True,
        graph_expansion_enabled=weights["graph"] > 0,
        scoring_weight_bm25=weights["bm25"],
        scoring_weight_actr=weights["actr"],
        scoring_weight_splade=weights["splade"],
        scoring_weight_graph=weights["graph"],
        scoring_weight_hierarchy=weights["hierarchy"],
        actr_threshold=weights["actr_threshold"] if has_actr else -999.0,
        intent_classification_enabled=has_hierarchy,
        contradiction_detection_enabled=False,
    )

    svc = MemoryService(
        store=store, index=index, graph=graph, config=config, splade=splade_engine,
    )

    rankings: dict[str, list[str]] = {}
    for query_id, query_text in queries.items():
        results = await svc.search(
            query=query_text,
            domain=domain if domain != "general" else None,
            limit=100,
        )
        ranked_doc_ids: list[str] = []
        for scored in results:
            doc_id = mem_to_doc.get(scored.memory.id)
            if doc_id:
                ranked_doc_ids.append(doc_id)
        rankings[query_id] = ranked_doc_ids

    return rankings


# ── Evaluation ───────────────────────────────────────────────────────────

async def evaluate_grid(
    dataset_name: str = "scifact",
) -> dict:
    """Run grid search on a single dataset.

    Returns full results dict with provenance.
    """
    from benchmarks.datasets import DATASET_TOPICS, load_beir_dataset
    from benchmarks.harness import ingest_corpus
    from benchmarks.metrics import compute_all_metrics

    configs = _generate_configs()
    total_configs = len(configs)

    # Get git SHA
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True,
        ).strip()
    except Exception:
        sha = "unknown"

    results = {
        "timestamp": datetime.now(UTC).isoformat(),
        "git_sha": sha,
        "dataset": dataset_name,
        "grid": GRID,
        "total_configs": total_configs,
        "configs": [],
        "best": None,
    }

    print(f"Grid search: {total_configs} configs on {dataset_name}", flush=True)

    # Load dataset
    corpus, queries, qrels = load_beir_dataset(dataset_name)
    topic_info = DATASET_TOPICS.get(dataset_name, {})
    domain = topic_info.get("domain", "general") if topic_info else "general"

    # Only query queries that have relevance judgments
    eval_queries = {qid: queries[qid] for qid in qrels if qid in queries}
    print(f"  Corpus: {len(corpus)} docs, Eval queries: {len(eval_queries)}", flush=True)

    # Ingest corpus ONCE
    print("Ingesting corpus (slow, ~2 min)...", flush=True)
    t0 = time.perf_counter()
    store, index, graph, splade, _config, doc_to_mem, mem_to_doc = (
        await ingest_corpus(corpus, dataset_name)
    )
    ingest_time = time.perf_counter() - t0
    print(f"  Ingested in {ingest_time:.0f}s", flush=True)

    results["ingest_seconds"] = round(ingest_time, 1)

    # Run each config
    best_ndcg = -1.0
    best_cfg = None
    grid_start = time.perf_counter()

    for i, cfg in enumerate(configs):
        t0 = time.perf_counter()
        rankings = await _run_grid_queries(
            store=store,
            index=index,
            graph=graph,
            splade_engine=splade,
            weights=cfg,
            queries=eval_queries,
            mem_to_doc=mem_to_doc,
            domain=domain,
        )
        metrics = compute_all_metrics(rankings, qrels)
        elapsed = time.perf_counter() - t0

        entry = {
            "index": i,
            "weights": cfg,
            "nDCG@10": round(metrics["nDCG@10"], 5),
            "MRR@10": round(metrics["MRR@10"], 5),
            "Recall@10": round(metrics["Recall@10"], 5),
            "Recall@100": round(metrics["Recall@100"], 5),
            "elapsed_s": round(elapsed, 1),
        }
        results["configs"].append(entry)

        is_new_best = metrics["nDCG@10"] > best_ndcg
        if is_new_best:
            best_ndcg = metrics["nDCG@10"]
            best_cfg = entry

        # Log every config to file for incremental progress
        total_elapsed = time.perf_counter() - grid_start
        rate = (i + 1) / total_elapsed
        eta = (total_configs - i - 1) / rate if rate > 0 else 0
        best_marker = " ★ NEW BEST" if is_new_best else ""
        logger.info(
            "[%d/%d] nDCG@10=%.5f bm25=%.1f actr=%.1f splade=%.1f "
            "graph=%.1f hier=%.1f (%.1fs, %.2f cfg/s, ETA %.0fs)%s",
            i + 1, total_configs, metrics["nDCG@10"],
            cfg["bm25"], cfg["actr"], cfg["splade"],
            cfg["graph"], cfg["hierarchy"],
            elapsed, rate, eta, best_marker,
        )

        # Save incremental results every 10 configs (crash recovery)
        if (i + 1) % 10 == 0:
            results["best"] = best_cfg
            _write_results(results)

        # Console progress every 10 configs
        if (i + 1) % 10 == 0 or i == total_configs - 1:
            print(
                f"  [{i+1}/{total_configs}] "
                f"best nDCG@10={best_ndcg:.4f} "
                f"({rate:.1f} cfg/s, ETA {eta:.0f}s)",
                flush=True,
            )

    results["best"] = best_cfg
    results["total_seconds"] = round(time.perf_counter() - grid_start, 1)

    await store.close()

    # Write results
    _write_results(results)
    _write_report(results)

    return results


def _write_results(results: dict) -> None:
    """Write full grid results to JSON."""
    path = TUNING_DIR / "ranking_grid_results.json"
    path.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nResults written to {path}", flush=True)


def _write_report(results: dict) -> None:
    """Write human-readable report."""
    path = TUNING_DIR / "ranking_grid_report.md"

    best = results["best"]
    configs = results["configs"]

    # Sort by nDCG@10 descending
    top_10 = sorted(configs, key=lambda c: c["nDCG@10"], reverse=True)[:10]

    lines = [
        f"# Ranking Weight Grid Search — {results['timestamp']}",
        "",
        f"Git SHA: `{results['git_sha']}`",
        f"Dataset: **{results['dataset']}**",
        f"Total configs: {results['total_configs']}",
        f"Total time: {results['total_seconds']:.0f}s "
        f"(+ {results.get('ingest_seconds', 0):.0f}s ingest)",
        "",
        "## Grid Ranges",
        "",
    ]
    for param, values in results["grid"].items():
        lines.append(f"- **{param}**: {values}")
    lines.append("")

    # Baseline reference
    lines.extend([
        "## Baseline Reference (Phase 6)",
        "",
        "| Config | nDCG@10 |",
        "|--------|---------|",
        "| BM25 Only | 0.6871 |",
        "| + SPLADE + Graph | 0.6976 |",
        "| Full (+ ACT-R) | 0.6903 |",
        "",
    ])

    # Best config
    lines.extend([
        "## Best Config",
        "",
        f"**nDCG@10 = {best['nDCG@10']:.5f}** "
        f"(MRR@10={best['MRR@10']:.4f}, Recall@100={best['Recall@100']:.4f})",
        "",
        "| Parameter | Value |",
        "|-----------|-------|",
    ])
    for k, v in best["weights"].items():
        lines.append(f"| {k} | {v} |")
    lines.append("")

    improvement = best["nDCG@10"] - 0.6976
    lines.append(
        f"**Improvement over baseline**: "
        f"{improvement:+.4f} ({improvement/0.6976*100:+.2f}%)"
    )
    lines.append("")

    # Top 10
    lines.extend([
        "## Top 10 Configs",
        "",
        "| Rank | nDCG@10 | MRR@10 | BM25 | ACT-R | SPLADE | Graph | Hierarchy | Threshold |",
        "|------|---------|--------|------|-------|--------|-------|-----------|-----------|",
    ])
    for rank, cfg in enumerate(top_10, 1):
        w = cfg["weights"]
        lines.append(
            f"| {rank} | {cfg['nDCG@10']:.5f} | {cfg['MRR@10']:.4f} "
            f"| {w['bm25']} | {w['actr']} | {w['splade']} "
            f"| {w['graph']} | {w['hierarchy']} | {w['actr_threshold']} |"
        )
    lines.append("")

    # Bottom 5
    bottom_5 = sorted(configs, key=lambda c: c["nDCG@10"])[:5]
    lines.extend([
        "## Bottom 5 Configs (worst performing)",
        "",
        "| Rank | nDCG@10 | BM25 | ACT-R | SPLADE | Graph | Hierarchy | Threshold |",
        "|------|---------|------|-------|--------|-------|-----------|-----------|",
    ])
    for rank, cfg in enumerate(bottom_5, 1):
        w = cfg["weights"]
        lines.append(
            f"| {rank} | {cfg['nDCG@10']:.5f} "
            f"| {w['bm25']} | {w['actr']} | {w['splade']} "
            f"| {w['graph']} | {w['hierarchy']} | {w['actr_threshold']} |"
        )
    lines.append("")

    path.write_text("\n".join(lines) + "\n")
    print(f"Report written to {path}", flush=True)


def main() -> None:
    from benchmarks.env import load_dotenv
    load_dotenv()
    # Log to file in tuning directory (not stderr) for provenance
    log_path = TUNING_DIR / "ranking_grid_run.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, mode="w"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    print(f"Logging to {log_path}", flush=True)
    dataset = sys.argv[1] if len(sys.argv) > 1 else "scifact"
    asyncio.run(evaluate_grid(dataset_name=dataset))


if __name__ == "__main__":
    main()
