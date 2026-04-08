"""LongMemEval evaluation harness — replay sessions into NCMS, evaluate QA retrieval.

Same conversation-replay pattern as LoCoMo.  Primary metric is Recall@5
(to compare against MemPalace's reported 96.6%).
"""

from __future__ import annotations

import logging
import time

from benchmarks.core.qa_metrics import contains_match, f1_token_overlap, recall_at_k_qa
from benchmarks.longmemeval.loader import LongMemQuestion, Session

logger = logging.getLogger(__name__)


class ConversationState:
    """Holds NCMS backends populated from session replays."""

    def __init__(
        self,
        store: object,
        index: object,
        graph: object,
        splade: object,
        config: object,
        svc: object,
        memory_ids: list[str],
        session_count: int,
    ):
        self.store = store
        self.index = index
        self.graph = graph
        self.splade = splade
        self.config = config
        self.svc = svc
        self.memory_ids = memory_ids
        self.session_count = session_count


async def replay_sessions(
    sessions: list[Session],
    config: object | None = None,
) -> ConversationState:
    """Replay all chat sessions into fresh in-memory NCMS backends.

    Each turn is stored as a separate memory with session metadata in tags.

    Args:
        sessions: List of chat sessions to ingest.
        config: Optional NCMSConfig override.

    Returns:
        ConversationState with populated backends.
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

    svc = MemoryService(
        store=store, index=index, graph=graph, config=config, splade=splade,
    )

    memory_ids: list[str] = []
    t0 = time.perf_counter()

    for session in sessions:
        for turn in session.turns:
            if not turn.content.strip():
                continue

            memory = await svc.store_memory(
                content=turn.content,
                memory_type="fact",
                source_agent=turn.role,
                tags=["longmemeval", f"session:{session.session_id}"],
            )
            memory_ids.append(memory.id)

    elapsed = time.perf_counter() - t0
    logger.info(
        "Replayed %d sessions (%d memories) in %.1fs",
        len(sessions), len(memory_ids), elapsed,
    )

    return ConversationState(
        store=store,
        index=index,
        graph=graph,
        splade=splade,
        config=config,
        svc=svc,
        memory_ids=memory_ids,
        session_count=len(sessions),
    )


async def evaluate_qa(
    state: ConversationState,
    questions: list[LongMemQuestion],
    top_k: int = 5,
) -> dict[str, float]:
    """Evaluate QA retrieval against populated NCMS backends.

    For each question, searches NCMS and checks whether the top-k results
    contain the ground-truth answer.

    Args:
        state: ConversationState from replay_sessions().
        questions: Evaluation questions.
        top_k: Number of top results to consider.

    Returns:
        Dict with Recall@k, Contains, F1, and per-category breakdowns.
    """
    from ncms.application.memory_service import MemoryService

    svc: MemoryService = state.svc  # type: ignore[assignment]

    recall_scores: list[float] = []
    contains_scores: list[float] = []
    f1_scores: list[float] = []
    category_scores: dict[str, list[float]] = {}

    for q in questions:
        results = await svc.search(query=q.question, limit=top_k)
        retrieved_contents = [s.memory.content for s in results]

        # Recall@k: does any top-k result contain the answer?
        recall = recall_at_k_qa(retrieved_contents, q.answer, k=top_k)
        recall_scores.append(recall)

        # Concatenate top-k content for token-level metrics
        concat_content = " ".join(retrieved_contents[:top_k])
        contains_scores.append(contains_match(concat_content, q.answer))
        f1_scores.append(f1_token_overlap(concat_content, q.answer))

        # Track per-category
        cat = q.category
        if cat not in category_scores:
            category_scores[cat] = []
        category_scores[cat].append(recall)

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

    return metrics


async def run_longmemeval_benchmark(
    sessions: list[Session],
    questions: list[LongMemQuestion],
    top_k: int = 5,
) -> dict:
    """Run the full LongMemEval benchmark.

    All sessions are replayed into a single NCMS instance, then all questions
    are evaluated against it (unlike LoCoMo which is per-conversation).

    Args:
        sessions: List of parsed chat sessions.
        questions: All evaluation questions.
        top_k: Number of top results for recall computation.

    Returns:
        Dict with overall metrics and category breakdowns.
    """
    logger.info("=" * 60)
    logger.info("LongMemEval Benchmark")
    logger.info("  Sessions: %d, Questions: %d", len(sessions), len(questions))
    logger.info("=" * 60)

    # Replay all sessions into one NCMS instance
    logger.info("Phase 1: Replaying sessions...")
    state = await replay_sessions(sessions)

    try:
        # Evaluate questions
        logger.info("Phase 2: Evaluating %d questions...", len(questions))
        metrics = await evaluate_qa(state, questions, top_k=top_k)
    finally:
        await state.store.close()  # type: ignore[union-attr]

    logger.info("=" * 60)
    logger.info(
        "LongMemEval Overall: Recall@%d=%.4f  Contains=%.4f  F1=%.4f  (%d questions)",
        top_k,
        metrics[f"Recall@{top_k}"],
        metrics["Contains"],
        metrics["F1"],
        int(metrics["num_questions"]),
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
        "sessions_count": len(sessions),
        "total_turns": sum(len(s.turns) for s in sessions),
        "memories_stored": len(state.memory_ids),
    }
