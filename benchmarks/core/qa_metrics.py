"""QA evaluation metrics: exact match, token F1, contains match, recall@k, LLM judge.

Self-contained — no external dependencies beyond litellm for LLM judge.
All functions operate on prediction/ground-truth string pairs or dicts
keyed by question ID.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    """Lowercase and strip whitespace for comparison."""
    return text.lower().strip()


def exact_match(prediction: str, ground_truth: str) -> float:
    """Normalized exact match between prediction and ground truth.

    Both strings are lowercased and stripped before comparison.

    Returns:
        1.0 if strings match after normalization, 0.0 otherwise.
    """
    return 1.0 if _normalize(prediction) == _normalize(ground_truth) else 0.0


def f1_token_overlap(prediction: str, ground_truth: str) -> float:
    """Token-level F1 between prediction and ground truth.

    Splits on whitespace, computes precision/recall/F1 over token sets.

    Returns:
        F1 score (0.0 to 1.0). Returns 0.0 if either string is empty.
    """
    pred_tokens = _normalize(prediction).split()
    truth_tokens = _normalize(ground_truth).split()

    if not pred_tokens or not truth_tokens:
        return 0.0

    common = set(pred_tokens) & set(truth_tokens)
    if not common:
        return 0.0

    # Count token occurrences for proper precision/recall
    from collections import Counter

    pred_counts = Counter(pred_tokens)
    truth_counts = Counter(truth_tokens)

    # Number of shared tokens (min of counts for each token)
    num_shared = sum(min(pred_counts[tok], truth_counts[tok]) for tok in common)

    precision = num_shared / len(pred_tokens)
    recall = num_shared / len(truth_tokens)

    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def contains_match(prediction: str, ground_truth: str) -> float:
    """Check if ground truth appears as a substring in prediction.

    Both strings are normalized (lowercase, stripped) before comparison.

    Returns:
        1.0 if ground truth is contained in prediction, 0.0 otherwise.
    """
    return 1.0 if _normalize(ground_truth) in _normalize(prediction) else 0.0


def recall_at_k_qa(
    retrieved_contents: list[str],
    ground_truth: str,
    k: int = 5,
) -> float:
    """Check if any of the top-k retrieved memory contents contain the answer.

    Args:
        retrieved_contents: List of retrieved text contents, ranked by relevance.
        ground_truth: The expected answer string.
        k: Number of top results to consider.

    Returns:
        1.0 if any top-k content contains the ground truth, 0.0 otherwise.
    """
    norm_truth = _normalize(ground_truth)
    if not norm_truth:
        return 0.0

    for content in retrieved_contents[:k]:
        if norm_truth in _normalize(content):
            return 1.0
    return 0.0


def compute_qa_metrics(
    predictions: dict[str, str],
    ground_truths: dict[str, str],
) -> dict[str, float]:
    """Aggregate all QA metrics averaged across all questions.

    Args:
        predictions: {question_id: predicted_answer}
        ground_truths: {question_id: ground_truth_answer}

    Returns:
        Dict with EM, F1, Contains averaged across all matched question IDs,
        plus num_questions count.
    """
    em_scores: list[float] = []
    f1_scores: list[float] = []
    contains_scores: list[float] = []

    for qid, pred in predictions.items():
        if qid not in ground_truths:
            continue
        truth = ground_truths[qid]
        em_scores.append(exact_match(pred, truth))
        f1_scores.append(f1_token_overlap(pred, truth))
        contains_scores.append(contains_match(pred, truth))

    n = len(em_scores) or 1
    return {
        "EM": sum(em_scores) / n,
        "F1": sum(f1_scores) / n,
        "Contains": sum(contains_scores) / n,
        "num_questions": len(em_scores),
    }


# ---------------------------------------------------------------------------
# LLM-as-Judge scoring
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"
_DEFAULT_API_BASE = "http://spark-ee7d.local:8000/v1"

_JUDGE_SYSTEM = (
    "You are an evaluation judge. Given a question, a ground truth answer, "
    "and a predicted answer, score how well the prediction matches the ground "
    "truth. Respond with ONLY a number between 0.0 and 1.0. No explanation."
)


async def llm_judge_score(
    question: str,
    prediction: str,
    ground_truth: str,
    *,
    model: str = _DEFAULT_MODEL,
    api_base: str = _DEFAULT_API_BASE,
) -> float:
    """Score a single prediction against ground truth using an LLM judge.

    The LLM is prompted to return a float between 0.0 and 1.0 indicating
    how well the prediction matches. Falls back to 0.0 on any failure.

    Args:
        question: The original question.
        prediction: The predicted answer.
        ground_truth: The expected answer.
        model: litellm model identifier.
        api_base: LLM API base URL.

    Returns:
        Score between 0.0 and 1.0, or 0.0 on failure.
    """
    try:
        import litellm  # noqa: E402

        user_prompt = (
            f"Question: {question}\n"
            f"Ground Truth: {ground_truth}\n"
            f"Prediction: {prediction}\n\n"
            f"Score:"
        )

        kwargs: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 10,
            "temperature": 0.0,
        }
        if api_base:
            kwargs["api_base"] = api_base

        # Disable thinking mode for Nemotron Nano
        if "Nemotron" in model or "nemotron" in model:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

        response = await litellm.acompletion(**kwargs)
        text = response.choices[0].message.content.strip()
        score = float(text)
        return max(0.0, min(1.0, score))
    except (ValueError, TypeError):
        logger.warning("LLM judge returned unparseable score: %s", text)  # type: ignore[possibly-undefined]
        return 0.0
    except Exception:
        logger.warning("LLM judge call failed", exc_info=True)
        return 0.0


async def compute_qa_metrics_with_judge(
    predictions: dict[str, str],
    ground_truths: dict[str, str],
    questions: dict[str, str] | None = None,
    *,
    model: str = _DEFAULT_MODEL,
    api_base: str = _DEFAULT_API_BASE,
) -> dict[str, float]:
    """Compute all QA metrics including LLM judge scores.

    Runs :func:`compute_qa_metrics` for deterministic metrics (EM, F1,
    Contains) and adds ``llm_judge_score`` averaged across all questions.

    Args:
        predictions: {question_id: predicted_answer}
        ground_truths: {question_id: ground_truth_answer}
        questions: Optional {question_id: question_text}. If *None*,
            the question_id itself is used as the question text.
        model: litellm model identifier for the judge.
        api_base: LLM API base URL.

    Returns:
        Dict with EM, F1, Contains, llm_judge_score, and num_questions.
    """
    base_metrics = compute_qa_metrics(predictions, ground_truths)

    # Collect (qid, pred, truth) triples for judge scoring
    pairs: list[tuple[str, str, str]] = []
    for qid, pred in predictions.items():
        if qid in ground_truths:
            pairs.append((qid, pred, ground_truths[qid]))

    if not pairs:
        base_metrics["llm_judge_score"] = 0.0
        return base_metrics

    # Run all judge calls concurrently
    tasks = [
        llm_judge_score(
            question=questions[qid] if questions and qid in questions else qid,
            prediction=pred,
            ground_truth=truth,
            model=model,
            api_base=api_base,
        )
        for qid, pred, truth in pairs
    ]
    scores = await asyncio.gather(*tasks)

    base_metrics["llm_judge_score"] = sum(scores) / len(scores)
    return base_metrics
