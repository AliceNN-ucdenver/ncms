"""Shared benchmark infrastructure: metrics, datasets, configs, reporting."""
from benchmarks.core.configs import ABLATION_CONFIGS, CORE_CONFIGS, TUNED_CONFIG, AblationConfig
from benchmarks.core.datasets import DATASET_TOPICS, SUPPORTED_DATASETS, load_beir_dataset
from benchmarks.core.metrics import compute_all_metrics, mrr_at_k, ndcg_at_k, recall_at_k
from benchmarks.core.qa_metrics import (
    compute_qa_metrics,
    contains_match,
    exact_match,
    f1_token_overlap,
    recall_at_k_qa,
)

__all__ = [
    "AblationConfig", "ABLATION_CONFIGS", "CORE_CONFIGS", "TUNED_CONFIG",
    "load_beir_dataset", "SUPPORTED_DATASETS", "DATASET_TOPICS",
    "compute_all_metrics", "ndcg_at_k", "mrr_at_k", "recall_at_k",
    "compute_qa_metrics", "exact_match", "f1_token_overlap", "contains_match", "recall_at_k_qa",
]
