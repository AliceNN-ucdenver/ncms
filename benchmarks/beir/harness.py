"""Core evaluation harness: ingest BEIR corpus, run queries, collect rankings.

Separates ingestion (slow, GLiNER per doc) from search (fast, config-swappable).
Ingests once per dataset, then runs all ablation configs against the same data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from benchmarks.core.configs import AblationConfig
from benchmarks.core.datasets import DATASET_TOPICS
from benchmarks.core.metrics import compute_all_metrics

logger = logging.getLogger(__name__)


async def ingest_corpus(
    corpus: dict[str, dict[str, str]],
    dataset_name: str,
) -> tuple[object, object, object, object, object, dict[str, str], dict[str, str]]:
    """Ingest a BEIR corpus into NCMS backends (single pass).

    Creates in-memory SQLite, Tantivy, NetworkX, and SPLADE backends.
    Seeds domain-specific topics before ingestion so GLiNER extracts
    relevant entity types.

    Args:
        corpus: {doc_id: {"title": str, "text": str}}
        dataset_name: Name of dataset (for topic seeding).

    Returns:
        Tuple of (store, index, graph, splade_engine, config, doc_to_mem, mem_to_doc)
        where doc_to_mem maps BEIR doc_id -> NCMS memory.id and vice versa.
    """
    from ncms.config import NCMSConfig
    from ncms.infrastructure.graph.networkx_store import NetworkXGraph
    from ncms.infrastructure.indexing.splade_engine import SpladeEngine
    from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
    from ncms.infrastructure.storage.sqlite_store import SQLiteStore

    # Create in-memory backends
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()

    index = TantivyEngine()
    index.initialize()

    graph = NetworkXGraph()

    splade = SpladeEngine()

    config = NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,  # Deterministic
        splade_enabled=True,
        scoring_weight_bm25=0.6,
        scoring_weight_actr=0.4,
        scoring_weight_splade=0.3,
    )

    # Seed domain-specific topics
    topic_info = DATASET_TOPICS.get(dataset_name, {})
    domain = topic_info.get("domain", "general") if topic_info else "general"
    labels = topic_info.get("labels", []) if topic_info else []

    if labels:
        await store.set_consolidation_value(
            f"entity_labels:{domain}",
            json.dumps(labels),
        )
        logger.info("Seeded topics for domain '%s': %s", domain, labels)

    # Create MemoryService for ingestion
    from ncms.application.memory_service import MemoryService

    svc = MemoryService(
        store=store, index=index, graph=graph, config=config, splade=splade,
    )
    await svc.start_index_pool()

    # Ingest corpus — concurrent with semaphore to overlap GPU/CPU work
    doc_to_mem: dict[str, str] = {}
    mem_to_doc: dict[str, str] = {}
    sem = asyncio.Semaphore(3)  # Max 3 concurrent store_memory calls

    async def _ingest_one(
        doc_id: str, doc: dict[str, str],
    ) -> tuple[str, str] | None:
        title = doc.get("title", "")
        text = doc.get("text", "")
        content = f"{title}\n{text}".strip() if title else text
        if not content:
            return None
        # Truncate very long documents (99.7% of BEIR docs are under 10K chars;
        # Spark vLLM max-model-len is 32768 tokens so 10K chars is safe)
        content = content[:10000]
        async with sem:
            memory = await svc.store_memory(
                content=content,
                memory_type="fact",
                domains=[domain] if domain != "general" else [],
            )
        return doc_id, memory.id

    total = len(corpus)
    t0 = time.perf_counter()
    completed = 0
    batch_size = 50
    items = list(corpus.items())

    for batch_start in range(0, total, batch_size):
        batch = items[batch_start : batch_start + batch_size]
        results = await asyncio.gather(
            *[_ingest_one(did, doc) for did, doc in batch],
        )
        for result in results:
            if result is not None:
                did, mid = result
                doc_to_mem[did] = mid
                mem_to_doc[mid] = did
        completed += len(batch)

        elapsed = time.perf_counter() - t0
        rate = completed / elapsed if elapsed > 0 else 0
        eta = (total - completed) / rate if rate > 0 else 0
        logger.info(
            "  Ingested %d/%d docs (%.1f docs/sec, ETA %.0fs)",
            completed, total, rate, eta,
        )

    elapsed = time.perf_counter() - t0
    logger.info(
        "Ingestion complete: %d docs in %.1fs (%.1f docs/sec)",
        len(doc_to_mem), elapsed, len(doc_to_mem) / elapsed if elapsed > 0 else 0,
    )

    # Wait for background indexing to finish before searching
    from benchmarks.core.runner import wait_for_indexing
    await wait_for_indexing(svc, run_logger=logger)

    return store, index, graph, splade, config, doc_to_mem, mem_to_doc


async def run_config_queries(
    store: object,
    index: object,
    graph: object,
    splade_engine: object,
    ablation_config: AblationConfig,
    queries: dict[str, str],
    mem_to_doc: dict[str, str],
    domain: str,
) -> dict[str, list[str]]:
    """Run all queries under a specific ablation configuration.

    Creates a new MemoryService with the ablation settings but reuses
    the pre-populated backends (store, index, graph, splade).

    Args:
        store: Pre-populated SQLiteStore.
        index: Pre-populated TantivyEngine.
        graph: Pre-populated NetworkXGraph.
        splade_engine: Pre-populated SpladeEngine (or None).
        ablation_config: Configuration for this ablation variant.
        queries: {query_id: query_text}
        mem_to_doc: Memory ID -> BEIR doc ID mapping.
        domain: Dataset domain for search filtering.

    Returns:
        {query_id: [doc_id_1, doc_id_2, ...]} ranked by score descending.
    """
    from ncms.application.memory_service import MemoryService
    from ncms.config import NCMSConfig

    config = NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
        splade_enabled=ablation_config.use_splade,
        scoring_weight_bm25=ablation_config.scoring_weight_bm25,
        scoring_weight_actr=ablation_config.scoring_weight_actr,
        scoring_weight_splade=ablation_config.scoring_weight_splade,
        scoring_weight_graph=ablation_config.scoring_weight_graph,
        actr_threshold=ablation_config.actr_threshold,
        contradiction_detection_enabled=False,
    )

    # Pass SPLADE engine only if this config uses it
    splade = splade_engine if ablation_config.use_splade else None

    svc = MemoryService(
        store=store, index=index, graph=graph, config=config, splade=splade,
    )

    rankings: dict[str, list[str]] = {}
    total = len(queries)
    t0 = time.perf_counter()
    last_log = t0

    for i, (query_id, query_text) in enumerate(queries.items()):
        results = await svc.search(
            query=query_text,
            domain=domain if domain != "general" else None,
            limit=100,
        )

        # Map NCMS memory IDs back to BEIR doc IDs
        ranked_doc_ids: list[str] = []
        for scored in results:
            doc_id = mem_to_doc.get(scored.memory.id)
            if doc_id:
                ranked_doc_ids.append(doc_id)

        rankings[query_id] = ranked_doc_ids

        # Progress logging every 30 seconds
        now = time.perf_counter()
        if now - last_log >= 30.0 or i == total - 1:
            elapsed = now - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            logger.info(
                "    Queried %d/%d (%.1f q/sec)", i + 1, total, rate,
            )
            last_log = now

    elapsed = time.perf_counter() - t0
    logger.info(
        "  Queries complete: %d queries in %.1fs (%.1f q/sec)",
        total, elapsed, total / elapsed if elapsed > 0 else 0,
    )

    return rankings


async def evaluate_dataset(
    dataset_name: str,
    corpus: dict[str, dict[str, str]],
    queries: dict[str, str],
    qrels: dict[str, dict[str, int]],
    configs: list[AblationConfig],
) -> dict[str, dict[str, float]]:
    """Run full ablation evaluation on a single dataset.

    Ingests once, then runs all configs.

    Args:
        dataset_name: Dataset name for topic seeding.
        corpus: BEIR corpus.
        queries: BEIR queries.
        qrels: BEIR relevance judgments.
        configs: List of ablation configurations to evaluate.

    Returns:
        {config_name: {metric_name: value}} for all configs.
    """
    logger.info("=" * 60)
    logger.info("Evaluating dataset: %s", dataset_name)
    logger.info("  Corpus: %d docs, Queries: %d", len(corpus), len(queries))
    logger.info("=" * 60)

    topic_info = DATASET_TOPICS.get(dataset_name, {})
    domain = topic_info.get("domain", "general") if topic_info else "general"

    results: dict[str, dict[str, float]] = {}

    # Ingest corpus
    logger.info("Ingesting corpus...")
    store, index, graph, splade, _config, doc_to_mem, mem_to_doc = (
        await ingest_corpus(corpus, dataset_name)
    )

    # Only query queries that have relevance judgments (saves ~70% for SciFact)
    eval_queries = {qid: queries[qid] for qid in qrels if qid in queries}
    logger.info("  Evaluation queries: %d (of %d total)", len(eval_queries), len(queries))

    # Run all configs against the ingested data
    for config in configs:
        logger.info("Running config '%s'...", config.display_name)
        t0 = time.perf_counter()

        rankings = await run_config_queries(
            store=store,
            index=index,
            graph=graph,
            splade_engine=splade,
            ablation_config=config,
            queries=eval_queries,
            mem_to_doc=mem_to_doc,
            domain=domain,
        )

        metrics = compute_all_metrics(rankings, qrels)
        elapsed = time.perf_counter() - t0

        results[config.name] = {
            **metrics,
            "elapsed_seconds": round(elapsed, 1),
        }

        logger.info(
            "  %s: nDCG@10=%.4f  MRR@10=%.4f  Recall@100=%.4f  (%.1fs)",
            config.display_name,
            metrics["nDCG@10"],
            metrics["MRR@10"],
            metrics["Recall@100"],
            elapsed,
        )

    # Cleanup
    await store.close()

    return results
