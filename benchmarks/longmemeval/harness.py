"""LongMemEval evaluation harness — replay sessions into NCMS, evaluate QA retrieval.

Each question has its own haystack (set of sessions to ingest).  We create a
fresh NCMS instance per question, ingest that question's sessions, then evaluate.

Primary metric is Recall@5 (to compare against MemPalace's reported 96.6%).

Supports two modes:
- Retrieval-only (default): fast containment/F1 metrics on retrieved content.
- RAG (use_rag=True): generate answer via LLM, judge via LLM with type-specific prompts.
"""

from __future__ import annotations

import json
import logging
import time

from benchmarks.core.qa_metrics import contains_match, f1_token_overlap, recall_at_k_qa
from benchmarks.core.rag_pipeline import (
    DEFAULT_API_BASE,
    DEFAULT_MODEL,
    build_context_from_memories,
    generate_answer,
    llm_judge,
)
from benchmarks.longmemeval.loader import LongMemQuestion, Session

logger = logging.getLogger(__name__)


async def _create_ncms_instance(
    config: object | None = None,
    shared_splade: object | None = None,
):
    """Create a fresh in-memory NCMS instance.

    Args:
        config: Optional NCMSConfig override.
        shared_splade: Pre-loaded SpladeEngine to reuse across questions
            (avoids 1s model reload per question). Created once by the
            benchmark runner and passed to each question's instance.

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
    splade = shared_splade if shared_splade is not None else SpladeEngine()

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
    await svc.start_index_pool()

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


def _get_judge_type(category: str, question_id: str) -> str:
    """Map question category/id to the appropriate judge type."""
    if "_abs" in question_id:
        return "abstention"
    if "temporal" in category:
        return "temporal"
    if category == "knowledge-update":
        return "knowledge-update"
    return "default"


async def evaluate_question(
    svc: object,
    question: LongMemQuestion,
    top_k: int = 5,
    use_rag: bool = False,
    answer_model: str = DEFAULT_MODEL,
    answer_api_base: str = DEFAULT_API_BASE,
    judge_model: str = DEFAULT_MODEL,
    judge_api_base: str = DEFAULT_API_BASE,
) -> dict[str, float]:
    """Evaluate a single question against an NCMS instance.

    When use_rag=True, generates an answer via LLM and judges it using
    question-type-specific prompts (temporal-reasoning, knowledge-update,
    abstention, etc.).

    Returns:
        Dict with recall, contains, f1, and (when use_rag) qa_f1, judge scores.
    """
    from ncms.application.memory_service import MemoryService

    svc_typed: MemoryService = svc  # type: ignore[assignment]

    results = await svc_typed.search(query=question.question, limit=top_k)
    retrieved_contents = [s.memory.content for s in results]

    recall = recall_at_k_qa(retrieved_contents, question.answer, k=top_k)
    concat_content = " ".join(retrieved_contents[:top_k])
    contains = contains_match(concat_content, question.answer)
    f1 = f1_token_overlap(concat_content, question.answer)

    scores: dict[str, float] = {"recall": recall, "contains": contains, "f1": f1}

    if use_rag:
        context = build_context_from_memories(results, max_chars=4000)

        # Determine system prompt based on question type
        is_abstention = "_abs" in question.question_id
        cat = question.category

        if is_abstention:
            system = (
                "You are answering questions about a user's conversation history. "
                "If the specific information asked about was never discussed, "
                'clearly state that the information is not available or "I don\'t know".'
            )
        elif "temporal" in cat:
            system = (
                "You are answering questions about a user's conversation history. "
                "Pay careful attention to dates and temporal ordering. "
                f"The current date is {question.question_date}. "
                "Answer with specific dates or time spans when asked."
            )
        elif cat == "knowledge-update":
            system = (
                "You are answering questions about a user's conversation history. "
                "The user's information may have changed over time. "
                "Always answer with the most recent/updated information."
            )
        else:
            system = (
                "You are answering questions about a user's conversation history. "
                "Answer concisely based on the available context."
            )

        prediction = await generate_answer(
            question.question,
            context,
            system_prompt=system,
            model=answer_model,
            api_base=answer_api_base,
            max_tokens=200,
        )

        # Token F1 between generated answer and ground truth
        scores["qa_f1"] = f1_token_overlap(prediction, question.answer)

        # LLM judge with type-specific prompt
        judge_type = _get_judge_type(cat, question.question_id)
        judge_ok = await llm_judge(
            question.question, question.answer, prediction,
            judge_type=judge_type,
            model=judge_model,
            api_base=judge_api_base,
        )
        scores["judge"] = 1.0 if judge_ok else 0.0

    return scores


async def run_longmemeval_benchmark(
    sessions_by_question: dict[str, list[Session]],
    questions: list[LongMemQuestion],
    top_k: int = 5,
    use_rag: bool = False,
    answer_model: str = DEFAULT_MODEL,
    answer_api_base: str = DEFAULT_API_BASE,
    judge_model: str = DEFAULT_MODEL,
    judge_api_base: str = DEFAULT_API_BASE,
) -> dict:
    """Run the full LongMemEval benchmark.

    Each question gets its own NCMS instance populated with that question's
    haystack sessions.

    Args:
        sessions_by_question: Dict mapping question_id to its list of sessions.
        questions: All evaluation questions.
        top_k: Number of top results for recall computation.
        use_rag: If True, generate answers and judge via LLM.
        answer_model: LLM model for answer generation.
        answer_api_base: API base URL for answer generation.
        judge_model: LLM model for judging.
        judge_api_base: API base URL for judging.

    Returns:
        Dict with overall metrics and category breakdowns.
    """
    logger.info("=" * 60)
    logger.info("LongMemEval Benchmark%s", " (RAG mode)" if use_rag else "")
    logger.info("  Questions: %d", len(questions))
    logger.info("=" * 60)

    recall_scores: list[float] = []
    contains_scores: list[float] = []
    f1_scores: list[float] = []
    category_scores: dict[str, list[float]] = {}
    total_memories = 0
    total_sessions = 0

    # RAG accumulators
    qa_f1_scores: list[float] = []
    judge_scores: list[float] = []
    category_qa_f1: dict[str, list[float]] = {}
    category_judge: dict[str, list[float]] = {}

    t0 = time.perf_counter()

    # Create SPLADE engine ONCE and share across all questions
    # (avoids 1s model reload × 500 questions = 8+ min wasted)
    from ncms.infrastructure.indexing.splade_engine import SpladeEngine

    shared_splade = SpladeEngine()
    logger.info("Shared SPLADE engine created (model loads on first use)")

    for qi, q in enumerate(questions):
        q_sessions = sessions_by_question.get(q.question_id, [])

        if not q_sessions:
            logger.warning(
                "Question %s has no sessions, skipping", q.question_id,
            )
            continue

        # Create fresh NCMS instance but SHARE the SPLADE engine (model stays loaded)
        # Clear vectors from previous question so search is clean
        shared_splade._vectors = {}
        store, _index, _graph, _splade, _config, svc = await _create_ncms_instance(
            shared_splade=shared_splade,
        )

        try:
            # Ingest this question's sessions
            mem_ids = await _ingest_sessions(svc, q_sessions)
            total_memories += len(mem_ids)
            total_sessions += len(q_sessions)

            # Wait for background indexing to finish before searching
            from benchmarks.core.runner import wait_for_indexing
            await wait_for_indexing(svc, run_logger=logger)

            # Evaluate
            scores = await evaluate_question(
                svc, q, top_k=top_k,
                use_rag=use_rag,
                answer_model=answer_model,
                answer_api_base=answer_api_base,
                judge_model=judge_model,
                judge_api_base=judge_api_base,
            )
        finally:
            await store.close()

        recall_scores.append(scores["recall"])
        contains_scores.append(scores["contains"])
        f1_scores.append(scores["f1"])

        cat = q.category
        if cat not in category_scores:
            category_scores[cat] = []
        category_scores[cat].append(scores["recall"])

        # RAG metrics
        if use_rag and "qa_f1" in scores:
            qa_f1_scores.append(scores["qa_f1"])
            judge_scores.append(scores["judge"])
            category_qa_f1.setdefault(cat, []).append(scores["qa_f1"])
            category_judge.setdefault(cat, []).append(scores["judge"])

        if (qi + 1) % 10 == 0 or qi == 0:
            elapsed = time.perf_counter() - t0
            log_msg = (
                f"  [{qi + 1}/{len(questions)}] {elapsed:.1f}s"
                f"  Recall@{top_k} so far: "
                f"{sum(recall_scores) / len(recall_scores):.4f}"
            )
            if use_rag and judge_scores:
                log_msg += (
                    f"  Judge: {sum(judge_scores) / len(judge_scores):.4f}"
                )
            logger.info(log_msg)

    elapsed = time.perf_counter() - t0
    n = len(recall_scores) or 1

    metrics: dict[str, float] = {
        f"Recall@{top_k}": sum(recall_scores) / n,
        "Contains": sum(contains_scores) / n,
        "F1": sum(f1_scores) / n,
        "num_questions": float(len(recall_scores)),
    }

    # Per-category recall
    for cat, cat_scores in sorted(category_scores.items()):
        cat_n = len(cat_scores) or 1
        metrics[f"Recall@{top_k}_{cat}"] = sum(cat_scores) / cat_n
        metrics[f"num_{cat}"] = float(len(cat_scores))

    # RAG aggregate metrics
    if use_rag and qa_f1_scores:
        qa_n = len(qa_f1_scores) or 1
        metrics["QA_F1"] = sum(qa_f1_scores) / qa_n
        metrics["Judge_Accuracy"] = sum(judge_scores) / qa_n

        for cat, cat_scores in sorted(category_qa_f1.items()):
            cat_n = len(cat_scores) or 1
            metrics[f"QA_F1_{cat}"] = sum(cat_scores) / cat_n

        for cat, cat_scores in sorted(category_judge.items()):
            cat_n = len(cat_scores) or 1
            metrics[f"Judge_{cat}"] = sum(cat_scores) / cat_n

    logger.info("=" * 60)
    log_msg = (
        f"LongMemEval Overall: Recall@{top_k}={metrics[f'Recall@{top_k}']:.4f}"
        f"  Contains={metrics['Contains']:.4f}  F1={metrics['F1']:.4f}"
    )
    if use_rag and "QA_F1" in metrics:
        log_msg += (
            f"  QA_F1={metrics['QA_F1']:.4f}"
            f"  Judge={metrics['Judge_Accuracy']:.4f}"
        )
    log_msg += (
        f"  ({int(metrics['num_questions'])} questions, {elapsed:.1f}s)"
    )
    logger.info(log_msg)

    # Log per-category results
    category_keys = [k for k in metrics if k.startswith(f"Recall@{top_k}_")]
    for key in sorted(category_keys):
        cat = key.split("_", 1)[1] if "_" in key else key
        count_key = f"num_{cat}"
        cat_log = f"  {cat}: Recall@{top_k}={metrics[key]:.4f}"
        if use_rag and f"Judge_{cat}" in metrics:
            cat_log += f"  Judge={metrics[f'Judge_{cat}']:.4f}"
        cat_log += f" ({int(metrics.get(count_key, 0))} questions)"
        logger.info(cat_log)

    logger.info("=" * 60)

    return {
        "overall": metrics,
        "questions_count": len(questions),
        "total_sessions": total_sessions,
        "total_memories": total_memories,
        "elapsed_seconds": round(elapsed, 1),
    }
