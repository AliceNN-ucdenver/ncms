"""NCMS Retrieval Pipeline Ablation Study.

Benchmarks the contribution of each pipeline component (BM25, SPLADE,
Graph Expansion, ACT-R Scoring) using standard BEIR IR benchmark datasets.

Usage:
    uv sync --group bench
    uv run python -m benchmarks.beir.run_ablation
"""
