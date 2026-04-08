"""Run NCMS vs Mem0 vs Letta comparison on SWE-bench Django.

Loads dataset once, runs all three systems, produces comparison table.

Usage:
    uv run python -m benchmarks.baselines.run_comparison \
        --systems ncms,mem0,letta \
        --llm-model openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
        --llm-api-base http://spark-ee7d.local:8000/v1

    # Just Mem0 vs NCMS:
    uv run python -m benchmarks.baselines.run_comparison --systems ncms,mem0

    # Just Mem0:
    uv run python -m benchmarks.baselines.run_comparison --systems mem0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def run_ncms(
    train: list[Any],
    test: list[Any],
    ar_queries: dict[str, str],
    ar_qrels: dict[str, dict[str, int]],
    ttl_labels: dict[str, str],
    cr_queries: dict[str, str],
    cr_qrels: dict[str, dict[str, int]],
    lru_queries: dict[str, str],
    lru_qrels: dict[str, dict[str, int]],
    llm_model: str,
    llm_api_base: str,
) -> dict[str, Any]:
    """Run NCMS baseline (no consolidation, no dream — raw retrieval only)."""
    from benchmarks.swebench.harness import (
        ingest_swebench,
        measure_ar,
        measure_cr,
        measure_lru,
        measure_ttl,
    )

    async def _run() -> dict[str, Any]:
        t0 = time.time()

        # Ingest
        logger.info("NCMS: Ingesting %d docs...", len(train))
        state = await ingest_swebench(train, llm_model, llm_api_base)
        ingest_time = time.time() - t0

        results: dict[str, Any] = {
            "system": "ncms",
            "ingestion": {
                "docs_ingested": state.docs_ingested,
                "docs_total": len(train),
                "ingestion_seconds": ingest_time,
            },
            "metrics": {},
        }

        # AR
        logger.info("NCMS: Measuring AR...")
        t1 = time.time()
        ar = await measure_ar(state, ar_queries, ar_qrels)
        logger.info("  AR nDCG@10=%.4f  (%.1fs)", ar["nDCG@10"], time.time() - t1)
        results["metrics"]["ar"] = ar

        # TTL
        logger.info("NCMS: Measuring TTL...")
        t1 = time.time()
        ttl = await measure_ttl(state, test, ttl_labels)
        logger.info("  TTL accuracy=%.4f  (%.1fs)", ttl["accuracy"], time.time() - t1)
        results["metrics"]["ttl"] = ttl

        # CR
        logger.info("NCMS: Measuring CR...")
        t1 = time.time()
        cr = await measure_cr(state, cr_queries, cr_qrels)
        logger.info("  CR temporal_mrr=%.4f  (%.1fs)", cr["temporal_mrr"], time.time() - t1)
        results["metrics"]["cr"] = cr

        # LRU
        logger.info("NCMS: Measuring LRU...")
        t1 = time.time()
        lru = await measure_lru(state, lru_queries, lru_qrels)
        logger.info("  LRU nDCG@10=%.4f  (%.1fs)", lru["nDCG@10"], time.time() - t1)
        results["metrics"]["lru"] = lru

        # Recall (Phase 11)
        try:
            from benchmarks.swebench.harness import measure_ar_recall, measure_lru_recall
            logger.info("NCMS: Measuring Recall AR...")
            t1 = time.time()
            recall_ar = await measure_ar_recall(state, ar_queries, ar_qrels)
            logger.info(
                "  Recall AR nDCG@10=%.4f  (%.1fs)",
                recall_ar["nDCG@10"], time.time() - t1,
            )
            results["metrics"]["recall_ar"] = recall_ar

            logger.info("NCMS: Measuring Recall LRU...")
            t1 = time.time()
            recall_lru = await measure_lru_recall(state, lru_queries, lru_qrels)
            logger.info(
                "  Recall LRU nDCG@10=%.4f  (%.1fs)",
                recall_lru["nDCG@10"], time.time() - t1,
            )
            results["metrics"]["recall_lru"] = recall_lru
        except Exception as e:
            logger.warning("Recall metrics skipped: %s", e)

        results["total_seconds"] = time.time() - t0
        return results

    return asyncio.run(_run())


def generate_comparison_table(all_results: dict[str, dict[str, Any]]) -> str:
    """Generate markdown comparison table."""
    systems = list(all_results.keys())

    lines = [
        "## SWE-bench Django: Agent Memory System Comparison\n",
        "| Metric | " + " | ".join(s.upper() for s in systems) + " |",
        "|--------|" + "|".join("--------" for _ in systems) + "|",
    ]

    metrics = [
        ("AR nDCG@10", lambda r: r.get("metrics", {}).get("ar", {}).get("nDCG@10", 0)),
        ("AR MRR@10", lambda r: r.get("metrics", {}).get("ar", {}).get("MRR@10", 0)),
        ("TTL Accuracy", lambda r: r.get("metrics", {}).get("ttl", {}).get("accuracy", 0)),
        ("CR Temporal MRR", lambda r: r.get("metrics", {}).get("cr", {}).get("temporal_mrr", 0)),
        ("LRU nDCG@10", lambda r: r.get("metrics", {}).get("lru", {}).get("nDCG@10", 0)),
        (
            "Recall AR nDCG@10",
            lambda r: r.get("metrics", {}).get("recall_ar", {}).get("nDCG@10", "—"),
        ),
        (
            "Recall LRU nDCG@10",
            lambda r: r.get("metrics", {}).get("recall_lru", {}).get("nDCG@10", "—"),
        ),
        ("Docs Ingested", lambda r: r.get("ingestion", {}).get("docs_ingested", 0)),
        ("Ingest Time (s)", lambda r: r.get("ingestion", {}).get("ingestion_seconds", 0)),
        ("Total Time (s)", lambda r: r.get("total_seconds", 0)),
    ]

    for label, extractor in metrics:
        values = []
        for sys_name in systems:
            val = extractor(all_results[sys_name])
            if isinstance(val, float):
                values.append(f"{val:.4f}")
            elif isinstance(val, int):
                values.append(str(val))
            else:
                values.append(str(val))
        lines.append(f"| {label} | " + " | ".join(values) + " |")

    # Add notes
    lines.extend([
        "",
        "**Notes:**",
        "- All systems evaluated on identical SWE-bench Django dataset (850 issues, "
        "80/20 chrono split)",
        "- Same queries, same relevance judgments (qrels), same metric computation",
        "- NCMS: BM25 + SPLADE + Graph (no dense vectors, no OpenAI API calls)",
        "- Mem0/Letta: OpenAI text-embedding-3-small dense vectors",
        "- Recall metrics only available for NCMS (Phase 11 structured recall)",
    ])

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SWE-bench Django: NCMS vs Mem0 vs Letta comparison",
    )
    parser.add_argument(
        "--systems",
        default="ncms,mem0",
        help="Comma-separated systems to benchmark (ncms,mem0,letta)",
    )
    parser.add_argument(
        "--embedding-model",
        default="text-embedding-3-small",
        help="OpenAI embedding model for Mem0/Letta",
    )
    parser.add_argument(
        "--llm-model",
        default="openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16",
        help="LLM model for NCMS consolidation",
    )
    parser.add_argument(
        "--llm-api-base",
        default="http://spark-ee7d.local:8000/v1",
        help="LLM API base for NCMS",
    )
    parser.add_argument(
        "--output-dir",
        default="benchmarks/results/baselines",
        help="Output directory",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # Load env
    try:
        from benchmarks.env import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    # Override from env
    if os.getenv("LLM_MODEL"):
        args.llm_model = os.environ["LLM_MODEL"]
    if os.getenv("LLM_API_BASE"):
        args.llm_api_base = os.environ["LLM_API_BASE"]

    systems = [s.strip() for s in args.systems.split(",")]

    # Validate API key for external systems
    if any(s in systems for s in ("mem0", "letta")) and not os.environ.get("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY required for Mem0/Letta. Add to .env file.")
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = output_dir / f"comparison_{timestamp}.log"

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )

    logger.info("SWE-bench Django Comparison Benchmark")
    logger.info("  Systems: %s", ", ".join(systems))
    logger.info("  Embedding model: %s", args.embedding_model)
    logger.info("  Log: %s", log_file)

    # ── Load dataset (shared across all systems) ─────────────────────
    from benchmarks.swebench.loader import load_swebench_django, split_train_test
    from benchmarks.swebench.qrels import (
        build_ar_qrels,
        build_cr_qrels,
        build_lru_queries,
        build_ttl_labels,
    )

    logger.info("Loading SWE-bench Django dataset...")
    instances = load_swebench_django()
    train, test = split_train_test(instances)
    logger.info("  Train: %d  Test: %d", len(train), len(test))

    ar_queries: dict[str, str] = {}
    for inst in test:
        ar_queries[inst.instance_id] = inst.content[:2000]

    ar_qrels = build_ar_qrels(train, test)
    ttl_labels = build_ttl_labels(test)
    cr_qrels, cr_queries = build_cr_qrels(instances)
    lru_queries, lru_qrels = build_lru_queries(instances)

    logger.info(
        "  Queries: AR=%d  TTL=%d  CR=%d  LRU=%d",
        len(ar_queries), len(ttl_labels), len(cr_queries), len(lru_queries),
    )

    # ── Run each system ──────────────────────────────────────────────
    all_results: dict[str, dict[str, Any]] = {}
    json_path = output_dir / f"comparison_{timestamp}.json"
    md_path = output_dir / f"comparison_{timestamp}.md"

    def _save_incremental() -> None:
        """Save accumulated results after each system completes."""
        with open(json_path, "w") as f:
            json.dump(all_results, f, indent=2, default=str)
        table = generate_comparison_table(all_results)
        with open(md_path, "w") as f:
            f.write(table)
        logger.info("Incremental results saved (%d systems): %s", len(all_results), json_path)

    if "ncms" in systems:
        logger.info("\n" + "=" * 60)
        logger.info("Running NCMS benchmark...")
        logger.info("=" * 60)
        all_results["ncms"] = run_ncms(
            train, test, ar_queries, ar_qrels, ttl_labels,
            cr_queries, cr_qrels, lru_queries, lru_qrels,
            args.llm_model, args.llm_api_base,
        )
        _save_incremental()

    if "mem0" in systems:
        logger.info("\n" + "=" * 60)
        logger.info("Running Mem0 benchmark...")
        logger.info("=" * 60)
        from benchmarks.baselines.mem0_harness import run_mem0_experiment
        all_results["mem0"] = run_mem0_experiment(
            train, test, ar_queries, ar_qrels, ttl_labels,
            cr_queries, cr_qrels, lru_queries, lru_qrels,
            args.embedding_model,
        )
        _save_incremental()

    if "letta" in systems:
        logger.info("\n" + "=" * 60)
        logger.info("Running Letta (MemGPT) benchmark...")
        logger.info("=" * 60)
        from benchmarks.baselines.letta_harness import run_letta_experiment
        all_results["letta"] = run_letta_experiment(
            train, test, ar_queries, ar_qrels, ttl_labels,
            cr_queries, cr_qrels, lru_queries, lru_qrels,
            args.embedding_model,
        )
        _save_incremental()

    # ── Results ──────────────────────────────────────────────────────
    logger.info("\n" + "=" * 60)
    logger.info("RESULTS")
    logger.info("=" * 60)

    # Comparison table
    table = generate_comparison_table(all_results)
    logger.info("\n%s", table)

    # Final save (overwrites incremental)
    with open(json_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    logger.info("JSON results: %s", json_path)

    with open(md_path, "w") as f:
        f.write(table)
    logger.info("Markdown table: %s", md_path)


if __name__ == "__main__":
    main()
