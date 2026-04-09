# LongMemEval Benchmark

## What It Tests

Evaluates NCMS on long-term interactive memory for user-assistant conversations. Each question has its own haystack (set of sessions to ingest), creating a fresh NCMS instance per question. This tests the system's ability to retrieve relevant information from extended multi-session dialogues.

Primary metric: Recall@5 (to compare against MemPalace's reported 96.6%).

## Dataset

| Component    | Size           | Source |
|-------------|---------------|--------|
| Questions    | ~500          | LongMemEval |
| Sessions     | Variable per question | User-assistant dialogues |
| Categories   | temporal-reasoning, knowledge-update, preference, action-reasoning, etc. | 5+ question types |

Source: [LongMemEval](https://huggingface.co/datasets/xiaowu0162/LongMemEval)
Paper: "LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory"

## GLiNER Topic Labels (Replace Mode)

**Domain**: `assistant`

Labels (10):
`person`, `location`, `date`, `product`, `event`, `organization`, `task`, `preference`, `vehicle`, `appointment`

Rationale: Content covers daily life assistant tasks -- car maintenance, organizing, travel planning, recommendations, scheduling. Labels target the entity types that distinguish relevant turns from noise in personal assistant conversations. "vehicle" and "appointment" capture common assistant task domains that generic labels miss.

## How to Run

```bash
# Full benchmark (oracle haystack)
uv run python -m benchmarks.longmemeval.run_longmemeval

# Quick test (subset)
uv run python -m benchmarks.longmemeval.run_longmemeval --test

# Specific dataset variant
uv run python -m benchmarks.longmemeval.run_longmemeval --dataset longmemeval_s_cleaned.json

# Verbose logging
uv run python -m benchmarks.longmemeval.run_longmemeval --test --verbose
```

## Expected Metrics

| Metric    | NCMS (oracle) |
|-----------|---------------|
| Recall@5  | ~0.55         |
| Contains  | ~0.60         |
| F1        | ~0.30         |

## Reference Scores

- **MemPalace**: Recall@5 = 96.6% (oracle haystack, GPT-4 reranking)
- **Kumiho**: Recall@5 ~0.85 (with LLM-based memory retrieval)
- **Baseline RAG**: Recall@5 ~0.40

Note: MemPalace and Kumiho use LLM-based retrieval/reranking at query time. NCMS uses zero-LLM retrieval (BM25 + SPLADE + graph), so lower Recall@5 is expected but query latency is orders of magnitude lower.
