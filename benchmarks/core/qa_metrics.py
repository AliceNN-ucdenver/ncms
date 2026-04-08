"""QA evaluation metrics: exact match, token F1, contains match, recall@k.

Self-contained — no external dependencies. All functions operate on
prediction/ground-truth string pairs or dicts keyed by question ID.
"""

from __future__ import annotations


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
