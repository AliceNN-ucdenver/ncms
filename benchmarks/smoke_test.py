"""Quick smoke test: 100 docs + tricky queries end-to-end.

Validates the full ablation pipeline with a subset of data,
including queries with special characters that stress Tantivy's parser.

Usage:
    uv run python -m benchmarks.smoke_test
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from benchmarks.configs import CORE_CONFIGS
from benchmarks.datasets import load_beir_dataset
from benchmarks.harness import evaluate_dataset

logger = logging.getLogger("benchmarks")


async def run_smoke_test() -> None:
    """Run a 100-doc smoke test on SciFact."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info("=" * 60)
    logger.info("SMOKE TEST: 100 docs + tricky queries")
    logger.info("=" * 60)

    # Load SciFact
    corpus, queries, qrels = load_beir_dataset("scifact")

    # Pick 100 random docs — but ensure we include docs that have qrels
    relevant_doc_ids = set()
    for qrel in qrels.values():
        relevant_doc_ids.update(qrel.keys())

    # Include all relevant docs + random fill to 100
    selected_ids = list(relevant_doc_ids & set(corpus.keys()))
    remaining = list(set(corpus.keys()) - relevant_doc_ids)
    random.seed(42)
    random.shuffle(remaining)
    selected_ids.extend(remaining[: max(0, 100 - len(selected_ids))])
    selected_ids = selected_ids[:100]

    small_corpus = {doc_id: corpus[doc_id] for doc_id in selected_ids}
    logger.info("Selected %d docs (including %d relevant)", len(small_corpus), len(relevant_doc_ids & set(selected_ids)))

    # Use only queries that have special chars + some regular ones
    tricky_chars = "'\"():[]/{}\\"
    tricky_qids = [qid for qid, text in queries.items() if any(c in text for c in tricky_chars)]
    regular_qids = [qid for qid in qrels if qid not in tricky_qids]

    # Take 20 tricky + 10 regular queries (only ones with qrels)
    eval_qids = [qid for qid in tricky_qids if qid in qrels][:20]
    eval_qids += [qid for qid in regular_qids if qid in qrels][:10]

    small_queries = {qid: queries[qid] for qid in eval_qids if qid in queries}
    small_qrels = {qid: qrels[qid] for qid in eval_qids if qid in qrels}

    logger.info("Selected %d queries (%d with special chars)", len(small_queries), len([q for q in small_queries if q in tricky_qids]))

    # Log some example tricky queries
    for qid in list(small_queries)[:5]:
        logger.info("  Example query: %s", small_queries[qid][:80])

    t0 = time.perf_counter()

    # Run ALL core configs to validate each pipeline configuration
    logger.info("Configs: %s", [c.display_name for c in CORE_CONFIGS])

    results = await evaluate_dataset(
        dataset_name="scifact",
        corpus=small_corpus,
        queries=small_queries,
        qrels=small_qrels,
        configs=CORE_CONFIGS,
    )

    elapsed = time.perf_counter() - t0
    logger.info("=" * 60)
    logger.info("SMOKE TEST COMPLETE in %.1fs", elapsed)
    logger.info("=" * 60)

    # Print results
    for config_name, metrics in results.items():
        logger.info(
            "  %s: nDCG@10=%.4f  MRR@10=%.4f  Recall@100=%.4f",
            config_name,
            metrics.get("nDCG@10", 0),
            metrics.get("MRR@10", 0),
            metrics.get("Recall@100", 0),
        )

    # Validate results are reasonable
    for config_name, metrics in results.items():
        assert "nDCG@10" in metrics, f"Missing nDCG@10 for {config_name}"
        assert "MRR@10" in metrics, f"Missing MRR@10 for {config_name}"
        assert "Recall@100" in metrics, f"Missing Recall@100 for {config_name}"
        assert metrics["num_queries"] > 0, f"No queries evaluated for {config_name}"
        logger.info("  %s: %d queries evaluated", config_name, metrics["num_queries"])

    logger.info("ALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(run_smoke_test())
