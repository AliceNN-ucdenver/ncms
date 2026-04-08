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
import statistics
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import networkx as nx

from benchmarks.core.configs import TUNED_CONFIG
from benchmarks.core.datasets import DATASET_TOPICS
from benchmarks.core.metrics import compute_all_metrics, ndcg_at_k
from benchmarks.dream.configs import DREAM_STAGES, DreamStage
from ncms.domain.scoring import base_level_activation

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


@dataclass
class RetrievalResult:
    """Enriched retrieval output with per-query breakdowns for diagnostics."""

    metrics: dict[str, float]  # Aggregate IR metrics (nDCG, MRR, Recall)
    per_query_ndcg: dict[str, float] = field(default_factory=dict)  # qid → nDCG@10
    per_query_results: dict[str, list] = field(default_factory=dict)  # qid → ScoredMemory list


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
    skipped_ephemeral = 0

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

        # Skip ephemeral/discarded — not indexed, would corrupt retrieval metrics
        route = (memory.structured or {}).get("admission", {}).get("route")
        if route in ("discard", "ephemeral_cache"):
            skipped_ephemeral += 1
            continue

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
    if skipped_ephemeral > 0:
        logger.warning(
            "Skipped %d/%d docs (ephemeral/discarded by admission)",
            skipped_ephemeral, total,
        )
    logger.info(
        "Ingestion complete: %d docs indexed in %.1fs (%.1f docs/sec, %d skipped)",
        len(doc_to_mem), elapsed,
        len(doc_to_mem) / elapsed if elapsed > 0 else 0,
        skipped_ephemeral,
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
    actr_weight_override: float | None = None,
) -> RetrievalResult:
    """Measure retrieval quality using TUNED config weights.

    Runs all queries, maps memory IDs to BEIR doc IDs, and computes
    standard IR metrics.  Also captures per-query nDCG@10 and raw
    ScoredMemory results for diagnostic analysis.

    Args:
        actr_weight_override: If set, override the ACT-R scoring weight
            (used by the crossover sweep to test ACT-R at different levels).
    """
    from ncms.application.memory_service import MemoryService
    from ncms.config import NCMSConfig

    config = state.config
    if actr_weight_override is not None:
        # Create a new config with the overridden ACT-R weight
        config = NCMSConfig(
            **{
                **{k: v for k, v in state.config.model_dump().items()
                   if k != "scoring_weight_actr" and k != "actr_threshold"},
                "scoring_weight_actr": actr_weight_override,
                "actr_threshold": -2.0 if actr_weight_override > 0 else -999.0,
            }
        )

    svc = MemoryService(
        store=state.store, index=state.index, graph=state.graph,
        config=config, splade=state.splade,
    )

    # Build rankings and capture raw scored results
    rankings: dict[str, list[str]] = {}
    per_query_results: dict[str, list] = {}
    for qid, query_text in queries.items():
        results = await svc.search(
            query=query_text,
            domain=state.domain,
            limit=100,
        )
        per_query_results[qid] = results
        doc_ids: list[str] = []
        for scored in results:
            doc_id = state.mem_to_doc.get(scored.memory.id)
            if doc_id and doc_id not in doc_ids:
                doc_ids.append(doc_id)
        rankings[qid] = doc_ids

    # Compute aggregate metrics
    metrics = compute_all_metrics(rankings, qrels)

    # Compute per-query nDCG@10
    per_query_ndcg: dict[str, float] = {}
    for qid, qrel in qrels.items():
        if not any(v > 0 for v in qrel.values()):
            continue
        ranked = rankings.get(qid, [])
        per_query_ndcg[qid] = ndcg_at_k(ranked, qrel, 10)

    # Count insight memories
    all_memories = await state.store.list_memories(limit=100000)
    insight_count = sum(1 for m in all_memories if m.type == "insight")
    metrics["insight_count"] = insight_count
    metrics["total_memories"] = len(all_memories)

    return RetrievalResult(
        metrics=metrics,
        per_query_ndcg=per_query_ndcg,
        per_query_results=per_query_results,
    )


# ── ACT-R crossover sweep ────────────────────────────────────────────────

# ACT-R weight values to test at each stage
ACTR_CROSSOVER_WEIGHTS: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4)


async def actr_crossover_sweep(
    state: DreamState,
    queries: dict[str, str],
    qrels: dict[str, dict[str, int]],
) -> dict[str, dict[str, float]]:
    """Sweep ACT-R weights at the current stage to find optimal value.

    Tests retrieval with multiple ACT-R weights while keeping all other
    weights fixed at their tuned values.  This reveals whether dream
    cycle access patterns make ACT-R beneficial.

    Returns:
        {"actr_0.0": {"nDCG@10": ..., "MRR@10": ...}, "actr_0.1": {...}, ...}
    """
    results: dict[str, dict[str, float]] = {}

    for actr_w in ACTR_CROSSOVER_WEIGHTS:
        t0 = time.perf_counter()
        retrieval = await measure_retrieval(
            state, queries, qrels, actr_weight_override=actr_w,
        )
        elapsed = time.perf_counter() - t0
        key = f"actr_{actr_w:.1f}"
        results[key] = {
            "nDCG@10": retrieval.metrics.get("nDCG@10", 0.0),
            "MRR@10": retrieval.metrics.get("MRR@10", 0.0),
            "Recall@10": retrieval.metrics.get("Recall@10", 0.0),
            "Recall@100": retrieval.metrics.get("Recall@100", 0.0),
            "elapsed_s": round(elapsed, 1),
        }
        logger.info(
            "    ACT-R=%.1f → nDCG@10=%.4f  MRR@10=%.4f  (%.1fs)",
            actr_w, results[key]["nDCG@10"], results[key]["MRR@10"], elapsed,
        )

    # Find best ACT-R weight
    best_key = max(results, key=lambda k: results[k]["nDCG@10"])
    best_w = float(best_key.split("_")[1])
    best_ndcg = results[best_key]["nDCG@10"]
    logger.info(
        "    Best ACT-R weight: %.1f (nDCG@10=%.4f)", best_w, best_ndcg,
    )

    return results


# ── Diagnostic statistics capture ────────────────────────────────────────


async def capture_diagnostics(
    state: DreamState,
    retrieval_result: RetrievalResult,
    qrels: dict[str, dict[str, int]],
    baseline_per_query: dict[str, float] | None,
) -> dict[str, Any]:
    """Capture rich diagnostic statistics at a stage checkpoint.

    Collects 8 categories of diagnostics for paper-quality analysis:
    ACT-R activation distribution, graph topology, association strengths,
    importance distribution, abstract counts, per-query deltas, insight
    retrieval contribution, and spreading activation effect.

    Args:
        state: Current DreamState with all backends.
        retrieval_result: RetrievalResult from measure_retrieval().
        qrels: BEIR qrels for insight contribution analysis.
        baseline_per_query: Per-query nDCG@10 from baseline stage (None for baseline).

    Returns:
        Nested dict suitable for JSON serialization.
    """
    diag: dict[str, Any] = {}

    # ── ACT-R activation distribution ────────────────────────────────
    all_memories = await state.store.list_memories(limit=100000)
    activations: list[float] = []
    for mem in all_memories:
        ages = await state.store.get_access_times(mem.id)
        if ages:
            act = base_level_activation(ages, decay=state.config.actr_decay)
            activations.append(act)

    threshold = state.config.actr_threshold
    if activations:
        diag["actr"] = {
            "mean": round(statistics.mean(activations), 4),
            "median": round(statistics.median(activations), 4),
            "std": round(statistics.stdev(activations), 4) if len(activations) > 1 else 0.0,
            "min": round(min(activations), 4),
            "max": round(max(activations), 4),
            "count_above_threshold": sum(1 for a in activations if a >= threshold),
            "count_below_threshold": sum(1 for a in activations if a < threshold),
            "total_with_access": len(activations),
            "total_memories": len(all_memories),
        }
    else:
        diag["actr"] = {
            "mean": 0.0, "median": 0.0, "std": 0.0, "min": 0.0, "max": 0.0,
            "count_above_threshold": 0, "count_below_threshold": 0,
            "total_with_access": 0, "total_memories": len(all_memories),
        }

    # ── Graph topology ───────────────────────────────────────────────
    entity_count = state.graph.entity_count()
    rel_count = state.graph.relationship_count()

    # Access internal nx.DiGraph for density / connected components
    g = state.graph._graph  # noqa: SLF001 — benchmark code, acceptable
    g_nodes = g.number_of_nodes()
    density = nx.density(g) if g_nodes > 0 else 0.0
    n_components = (
        nx.number_weakly_connected_components(g) if g_nodes > 0 else 0
    )

    # PageRank statistics
    pr = state.graph.pagerank()
    pr_vals = list(pr.values()) if pr else []
    top5_entities: list[list[Any]] = []
    if pr_vals:
        pr_mean = statistics.mean(pr_vals)
        pr_max = max(pr_vals)
        pr_std = statistics.stdev(pr_vals) if len(pr_vals) > 1 else 0.0
        # Top-5 entities by PageRank
        sorted_pr = sorted(pr.items(), key=lambda x: x[1], reverse=True)[:5]
        for eid, score in sorted_pr:
            node_data = g.nodes.get(eid, {})
            name = node_data.get("name", eid[:12])
            top5_entities.append([name, round(score, 6)])
    else:
        pr_mean = pr_max = pr_std = 0.0

    diag["graph"] = {
        "entity_count": entity_count,
        "relationship_count": rel_count,
        "density": round(density, 6),
        "weakly_connected_components": n_components,
        "pagerank_mean": round(pr_mean, 6),
        "pagerank_max": round(pr_max, 6),
        "pagerank_std": round(pr_std, 6),
        "top5_entities": top5_entities,
    }

    # ── Association strengths (dream cycle PMI) ──────────────────────
    assocs = await state.store.get_association_strengths()
    # Deduplicate: get_association_strengths returns both (A,B) and (B,A)
    unique_strengths: list[float] = []
    seen_pairs: set[tuple[str, str]] = set()
    for (e1, e2), strength in assocs.items():
        pair = (min(e1, e2), max(e1, e2))
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            unique_strengths.append(strength)

    if unique_strengths:
        diag["associations"] = {
            "pair_count": len(unique_strengths),
            "mean": round(statistics.mean(unique_strengths), 4),
            "max": round(max(unique_strengths), 4),
            "std": (
                round(statistics.stdev(unique_strengths), 4)
                if len(unique_strengths) > 1 else 0.0
            ),
        }
    else:
        diag["associations"] = {
            "pair_count": 0, "mean": 0.0, "max": 0.0, "std": 0.0,
        }

    # ── Importance distribution ──────────────────────────────────────
    importances = [m.importance for m in all_memories]
    if importances:
        diag["importance"] = {
            "mean": round(statistics.mean(importances), 4),
            "median": round(statistics.median(importances), 4),
            "std": (
                round(statistics.stdev(importances), 4)
                if len(importances) > 1 else 0.0
            ),
            "count_above_baseline": sum(1 for imp in importances if imp > 5.0),
        }
    else:
        diag["importance"] = {
            "mean": 0.0, "median": 0.0, "std": 0.0, "count_above_baseline": 0,
        }

    # ── Abstract memory breakdown ────────────────────────────────────
    abstract_counts: dict[str, int] = {}
    for atype in (
        "episode_summary", "state_trajectory",
        "recurring_pattern", "strategic_insight",
    ):
        nodes = await state.store.get_abstract_nodes_by_type(atype)
        abstract_counts[atype] = len(nodes)
    diag["abstracts"] = abstract_counts

    # ── Per-query deltas (vs baseline) ───────────────────────────────
    if baseline_per_query is not None:
        improved = 0
        degraded = 0
        unchanged = 0
        deltas: list[tuple[str, float]] = []

        for qid, ndcg_val in retrieval_result.per_query_ndcg.items():
            base_ndcg = baseline_per_query.get(qid, 0.0)
            delta = ndcg_val - base_ndcg
            if delta > 0.001:
                improved += 1
            elif delta < -0.001:
                degraded += 1
            else:
                unchanged += 1
            deltas.append((qid, round(delta, 4)))

        deltas.sort(key=lambda x: x[1], reverse=True)
        diag["per_query_deltas"] = {
            "improved": improved,
            "degraded": degraded,
            "unchanged": unchanged,
            "top5_improved": [list(d) for d in deltas[:5]],
            "top5_degraded": [list(d) for d in deltas[-5:][::-1]],
        }
    else:
        diag["per_query_deltas"] = None

    # ── Insight retrieval contribution ───────────────────────────────
    insight_in_top10 = 0
    insight_in_top100 = 0
    insight_contributed_relevant = 0
    total_queries = len(retrieval_result.per_query_results)

    for qid, scored_list in retrieval_result.per_query_results.items():
        has_insight_top10 = any(
            s.memory.type == "insight" for s in scored_list[:10]
        )
        has_insight_top100 = any(
            s.memory.type == "insight" for s in scored_list[:100]
        )
        if has_insight_top10:
            insight_in_top10 += 1
        if has_insight_top100:
            insight_in_top100 += 1

        # Check if insight co-occurs with a relevant document
        qrel = qrels.get(qid, {})
        if has_insight_top100 and qrel:
            has_relevant = any(
                state.mem_to_doc.get(s.memory.id) in qrel
                for s in scored_list[:100]
                if s.memory.type != "insight"
            )
            if has_relevant:
                insight_contributed_relevant += 1

    diag["insight_contribution"] = {
        "in_top10": insight_in_top10,
        "in_top100": insight_in_top100,
        "contributed_relevant": insight_contributed_relevant,
        "total_queries": total_queries,
    }

    # ── Spreading activation effect ──────────────────────────────────
    all_spreading: list[float] = []
    for scored_list in retrieval_result.per_query_results.values():
        for s in scored_list:
            all_spreading.append(s.spreading)

    diag["spreading"] = {
        "mean": (
            round(statistics.mean(all_spreading), 6)
            if all_spreading else 0.0
        ),
        "nonzero_count": sum(1 for s in all_spreading if s > 0.0),
        "total_scored": len(all_spreading),
    }

    return diag


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
    baseline_per_query: dict[str, float] | None = None

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
        retrieval_result = await measure_retrieval(state, queries, qrels)
        retrieval_metrics = retrieval_result.metrics

        # ACT-R crossover sweep
        logger.info("  Running ACT-R crossover sweep...")
        crossover_results = await actr_crossover_sweep(state, queries, qrels)

        # Capture diagnostics
        logger.info("  Capturing diagnostics...")
        diagnostics = await capture_diagnostics(
            state, retrieval_result, qrels, baseline_per_query,
        )

        elapsed = time.perf_counter() - t0

        # Compute delta from baseline
        ndcg = retrieval_metrics.get("nDCG@10", 0.0)
        if baseline_ndcg is None:
            baseline_ndcg = ndcg
            baseline_per_query = retrieval_result.per_query_ndcg
            delta_pct = 0.0
        else:
            delta_pct = (
                ((ndcg - baseline_ndcg) / baseline_ndcg * 100)
                if baseline_ndcg > 0 else 0.0
            )

        stage_results[stage.name] = {
            "display_name": stage.display_name,
            "retrieval_metrics": retrieval_metrics,
            "consolidation_metrics": consolidation_metrics,
            "actr_crossover": crossover_results,
            "insight_count": int(retrieval_metrics.get("insight_count", 0)),
            "total_memories": int(retrieval_metrics.get("total_memories", 0)),
            "delta_pct": round(delta_pct, 2),
            "elapsed_seconds": round(elapsed, 2),
            "diagnostics": diagnostics,
        }

        logger.info(
            "  nDCG@10=%.4f  MRR@10=%.4f  Recall@100=%.4f"
            "  delta=%.2f%%  insights=%d  (%.1fs)",
            ndcg,
            retrieval_metrics.get("MRR@10", 0),
            retrieval_metrics.get("Recall@100", 0),
            delta_pct,
            int(retrieval_metrics.get("insight_count", 0)),
            elapsed,
        )
        # Log key diagnostic highlights
        actr_diag = diagnostics.get("actr", {})
        graph_diag = diagnostics.get("graph", {})
        assoc_diag = diagnostics.get("associations", {})
        logger.info(
            "  Diagnostics: ACT-R mean=%.3f  entities=%d  edges=%d"
            "  assoc_pairs=%d  abstracts=%d",
            actr_diag.get("mean", 0),
            graph_diag.get("entity_count", 0),
            graph_diag.get("relationship_count", 0),
            assoc_diag.get("pair_count", 0),
            sum(diagnostics.get("abstracts", {}).values()),
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
