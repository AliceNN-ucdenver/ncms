"""LongMemEval evaluation harness — replay sessions into NCMS, evaluate QA retrieval.

Each question has its own haystack (set of sessions to ingest).  We create a
fresh NCMS instance per question, ingest that question's sessions, then evaluate.

Primary metric is Recall@5 (to compare against MemPalace's reported 96.6%).
"""

from __future__ import annotations

import json
import logging
import time

from benchmarks.core.qa_metrics import contains_match, f1_token_overlap, recall_at_k_qa
from benchmarks.longmemeval.loader import LongMemQuestion, Session

logger = logging.getLogger(__name__)


async def _create_ncms_instance(config: object | None = None):
    """Create a fresh in-memory NCMS instance.

    Returns:
        Tuple of (store, index, graph, splade, config, svc).
    """
    from ncms.application.memory_service import MemoryService
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
            actr_noise=0.0,
            splade_enabled=True,
            graph_expansion_enabled=True,
            scoring_weight_bm25=0.6,
            scoring_weight_actr=0.0,
            scoring_weight_splade=0.3,
            scoring_weight_graph=0.3,
            contradiction_detection_enabled=False,
        )

    # Seed domain-specific topics for GLiNER entity extraction
    from benchmarks.core.datasets import LONGMEMEVAL_TOPICS

    topic_info = LONGMEMEVAL_TOPICS.get("longmemeval", {})
    domain = topic_info.get("domain", "assistant")
    labels = topic_info.get("labels", [])
    if labels:
        await store.set_consolidation_value(
            f"entity_labels:{domain}",
            json.dumps(labels),
        )

    svc = MemoryService(
        store=store, index=index, graph=graph, config=config, splade=splade,
    )

    return store, index, graph, splade, config, svc


async def _ingest_sessions(
    svc: object,
    sessions: list[Session],
) -> list[str]:
    """Ingest sessions into an NCMS MemoryService, return stored memory IDs."""
    from ncms.application.memory_service import MemoryService

    svc_typed: MemoryService = svc  # type: ignore[assignment]
    memory_ids: list[str] = []

    for session in sessions:
        for turn in session.turns:
            if not turn.content.strip():
                continue
            memory = await svc_typed.store_memory(
                content=turn.content,
                memory_type="fact",
                source_agent=turn.role,
                domains=["assistant"],
                tags=["longmemeval", f"session:{session.session_id}"],
            )
            memory_ids.append(memory.id)

    return memory_ids


async def evaluate_question(
    svc: object,
    question: LongMemQuestion,
    top_k: int = 5,
) -> dict[str, float]:
    """Evaluate a single question against an NCMS instance.

    Returns:
        Dict with recall, contains, and f1 scores.
    """
    from ncms.application.memory_service import MemoryService

    svc_typed: MemoryService = svc  # type: ignore[assignment]

    results = await svc_typed.search(query=question.question, limit=top_k)
    retrieved_contents = [s.memory.content for s in results]

    recall = recall_at_k_qa(retrieved_contents, question.answer, k=top_k)
    concat_content = " ".join(retrieved_contents[:top_k])
    contains = contains_match(concat_content, question.answer)
    f1 = f1_token_overlap(concat_content, question.answer)

    return {"recall": recall, "contains": contains, "f1": f1}


async def run_longmemeval_benchmark(
    sessions_by_question: dict[str, list[Session]],
    questions: list[LongMemQuestion],
    top_k: int = 5,
) -> dict:
    """Run the full LongMemEval benchmark.

    Each question gets its own NCMS instance populated with that question's
    haystack sessions.

    Args:
        sessions_by_question: Dict mapping question_id to its list of sessions.
        questions: All evaluation questions.
        top_k: Number of top results for recall computation.

    Returns:
        Dict with overall metrics and category breakdowns.
    """
    logger.info("=" * 60)
    logger.info("LongMemEval Benchmark")
    logger.info("  Questions: %d", len(questions))
    logger.info("=" * 60)

    recall_scores: list[float] = []
    contains_scores: list[float] = []
    f1_scores: list[float] = []
    category_scores: dict[str, list[float]] = {}
    total_memories = 0
    total_sessions = 0

    t0 = time.perf_counter()

    for qi, q in enumerate(questions):
        q_sessions = sessions_by_question.get(q.question_id, [])

        if not q_sessions:
            logger.warning(
                "Question %s has no sessions, skipping", q.question_id,
            )
            continue

        # Create fresh NCMS instance for this question
        store, _index, _graph, _splade, _config, svc = await _create_ncms_instance()

        try:
            # Ingest this question's sessions
            mem_ids = await _ingest_sessions(svc, q_sessions)
            total_memories += len(mem_ids)
            total_sessions += len(q_sessions)

            # Evaluate
            scores = await evaluate_question(svc, q, top_k=top_k)
        finally:
            await store.close()

        recall_scores.append(scores["recall"])
        contains_scores.append(scores["contains"])
        f1_scores.append(scores["f1"])

        cat = q.category
        if cat not in category_scores:
            category_scores[cat] = []
        category_scores[cat].append(scores["recall"])

        if (qi + 1) % 10 == 0 or qi == 0:
            elapsed = time.perf_counter() - t0
            logger.info(
                "  [%d/%d] %.1fs  Recall@%d so far: %.4f",
                qi + 1, len(questions), elapsed, top_k,
                sum(recall_scores) / len(recall_scores),
            )

    elapsed = time.perf_counter() - t0
    n = len(recall_scores) or 1

    metrics: dict[str, float] = {
        f"Recall@{top_k}": sum(recall_scores) / n,
        "Contains": sum(contains_scores) / n,
        "F1": sum(f1_scores) / n,
        "num_questions": float(len(recall_scores)),
    }

    # Per-category recall
    for cat, scores in sorted(category_scores.items()):
        cat_n = len(scores) or 1
        metrics[f"Recall@{top_k}_{cat}"] = sum(scores) / cat_n
        metrics[f"num_{cat}"] = float(len(scores))

    logger.info("=" * 60)
    logger.info(
        "LongMemEval Overall: Recall@%d=%.4f  Contains=%.4f  F1=%.4f  (%d questions, %.1fs)",
        top_k,
        metrics[f"Recall@{top_k}"],
        metrics["Contains"],
        metrics["F1"],
        int(metrics["num_questions"]),
        elapsed,
    )

    # Log per-category results
    category_keys = [k for k in metrics if k.startswith(f"Recall@{top_k}_")]
    for key in sorted(category_keys):
        cat = key.split("_", 1)[1] if "_" in key else key
        count_key = f"num_{cat}"
        logger.info(
            "  %s: Recall@%d=%.4f (%d questions)",
            cat, top_k, metrics[key], int(metrics.get(count_key, 0)),
        )

    logger.info("=" * 60)

    return {
        "overall": metrics,
        "questions_count": len(questions),
        "total_sessions": total_sessions,
        "total_memories": total_memories,
        "elapsed_seconds": round(elapsed, 1),
    }
