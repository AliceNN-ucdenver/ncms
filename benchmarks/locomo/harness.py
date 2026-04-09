"""LoCoMo evaluation harness — replay conversations into NCMS, evaluate QA retrieval.

Pattern follows the hub_replay and BEIR harnesses: create in-memory backends,
ingest data, run queries, compute metrics.
"""

from __future__ import annotations

import json
import logging
import time

from benchmarks.core.qa_metrics import (
    compute_qa_metrics,
    contains_match,
    f1_token_overlap,
    llm_judge_score,
    recall_at_k_qa,
)
from benchmarks.locomo.loader import Conversation, PlusQuestion, QAQuestion

logger = logging.getLogger(__name__)


class ConversationState:
    """Holds NCMS backends populated from a single conversation replay."""

    def __init__(
        self,
        store: object,
        index: object,
        graph: object,
        splade: object,
        config: object,
        svc: object,
        conversation_id: str,
        memory_ids: list[str],
        turn_to_memory: dict[int, str],
    ):
        self.store = store
        self.index = index
        self.graph = graph
        self.splade = splade
        self.config = config
        self.svc = svc
        self.conversation_id = conversation_id
        self.memory_ids = memory_ids
        self.turn_to_memory: dict[int, str] = turn_to_memory


async def replay_conversation(
    conversation: Conversation,
    config: object | None = None,
) -> ConversationState:
    """Replay a LoCoMo conversation into fresh in-memory NCMS backends.

    Each turn is stored as a separate memory.  Turn metadata (role, session,
    turn index) is preserved via source_agent and tags.

    Args:
        conversation: Conversation with turns to ingest.
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

    # Seed domain-specific topics for GLiNER entity extraction
    from benchmarks.core.datasets import LOCOMO_TOPICS

    topic_info = LOCOMO_TOPICS.get("locomo", {})
    domain = topic_info.get("domain", "personal")
    labels = topic_info.get("labels", [])
    if labels:
        await store.set_consolidation_value(
            f"entity_labels:{domain}",
            json.dumps(labels),
        )

    svc = MemoryService(
        store=store, index=index, graph=graph, config=config, splade=splade,
    )

    memory_ids: list[str] = []
    turn_to_memory: dict[int, str] = {}

    t0 = time.perf_counter()
    for turn in conversation.turns:
        if not turn.content.strip():
            continue

        memory = await svc.store_memory(
            content=turn.content,
            memory_type="fact",
            source_agent=turn.role,
            domains=["personal"],
            tags=["locomo", conversation.conversation_id, f"session:{turn.session_id}"],
        )
        memory_ids.append(memory.id)
        turn_to_memory[turn.turn_id] = memory.id

    elapsed = time.perf_counter() - t0
    logger.info(
        "Replayed conversation %s: %d turns -> %d memories in %.1fs",
        conversation.conversation_id,
        len(conversation.turns),
        len(memory_ids),
        elapsed,
    )

    return ConversationState(
        store=store,
        index=index,
        graph=graph,
        splade=splade,
        config=config,
        svc=svc,
        conversation_id=conversation.conversation_id,
        memory_ids=memory_ids,
        turn_to_memory=turn_to_memory,
    )


async def evaluate_qa(
    state: ConversationState,
    questions: list[QAQuestion],
    top_k: int = 5,
) -> dict[str, float]:
    """Evaluate QA retrieval against a populated conversation state.

    For each question, searches NCMS and checks whether the top-k results
    contain the ground-truth answer.

    Args:
        state: ConversationState from replay_conversation().
        questions: QA questions for this conversation.
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


async def run_locomo_benchmark(
    conversations: list[Conversation],
    questions: list[QAQuestion],
    top_k: int = 5,
) -> dict:
    """Run the full LoCoMo benchmark: replay each conversation, evaluate QA.

    Args:
        conversations: List of parsed conversations.
        questions: All QA questions (will be grouped by conversation_id).
        top_k: Number of top results for recall computation.

    Returns:
        Dict with per-conversation metrics and overall aggregates.
    """
    # Group questions by conversation
    questions_by_conv: dict[str, list[QAQuestion]] = {}
    for q in questions:
        questions_by_conv.setdefault(q.conversation_id, []).append(q)

    per_conversation: dict[str, dict[str, float]] = {}
    all_recall: list[float] = []
    all_contains: list[float] = []
    all_f1: list[float] = []

    for conv in conversations:
        conv_questions = questions_by_conv.get(conv.conversation_id, [])
        if not conv_questions:
            logger.warning(
                "No questions for conversation %s, skipping", conv.conversation_id,
            )
            continue

        logger.info(
            "Processing conversation %s (%d turns, %d questions)",
            conv.conversation_id,
            len(conv.turns),
            len(conv_questions),
        )

        state = await replay_conversation(conv)
        try:
            metrics = await evaluate_qa(state, conv_questions, top_k=top_k)
        finally:
            await state.store.close()  # type: ignore[union-attr]

        per_conversation[conv.conversation_id] = metrics

        # Accumulate for overall averages
        n = int(metrics.get("num_questions", 0))
        if n > 0:
            all_recall.extend([metrics[f"Recall@{top_k}"]] * n)
            all_contains.extend([metrics["Contains"]] * n)
            all_f1.extend([metrics["F1"]] * n)

        logger.info(
            "  %s: Recall@%d=%.4f  Contains=%.4f  F1=%.4f  (%d questions)",
            conv.conversation_id,
            top_k,
            metrics[f"Recall@{top_k}"],
            metrics["Contains"],
            metrics["F1"],
            n,
        )

    # Overall aggregates
    total_n = len(all_recall) or 1
    overall: dict[str, float] = {
        f"Recall@{top_k}": sum(all_recall) / total_n,
        "Contains": sum(all_contains) / total_n,
        "F1": sum(all_f1) / total_n,
        "num_questions": float(len(all_recall)),
        "num_conversations": float(len(per_conversation)),
    }

    logger.info("=" * 60)
    logger.info("LoCoMo Overall: Recall@%d=%.4f  Contains=%.4f  F1=%.4f  (%d questions)",
                top_k, overall[f"Recall@{top_k}"], overall["Contains"],
                overall["F1"], len(all_recall))
    logger.info("=" * 60)

    return {
        "overall": overall,
        "per_conversation": per_conversation,
    }


async def run_locomo_plus_benchmark(
    conversations: list[Conversation],
    plus_questions: list[PlusQuestion],
    top_k: int = 5,
    use_llm_judge: bool = False,
    llm_model: str | None = None,
    llm_api_base: str | None = None,
) -> dict:
    """Run the LoCoMo-Plus benchmark: replay conversations, evaluate cognitive QA.

    LoCoMo-Plus adds 401 cognitive reasoning questions (causal, goal, state,
    value) stitched into the standard LoCoMo-10 conversations.  Each Plus
    question maps to a base conversation via ``base_conv_idx``.

    Args:
        conversations: Conversations from :func:`load_locomo_dataset`.
        plus_questions: Questions from :func:`load_locomo_plus_dataset`.
        top_k: Number of top results for recall computation.
        use_llm_judge: Whether to compute LLM judge scores.
        llm_model: litellm model identifier (required if use_llm_judge=True).
        llm_api_base: LLM API base URL (required if use_llm_judge=True).

    Returns:
        Dict with per-question-type metrics, per-conversation metrics, and
        overall aggregates.
    """
    # Build conversation index by position (base_conv_idx is 0-based)
    conv_by_idx: dict[int, Conversation] = {
        i: conv for i, conv in enumerate(conversations)
    }

    # Group Plus questions by base_conv_idx
    questions_by_conv: dict[int, list[PlusQuestion]] = {}
    for q in plus_questions:
        questions_by_conv.setdefault(q.base_conv_idx, []).append(q)

    # Replay each needed conversation once and evaluate all its questions
    per_conversation: dict[str, dict[str, float]] = {}
    per_question_type: dict[str, list[float]] = {}
    all_predictions: dict[str, str] = {}
    all_ground_truths: dict[str, str] = {}
    all_question_texts: dict[str, str] = {}
    all_recall: list[float] = []
    all_f1: list[float] = []
    all_contains: list[float] = []

    for conv_idx, conv_questions in sorted(questions_by_conv.items()):
        conv = conv_by_idx.get(conv_idx)
        if conv is None:
            logger.warning(
                "No conversation at index %d for %d Plus questions, skipping",
                conv_idx, len(conv_questions),
            )
            continue

        logger.info(
            "Processing conversation idx=%d (%s, %d turns, %d Plus questions)",
            conv_idx,
            conv.conversation_id,
            len(conv.turns),
            len(conv_questions),
        )

        state = await replay_conversation(conv)
        try:
            from ncms.application.memory_service import MemoryService

            svc: MemoryService = state.svc  # type: ignore[assignment]

            conv_recall: list[float] = []
            conv_f1: list[float] = []
            conv_contains: list[float] = []

            for q in conv_questions:
                results = await svc.search(query=q.question, limit=top_k)
                retrieved_contents = [s.memory.content for s in results]

                # Recall@k
                recall = recall_at_k_qa(retrieved_contents, q.ground_truth, k=top_k)
                conv_recall.append(recall)

                # Token-level metrics against concatenated top-k
                concat_content = " ".join(retrieved_contents[:top_k])
                f1 = f1_token_overlap(concat_content, q.ground_truth)
                contains = contains_match(concat_content, q.ground_truth)
                conv_f1.append(f1)
                conv_contains.append(contains)

                # Track per question type
                per_question_type.setdefault(q.question_type, []).append(recall)

                # Collect for LLM judge
                all_predictions[q.question_id] = concat_content
                all_ground_truths[q.question_id] = q.ground_truth
                all_question_texts[q.question_id] = q.question

            all_recall.extend(conv_recall)
            all_f1.extend(conv_f1)
            all_contains.extend(conv_contains)

            n = len(conv_recall) or 1
            per_conversation[conv.conversation_id] = {
                f"Recall@{top_k}": sum(conv_recall) / n,
                "F1": sum(conv_f1) / n,
                "Contains": sum(conv_contains) / n,
                "num_questions": float(len(conv_recall)),
            }

            logger.info(
                "  %s: Recall@%d=%.4f  F1=%.4f  Contains=%.4f  (%d questions)",
                conv.conversation_id,
                top_k,
                per_conversation[conv.conversation_id][f"Recall@{top_k}"],
                per_conversation[conv.conversation_id]["F1"],
                per_conversation[conv.conversation_id]["Contains"],
                len(conv_recall),
            )
        finally:
            await state.store.close()  # type: ignore[union-attr]

    # Overall aggregates
    total_n = len(all_recall) or 1
    overall: dict[str, float] = {
        f"Recall@{top_k}": sum(all_recall) / total_n,
        "F1": sum(all_f1) / total_n,
        "Contains": sum(all_contains) / total_n,
        "num_questions": float(len(all_recall)),
        "num_conversations": float(len(per_conversation)),
    }

    # Per-question-type breakdown
    type_metrics: dict[str, dict[str, float]] = {}
    for qtype, scores in sorted(per_question_type.items()):
        n = len(scores) or 1
        type_metrics[qtype] = {
            f"Recall@{top_k}": sum(scores) / n,
            "num_questions": float(len(scores)),
        }
        overall[f"Recall@{top_k}_{qtype}"] = sum(scores) / n
        overall[f"num_{qtype}"] = float(len(scores))

    # Deterministic QA metrics (EM, F1, Contains) via compute_qa_metrics
    deterministic = compute_qa_metrics(all_predictions, all_ground_truths)
    overall["EM"] = deterministic.get("EM", 0.0)
    overall["F1_token"] = deterministic.get("F1", 0.0)

    # Optional LLM judge
    if use_llm_judge and llm_model and llm_api_base:
        logger.info("Running LLM judge scoring on %d questions...", len(all_predictions))
        import asyncio

        tasks = [
            llm_judge_score(
                question=all_question_texts.get(qid, qid),
                prediction=pred,
                ground_truth=all_ground_truths[qid],
                model=llm_model,
                api_base=llm_api_base,
            )
            for qid, pred in all_predictions.items()
            if qid in all_ground_truths
        ]
        scores = await asyncio.gather(*tasks)
        overall["llm_judge_score"] = sum(scores) / len(scores) if scores else 0.0
        logger.info("LLM judge score: %.4f", overall["llm_judge_score"])

    logger.info("=" * 60)
    logger.info(
        "LoCoMo-Plus Overall: Recall@%d=%.4f  F1=%.4f  Contains=%.4f  (%d questions)",
        top_k, overall[f"Recall@{top_k}"], overall["F1"],
        overall["Contains"], len(all_recall),
    )
    for qtype, tm in type_metrics.items():
        logger.info(
            "  %s: Recall@%d=%.4f  (%d questions)",
            qtype, top_k, tm[f"Recall@{top_k}"], int(tm["num_questions"]),
        )
    logger.info("=" * 60)

    return {
        "overall": overall,
        "per_question_type": type_metrics,
        "per_conversation": per_conversation,
    }
