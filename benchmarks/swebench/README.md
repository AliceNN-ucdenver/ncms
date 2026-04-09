# SWE-bench Django Benchmark

## What It Tests

Evaluates NCMS against real-world software engineering issues from the Django project across 4 memory competencies:

- **AR (Accurate Retrieval)** -- Standard IR retrieval (nDCG@10, MRR@10)
- **TTL (Test-Time Learning)** -- Subsystem classification via majority vote on retrieved results
- **LRU (Long-Range Understanding)** -- Cross-subsystem connection retrieval
- **CR (Conflict Resolution)** -- Temporal ordering of file state changes (temporal MRR)

Runs a full dream cycle experiment with ACT-R crossover sweeps at each stage.

## Dataset

| Split    | Size         | Source                       |
|----------|-------------|------------------------------|
| Train    | ~300 issues  | SWE-bench Django (princeton-nlp) |
| Test     | ~100 issues  | SWE-bench Django holdout     |

Issues span 15+ Django subsystems (db, forms, queries, admin, etc.) with chronological ordering by created_at.

## GLiNER Topic Labels (Replace Mode)

**Domain**: `django`

Labels (10, trimmed from original 20):
`class`, `method`, `function`, `module`, `field`, `model`, `view`, `middleware`, `form`, `command`

Rationale: Kept the 10 labels GLiNER can reliably detect in issue text. Dropped code-internal labels (queryset, manager, migration, signal, test_case, exception, setting, mixin, url_pattern, template) that produce low-quality entity extraction from natural language issue descriptions.

## How to Run

```bash
# Requires LLM endpoint for consolidation
uv run python -m benchmarks.swebench.run_swebench \
    --llm-model openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
    --llm-api-base http://spark-ee7d.local:8000/v1

# With Ollama
uv run python -m benchmarks.swebench.run_swebench \
    --llm-model ollama_chat/qwen3.5:35b-a3b

# Analysis only (no LLM needed)
uv run python -m benchmarks.swebench.run_swebench --analysis-only
```

## Expected Metrics

| Competency | Metric         | Baseline | After Dream |
|------------|---------------|----------|-------------|
| AR         | nDCG@10       | ~0.17    | ~0.18       |
| TTL        | accuracy      | ~0.35    | ~0.35       |
| CR         | temporal_mrr  | ~0.30    | ~0.30       |
| LRU        | nDCG@10       | ~0.12    | ~0.14       |

Phase 11 recall-based measurements (structured retrieval) provide additional AR and LRU improvements via episode expansion.
