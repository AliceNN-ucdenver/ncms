# Dream Cycle / Consolidation Benchmark

## What It Tests

Measures whether LLM-generated abstract memories (episode summaries, state trajectories, recurring patterns) improve retrieval quality. Runs incremental consolidation stages on BEIR corpora and tracks nDCG@10 delta at each checkpoint.

Key hypothesis: dream cycle rehearsal creates differential access patterns that make ACT-R scoring beneficial (ACT-R weight defaults to 0.0 because it hurts on cold corpora).

## Datasets

Same BEIR datasets as the retrieval ablation benchmark (SciFact, NFCorpus, ArguAna) but ingested with phases 1-3 enabled (admission, reconciliation, episodes).

## GLiNER Topic Labels

Inherits labels from the BEIR benchmark via `DATASET_TOPICS`. See `benchmarks/beir/README.md` for per-dataset labels.

## How to Run

```bash
# Install benchmark deps
uv sync --group bench

# Sequential (all datasets) -- requires LLM endpoint
./benchmarks/run_dream.sh

# Single dataset
./benchmarks/run_dream.sh scifact

# Parallel execution
./benchmarks/run_dream_parallel.sh

# Smoke test (13 docs, ~2-3 min)
./benchmarks/run_dream_test13.sh

# Override LLM endpoint
LLM_MODEL=ollama_chat/qwen3.5:35b-a3b LLM_API_BASE="" ./benchmarks/run_dream.sh scifact

# Direct Python
uv run python -m benchmarks.dream.run_dream --datasets scifact \
    --llm-model openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
    --llm-api-base http://spark-ee7d.local:8000/v1
```

## Consolidation Stages

1. **Baseline** -- No consolidation, phases 1-3 only
2. **Episode summaries** (5A) -- LLM synthesizes closed episodes
3. **State trajectories** (5B) -- Temporal progression narratives
4. **Recurring patterns** (5C) -- Cross-episode pattern detection
5. **Dream cycle** (8) -- Rehearsal + PMI associations + importance drift

## Expected Metrics

Each stage produces an ACT-R crossover sweep showing optimal weight. Dream cycles should shift the optimal ACT-R weight from 0.0 to >0.0, confirming that rehearsal creates useful access patterns.
