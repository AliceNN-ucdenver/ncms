"""SWE-bench experiment configuration.

Reuses the dream stage progression from dream_configs.py.
Adds SWE-bench-specific settings for ingestion and evaluation.
"""

from __future__ import annotations

from benchmarks.dream_configs import DREAM_STAGES, DreamStage  # noqa: F401

# ACT-R weights to sweep at each dream stage
ACTR_SWEEP_WEIGHTS: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4)

# Retrieval weights (from tuned config)
TUNED_WEIGHTS = {
    "bm25": 0.6,
    "splade": 0.3,
    "graph": 0.3,
    "actr": 0.0,
    "hierarchy": 0.0,
}

# Ingestion concurrency
INGEST_SEMAPHORE = 3

# GLiNER labels for Django code entities
DJANGO_LABELS = [
    "class", "method", "function", "module", "field",
    "model", "view", "middleware", "url_pattern", "form",
    "template", "queryset", "manager", "migration", "signal",
    "test_case", "exception", "setting", "command", "mixin",
]
