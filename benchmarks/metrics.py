"""IR evaluation metrics: nDCG@k, MRR@k, Recall@k.

Self-contained — no external dependencies. All functions operate on
BEIR-style qrels (dict[query_id, dict[doc_id, int]]) and ranked result
lists (dict[query_id, list[doc_id]]).
"""

from __future__ import annotations

import math


def dcg_at_k(ranked_ids: list[str], qrel: dict[str, int], k: int) -> float:
    """Discounted Cumulative Gain at rank k.

    DCG@k = sum_{i=1}^{k} rel_i / log2(i + 1)
    """
    score = 0.0
    for i, doc_id in enumerate(ranked_ids[:k]):
        rel = qrel.get(doc_id, 0)
        if rel > 0:
            score += rel / math.log2(i + 2)  # i+2 because i is 0-indexed
    return score


def ndcg_at_k(ranked_ids: list[str], qrel: dict[str, int], k: int) -> float:
    """Normalized Discounted Cumulative Gain at rank k.

    nDCG@k = DCG@k / IDCG@k where IDCG is the ideal DCG from perfect ranking.
    """
    dcg = dcg_at_k(ranked_ids, qrel, k)

    # Ideal DCG: sort relevant docs by descending relevance
    ideal_rels = sorted(qrel.values(), reverse=True)[:k]
    idcg = 0.0
    for i, rel in enumerate(ideal_rels):
        if rel > 0:
            idcg += rel / math.log2(i + 2)

    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def mrr_at_k(ranked_ids: list[str], qrel: dict[str, int], k: int) -> float:
    """Mean Reciprocal Rank at rank k.

    MRR@k = 1 / rank of first relevant document (0 if none in top k).
    """
    for i, doc_id in enumerate(ranked_ids[:k]):
        if qrel.get(doc_id, 0) > 0:
            return 1.0 / (i + 1)
    return 0.0


def recall_at_k(ranked_ids: list[str], qrel: dict[str, int], k: int) -> float:
    """Recall at rank k.

    Recall@k = |relevant ∩ retrieved@k| / |relevant|
    """
    relevant = {doc_id for doc_id, rel in qrel.items() if rel > 0}
    if not relevant:
        return 0.0

    retrieved = set(ranked_ids[:k])
    return len(relevant & retrieved) / len(relevant)


def compute_all_metrics(
    rankings: dict[str, list[str]],
    qrels: dict[str, dict[str, int]],
) -> dict[str, float]:
    """Compute aggregate metrics across all queries.

    Args:
        rankings: query_id -> ranked list of doc_ids
        qrels: query_id -> {doc_id: relevance_grade}

    Returns:
        Dict with nDCG@10, MRR@10, Recall@10, Recall@100 averaged across queries.
    """
    ndcg_10_scores: list[float] = []
    mrr_10_scores: list[float] = []
    recall_10_scores: list[float] = []
    recall_100_scores: list[float] = []

    for query_id, qrel in qrels.items():
        ranked = rankings.get(query_id, [])
        # Skip queries with no relevant documents in qrels
        if not any(v > 0 for v in qrel.values()):
            continue

        ndcg_10_scores.append(ndcg_at_k(ranked, qrel, 10))
        mrr_10_scores.append(mrr_at_k(ranked, qrel, 10))
        recall_10_scores.append(recall_at_k(ranked, qrel, 10))
        recall_100_scores.append(recall_at_k(ranked, qrel, 100))

    n = len(ndcg_10_scores) or 1
    return {
        "nDCG@10": sum(ndcg_10_scores) / n,
        "MRR@10": sum(mrr_10_scores) / n,
        "Recall@10": sum(recall_10_scores) / n,
        "Recall@100": sum(recall_100_scores) / n,
        "num_queries": len(ndcg_10_scores),
    }
