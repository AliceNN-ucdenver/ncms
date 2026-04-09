"""LoCoMo evaluation harness — replay conversations into NCMS, evaluate QA retrieval.

Pattern follows the hub_replay and BEIR harnesses: create in-memory backends,
ingest data, run queries, compute metrics.

Supports two modes:
- Retrieval-only (default): fast containment/F1 metrics on retrieved content.
- RAG (use_rag=True): generate answer via LLM, judge via LLM, plus token F1.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta

from benchmarks.core.qa_metrics import (
    compute_qa_metrics,
    contains_match,
    f1_token_overlap,
    llm_judge_score,
    recall_at_k_qa,
)
from benchmarks.core.rag_pipeline import (
    DEFAULT_API_BASE,
    DEFAULT_MODEL,
    build_context_from_memories,
    generate_answer,
    llm_judge,
)
from benchmarks.locomo.loader import Conversation, PlusQuestion, QAQuestion

logger = logging.getLogger(__name__)

# Refusal phrases for adversarial (category 5) detection
_REFUSAL_PHRASES = (
    "no information available",
    "not mentioned",
    "not provided",
    "cannot answer",
    "can't answer",
    "no relevant information",
    "i don't know",
    "i don't have",
    "not discussed",
    "not available",
    "no evidence",
    "not enough information",
    "insufficient information",
    "unable to determine",
    "unable to answer",
    "not specified",
    "no record",
    "not in the context",
    "not in the conversation",
)


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
    use_rag: bool = False,
    answer_model: str = DEFAULT_MODEL,
    answer_api_base: str = DEFAULT_API_BASE,
    judge_model: str = DEFAULT_MODEL,
    judge_api_base: str = DEFAULT_API_BASE,
) -> dict[str, float]:
    """Evaluate QA retrieval against a populated conversation state.

    For each question, searches NCMS and checks whether the top-k results
    contain the ground-truth answer.

    When use_rag=True, also generates answers via LLM and judges them,
    with category-specific evaluation matching the LoCoMo reference:
      - Category 1 (multi-hop): comma-split F1
      - Category 2 (temporal): append date context to question
      - Category 5 (adversarial): check for refusal phrases

    Args:
        state: ConversationState from replay_conversation().
        questions: QA questions for this conversation.
        top_k: Number of top results to consider.
        use_rag: If True, generate answers and judge them via LLM.
        answer_model: LLM model for answer generation.
        answer_api_base: API base URL for answer generation.
        judge_model: LLM model for judging.
        judge_api_base: API base URL for judging.

    Returns:
        Dict with Recall@k, Contains, F1, and per-category breakdowns.
        When use_rag=True, also includes QA_F1, Judge_Accuracy, and
        per-category QA metrics.
    """
    from ncms.application.memory_service import MemoryService

    svc: MemoryService = state.svc  # type: ignore[assignment]

    recall_scores: list[float] = []
    contains_scores: list[float] = []
    f1_scores: list[float] = []
    category_scores: dict[str, list[float]] = {}

    # RAG-specific accumulators
    qa_f1_scores: list[float] = []
    judge_scores: list[float] = []
    category_qa_f1: dict[str, list[float]] = {}
    category_judge: dict[str, list[float]] = {}

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

        # RAG evaluation
        if use_rag:
            context = build_context_from_memories(results, max_chars=4000)

            # Category-specific question modification
            eval_question = q.question
            if cat == "temporal":
                eval_question = (
                    q.question
                    + " Use the date of the conversation to "
                    "answer with an approximate date."
                )

            # Category-specific system prompt
            if cat == "adversarial":
                system = (
                    "Answer the following question based on the context. "
                    "If the information is not available in the context, "
                    'say "No information available".'
                )
            else:
                system = (
                    "Answer in 1-5 words only. Use exact names, dates, "
                    "places and terms from the context. "
                    "Never write full sentences."
                )

            prediction = await generate_answer(
                eval_question,
                context,
                system_prompt=system,
                model=answer_model,
                api_base=answer_api_base,
                max_tokens=50,
            )

            # Score: category-specific token F1
            if cat == "multi-hop":
                # Comma-split F1 for multi-hop questions
                qa_f1 = _multihop_f1(prediction, q.answer)
            elif cat == "adversarial":
                # Binary: model should refuse
                lower = prediction.lower()
                qa_f1 = 1.0 if any(p in lower for p in _REFUSAL_PHRASES) else 0.0
            elif cat == "open-domain":
                # Use primary answer (before semicolon)
                primary = q.answer.split(";")[0].strip()
                qa_f1 = f1_token_overlap(prediction, primary)
            else:
                qa_f1 = f1_token_overlap(prediction, q.answer)
            qa_f1_scores.append(qa_f1)

            # LLM judge (skip for adversarial — use F1 result)
            if cat == "adversarial":
                judge_ok = 1.0 if qa_f1 == 1.0 else 0.0
            else:
                judge_ok = 1.0 if await llm_judge(
                    q.question, q.answer, prediction,
                    judge_type="temporal" if cat == "temporal" else "default",
                    model=judge_model,
                    api_base=judge_api_base,
                ) else 0.0
            judge_scores.append(judge_ok)

            # Per-category RAG metrics
            category_qa_f1.setdefault(cat, []).append(qa_f1)
            category_judge.setdefault(cat, []).append(judge_ok)

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

    # RAG metrics
    if use_rag and qa_f1_scores:
        qa_n = len(qa_f1_scores) or 1
        metrics["QA_F1"] = sum(qa_f1_scores) / qa_n
        metrics["Judge_Accuracy"] = sum(judge_scores) / qa_n

        for cat, scores in sorted(category_qa_f1.items()):
            cat_n = len(scores) or 1
            metrics[f"QA_F1_{cat}"] = sum(scores) / cat_n

        for cat, scores in sorted(category_judge.items()):
            cat_n = len(scores) or 1
            metrics[f"Judge_{cat}"] = sum(scores) / cat_n

    return metrics


def _multihop_f1(prediction: str, answer: str) -> float:
    """Compute F1 over comma-separated answer elements (for multi-hop questions)."""
    pred_parts = {p.strip().lower() for p in prediction.split(",") if p.strip()}
    answer_parts = {a.strip().lower() for a in answer.split(",") if a.strip()}

    if not pred_parts or not answer_parts:
        return f1_token_overlap(prediction, answer)

    tp = len(pred_parts & answer_parts)
    precision = tp / len(pred_parts) if pred_parts else 0.0
    recall = tp / len(answer_parts) if answer_parts else 0.0

    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


async def run_locomo_benchmark(
    conversations: list[Conversation],
    questions: list[QAQuestion],
    top_k: int = 5,
    use_rag: bool = False,
    answer_model: str = DEFAULT_MODEL,
    answer_api_base: str = DEFAULT_API_BASE,
    judge_model: str = DEFAULT_MODEL,
    judge_api_base: str = DEFAULT_API_BASE,
) -> dict:
    """Run the full LoCoMo benchmark: replay each conversation, evaluate QA.

    Args:
        conversations: List of parsed conversations.
        questions: All QA questions (will be grouped by conversation_id).
        top_k: Number of top results for recall computation.
        use_rag: If True, generate answers and judge via LLM.
        answer_model: LLM model for answer generation.
        answer_api_base: API base URL for answer generation.
        judge_model: LLM model for judging.
        judge_api_base: API base URL for judging.

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
    all_qa_f1: list[float] = []
    all_judge: list[float] = []

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
            metrics = await evaluate_qa(
                state, conv_questions, top_k=top_k,
                use_rag=use_rag,
                answer_model=answer_model,
                answer_api_base=answer_api_base,
                judge_model=judge_model,
                judge_api_base=judge_api_base,
            )
        finally:
            await state.store.close()  # type: ignore[union-attr]

        per_conversation[conv.conversation_id] = metrics

        # Accumulate for overall averages
        n = int(metrics.get("num_questions", 0))
        if n > 0:
            all_recall.extend([metrics[f"Recall@{top_k}"]] * n)
            all_contains.extend([metrics["Contains"]] * n)
            all_f1.extend([metrics["F1"]] * n)
            if use_rag and "QA_F1" in metrics:
                all_qa_f1.extend([metrics["QA_F1"]] * n)
                all_judge.extend([metrics["Judge_Accuracy"]] * n)

        log_msg = (
            f"  {conv.conversation_id}: Recall@{top_k}={metrics[f'Recall@{top_k}']:.4f}"
            f"  Contains={metrics['Contains']:.4f}  F1={metrics['F1']:.4f}"
        )
        if use_rag and "QA_F1" in metrics:
            log_msg += (
                f"  QA_F1={metrics['QA_F1']:.4f}"
                f"  Judge={metrics['Judge_Accuracy']:.4f}"
            )
        log_msg += f"  ({n} questions)"
        logger.info(log_msg)

    # Overall aggregates
    total_n = len(all_recall) or 1
    overall: dict[str, float] = {
        f"Recall@{top_k}": sum(all_recall) / total_n,
        "Contains": sum(all_contains) / total_n,
        "F1": sum(all_f1) / total_n,
        "num_questions": float(len(all_recall)),
        "num_conversations": float(len(per_conversation)),
    }

    if use_rag and all_qa_f1:
        qa_n = len(all_qa_f1) or 1
        overall["QA_F1"] = sum(all_qa_f1) / qa_n
        overall["Judge_Accuracy"] = sum(all_judge) / qa_n

    logger.info("=" * 60)
    log_msg = (
        f"LoCoMo Overall: Recall@{top_k}={overall[f'Recall@{top_k}']:.4f}"
        f"  Contains={overall['Contains']:.4f}  F1={overall['F1']:.4f}"
    )
    if use_rag and "QA_F1" in overall:
        log_msg += (
            f"  QA_F1={overall['QA_F1']:.4f}"
            f"  Judge={overall['Judge_Accuracy']:.4f}"
        )
    log_msg += f"  ({len(all_recall)} questions)"
    logger.info(log_msg)
    logger.info("=" * 60)

    return {
        "overall": overall,
        "per_conversation": per_conversation,
    }


# ---------------------------------------------------------------------------
# Cue-trigger stitching for LoCoMo-Plus
# ---------------------------------------------------------------------------

_WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "several": 4,
}

_TIME_GAP_RE = re.compile(
    r"(?:about\s+|around\s+)?(\w+)\s+(week|month|year)s?\s+later",
    re.IGNORECASE,
)


def parse_time_gap(gap_str: str) -> int:
    """Convert a time-gap string like 'two weeks later' to a number of days."""
    m = _TIME_GAP_RE.search(gap_str)
    if not m:
        return 14  # default: 2 weeks
    num_word, unit = m.group(1).lower(), m.group(2).lower()
    num = _WORD_NUMS.get(num_word)
    if num is None:
        try:
            num = int(num_word)
        except ValueError:
            num = 2
    if unit == "week":
        return num * 7
    elif unit == "month":
        return num * 30
    elif unit == "year":
        return num * 365
    return num * 7


_DATE_PATTERNS = [
    # "1:56 pm on 8 May, 2023"
    re.compile(r"(\d{1,2}:\d{2}\s*[ap]m)\s+on\s+(\d{1,2}\s+\w+,?\s+\d{4})", re.I),
    # "8 May, 2023"
    re.compile(r"(\d{1,2}\s+\w+,?\s+\d{4})", re.I),
]


def _parse_session_date(dt_str: str) -> datetime | None:
    """Parse LoCoMo date_time strings into datetime objects."""
    for pat in _DATE_PATTERNS:
        m = pat.search(dt_str)
        if m:
            date_part = m.groups()[-1].replace(",", "")
            try:
                return datetime.strptime(date_part, "%d %B %Y")
            except ValueError:
                pass
    return None


def _extract_raw_sessions(raw_conv: dict) -> list[dict]:
    """Extract ordered sessions with parsed dates from a raw LoCoMo conversation dict."""
    sessions: list[dict] = []
    idx = 1
    while True:
        key = f"session_{idx}"
        if key not in raw_conv:
            break
        dt_str = raw_conv.get(f"session_{idx}_date_time", "")
        sessions.append({
            "session_num": idx,
            "date_time": dt_str,
            "parsed_date": _parse_session_date(dt_str),
            "turns": raw_conv[key],
        })
        idx += 1
    return sessions


def _map_ab_speakers(text: str, speaker_a: str, speaker_b: str) -> str:
    """Replace A:/B: with actual speaker names in dialogue text."""
    lines = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if line.startswith("A:"):
            lines.append(f"{speaker_a}: {line[2:].strip()}")
        elif line.startswith("B:"):
            lines.append(f"{speaker_b}: {line[2:].strip()}")
        else:
            lines.append(line)
    return "\n".join(lines)


def stitch_conversation(
    raw_conv: dict,
    cue_dialogue: str,
    trigger_query: str,
    time_gap_str: str,
) -> tuple[list[dict], str, str]:
    """Stitch cue dialogue and trigger query into a LoCoMo conversation.

    The stitching protocol:
    1. Parse the base conversation into sessions with dates.
    2. Set trigger_date = last_session_date + 7 days.
    3. Set cue_date = trigger_date - time_gap_days.
    4. Insert the cue dialogue at the temporally correct position.
    5. Append the trigger query as the final session.

    Args:
        raw_conv: Raw LoCoMo conversation dict (with speaker_a, speaker_b,
            session_N, session_N_date_time keys).
        cue_dialogue: The cue dialogue text (ground_truth from the checkpoint).
        trigger_query: The trigger query text (question from the checkpoint).
        time_gap_str: Temporal gap description (e.g. "two weeks later").

    Returns:
        Tuple of (sessions, mapped_cue, mapped_trigger) where sessions is the
        full list including stitched cue and trigger sessions.
    """
    speaker_a = raw_conv.get("speaker_a", "A")
    speaker_b = raw_conv.get("speaker_b", "B")

    sessions = _extract_raw_sessions(raw_conv)
    if not sessions:
        return [], cue_dialogue, trigger_query

    # Map A/B to actual speaker names
    mapped_cue = _map_ab_speakers(cue_dialogue, speaker_a, speaker_b)
    mapped_trigger = _map_ab_speakers(trigger_query, speaker_a, speaker_b)

    # Compute insertion points
    time_gap_days = parse_time_gap(time_gap_str)
    last_date = None
    for s in reversed(sessions):
        if s["parsed_date"]:
            last_date = s["parsed_date"]
            break

    if last_date is None:
        # Fallback: put cue before last session, trigger after
        cue_insert_idx = max(0, len(sessions) - 2)
    else:
        trigger_date = last_date + timedelta(days=7)
        cue_date = trigger_date - timedelta(days=time_gap_days)

        # Find insertion index for cue (first session at or after cue_date)
        cue_insert_idx = len(sessions)
        for i, s in enumerate(sessions):
            if s["parsed_date"] and s["parsed_date"] >= cue_date:
                cue_insert_idx = i
                break

    # Build cue turns
    cue_turns = []
    for line in mapped_cue.split("\n"):
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            speaker, text = line.split(":", 1)
            cue_turns.append({"speaker": speaker.strip(), "text": text.strip()})
        else:
            cue_turns.append({"speaker": speaker_a, "text": line})

    # Compute cue session date string
    cue_dt = ""
    if last_date is not None:
        cue_target = last_date + timedelta(days=7) - timedelta(days=time_gap_days)
        try:
            cue_dt = cue_target.strftime("%-I:%M %p on %-d %B, %Y")
        except ValueError:
            cue_dt = cue_target.strftime("%I:%M %p on %d %B, %Y").lstrip("0")
    elif cue_insert_idx < len(sessions):
        cue_dt = sessions[cue_insert_idx]["date_time"]

    cue_session = {
        "session_num": -1,
        "date_time": cue_dt,
        "parsed_date": None,
        "turns": cue_turns,
        "is_cue": True,
    }

    # Build trigger turns
    trigger_turns = []
    for line in mapped_trigger.split("\n"):
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            speaker, text = line.split(":", 1)
            trigger_turns.append({"speaker": speaker.strip(), "text": text.strip()})
        else:
            trigger_turns.append({"speaker": speaker_a, "text": line})

    trigger_dt = ""
    if last_date is not None:
        trigger_target = last_date + timedelta(days=7)
        try:
            trigger_dt = trigger_target.strftime("%-I:%M %p on %-d %B, %Y")
        except ValueError:
            trigger_dt = trigger_target.strftime("%I:%M %p on %d %B, %Y").lstrip("0")

    trigger_session = {
        "session_num": -2,
        "date_time": trigger_dt,
        "parsed_date": None,
        "turns": trigger_turns,
        "is_trigger": True,
    }

    # Insert cue at the correct temporal position, trigger at the end
    result = list(sessions)
    result.insert(cue_insert_idx, cue_session)
    result.append(trigger_session)

    return result, mapped_cue, mapped_trigger


async def _replay_stitched_conversation(
    stitched_sessions: list[dict],
    conversation_id: str,
    config: object | None = None,
) -> ConversationState:
    """Replay stitched sessions (base + cue, excluding trigger) into NCMS.

    All sessions except the trigger session (``is_trigger=True``) are ingested.
    Each session's turns are stored as individual memories.

    Args:
        stitched_sessions: Sessions from :func:`stitch_conversation`.
        conversation_id: Identifier for this conversation.
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
    turn_idx = 0
    for session in stitched_sessions:
        # Skip the trigger session — it's the query, not data to ingest
        if session.get("is_trigger", False):
            continue

        session_tag = "cue" if session.get("is_cue", False) else str(session["session_num"])

        for turn in session.get("turns", []):
            if isinstance(turn, dict):
                speaker = turn.get("speaker", "unknown")
                text = turn.get("text", "")
            else:
                continue

            if not text.strip():
                continue

            memory = await svc.store_memory(
                content=text,
                memory_type="fact",
                source_agent=speaker,
                domains=["personal"],
                tags=["locomo", conversation_id, f"session:{session_tag}"],
            )
            memory_ids.append(memory.id)
            turn_to_memory[turn_idx] = memory.id
            turn_idx += 1

    elapsed = time.perf_counter() - t0
    logger.info(
        "Replayed stitched conversation %s: %d memories in %.1fs",
        conversation_id,
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
        conversation_id=conversation_id,
        memory_ids=memory_ids,
        turn_to_memory=turn_to_memory,
    )


async def run_locomo_plus_benchmark(
    conversations: list[Conversation],
    plus_questions: list[PlusQuestion],
    top_k: int = 5,
    use_llm_judge: bool = False,
    llm_model: str | None = None,
    llm_api_base: str | None = None,
) -> dict:
    """Run the LoCoMo-Plus benchmark with proper cue-trigger stitching.

    For each Plus question:
    1. Load the raw base LoCoMo conversation (via base_conv_idx).
    2. Stitch the cue dialogue into the conversation at the temporally
       correct position based on the time gap.
    3. Ingest all sessions INCLUDING the stitched cue (but NOT the trigger).
    4. Use the trigger (the question) as the search query.
    5. Score: does the recalled context contain the cue evidence?

    When ``use_llm_judge=True``, also evaluates whether the LLM response
    demonstrates awareness of the implicit connection between trigger and cue.

    Args:
        conversations: Conversations from :func:`load_locomo_dataset`.
        plus_questions: Questions from :func:`load_locomo_plus_dataset`.
        top_k: Number of top results for recall computation.
        use_llm_judge: Whether to compute LLM cognitive judge scores.
        llm_model: litellm model identifier (required if use_llm_judge=True).
        llm_api_base: LLM API base URL (required if use_llm_judge=True).

    Returns:
        Dict with per-question-type metrics, per-conversation metrics, and
        overall aggregates.
    """
    from benchmarks.locomo.loader import download_locomo

    # Load raw LoCoMo data for stitching (need the raw conversation dicts)
    repo_dir = download_locomo()
    candidates = [
        repo_dir / "data" / "locomo10.json",
        repo_dir / "locomo10.json",
    ]
    raw_data_file = None
    for candidate in candidates:
        if candidate.is_file():
            raw_data_file = candidate
            break

    raw_conversations: list[dict] = []
    if raw_data_file:
        with open(raw_data_file) as f:
            raw_data = json.load(f)
        if isinstance(raw_data, list):
            raw_conversations = raw_data
        elif isinstance(raw_data, dict):
            raw_conversations = list(raw_data.values())

    # Build raw conversation index by position (base_conv_idx is 0-based)
    raw_conv_by_idx: dict[int, dict] = {}
    for i, entry in enumerate(raw_conversations):
        if isinstance(entry, dict):
            raw_conv_by_idx[i] = entry.get("conversation", entry)

    # Group Plus questions by base_conv_idx
    questions_by_conv: dict[int, list[PlusQuestion]] = {}
    for q in plus_questions:
        questions_by_conv.setdefault(q.base_conv_idx, []).append(q)

    per_conversation: dict[str, dict[str, float]] = {}
    per_question_type: dict[str, list[float]] = {}
    all_predictions: dict[str, str] = {}
    all_ground_truths: dict[str, str] = {}
    all_question_texts: dict[str, str] = {}
    all_recall: list[float] = []
    all_f1: list[float] = []
    all_contains: list[float] = []

    for conv_idx, conv_questions in sorted(questions_by_conv.items()):
        raw_conv = raw_conv_by_idx.get(conv_idx)
        if raw_conv is None:
            logger.warning(
                "No raw conversation at index %d for %d Plus questions, skipping",
                conv_idx, len(conv_questions),
            )
            continue

        conv_id = f"conv_{conv_idx}"
        logger.info(
            "Processing conversation idx=%d (%d Plus questions, stitching each)",
            conv_idx, len(conv_questions),
        )

        conv_recall: list[float] = []
        conv_f1: list[float] = []
        conv_contains: list[float] = []

        for q in conv_questions:
            # Stitch this question's cue into the base conversation
            stitched_sessions, mapped_cue, mapped_trigger = stitch_conversation(
                raw_conv=raw_conv,
                cue_dialogue=q.cue_dialogue,
                trigger_query=q.question,
                time_gap_str=q.time_gap,
            )

            if not stitched_sessions:
                logger.warning(
                    "Stitching failed for question %s, skipping", q.question_id,
                )
                continue

            # Count ingested turns (all sessions except trigger)
            ingest_turns = sum(
                len(s.get("turns", []))
                for s in stitched_sessions
                if not s.get("is_trigger", False)
            )
            logger.debug(
                "  %s: stitched %d sessions (%d ingest turns), "
                "time_gap=%s, cue_lines=%d",
                q.question_id,
                len(stitched_sessions),
                ingest_turns,
                q.time_gap,
                len(mapped_cue.split("\n")),
            )

            # Replay stitched conversation (ingest all except trigger)
            state = await _replay_stitched_conversation(
                stitched_sessions,
                conversation_id=f"{conv_id}_{q.question_id}",
            )

            try:
                from ncms.application.memory_service import MemoryService

                svc: MemoryService = state.svc  # type: ignore[assignment]

                # Use the trigger query to search
                results = await svc.search(query=q.question, limit=top_k)
                retrieved_contents = [s.memory.content for s in results]

                # Score against the cue dialogue (ground truth)
                recall = recall_at_k_qa(
                    retrieved_contents, q.ground_truth, k=top_k,
                )
                conv_recall.append(recall)

                concat_content = " ".join(retrieved_contents[:top_k])
                f1 = f1_token_overlap(concat_content, q.ground_truth)
                contains = contains_match(concat_content, q.ground_truth)
                conv_f1.append(f1)
                conv_contains.append(contains)

                per_question_type.setdefault(q.question_type, []).append(recall)

                all_predictions[q.question_id] = concat_content
                all_ground_truths[q.question_id] = q.ground_truth
                all_question_texts[q.question_id] = q.question
            finally:
                await state.store.close()  # type: ignore[union-attr]

        all_recall.extend(conv_recall)
        all_f1.extend(conv_f1)
        all_contains.extend(conv_contains)

        n = len(conv_recall) or 1
        per_conversation[conv_id] = {
            f"Recall@{top_k}": sum(conv_recall) / n,
            "F1": sum(conv_f1) / n,
            "Contains": sum(conv_contains) / n,
            "num_questions": float(len(conv_recall)),
        }

        logger.info(
            "  %s: Recall@%d=%.4f  F1=%.4f  Contains=%.4f  (%d questions)",
            conv_id,
            top_k,
            per_conversation[conv_id][f"Recall@{top_k}"],
            per_conversation[conv_id]["F1"],
            per_conversation[conv_id]["Contains"],
            len(conv_recall),
        )

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

    # Optional LLM cognitive judge
    if use_llm_judge and llm_model and llm_api_base:
        logger.info(
            "Running cognitive LLM judge on %d questions...", len(all_predictions),
        )
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
        logger.info("Cognitive LLM judge score: %.4f", overall["llm_judge_score"])

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
