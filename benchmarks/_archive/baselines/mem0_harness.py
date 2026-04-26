"""Mem0 baseline harness for SWE-bench Django comparison.

Ingests the same SWE-bench Django corpus and evaluates the same
4 retrieval splits (AR, TTL, CR, LRU) using identical metrics,
enabling direct comparison with NCMS.

Requirements:
    pip install mem0ai

Usage:
    uv run python -m benchmarks.baselines.mem0_harness
    uv run python -m benchmarks.baselines.mem0_harness --embedding-model text-embedding-3-small
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Ingestion ────────────────────────────────────────────────────────────


def ingest_mem0(
    train: list[Any],
    embedding_model: str = "text-embedding-3-small",
) -> tuple[Any, dict[str, str], dict[str, str]]:
    """Ingest training instances into Mem0.

    Returns:
        (memory_client, doc_to_mem, mem_to_doc)
    """
    from mem0 import Memory  # type: ignore[import-untyped]

    config = {
        "llm": {
            "provider": "openai",
            "config": {
                "model": "gpt-4o-mini",
                "temperature": 0,
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": embedding_model,
            },
        },
    }

    m = Memory.from_config(config)

    doc_to_mem: dict[str, str] = {}
    mem_to_doc: dict[str, str] = {}

    total = len(train)
    t0 = time.time()

    for i, inst in enumerate(train):
        content = inst.content[:10000]
        metadata = {
            "instance_id": inst.instance_id,
            "version": inst.version,
            "subsystem": inst.subsystem,
            "created_at": inst.created_at,
        }

        try:
            result = m.add(
                content,
                user_id="swebench",
                metadata=metadata,
            )
            # Mem0 extracts memories via LLM — one doc can produce multiple
            # memories. Map ALL extracted memory IDs back to the source doc.
            if result and "results" in result:
                for mem_entry in result["results"]:
                    mem_id = mem_entry.get("id", "")
                    if mem_id:
                        # First memory becomes the primary mapping
                        if inst.instance_id not in doc_to_mem:
                            doc_to_mem[inst.instance_id] = mem_id
                        mem_to_doc[mem_id] = inst.instance_id

        except Exception as e:
            logger.warning("Failed to ingest %s: %s", inst.instance_id, e)

        if (i + 1) % 50 == 0 or i + 1 == total:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            logger.info(
                "Ingested %d/%d docs (%.1f docs/sec)",
                i + 1,
                total,
                rate,
            )

    elapsed = time.time() - t0
    logger.info(
        "Mem0 ingestion complete: %d docs mapped in %.1fs",
        len(doc_to_mem),
        elapsed,
    )

    return m, doc_to_mem, mem_to_doc


# ── Search adapter ───────────────────────────────────────────────────────


def search_mem0(
    m: Any,
    query: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Search Mem0 and return results with IDs and scores."""
    try:
        results = m.search(query, user_id="swebench", limit=limit)
        if isinstance(results, dict) and "results" in results:
            return results["results"]
        if isinstance(results, list):
            return results
        return []
    except Exception as e:
        logger.warning("Mem0 search failed: %s", e)
        return []


def results_to_doc_ids(
    results: list[dict[str, Any]],
    mem_to_doc: dict[str, str],
) -> list[str]:
    """Convert Mem0 results to ranked doc_id list."""
    doc_ids: list[str] = []
    for r in results:
        mem_id = r.get("id", "")
        # Also check metadata for instance_id fallback
        meta = r.get("metadata", {}) or {}
        doc_id = mem_to_doc.get(mem_id) or meta.get("instance_id", "")
        if doc_id and doc_id not in doc_ids:
            doc_ids.append(doc_id)
    return doc_ids


# ── Measurement functions ────────────────────────────────────────────────


def measure_ar(
    m: Any,
    mem_to_doc: dict[str, str],
    ar_queries: dict[str, str],
    ar_qrels: dict[str, dict[str, int]],
) -> dict[str, float]:
    """Measure Accurate Retrieval (AR) — file overlap matching."""
    from benchmarks.core.metrics import compute_all_metrics

    rankings: dict[str, list[str]] = {}
    for qid, query_text in ar_queries.items():
        results = search_mem0(m, query_text, limit=100)
        rankings[qid] = results_to_doc_ids(results, mem_to_doc)

    return compute_all_metrics(rankings, ar_qrels)


def measure_ttl(
    m: Any,
    mem_to_doc: dict[str, str],
    test_instances: list[Any],
    train_instances: list[Any],
    ttl_labels: dict[str, str],
) -> dict[str, float]:
    """Measure Test-Time Learning (TTL) — subsystem classification."""
    from benchmarks.core.metrics import classification_accuracy

    # Build train lookup
    train_lookup: dict[str, Any] = {inst.instance_id: inst for inst in train_instances}

    predictions: dict[str, str] = {}
    for inst in test_instances:
        if inst.instance_id not in ttl_labels:
            continue

        results = search_mem0(m, inst.content[:2000], limit=5)
        doc_ids = results_to_doc_ids(results, mem_to_doc)

        subsystem_votes: Counter[str] = Counter()
        for doc_id in doc_ids:
            train_inst = train_lookup.get(doc_id)
            if train_inst:
                subsystem_votes[train_inst.subsystem] += 1

        if subsystem_votes:
            predictions[inst.instance_id] = subsystem_votes.most_common(1)[0][0]
        else:
            predictions[inst.instance_id] = "other"

    acc = classification_accuracy(predictions, ttl_labels)
    return {"accuracy": acc, "num_queries": len(predictions)}


def measure_cr(
    m: Any,
    mem_to_doc: dict[str, str],
    cr_queries: dict[str, str],
    cr_qrels: dict[str, dict[str, int]],
) -> dict[str, float]:
    """Measure Conflict Resolution (CR) — temporal state ordering."""
    from benchmarks.core.metrics import compute_all_metrics, temporal_mrr

    targets: dict[str, str] = {}
    for qid, rels in cr_qrels.items():
        for doc_id, grade in rels.items():
            if grade == 2:
                targets[qid] = doc_id
                break

    rankings: dict[str, list[str]] = {}
    for qid, query_text in cr_queries.items():
        results = search_mem0(m, query_text, limit=100)
        rankings[qid] = results_to_doc_ids(results, mem_to_doc)

    ir_metrics = compute_all_metrics(rankings, cr_qrels)
    t_mrr = temporal_mrr(rankings, targets)

    return {
        "temporal_mrr": t_mrr,
        "nDCG@10": ir_metrics["nDCG@10"],
        "num_queries": ir_metrics["num_queries"],
    }


def measure_lru(
    m: Any,
    mem_to_doc: dict[str, str],
    lru_queries: dict[str, str],
    lru_qrels: dict[str, dict[str, int]],
) -> dict[str, float]:
    """Measure Long-Range Understanding (LRU) — subsystem coverage."""
    from benchmarks.core.metrics import compute_all_metrics

    rankings: dict[str, list[str]] = {}
    for qid, query_text in lru_queries.items():
        results = search_mem0(m, query_text, limit=100)
        rankings[qid] = results_to_doc_ids(results, mem_to_doc)

    return compute_all_metrics(rankings, lru_qrels)


# ── Experiment runner ────────────────────────────────────────────────────


def run_mem0_experiment(
    train: list[Any],
    test: list[Any],
    ar_queries: dict[str, str],
    ar_qrels: dict[str, dict[str, int]],
    ttl_labels: dict[str, str],
    cr_queries: dict[str, str],
    cr_qrels: dict[str, dict[str, int]],
    lru_queries: dict[str, str],
    lru_qrels: dict[str, dict[str, int]],
    embedding_model: str = "text-embedding-3-small",
) -> dict[str, Any]:
    """Run full Mem0 benchmark experiment."""
    t0 = time.time()

    # Ingest
    logger.info("Ingesting %d docs into Mem0...", len(train))
    m, doc_to_mem, mem_to_doc = ingest_mem0(train, embedding_model)
    ingest_time = time.time() - t0

    results: dict[str, Any] = {
        "system": "mem0",
        "embedding_model": embedding_model,
        "ingestion": {
            "docs_ingested": len(doc_to_mem),
            "docs_total": len(train),
            "ingestion_seconds": ingest_time,
        },
        "metrics": {},
    }

    # AR
    logger.info("Measuring AR (Accurate Retrieval)...")
    t1 = time.time()
    ar = measure_ar(m, mem_to_doc, ar_queries, ar_qrels)
    logger.info(
        "  AR nDCG@10=%.4f  MRR@10=%.4f  (%.1fs)",
        ar["nDCG@10"],
        ar["MRR@10"],
        time.time() - t1,
    )
    results["metrics"]["ar"] = ar

    # TTL
    logger.info("Measuring TTL (Test-Time Learning)...")
    t1 = time.time()
    ttl = measure_ttl(m, mem_to_doc, test, train, ttl_labels)
    logger.info("  TTL accuracy=%.4f  (%.1fs)", ttl["accuracy"], time.time() - t1)
    results["metrics"]["ttl"] = ttl

    # CR
    logger.info("Measuring CR (Conflict Resolution)...")
    t1 = time.time()
    cr = measure_cr(m, mem_to_doc, cr_queries, cr_qrels)
    logger.info("  CR temporal_mrr=%.4f  (%.1fs)", cr["temporal_mrr"], time.time() - t1)
    results["metrics"]["cr"] = cr

    # LRU
    logger.info("Measuring LRU (Long-Range Understanding)...")
    t1 = time.time()
    lru = measure_lru(m, mem_to_doc, lru_queries, lru_qrels)
    logger.info("  LRU nDCG@10=%.4f  (%.1fs)", lru["nDCG@10"], time.time() - t1)
    results["metrics"]["lru"] = lru

    results["total_seconds"] = time.time() - t0

    logger.info(
        "Mem0 experiment complete: AR=%.4f  TTL=%.4f  CR=%.4f  LRU=%.4f  (%.1fs)",
        ar["nDCG@10"],
        ttl["accuracy"],
        cr["temporal_mrr"],
        lru["nDCG@10"],
        results["total_seconds"],
    )

    return results


# ── CLI entry point ──────────────────────────────────────────────────────


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Mem0 SWE-bench benchmark")
    parser.add_argument(
        "--embedding-model",
        default="text-embedding-3-small",
        help="OpenAI embedding model (default: text-embedding-3-small)",
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

    if not os.environ.get("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY not set. Add it to .env or export it.")
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = output_dir / f"mem0_{timestamp}.log"

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )

    logger.info("Mem0 SWE-bench Benchmark")
    logger.info("  Embedding model: %s", args.embedding_model)
    logger.info("  Log: %s", log_file)

    # Load dataset
    from benchmarks.swebench.loader import load_swebench_django, split_train_test
    from benchmarks.swebench.qrels import (
        build_ar_qrels,
        build_cr_qrels,
        build_lru_queries,
        build_ttl_labels,
    )

    instances = load_swebench_django()
    train, test = split_train_test(instances)

    ar_queries: dict[str, str] = {}
    for inst in test:
        ar_queries[inst.instance_id] = inst.content[:2000]

    ar_qrels = build_ar_qrels(train, test)
    ttl_labels = build_ttl_labels(test)
    cr_qrels, cr_queries = build_cr_qrels(instances)
    lru_queries, lru_qrels = build_lru_queries(instances)

    # Run
    results = run_mem0_experiment(
        train=train,
        test=test,
        ar_queries=ar_queries,
        ar_qrels=ar_qrels,
        ttl_labels=ttl_labels,
        cr_queries=cr_queries,
        cr_qrels=cr_qrels,
        lru_queries=lru_queries,
        lru_qrels=lru_qrels,
        embedding_model=args.embedding_model,
    )

    # Save
    json_path = output_dir / f"mem0_{timestamp}.json"
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info("Results saved: %s", json_path)


if __name__ == "__main__":
    main()
