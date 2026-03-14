"""Dream cycle / consolidation evaluation harness.

Ingests a BEIR corpus with phases 1-3 enabled (admission, reconciliation,
episodes), then incrementally runs consolidation sub-phases while measuring
retrieval quality at each checkpoint.

Key difference from the retrieval ablation harness:
- Retrieval ablation swaps scoring weights on a fixed index.
- Dream harness measures how LLM-generated abstract memories affect retrieval.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from benchmarks.configs import TUNED_CONFIG
from benchmarks.datasets import DATASET_TOPICS
from benchmarks.dream_configs import DREAM_STAGES, DreamStage
from benchmarks.metrics import compute_all_metrics

logger = logging.getLogger(__name__)


# ── Data containers ──────────────────────────────────────────────────────


@dataclass
class DreamState:
    """Holds all in-memory backends and mappings for a dream experiment."""

    store: Any  # SQLiteStore
    index: Any  # TantivyEngine
    graph: Any  # NetworkXGraph
    splade: Any  # SpladeEngine
    config: Any  # NCMSConfig
    doc_to_mem: dict[str, str] = field(default_factory=dict)
    mem_to_doc: dict[str, str] = field(default_factory=dict)
    domain: str = "general"
    llm_model: str = ""
    llm_api_base: str = ""
    # Ingestion stats
    docs_ingested: int = 0
    episodes_created: int = 0
    ingestion_seconds: float = 0.0


@dataclass
class StageResult:
    """Results from a single dream stage."""

    stage_name: str
    display_name: str
    retrieval_metrics: dict[str, float] = field(default_factory=dict)
    consolidation_metrics: dict[str, int] = field(default_factory=dict)
    total_memories: int = 0
    insight_count: int = 0
    elapsed_seconds: float = 0.0


# ── Ingestion with phases 1-3 ────────────────────────────────────────────


async def ingest_with_phases(
    corpus: dict[str, dict[str, str]],
    dataset_name: str,
    llm_model: str,
    llm_api_base: str,
) -> DreamState:
    """Ingest a BEIR corpus with admission, reconciliation, and episodes enabled.

    Creates in-memory backends and wires the full phase 1-3 pipeline.
    Returns a DreamState with all backends ready for consolidation.
    """
    from ncms.application.admission_service import AdmissionService
    from ncms.application.episode_service import EpisodeService
    from ncms.application.memory_service import MemoryService
    from ncms.application.reconciliation_service import ReconciliationService
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
        actr_noise=0.0,  # Deterministic for benchmarks
        splade_enabled=True,
        graph_expansion_enabled=True,
        # Use tuned retrieval weights
        scoring_weight_bm25=TUNED_CONFIG.scoring_weight_bm25,
        scoring_weight_actr=TUNED_CONFIG.scoring_weight_actr,
        scoring_weight_splade=TUNED_CONFIG.scoring_weight_splade,
        scoring_weight_graph=TUNED_CONFIG.scoring_weight_graph,
        actr_threshold=TUNED_CONFIG.actr_threshold,
        # Enable phases 1-3
        admission_enabled=True,
        reconciliation_enabled=True,
        episodes_enabled=True,
        # LLM config for consolidation (set now, used later)
        consolidation_knowledge_model=llm_model,
        consolidation_knowledge_api_base=llm_api_base,
        # Higher cap for benchmark completeness
        consolidation_max_abstracts_per_run=100,
    )

    # Resolve domain and seed topics
    topic_info = DATASET_TOPICS.get(dataset_name, {})
    domain = topic_info.get("domain", "general") if topic_info else "general"
    labels = topic_info.get("labels", []) if topic_info else []

    if labels:
        await store.set_consolidation_value(
            f"entity_labels:{domain}",
            json.dumps(labels),
        )
        logger.info("Seeded topics for domain '%s': %s", domain, labels)

    # Create phase services
    admission = AdmissionService(store=store, index=index, graph=graph, config=config)
    reconciliation = ReconciliationService(store=store, config=config)
    episode = EpisodeService(store=store, index=index, config=config, splade=splade)

    # Wire into MemoryService
    svc = MemoryService(
        store=store, index=index, graph=graph, config=config,
        splade=splade, admission=admission, reconciliation=reconciliation,
        episode=episode,
    )

    # Ingest corpus
    doc_to_mem: dict[str, str] = {}
    mem_to_doc: dict[str, str] = {}

    total = len(corpus)
    t0 = time.perf_counter()
    last_log = t0

    for i, (doc_id, doc) in enumerate(corpus.items()):
        title = doc.get("title", "")
        text = doc.get("text", "")
        content = f"{title}\n{text}".strip() if title else text

        if not content:
            continue

        # Truncate very long documents (99.7% of BEIR docs < 10K chars)
        content = content[:10000]

        memory = await svc.store_memory(
            content=content,
            memory_type="fact",
            domains=[domain] if domain != "general" else [],
        )

        doc_to_mem[doc_id] = memory.id
        mem_to_doc[memory.id] = doc_id

        # Progress logging every 30 seconds
        now = time.perf_counter()
        if now - last_log >= 30.0 or i == total - 1:
            elapsed = now - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (total - i - 1) / rate if rate > 0 else 0
            logger.info(
                "  Ingested %d/%d docs (%.1f docs/sec, ETA %.0fs)",
                i + 1, total, rate, eta,
            )
            last_log = now

    elapsed = time.perf_counter() - t0
    logger.info(
        "Ingestion complete: %d docs in %.1fs (%.1f docs/sec)",
        len(doc_to_mem), elapsed, len(doc_to_mem) / elapsed if elapsed > 0 else 0,
    )

    state = DreamState(
        store=store, index=index, graph=graph, splade=splade,
        config=config, doc_to_mem=doc_to_mem, mem_to_doc=mem_to_doc,
        domain=domain, llm_model=llm_model, llm_api_base=llm_api_base,
        docs_ingested=len(doc_to_mem),
        ingestion_seconds=elapsed,
    )

    return state


# ── Episode closure ──────────────────────────────────────────────────────


async def force_close_episodes(state: DreamState) -> int:
    """Force-close all open episodes for benchmark consolidation.

    BEIR docs ingest instantaneously (no 24h temporal gap), so episodes
    never auto-close.  We close them manually so consolidation can
    generate summaries.

    Returns the number of episodes closed.
    """
    open_episodes = await state.store.get_open_episodes()
    if not open_episodes:
        logger.info("No open episodes to close")
        return 0

    now = datetime.now(UTC).isoformat()
    for ep_node in open_episodes:
        meta = dict(ep_node.metadata)
        meta["status"] = "closed"
        meta["closed_reason"] = "benchmark_force_close"
        meta["closed_at"] = now
        ep_node.metadata = meta
        await state.store.update_memory_node(ep_node)

    logger.info("Force-closed %d episodes for consolidation", len(open_episodes))
    state.episodes_created = len(open_episodes)
    return len(open_episodes)


# ── Access history injection ─────────────────────────────────────────────


async def inject_access_history(
    state: DreamState,
    queries: dict[str, str],
    qrels: dict[str, dict[str, int]],
) -> int:
    """Run queries to build access records for dream cycle rehearsal.

    Dream rehearsal needs min_access_count >= 3.  We run the query set
    multiple times to ensure sufficient access history.

    Returns total access records created.
    """
    from ncms.application.memory_service import MemoryService

    svc = MemoryService(
        store=state.store, index=state.index, graph=state.graph,
        config=state.config, splade=state.splade,
    )

    total_accesses = 0
    # Run queries 3 times to build sufficient access history
    for pass_num in range(3):
        for _qid, query_text in queries.items():
            results = await svc.search(
                query=query_text,
                domain=state.domain,
                limit=20,
            )
            total_accesses += len(results)

        logger.debug(
            "Access injection pass %d: %d cumulative accesses",
            pass_num + 1, total_accesses,
        )

    logger.info(
        "Injected %d access records across %d queries (3 passes)",
        total_accesses, len(queries),
    )
    return total_accesses


# ── Consolidation stage execution ────────────────────────────────────────


async def run_consolidation_stage(
    state: DreamState,
    stage: DreamStage,
) -> dict[str, int]:
    """Run a consolidation sub-phase and return metrics.

    Creates a ConsolidationService with the appropriate flags enabled
    for this stage, runs consolidation, and returns counts.
    """
    from ncms.application.consolidation_service import ConsolidationService
    from ncms.config import NCMSConfig

    # Build config with this stage's flags enabled
    config = NCMSConfig(
        db_path=":memory:",
        # Consolidation sub-phase flags
        episode_consolidation_enabled=stage.episode_consolidation,
        trajectory_consolidation_enabled=stage.trajectory_consolidation,
        pattern_consolidation_enabled=stage.pattern_consolidation,
        dream_cycle_enabled=stage.dream_cycle,
        # LLM config
        consolidation_knowledge_enabled=True,
        consolidation_knowledge_model=state.llm_model,
        consolidation_knowledge_api_base=state.llm_api_base,
        # Higher cap for benchmarks
        consolidation_max_abstracts_per_run=100,
        consolidation_knowledge_max_insights_per_run=50,
        # Lower thresholds for benchmark (standalone docs may not hit defaults)
        trajectory_min_transitions=2,
        pattern_min_episodes=2,
        pattern_entity_overlap_threshold=0.2,
        # Dream cycle: lower access threshold for benchmark
        dream_min_access_count=1,
        dream_rehearsal_fraction=0.20,
    )

    consolidation_svc = ConsolidationService(
        store=state.store, index=state.index, graph=state.graph,
        config=config, splade=state.splade,
    )

    all_metrics: dict[str, int] = {}

    for cycle in range(stage.cycles):
        result = await consolidation_svc.run_consolidation_pass()
        if cycle == 0:
            all_metrics = dict(result)
        else:
            # Accumulate across cycles
            for k, v in result.items():
                all_metrics[k] = all_metrics.get(k, 0) + v

        if stage.cycles > 1:
            logger.info("  Cycle %d/%d: %s", cycle + 1, stage.cycles, result)

    return all_metrics


# ── Retrieval measurement ────────────────────────────────────────────────


async def measure_retrieval(
    state: DreamState,
    queries: dict[str, str],
    qrels: dict[str, dict[str, int]],
) -> dict[str, float]:
    """Measure retrieval quality using TUNED config weights.

    Runs all queries, maps memory IDs to BEIR doc IDs, and computes
    standard IR metrics.  Also counts insight memories in the index.
    """
    from ncms.application.memory_service import MemoryService

    svc = MemoryService(
        store=state.store, index=state.index, graph=state.graph,
        config=state.config, splade=state.splade,
    )

    # Build rankings
    rankings: dict[str, list[str]] = {}
    for qid, query_text in queries.items():
        results = await svc.search(
            query=query_text,
            domain=state.domain,
            limit=100,
        )
        doc_ids: list[str] = []
        for scored in results:
            doc_id = state.mem_to_doc.get(scored.memory.id)
            if doc_id and doc_id not in doc_ids:
                doc_ids.append(doc_id)
        rankings[qid] = doc_ids

    # Compute metrics
    metrics = compute_all_metrics(rankings, qrels)

    # Count insight memories
    all_memories = await state.store.list_memories(limit=100000)
    insight_count = sum(1 for m in all_memories if m.type == "insight")
    metrics["insight_count"] = insight_count
    metrics["total_memories"] = len(all_memories)

    return metrics


# ── Main orchestrator ────────────────────────────────────────────────────


async def run_dream_experiment(
    dataset_name: str,
    corpus: dict[str, dict[str, str]],
    queries: dict[str, str],
    qrels: dict[str, dict[str, int]],
    llm_model: str,
    llm_api_base: str,
    stages: tuple[DreamStage, ...] | None = None,
) -> dict[str, Any]:
    """Run the full dream cycle experiment on a single dataset.

    Steps:
    1. Ingest corpus with phases 1-3 (admission, reconciliation, episodes)
    2. Force-close all episodes
    3. Measure baseline retrieval
    4. For each consolidation stage: run consolidation, measure retrieval
    5. Before dream stages, inject access history

    Returns:
        {
            "dataset": str,
            "ingestion": {...},
            "stages": {stage_name: StageResult},
        }
    """
    if stages is None:
        stages = DREAM_STAGES

    total_start = time.perf_counter()

    # Step 1: Ingest with phases 1-3
    logger.info("=" * 60)
    logger.info("Step 1: Ingesting %d documents with phases 1-3...", len(corpus))
    logger.info("=" * 60)
    state = await ingest_with_phases(corpus, dataset_name, llm_model, llm_api_base)

    # Step 2: Force-close episodes
    logger.info("Step 2: Force-closing episodes...")
    episodes_closed = await force_close_episodes(state)
    logger.info("  Episodes closed: %d", episodes_closed)

    # Track whether we've injected access history
    access_injected = False

    # Step 3+: Run stages
    stage_results: dict[str, dict[str, Any]] = {}
    baseline_ndcg: float | None = None

    for i, stage in enumerate(stages):
        logger.info("-" * 60)
        logger.info("Stage %d/%d: %s", i + 1, len(stages), stage.display_name)
        logger.info("-" * 60)

        t0 = time.perf_counter()

        # Inject access history before dream stages
        if stage.dream_cycle and not access_injected:
            logger.info("  Injecting access history for dream cycle...")
            await inject_access_history(state, queries, qrels)
            access_injected = True

        # Run consolidation (skip for baseline)
        consolidation_metrics: dict[str, int] = {}
        if stage.name != "baseline":
            logger.info("  Running consolidation...")
            consolidation_metrics = await run_consolidation_stage(state, stage)
            logger.info("  Consolidation: %s", consolidation_metrics)

        # Measure retrieval
        logger.info("  Measuring retrieval quality...")
        retrieval_metrics = await measure_retrieval(state, queries, qrels)

        elapsed = time.perf_counter() - t0

        # Compute delta from baseline
        ndcg = retrieval_metrics.get("nDCG@10", 0.0)
        if baseline_ndcg is None:
            baseline_ndcg = ndcg
            delta_pct = 0.0
        else:
            delta_pct = ((ndcg - baseline_ndcg) / baseline_ndcg * 100) if baseline_ndcg > 0 else 0.0

        stage_results[stage.name] = {
            "display_name": stage.display_name,
            "retrieval_metrics": retrieval_metrics,
            "consolidation_metrics": consolidation_metrics,
            "insight_count": int(retrieval_metrics.get("insight_count", 0)),
            "total_memories": int(retrieval_metrics.get("total_memories", 0)),
            "delta_pct": round(delta_pct, 2),
            "elapsed_seconds": round(elapsed, 2),
        }

        logger.info(
            "  nDCG@10=%.4f  MRR@10=%.4f  Recall@100=%.4f  delta=%.2f%%  insights=%d  (%.1fs)",
            ndcg,
            retrieval_metrics.get("MRR@10", 0),
            retrieval_metrics.get("Recall@100", 0),
            delta_pct,
            int(retrieval_metrics.get("insight_count", 0)),
            elapsed,
        )

    # Cleanup
    await state.store.close()

    total_elapsed = time.perf_counter() - total_start

    return {
        "dataset": dataset_name,
        "ingestion": {
            "docs_ingested": state.docs_ingested,
            "episodes_created": state.episodes_created,
            "ingestion_seconds": round(state.ingestion_seconds, 2),
            "llm_model": llm_model,
            "llm_api_base": llm_api_base,
        },
        "stages": stage_results,
        "total_elapsed_seconds": round(total_elapsed, 2),
    }
