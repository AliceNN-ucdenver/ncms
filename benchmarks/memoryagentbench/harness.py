"""MemoryAgentBench evaluation harness.

Evaluates NCMS against the MemoryAgentBench benchmark across 4 memory
competencies: Accurate Retrieval (AR), Test-Time Learning (TTL),
Long-Range Understanding (LRU), and Selective Forgetting (SF).

Follows the same patterns as benchmarks/swebench/harness.py:
- In-memory backends (no disk state)
- Phases 1-3 enabled (admission, reconciliation, episodes)
- Reuses core metrics (nDCG@10, classification_accuracy, etc.)
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from benchmarks.core.metrics import (
    classification_accuracy,
    compute_all_metrics,
    temporal_mrr,
)

logger = logging.getLogger(__name__)

# Retrieval weights (from SciFact-tuned config)
TUNED_WEIGHTS = {
    "bm25": 0.6,
    "splade": 0.3,
    "graph": 0.3,
    "actr": 0.0,
    "hierarchy": 0.0,
}


# ── Data containers ──────────────────────────────────────────────────────


@dataclass
class MABState:
    """Holds all in-memory backends and mappings for a MAB experiment."""

    store: Any  # SQLiteStore
    index: Any  # TantivyEngine
    graph: Any  # NetworkXGraph
    splade: Any  # SpladeEngine
    config: Any  # NCMSConfig
    doc_to_mem: dict[str, str] = field(default_factory=dict)
    mem_to_doc: dict[str, str] = field(default_factory=dict)
    domain: str = "mab"
    # Ingestion stats
    docs_ingested: int = 0
    ingestion_seconds: float = 0.0


# ── Helpers for extracting fields from MAB data ─────────────────────────


def _get_text(item: dict[str, Any], keys: tuple[str, ...] = ("text", "content", "memory")) -> str:
    """Extract text content from a MAB data item, trying common field names."""
    for key in keys:
        if key in item and item[key]:
            return str(item[key])
    # Fallback: concatenate all string values
    parts = [str(v) for v in item.values() if isinstance(v, str) and len(str(v)) > 10]
    return " ".join(parts) if parts else ""


def _get_id(item: dict[str, Any], index: int) -> str:
    """Extract or generate a document ID from a MAB data item."""
    for key in ("id", "doc_id", "instance_id", "memory_id"):
        if key in item and item[key]:
            return str(item[key])
    return f"mab_doc_{index}"


def _get_query(item: dict[str, Any]) -> str:
    """Extract query text from a MAB data item."""
    for key in ("query", "question", "prompt"):
        if key in item and item[key]:
            return str(item[key])
    return ""


def _get_label(item: dict[str, Any]) -> str:
    """Extract label/category from a MAB data item."""
    for key in ("label", "category", "class", "type", "answer"):
        if key in item and item[key]:
            return str(item[key])
    return "unknown"


def _get_relevant_ids(item: dict[str, Any]) -> list[str]:
    """Extract relevant document IDs from a MAB data item."""
    for key in ("relevant_ids", "relevant_docs", "gold_ids", "positive_ids"):
        if key in item and item[key]:
            val = item[key]
            if isinstance(val, list):
                return [str(v) for v in val]
            if isinstance(val, str):
                return [v.strip() for v in val.split(",") if v.strip()]
    return []


def _get_outdated_ids(item: dict[str, Any]) -> list[str]:
    """Extract outdated/superseded document IDs from a MAB data item."""
    for key in ("outdated_ids", "superseded_ids", "forget_ids", "obsolete_ids"):
        if key in item and item[key]:
            val = item[key]
            if isinstance(val, list):
                return [str(v) for v in val]
            if isinstance(val, str):
                return [v.strip() for v in val.split(",") if v.strip()]
    return []


# ── Ingestion ────────────────────────────────────────────────────────────


async def ingest_mab_corpus(
    data: dict[str, Any],
    config: Any | None = None,
) -> MABState:
    """Ingest memory chunks from MAB dataset into in-memory NCMS.

    Collects all unique documents across all competency splits and
    ingests them into a fresh NCMS instance with phases 1-3 enabled.

    Args:
        data: MAB dataset dict with keys 'ar', 'ttl', 'lru', 'sf'.
        config: Optional NCMSConfig override.

    Returns:
        MABState with populated backends and ID mappings.
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

    store = SQLiteStore(db_path=":memory:")
    await store.initialize()

    index = TantivyEngine()
    index.initialize()

    graph = NetworkXGraph()
    splade = SpladeEngine()

    if config is None:
        config = NCMSConfig(
            db_path=":memory:",
            actr_noise=0.0,  # Deterministic for benchmarks
            splade_enabled=True,
            graph_expansion_enabled=True,
            scoring_weight_bm25=TUNED_WEIGHTS["bm25"],
            scoring_weight_actr=TUNED_WEIGHTS["actr"],
            scoring_weight_splade=TUNED_WEIGHTS["splade"],
            scoring_weight_graph=TUNED_WEIGHTS["graph"],
            actr_threshold=-999.0,  # ACT-R disabled
            # Enable phases 1-3
            admission_enabled=True,
            reconciliation_enabled=True,
            episodes_enabled=True,
            # Intent classification for structured retrieval
            intent_classification_enabled=True,
        )

    # Create phase services
    admission = AdmissionService(store=store, index=index, graph=graph, config=config)
    reconciliation = ReconciliationService(store=store, config=config)
    episode = EpisodeService(store=store, index=index, config=config, splade=splade)

    svc = MemoryService(
        store=store, index=index, graph=graph, config=config,
        splade=splade, admission=admission, reconciliation=reconciliation,
        episode=episode,
    )

    # Collect all unique documents across splits
    all_docs: dict[str, str] = {}  # doc_id -> content

    for split_name, split_data in data.items():
        if not isinstance(split_data, list):
            continue
        for i, item in enumerate(split_data):
            # Items may contain corpus documents or queries with references
            doc_id = _get_id(item, i)
            text = _get_text(item)
            if text and doc_id not in all_docs:
                all_docs[doc_id] = text

            # Also extract any inline corpus/memory items
            for key in ("memories", "corpus", "documents", "context"):
                if key in item and isinstance(item[key], list):
                    for j, sub in enumerate(item[key]):
                        if isinstance(sub, dict):
                            sub_id = _get_id(sub, j)
                            sub_text = _get_text(sub)
                            if sub_text and sub_id not in all_docs:
                                all_docs[sub_id] = sub_text
                        elif isinstance(sub, str) and len(sub) > 10:
                            sub_id = f"{split_name}_{doc_id}_ctx_{j}"
                            if sub_id not in all_docs:
                                all_docs[sub_id] = sub

    logger.info("Collected %d unique documents from MAB dataset", len(all_docs))

    # Ingest all documents
    doc_to_mem: dict[str, str] = {}
    mem_to_doc: dict[str, str] = {}
    t0 = time.perf_counter()
    skipped = 0

    for i, (doc_id, content) in enumerate(all_docs.items()):
        # Truncate very long documents
        content = content[:10000]

        memory = await svc.store_memory(
            content=content,
            memory_type="fact",
            domains=["mab"],
            tags=[],
            structured={"mab_doc_id": doc_id},
        )

        route = (memory.structured or {}).get("admission", {}).get("route")
        if route in ("discard", "ephemeral_cache"):
            skipped += 1
            continue

        doc_to_mem[doc_id] = memory.id
        mem_to_doc[memory.id] = doc_id

        if (i + 1) % 100 == 0:
            logger.info("  Ingested %d/%d docs", i + 1, len(all_docs))

    elapsed = time.perf_counter() - t0

    if skipped > 0:
        logger.warning("Skipped %d/%d docs (ephemeral/discarded)", skipped, len(all_docs))
    logger.info(
        "Ingestion complete: %d docs in %.1fs (%.1f docs/sec)",
        len(doc_to_mem), elapsed,
        len(doc_to_mem) / elapsed if elapsed > 0 else 0,
    )

    return MABState(
        store=store, index=index, graph=graph, splade=splade,
        config=config, doc_to_mem=doc_to_mem, mem_to_doc=mem_to_doc,
        domain="mab", docs_ingested=len(doc_to_mem),
        ingestion_seconds=elapsed,
    )


# ── AR: Accurate Retrieval ──────────────────────────────────────────────


async def evaluate_ar(state: MABState, data: list[dict[str, Any]]) -> dict[str, float]:
    """Evaluate Accurate Retrieval competency.

    For each query in the AR split, search NCMS and compute nDCG@10,
    MRR@10, and Recall@10/100 against relevance judgments.

    Args:
        state: Ingested MABState.
        data: AR split items, each containing a query and relevant doc IDs.

    Returns:
        Dict with nDCG@10, MRR@10, Recall@10, Recall@100, num_queries.
    """
    from ncms.application.memory_service import MemoryService

    svc = MemoryService(
        store=state.store, index=state.index, graph=state.graph,
        config=state.config, splade=state.splade,
    )

    queries: dict[str, str] = {}
    qrels: dict[str, dict[str, int]] = {}

    for i, item in enumerate(data):
        query = _get_query(item)
        if not query:
            continue
        qid = _get_id(item, i)
        queries[qid] = query

        # Build relevance judgments
        relevant = _get_relevant_ids(item)
        if relevant:
            qrels[qid] = {doc_id: 1 for doc_id in relevant}

    if not queries or not qrels:
        logger.warning("AR: No valid queries with relevance judgments found")
        return {"nDCG@10": 0.0, "MRR@10": 0.0, "Recall@10": 0.0, "Recall@100": 0.0,
                "num_queries": 0}

    rankings: dict[str, list[str]] = {}
    for qid, query_text in queries.items():
        results = await svc.search(query=query_text, domain="mab", limit=100)
        doc_ids: list[str] = []
        for scored in results:
            doc_id = state.mem_to_doc.get(scored.memory.id)
            if doc_id and doc_id not in doc_ids:
                doc_ids.append(doc_id)
        rankings[qid] = doc_ids

    metrics = compute_all_metrics(rankings, qrels)
    logger.info(
        "AR: nDCG@10=%.4f  MRR@10=%.4f  Recall@10=%.4f  (%d queries)",
        metrics["nDCG@10"], metrics["MRR@10"], metrics["Recall@10"],
        metrics["num_queries"],
    )
    return metrics


# ── TTL: Test-Time Learning ─────────────────────────────────────────────


async def evaluate_ttl(state: MABState, data: list[dict[str, Any]]) -> dict[str, float]:
    """Evaluate Test-Time Learning competency.

    For each query, retrieve top-5 results and predict the label via
    majority vote on the labels of retrieved documents. Measures whether
    NCMS can effectively learn from ingested context.

    Args:
        state: Ingested MABState.
        data: TTL split items with queries and expected labels.

    Returns:
        Dict with accuracy and num_queries.
    """
    from ncms.application.memory_service import MemoryService

    svc = MemoryService(
        store=state.store, index=state.index, graph=state.graph,
        config=state.config, splade=state.splade,
    )

    # Build label mapping from corpus documents
    doc_labels: dict[str, str] = {}
    predictions: dict[str, str] = {}
    labels: dict[str, str] = {}

    for i, item in enumerate(data):
        # Each TTL item should have a query, expected label, and context docs
        qid = _get_id(item, i)
        query = _get_query(item)
        expected = _get_label(item)

        if not query or expected == "unknown":
            continue

        labels[qid] = expected

        # Extract labels from context/corpus items
        for key in ("memories", "corpus", "documents", "context"):
            if key in item and isinstance(item[key], list):
                for j, sub in enumerate(item[key]):
                    if isinstance(sub, dict):
                        sub_id = _get_id(sub, j)
                        sub_label = _get_label(sub)
                        if sub_label != "unknown":
                            doc_labels[sub_id] = sub_label

        # Retrieve and predict via majority vote
        results = await svc.search(query=query[:2000], domain="mab", limit=5)
        votes: Counter[str] = Counter()
        for scored in results:
            doc_id = state.mem_to_doc.get(scored.memory.id)
            if doc_id and doc_id in doc_labels:
                votes[doc_labels[doc_id]] += 1

        if votes:
            predictions[qid] = votes.most_common(1)[0][0]
        else:
            predictions[qid] = "unknown"

    if not labels:
        logger.warning("TTL: No valid queries with labels found")
        return {"accuracy": 0.0, "num_queries": 0}

    acc = classification_accuracy(predictions, labels)
    logger.info("TTL: accuracy=%.4f  (%d queries)", acc, len(predictions))
    return {"accuracy": acc, "num_queries": len(predictions)}


# ── LRU: Long-Range Understanding ───────────────────────────────────────


async def evaluate_lru(state: MABState, data: list[dict[str, Any]]) -> dict[str, float]:
    """Evaluate Long-Range Understanding competency.

    Tests whether NCMS can find cross-topic connections by retrieving
    documents that span multiple memory contexts. Uses standard IR
    metrics (nDCG@10) over relevance judgments.

    Args:
        state: Ingested MABState.
        data: LRU split items with cross-topic queries and relevance judgments.

    Returns:
        Dict with nDCG@10, MRR@10, Recall@10, Recall@100, num_queries.
    """
    from ncms.application.memory_service import MemoryService

    svc = MemoryService(
        store=state.store, index=state.index, graph=state.graph,
        config=state.config, splade=state.splade,
    )

    queries: dict[str, str] = {}
    qrels: dict[str, dict[str, int]] = {}

    for i, item in enumerate(data):
        query = _get_query(item)
        if not query:
            continue
        qid = _get_id(item, i)
        queries[qid] = query

        relevant = _get_relevant_ids(item)
        if relevant:
            qrels[qid] = {doc_id: 1 for doc_id in relevant}

    if not queries or not qrels:
        logger.warning("LRU: No valid queries with relevance judgments found")
        return {"nDCG@10": 0.0, "MRR@10": 0.0, "Recall@10": 0.0, "Recall@100": 0.0,
                "num_queries": 0}

    rankings: dict[str, list[str]] = {}
    for qid, query_text in queries.items():
        results = await svc.search(query=query_text, domain="mab", limit=100)
        doc_ids: list[str] = []
        for scored in results:
            doc_id = state.mem_to_doc.get(scored.memory.id)
            if doc_id and doc_id not in doc_ids:
                doc_ids.append(doc_id)
        rankings[qid] = doc_ids

    metrics = compute_all_metrics(rankings, qrels)
    logger.info(
        "LRU: nDCG@10=%.4f  MRR@10=%.4f  (%d queries)",
        metrics["nDCG@10"], metrics["MRR@10"], metrics["num_queries"],
    )
    return metrics


# ── SF: Selective Forgetting ────────────────────────────────────────────


async def evaluate_sf(state: MABState, data: list[dict[str, Any]]) -> dict[str, float]:
    """Evaluate Selective Forgetting competency.

    The hardest competency. Tests whether NCMS properly suppresses
    outdated/superseded memories. For each SF query:

    1. Identify which documents should be "forgotten" (superseded)
    2. Mark them via NCMS reconciliation (is_current=False)
    3. Search and verify forgotten content is ranked below current content

    Uses temporal_mrr (current doc should rank first) and a forgetting
    accuracy metric (fraction of queries where superseded docs rank
    below current docs).

    Args:
        state: Ingested MABState.
        data: SF split items with outdated/current document pairs.

    Returns:
        Dict with forgetting_accuracy, temporal_mrr, num_queries.
    """
    from ncms.application.memory_service import MemoryService

    svc = MemoryService(
        store=state.store, index=state.index, graph=state.graph,
        config=state.config, splade=state.splade,
    )

    queries: dict[str, str] = {}
    targets: dict[str, str] = {}  # qid -> current (non-forgotten) doc
    outdated: dict[str, list[str]] = {}  # qid -> list of outdated doc IDs
    qrels: dict[str, dict[str, int]] = {}

    for i, item in enumerate(data):
        query = _get_query(item)
        if not query:
            continue
        qid = _get_id(item, i)
        queries[qid] = query

        # Current (non-outdated) relevant docs
        current_ids = _get_relevant_ids(item)
        # Outdated/superseded docs that should be forgotten
        forget_ids = _get_outdated_ids(item)

        if not current_ids and not forget_ids:
            continue

        # Build graded qrels: current=2, outdated=0
        qrel: dict[str, int] = {}
        for doc_id in current_ids:
            qrel[doc_id] = 2
        for doc_id in forget_ids:
            qrel[doc_id] = 0
        qrels[qid] = qrel

        # Target = first current doc
        if current_ids:
            targets[qid] = current_ids[0]
        outdated[qid] = forget_ids

        # Apply supersession: mark outdated docs as not current
        # This uses NCMS's reconciliation mechanism
        for forget_id in forget_ids:
            mem_id = state.doc_to_mem.get(forget_id)
            if not mem_id:
                continue
            try:
                nodes = await state.store.get_nodes_for_memory(mem_id)
                for node in nodes:
                    if node.node_type == "entity_state" and node.metadata.get("is_current", True):
                        meta = dict(node.metadata)
                        meta["is_current"] = False
                        meta["superseded_reason"] = "mab_selective_forgetting"
                        node.metadata = meta
                        await state.store.update_memory_node(node)
            except Exception:
                # Not all memories have entity_state nodes; that's OK
                pass

    if not queries:
        logger.warning("SF: No valid queries found")
        return {"forgetting_accuracy": 0.0, "temporal_mrr": 0.0, "num_queries": 0}

    # Search and evaluate
    rankings: dict[str, list[str]] = {}
    for qid, query_text in queries.items():
        results = await svc.search(query=query_text, domain="mab", limit=100)
        doc_ids: list[str] = []
        for scored in results:
            doc_id = state.mem_to_doc.get(scored.memory.id)
            if doc_id and doc_id not in doc_ids:
                doc_ids.append(doc_id)
        rankings[qid] = doc_ids

    # Temporal MRR: current doc should rank first
    t_mrr = temporal_mrr(rankings, targets) if targets else 0.0

    # Forgetting accuracy: fraction of queries where ALL outdated docs
    # rank below ALL current docs (or don't appear at all)
    correct = 0
    total = 0
    for qid in queries:
        if qid not in outdated or qid not in targets:
            continue
        total += 1
        ranked = rankings.get(qid, [])

        # Find position of the target (current) doc
        target_pos = len(ranked)  # Not found = worst
        target_id = targets.get(qid)
        if target_id and target_id in ranked:
            target_pos = ranked.index(target_id)

        # Check all outdated docs rank below target
        all_below = True
        for forget_id in outdated[qid]:
            if forget_id in ranked:
                forget_pos = ranked.index(forget_id)
                if forget_pos <= target_pos:
                    all_below = False
                    break
        if all_below:
            correct += 1

    forgetting_acc = correct / max(total, 1)

    logger.info(
        "SF: forgetting_accuracy=%.4f  temporal_mrr=%.4f  (%d queries)",
        forgetting_acc, t_mrr, total,
    )
    return {
        "forgetting_accuracy": forgetting_acc,
        "temporal_mrr": t_mrr,
        "num_queries": total,
    }


# ── Main benchmark runner ───────────────────────────────────────────────


async def run_mab_benchmark(
    data: dict[str, Any],
    competencies: tuple[str, ...] = ("ar", "ttl", "lru", "sf"),
) -> dict[str, Any]:
    """Run all requested MemoryAgentBench competency evaluations.

    Args:
        data: MAB dataset dict with keys 'ar', 'ttl', 'lru', 'sf'.
        competencies: Which competencies to evaluate.

    Returns:
        Nested dict with per-competency results and aggregate stats.
    """
    t0 = time.perf_counter()

    logger.info("=" * 60)
    logger.info("MemoryAgentBench Evaluation")
    logger.info("=" * 60)
    logger.info("  Competencies: %s", ", ".join(competencies))
    logger.info("  Available splits: %s", ", ".join(data.keys()))

    # Ingest all data
    logger.info("Ingesting corpus...")
    state = await ingest_mab_corpus(data)

    results: dict[str, Any] = {
        "ingestion": {
            "docs_ingested": state.docs_ingested,
            "ingestion_seconds": round(state.ingestion_seconds, 2),
        },
        "competencies": {},
    }

    # Evaluate each requested competency
    evaluators = {
        "ar": evaluate_ar,
        "ttl": evaluate_ttl,
        "lru": evaluate_lru,
        "sf": evaluate_sf,
    }

    for comp in competencies:
        if comp not in data:
            logger.warning("Skipping %s: split not available in dataset", comp.upper())
            results["competencies"][comp] = {"skipped": True, "reason": "split_not_available"}
            continue

        if comp not in evaluators:
            logger.warning("Skipping %s: no evaluator implemented", comp.upper())
            results["competencies"][comp] = {"skipped": True, "reason": "no_evaluator"}
            continue

        logger.info("")
        logger.info("Evaluating %s...", comp.upper())
        comp_start = time.perf_counter()

        try:
            comp_results = await evaluators[comp](state, data[comp])
            comp_results["elapsed_seconds"] = round(time.perf_counter() - comp_start, 2)
            results["competencies"][comp] = comp_results
        except Exception as exc:
            logger.error("Failed to evaluate %s: %s", comp.upper(), exc, exc_info=True)
            results["competencies"][comp] = {
                "error": str(exc),
                "elapsed_seconds": round(time.perf_counter() - comp_start, 2),
            }

    total_elapsed = time.perf_counter() - t0
    results["total_seconds"] = round(total_elapsed, 1)

    logger.info("")
    logger.info("MemoryAgentBench evaluation complete: %.1fs total", total_elapsed)

    # Summary
    for comp in competencies:
        comp_data = results["competencies"].get(comp, {})
        if comp_data.get("skipped"):
            logger.info("  %s: SKIPPED (%s)", comp.upper(), comp_data.get("reason"))
        elif comp_data.get("error"):
            logger.info("  %s: ERROR (%s)", comp.upper(), comp_data["error"])
        elif comp == "ar":
            logger.info("  AR:  nDCG@10=%.4f", comp_data.get("nDCG@10", 0))
        elif comp == "ttl":
            logger.info("  TTL: accuracy=%.4f", comp_data.get("accuracy", 0))
        elif comp == "lru":
            logger.info("  LRU: nDCG@10=%.4f", comp_data.get("nDCG@10", 0))
        elif comp == "sf":
            logger.info(
                "  SF:  forgetting_acc=%.4f  temporal_mrr=%.4f",
                comp_data.get("forgetting_accuracy", 0),
                comp_data.get("temporal_mrr", 0),
            )

    return results
