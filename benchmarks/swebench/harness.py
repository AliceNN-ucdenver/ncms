"""SWE-bench multi-split dream cycle evaluation harness.

Adapts the BEIR dream harness for SWE-bench Django issues with
4 memory competency splits (AR, TTL, LRU, CR) evaluated at each
consolidation stage.

Key differences from dream_harness.py:
- Chronological ingestion (by created_at)
- Multi-split evaluation at each stage
- Extended graph connectivity diagnostics
- SWE-bench-specific metadata per memory
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import networkx as nx

from benchmarks.core.metrics import (
    classification_accuracy,
    compute_all_metrics,
    temporal_mrr,
)
from benchmarks.dream.configs import DreamStage
from benchmarks.swebench.configs import ACTR_SWEEP_WEIGHTS, DREAM_STAGES, TUNED_WEIGHTS
from benchmarks.swebench.loader import SWEInstance

logger = logging.getLogger(__name__)


# ── Data containers ──────────────────────────────────────────────────────


@dataclass
class SWEState:
    """Holds all in-memory backends and mappings for a SWE-bench experiment."""

    store: Any  # SQLiteStore
    index: Any  # TantivyEngine
    graph: Any  # NetworkXGraph
    splade: Any  # SpladeEngine
    config: Any  # NCMSConfig
    doc_to_mem: dict[str, str] = field(default_factory=dict)
    mem_to_doc: dict[str, str] = field(default_factory=dict)
    domain: str = "django"
    llm_model: str = ""
    llm_api_base: str = ""
    reranker: Any = None  # CrossEncoderReranker (Phase 10)
    # Metadata
    train_instances: list[SWEInstance] = field(default_factory=list)
    test_instances: list[SWEInstance] = field(default_factory=list)
    # Ingestion stats
    docs_ingested: int = 0
    episodes_created: int = 0
    ingestion_seconds: float = 0.0


@dataclass
class SplitResults:
    """Results for all 4 competency splits at one stage."""

    ar: dict[str, float] = field(default_factory=dict)  # nDCG@10, MRR@10, Recall@100
    ttl: dict[str, float] = field(default_factory=dict)  # accuracy
    lru: dict[str, float] = field(default_factory=dict)  # entity_coverage_f1
    cr: dict[str, float] = field(default_factory=dict)  # temporal_mrr


@dataclass
class StageResult:
    """Results from a single experiment stage."""

    stage_name: str
    display_name: str
    split_results: SplitResults = field(default_factory=SplitResults)
    consolidation_metrics: dict[str, int] = field(default_factory=dict)
    actr_crossover: dict[str, dict[str, float]] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


# ── Ingestion ────────────────────────────────────────────────────────────


async def ingest_swebench(
    train: list[SWEInstance],
    llm_model: str,
    llm_api_base: str,
) -> SWEState:
    """Ingest SWE-bench Django training instances with phases 1-3 enabled.

    Documents are ingested in chronological order (by created_at).
    Each memory carries structured metadata from the SWE-bench instance.
    """
    from benchmarks.core.datasets import SWEBENCH_TOPICS
    from ncms.application.admission_service import AdmissionService
    from ncms.application.episode_service import EpisodeService
    from ncms.application.memory_service import MemoryService
    from ncms.application.reconciliation_service import ReconciliationService
    from ncms.config import NCMSConfig
    from ncms.infrastructure.graph.networkx_store import NetworkXGraph
    from ncms.infrastructure.indexing.splade_engine import SpladeEngine
    from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
    from ncms.infrastructure.storage.sqlite_store import SQLiteStore

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
        # Tuned retrieval weights
        scoring_weight_bm25=TUNED_WEIGHTS["bm25"],
        scoring_weight_actr=TUNED_WEIGHTS["actr"],
        scoring_weight_splade=TUNED_WEIGHTS["splade"],
        scoring_weight_graph=TUNED_WEIGHTS["graph"],
        actr_threshold=-2.0,
        # Enable phases 1-3
        admission_enabled=True,
        reconciliation_enabled=True,
        episodes_enabled=True,
        # Enable dream cycle flag globally so search logging populates
        # search_log table for PMI computation across ALL stages
        dream_cycle_enabled=True,
        # Phase 9: Query expansion disabled (Phase 10: 40K terms adds noise)
        dream_query_expansion_enabled=False,
        # Phase 4: Intent classification (required for Phase 11 recall routing)
        intent_classification_enabled=True,
        # Phase 10: Cross-encoder reranking (selective by intent in Phase 11)
        reranker_enabled=True,
        reranker_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
        # LLM config
        consolidation_knowledge_model=llm_model,
        consolidation_knowledge_api_base=llm_api_base,
        consolidation_max_abstracts_per_run=100,
    )

    # Seed Django entity labels
    topic_info = SWEBENCH_TOPICS.get("swebench_django", {})
    labels = topic_info.get("labels", [])
    if labels:
        await store.set_consolidation_value(
            "entity_labels:django",
            json.dumps(labels),
        )
        logger.info("Seeded Django entity labels: %s", labels)

    # Create phase services
    admission = AdmissionService(store=store, index=index, graph=graph, config=config)
    reconciliation = ReconciliationService(store=store, config=config)
    episode = EpisodeService(store=store, index=index, config=config, splade=splade)

    # Cross-encoder reranker (Phase 10)
    reranker = None
    if config.reranker_enabled:
        from ncms.infrastructure.reranking.cross_encoder_reranker import (
            CrossEncoderReranker,
        )

        reranker = CrossEncoderReranker(
            model_name=config.reranker_model,
            cache_dir=config.model_cache_dir,
        )
        # Eagerly load model so first search doesn't pay load penalty
        reranker._ensure_model()
        logger.info("Cross-encoder reranker enabled: %s", config.reranker_model)

    svc = MemoryService(
        store=store, index=index, graph=graph, config=config,
        splade=splade, admission=admission, reconciliation=reconciliation,
        episode=episode, reranker=reranker,
    )

    # Ingest in chronological order
    doc_to_mem: dict[str, str] = {}
    mem_to_doc: dict[str, str] = {}
    skipped = 0
    total = len(train)
    t0 = time.perf_counter()
    last_log = t0

    for i, inst in enumerate(train):
        content = inst.content
        if not content:
            continue

        # Truncate very long issues
        content = content[:10000]

        memory = await svc.store_memory(
            content=content,
            memory_type="fact",
            domains=["django"],
            tags=[inst.subsystem, inst.version],
            structured={
                "instance_id": inst.instance_id,
                "version": inst.version,
                "created_at": inst.created_at,
                "files_touched": inst.files_touched,
                "subsystem": inst.subsystem,
            },
        )

        route = (memory.structured or {}).get("admission", {}).get("route")
        if route in ("discard", "ephemeral_cache"):
            skipped += 1
            continue

        doc_to_mem[inst.instance_id] = memory.id
        mem_to_doc[memory.id] = inst.instance_id

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
    if skipped > 0:
        logger.warning("Skipped %d/%d docs (ephemeral/discarded)", skipped, total)
    logger.info(
        "Ingestion complete: %d docs in %.1fs (%.1f docs/sec)",
        len(doc_to_mem), elapsed, len(doc_to_mem) / elapsed if elapsed > 0 else 0,
    )

    return SWEState(
        store=store, index=index, graph=graph, splade=splade,
        config=config, doc_to_mem=doc_to_mem, mem_to_doc=mem_to_doc,
        domain="django", llm_model=llm_model, llm_api_base=llm_api_base,
        reranker=reranker,
        train_instances=train,
        docs_ingested=len(doc_to_mem),
        ingestion_seconds=elapsed,
    )


# ── Episode closure (reuse from dream harness) ──────────────────────────


async def force_close_episodes(state: SWEState) -> int:
    """Force-close all open episodes for consolidation."""
    open_episodes = await state.store.get_open_episodes()
    if not open_episodes:
        return 0

    now = datetime.now(UTC).isoformat()
    for ep_node in open_episodes:
        meta = dict(ep_node.metadata)
        meta["status"] = "closed"
        meta["closed_reason"] = "benchmark_force_close"
        meta["closed_at"] = now
        ep_node.metadata = meta
        await state.store.update_memory_node(ep_node)

    logger.info("Force-closed %d episodes", len(open_episodes))
    state.episodes_created = len(open_episodes)
    return len(open_episodes)


# ── Access history injection ─────────────────────────────────────────────


async def inject_access_history(
    state: SWEState,
    queries: dict[str, str],
) -> int:
    """Run queries 3 times to build access records for dream rehearsal."""
    from ncms.application.memory_service import MemoryService

    svc = MemoryService(
        store=state.store, index=state.index, graph=state.graph,
        config=state.config, splade=state.splade,
        reranker=state.reranker,
    )

    total_accesses = 0
    for _pass_num in range(3):
        for query_text in queries.values():
            results = await svc.search(query=query_text, domain="django", limit=20)
            total_accesses += len(results)

    logger.info("Injected %d access records (3 passes × %d queries)", total_accesses, len(queries))
    return total_accesses


# ── Consolidation ────────────────────────────────────────────────────────


async def run_consolidation_stage(state: SWEState, stage: DreamStage) -> dict[str, int]:
    """Run a consolidation sub-phase."""
    from ncms.application.consolidation_service import ConsolidationService
    from ncms.config import NCMSConfig

    config = NCMSConfig(
        db_path=":memory:",
        episode_consolidation_enabled=stage.episode_consolidation,
        trajectory_consolidation_enabled=stage.trajectory_consolidation,
        pattern_consolidation_enabled=stage.pattern_consolidation,
        dream_cycle_enabled=stage.dream_cycle,
        consolidation_knowledge_enabled=True,
        consolidation_knowledge_model=state.llm_model,
        consolidation_knowledge_api_base=state.llm_api_base,
        consolidation_max_abstracts_per_run=100,
        trajectory_min_transitions=2,
        pattern_min_episodes=2,
        pattern_entity_overlap_threshold=0.2,
        dream_min_access_count=1,
        dream_rehearsal_fraction=0.20,
        # Phase 9: Enable query expansion + active forgetting during dream cycles
        dream_query_expansion_enabled=stage.dream_cycle,
        dream_active_forgetting_enabled=stage.dream_cycle,
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
            for k, v in result.items():
                all_metrics[k] = all_metrics.get(k, 0) + v
        if stage.cycles > 1:
            logger.info("  Cycle %d/%d: %s", cycle + 1, stage.cycles, result)

    return all_metrics


# ── AR: Accurate Retrieval measurement ───────────────────────────────────


async def measure_ar(
    state: SWEState,
    queries: dict[str, str],
    qrels: dict[str, dict[str, int]],
    actr_weight: float = 0.0,
) -> dict[str, float]:
    """Measure AR retrieval quality (nDCG@10, MRR@10, Recall@100)."""
    from ncms.application.memory_service import MemoryService
    from ncms.config import NCMSConfig

    config = NCMSConfig(
        **{
            **{k: v for k, v in state.config.model_dump().items()
               if k not in ("scoring_weight_actr", "actr_threshold")},
            "scoring_weight_actr": actr_weight,
            "actr_threshold": -2.0 if actr_weight > 0 else -999.0,
        }
    )

    svc = MemoryService(
        store=state.store, index=state.index, graph=state.graph,
        config=config, splade=state.splade,
    )

    rankings: dict[str, list[str]] = {}
    for qid, query_text in queries.items():
        results = await svc.search(query=query_text, domain="django", limit=100)
        doc_ids: list[str] = []
        for scored in results:
            doc_id = state.mem_to_doc.get(scored.memory.id)
            if doc_id and doc_id not in doc_ids:
                doc_ids.append(doc_id)
        rankings[qid] = doc_ids

    return compute_all_metrics(rankings, qrels)


# ── TTL: Test-Time Learning measurement ─────────────────────────────────


async def measure_ttl(
    state: SWEState,
    test_instances: list[SWEInstance],
    ttl_labels: dict[str, str],
) -> dict[str, float]:
    """Measure TTL classification accuracy.

    For each test issue, retrieve top-5 results. Majority subsystem
    among retrieved results = predicted label.
    """
    from ncms.application.memory_service import MemoryService

    svc = MemoryService(
        store=state.store, index=state.index, graph=state.graph,
        config=state.config, splade=state.splade,
        reranker=state.reranker,
    )

    predictions: dict[str, str] = {}
    for inst in test_instances:
        if inst.instance_id not in ttl_labels:
            continue

        results = await svc.search(
            query=inst.content[:2000],  # Truncate query
            domain="django",
            limit=5,
        )

        # Majority vote on subsystem
        subsystem_votes: Counter[str] = Counter()
        for scored in results:
            doc_id = state.mem_to_doc.get(scored.memory.id)
            if doc_id:
                # Look up subsystem from training instances
                for train_inst in state.train_instances:
                    if train_inst.instance_id == doc_id:
                        subsystem_votes[train_inst.subsystem] += 1
                        break

        if subsystem_votes:
            predictions[inst.instance_id] = subsystem_votes.most_common(1)[0][0]
        else:
            predictions[inst.instance_id] = "other"

    acc = classification_accuracy(predictions, ttl_labels)
    return {"accuracy": acc, "num_queries": len(predictions)}


# ── CR: Conflict Resolution measurement ─────────────────────────────────


async def measure_cr(
    state: SWEState,
    cr_queries: dict[str, str],
    cr_qrels: dict[str, dict[str, int]],
) -> dict[str, float]:
    """Measure CR temporal ordering accuracy.

    For each file-state query, retrieve results and check if the
    most recent issue (grade 2) ranks highest.
    """
    from ncms.application.memory_service import MemoryService

    svc = MemoryService(
        store=state.store, index=state.index, graph=state.graph,
        config=state.config, splade=state.splade,
        reranker=state.reranker,
    )

    # Build targets: query_id → most recent doc_id (grade 2)
    targets: dict[str, str] = {}
    for qid, rels in cr_qrels.items():
        for doc_id, grade in rels.items():
            if grade == 2:
                targets[qid] = doc_id
                break

    rankings: dict[str, list[str]] = {}
    for qid, query_text in cr_queries.items():
        results = await svc.search(query=query_text, domain="django", limit=100)
        doc_ids: list[str] = []
        for scored in results:
            doc_id = state.mem_to_doc.get(scored.memory.id)
            if doc_id and doc_id not in doc_ids:
                doc_ids.append(doc_id)
        rankings[qid] = doc_ids

    # Standard IR metrics on CR qrels
    ir_metrics = compute_all_metrics(rankings, cr_qrels)

    # Temporal MRR (most recent should rank first)
    t_mrr = temporal_mrr(rankings, targets)

    return {
        "temporal_mrr": t_mrr,
        "nDCG@10": ir_metrics["nDCG@10"],
        "num_queries": ir_metrics["num_queries"],
    }


# ── LRU: Long-Range Understanding measurement ───────────────────────────


async def measure_lru(
    state: SWEState,
    lru_queries: dict[str, str],
    lru_qrels: dict[str, dict[str, int]],
) -> dict[str, float]:
    """Measure LRU holistic understanding.

    Uses standard IR metrics — nDCG@10 measures whether results from
    the correct subsystem are ranked highly.
    """
    from ncms.application.memory_service import MemoryService

    svc = MemoryService(
        store=state.store, index=state.index, graph=state.graph,
        config=state.config, splade=state.splade,
        reranker=state.reranker,
    )

    rankings: dict[str, list[str]] = {}
    for qid, query_text in lru_queries.items():
        results = await svc.search(query=query_text, domain="django", limit=100)
        doc_ids: list[str] = []
        for scored in results:
            doc_id = state.mem_to_doc.get(scored.memory.id)
            if doc_id and doc_id not in doc_ids:
                doc_ids.append(doc_id)
        rankings[qid] = doc_ids

    return compute_all_metrics(rankings, lru_qrels)


# ── Recall-based measurements (Phase 11) ─────────────────────────────────


async def measure_ar_recall(
    state: SWEState,
    queries: dict[str, str],
    qrels: dict[str, dict[str, int]],
) -> dict[str, float]:
    """AR using structured recall — episode expansion adds recall."""
    from ncms.application.memory_service import MemoryService

    svc = MemoryService(
        store=state.store, index=state.index, graph=state.graph,
        config=state.config, splade=state.splade,
        reranker=state.reranker,
    )

    rankings: dict[str, list[str]] = {}
    for qid, query_text in queries.items():
        results = await svc.recall(query=query_text, domain="django", limit=10)
        # Primary results first (preserve BM25 ranking)
        doc_ids: list[str] = []
        for r in results:
            doc_id = state.mem_to_doc.get(r.memory.memory.id)
            if doc_id and doc_id not in doc_ids:
                doc_ids.append(doc_id)
        # Episode siblings appended AFTER all primary results
        for r in results:
            if r.context.episode:
                for sib_id in r.context.episode.sibling_ids:
                    sib_doc = state.mem_to_doc.get(sib_id)
                    if sib_doc and sib_doc not in doc_ids:
                        doc_ids.append(sib_doc)
        rankings[qid] = doc_ids

    return compute_all_metrics(rankings, qrels)


async def measure_cr_recall(
    state: SWEState,
    cr_queries: dict[str, str],
    cr_qrels: dict[str, dict[str, int]],
) -> dict[str, float]:
    """CR using structured recall — state lookup bypasses BM25."""
    from ncms.application.memory_service import MemoryService

    svc = MemoryService(
        store=state.store, index=state.index, graph=state.graph,
        config=state.config, splade=state.splade,
        reranker=state.reranker,
    )

    targets: dict[str, str] = {}
    for qid, rels in cr_qrels.items():
        for doc_id, grade in rels.items():
            if grade == 2:
                targets[qid] = doc_id
                break

    rankings: dict[str, list[str]] = {}
    for qid, query_text in cr_queries.items():
        results = await svc.recall(query=query_text, domain="django", limit=10)
        doc_ids: list[str] = []
        for r in results:
            doc_id = state.mem_to_doc.get(r.memory.memory.id)
            if doc_id and doc_id not in doc_ids:
                doc_ids.append(doc_id)
        rankings[qid] = doc_ids

    ir_metrics = compute_all_metrics(rankings, cr_qrels)
    t_mrr = temporal_mrr(rankings, targets)
    return {
        "temporal_mrr": t_mrr,
        "nDCG@10": ir_metrics["nDCG@10"],
        "num_queries": ir_metrics["num_queries"],
    }


async def measure_lru_recall(
    state: SWEState,
    lru_queries: dict[str, str],
    lru_qrels: dict[str, dict[str, int]],
) -> dict[str, float]:
    """LRU using structured recall — episode expansion helps long-range."""
    from ncms.application.memory_service import MemoryService

    svc = MemoryService(
        store=state.store, index=state.index, graph=state.graph,
        config=state.config, splade=state.splade,
        reranker=state.reranker,
    )

    rankings: dict[str, list[str]] = {}
    for qid, query_text in lru_queries.items():
        results = await svc.recall(query=query_text, domain="django", limit=10)
        # Primary results first (preserve BM25 ranking)
        doc_ids: list[str] = []
        for r in results:
            doc_id = state.mem_to_doc.get(r.memory.memory.id)
            if doc_id and doc_id not in doc_ids:
                doc_ids.append(doc_id)
        # Episode siblings appended AFTER all primary results
        for r in results:
            if r.context.episode:
                for sib_id in r.context.episode.sibling_ids:
                    sib_doc = state.mem_to_doc.get(sib_id)
                    if sib_doc and sib_doc not in doc_ids:
                        doc_ids.append(sib_doc)
        rankings[qid] = doc_ids

    return compute_all_metrics(rankings, lru_qrels)


# ── Graph diagnostics ────────────────────────────────────────────────────


async def capture_graph_diagnostics(state: SWEState) -> dict[str, Any]:
    """Capture knowledge graph connectivity diagnostics."""
    g = state.graph._graph  # Access underlying NetworkX graph

    n_entities = g.number_of_nodes()
    n_edges = g.number_of_edges()
    density = nx.density(g) if n_entities > 0 else 0.0

    # Connected components
    if n_entities > 0:
        components = list(nx.weakly_connected_components(g))
        n_components = len(components)
        largest_component = max(len(c) for c in components)
    else:
        n_components = 0
        largest_component = 0

    # PageRank
    if n_entities > 0 and n_edges > 0:
        pr = nx.pagerank(g)
        pr_values = list(pr.values())
        pr_sorted = sorted(pr.items(), key=lambda x: -x[1])[:10]
    else:
        pr_values = [0.0]
        pr_sorted = []

    # Degree distribution
    if n_entities > 0:
        degrees = [d for _, d in g.degree()]
        degree_mean = statistics.mean(degrees) if degrees else 0.0
        degree_max = max(degrees) if degrees else 0
        nodes_with_edges = sum(1 for d in degrees if d > 0)
    else:
        degree_mean = 0.0
        degree_max = 0
        nodes_with_edges = 0

    return {
        "entity_count": n_entities,
        "edge_count": n_edges,
        "density": round(density, 6),
        "components": n_components,
        "largest_component": largest_component,
        "nodes_with_edges": nodes_with_edges,
        "degree_mean": round(degree_mean, 2),
        "degree_max": degree_max,
        "pagerank_mean": round(statistics.mean(pr_values), 6),
        "pagerank_max": round(max(pr_values), 6) if pr_values else 0.0,
        "pagerank_top10": [(name, round(score, 6)) for name, score in pr_sorted],
    }


# ── ACT-R crossover sweep ───────────────────────────────────────────────


async def actr_crossover_sweep(
    state: SWEState,
    ar_queries: dict[str, str],
    ar_qrels: dict[str, dict[str, int]],
) -> dict[str, dict[str, float]]:
    """Sweep ACT-R weights to find crossover point."""
    results: dict[str, dict[str, float]] = {}
    best_ndcg = 0.0
    best_weight = 0.0

    for weight in ACTR_SWEEP_WEIGHTS:
        t0 = time.perf_counter()
        metrics = await measure_ar(state, ar_queries, ar_qrels, actr_weight=weight)
        elapsed = time.perf_counter() - t0

        key = f"actr_{weight}"
        metrics["elapsed_s"] = round(elapsed, 1)
        results[key] = metrics

        if metrics["nDCG@10"] > best_ndcg:
            best_ndcg = metrics["nDCG@10"]
            best_weight = weight

        logger.info(
            "    ACT-R=%.1f → nDCG@10=%.4f  MRR@10=%.4f  (%.1fs)",
            weight, metrics["nDCG@10"], metrics["MRR@10"], elapsed,
        )

    logger.info("    Best ACT-R weight: %.1f (nDCG@10=%.4f)", best_weight, best_ndcg)
    return results


# ── Main experiment orchestrator ─────────────────────────────────────────


async def run_swebench_experiment(
    train: list[SWEInstance],
    test: list[SWEInstance],
    ar_queries: dict[str, str],
    ar_qrels: dict[str, dict[str, int]],
    ttl_labels: dict[str, str],
    cr_queries: dict[str, str],
    cr_qrels: dict[str, dict[str, int]],
    lru_queries: dict[str, str],
    lru_qrels: dict[str, dict[str, int]],
    llm_model: str,
    llm_api_base: str,
    stages: tuple[DreamStage, ...] | None = None,
) -> dict[str, Any]:
    """Run the full SWE-bench dream cycle experiment.

    Returns a nested dict with all results, diagnostics, and ACT-R sweeps.
    """
    if stages is None:
        stages = DREAM_STAGES

    experiment_start = time.perf_counter()

    # Step 1: Ingest corpus
    logger.info("=" * 60)
    logger.info("SWE-bench Dream Cycle Experiment")
    logger.info("=" * 60)
    logger.info("  Train (corpus): %d issues", len(train))
    logger.info("  Test (queries): %d issues", len(test))
    logger.info("  AR queries: %d", len(ar_queries))
    logger.info("  CR queries: %d", len(cr_queries))
    logger.info("  LRU queries: %d", len(lru_queries))
    logger.info("  TTL labels: %d", len(ttl_labels))
    logger.info("  LLM: %s", llm_model)

    state = await ingest_swebench(train, llm_model, llm_api_base)
    state.test_instances = test

    # Step 2: Force-close episodes
    await force_close_episodes(state)

    # Step 3: Run stages
    results: dict[str, Any] = {
        "ingestion": {
            "docs_ingested": state.docs_ingested,
            "episodes_created": state.episodes_created,
            "ingestion_seconds": round(state.ingestion_seconds, 2),
            "llm_model": llm_model,
            "llm_api_base": llm_api_base,
        },
        "stages": {},
    }

    access_injected = False

    for stage in stages:
        logger.info("")
        logger.info("Stage: %s", stage.display_name)
        stage_start = time.perf_counter()

        # Inject access history before dream stages
        if stage.dream_cycle and not access_injected:
            logger.info("  Injecting access history for dream rehearsal...")
            await inject_access_history(state, ar_queries)
            access_injected = True

        # Run consolidation (skip for baseline)
        consolidation_metrics: dict[str, int] = {}
        if any([
            stage.episode_consolidation,
            stage.trajectory_consolidation,
            stage.pattern_consolidation,
            stage.dream_cycle,
        ]):
            logger.info("  Running consolidation...")
            consolidation_metrics = await run_consolidation_stage(state, stage)
            logger.info("  Consolidation: %s", consolidation_metrics)

        # Measure all splits
        logger.info("  Measuring AR (Accurate Retrieval)...")
        ar_metrics = await measure_ar(state, ar_queries, ar_qrels)
        logger.info("    AR nDCG@10=%.4f  MRR@10=%.4f", ar_metrics["nDCG@10"], ar_metrics["MRR@10"])

        logger.info("  Measuring TTL (Test-Time Learning)...")
        ttl_metrics = await measure_ttl(state, test, ttl_labels)
        logger.info("    TTL accuracy=%.4f", ttl_metrics["accuracy"])

        logger.info("  Measuring CR (Conflict Resolution)...")
        cr_metrics = await measure_cr(state, cr_queries, cr_qrels)
        logger.info("    CR temporal_mrr=%.4f", cr_metrics["temporal_mrr"])

        logger.info("  Measuring LRU (Long-Range Understanding)...")
        lru_metrics = await measure_lru(state, lru_queries, lru_qrels)
        logger.info("    LRU nDCG@10=%.4f", lru_metrics["nDCG@10"])

        # Phase 11: Recall-based measurements (structured retrieval)
        recall_metrics: dict[str, Any] = {}
        if state.config.intent_classification_enabled:
            logger.info("  Measuring recall-based metrics (Phase 11)...")
            try:
                ar_recall = await measure_ar_recall(state, ar_queries, ar_qrels)
                cr_recall = await measure_cr_recall(state, cr_queries, cr_qrels)
                lru_recall = await measure_lru_recall(
                    state, lru_queries, lru_qrels,
                )
                recall_metrics = {
                    "ar_ndcg10": ar_recall["nDCG@10"],
                    "ar_mrr10": ar_recall.get("MRR@10", 0.0),
                    "cr_temporal_mrr": cr_recall["temporal_mrr"],
                    "lru_ndcg10": lru_recall["nDCG@10"],
                }
                logger.info(
                    "    Recall: AR=%.4f  CR=%.4f  LRU=%.4f",
                    recall_metrics["ar_ndcg10"],
                    recall_metrics["cr_temporal_mrr"],
                    recall_metrics["lru_ndcg10"],
                )
            except Exception:
                logger.warning(
                    "Recall metrics failed, skipping", exc_info=True,
                )

        # ACT-R crossover sweep (AR only)
        logger.info("  Running ACT-R crossover sweep...")
        actr_results = await actr_crossover_sweep(state, ar_queries, ar_qrels)

        # Graph diagnostics
        logger.info("  Capturing diagnostics...")
        graph_diag = await capture_graph_diagnostics(state)

        stage_elapsed = time.perf_counter() - stage_start

        # Assemble stage results
        stage_result = {
            "display_name": stage.display_name,
            "ar": ar_metrics,
            "ttl": ttl_metrics,
            "cr": cr_metrics,
            "lru": lru_metrics,
            "recall": recall_metrics,
            "consolidation_metrics": consolidation_metrics,
            "actr_crossover": actr_results,
            "graph_diagnostics": graph_diag,
            "elapsed_seconds": round(stage_elapsed, 1),
        }
        results["stages"][stage.name] = stage_result

        logger.info(
            "  Stage complete: AR=%.4f  TTL=%.4f  CR=%.4f  LRU=%.4f  (%.1fs)",
            ar_metrics["nDCG@10"], ttl_metrics["accuracy"],
            cr_metrics["temporal_mrr"], lru_metrics["nDCG@10"],
            stage_elapsed,
        )

    total_elapsed = time.perf_counter() - experiment_start
    results["total_seconds"] = round(total_elapsed, 1)
    logger.info("")
    logger.info("Experiment complete: %.1fs total", total_elapsed)

    return results
