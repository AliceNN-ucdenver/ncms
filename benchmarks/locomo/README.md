# LoCoMo Benchmark

## What It Tests

Evaluates NCMS retrieval on long-context conversational memory. Each conversation is replayed into NCMS as individual turn memories, then QA questions test whether the system can retrieve relevant turns to answer questions about past conversations.

Two evaluation modes:
- **LoCoMo base** -- Standard QA retrieval across 5 question categories (single-hop, multi-hop, temporal, open-domain, knowledge-graph)
- **LoCoMo-Plus** -- 401 cognitive reasoning questions (causal, goal, state, value) requiring deeper understanding of conversation context

## Dataset

| Component       | Size                | Source |
|----------------|---------------------|--------|
| Conversations   | 10 conversations    | LoCoMo-10 |
| Turns           | ~600 turns total    | Multi-session dialogues |
| QA questions    | ~200 base + 401 Plus | Snap Research |

Source: [LoCoMo](https://github.com/snap-research/locomo)
Paper: "LoCoMo: Long Context Conversational Memory Benchmark"

## GLiNER Topic Labels (Replace Mode)

**Domain**: `personal`

Labels (10):
`person`, `location`, `event`, `hobby`, `food`, `organization`, `health_condition`, `date`, `travel_destination`, `occupation`

Rationale: Conversations between friends cover daily life topics -- hobbies, relationships, health, work, events, travel, food, education. Labels target entities that naturally appear in informal dialogue rather than technical or scientific content.

## How to Run

```bash
# Full benchmark
uv run python -m benchmarks.locomo.run_locomo

# Quick test (subset of conversations)
uv run python -m benchmarks.locomo.run_locomo --test

# With verbose logging
uv run python -m benchmarks.locomo.run_locomo --verbose

# With LLM judge scoring (LoCoMo-Plus)
uv run python -m benchmarks.locomo.run_locomo --plus \
    --llm-model openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
    --llm-api-base http://spark-ee7d.local:8000/v1
```

## Expected Metrics

| Metric       | LoCoMo Base | LoCoMo-Plus |
|-------------|-------------|-------------|
| Recall@5    | ~0.45       | ~0.40       |
| Contains    | ~0.50       | ~0.45       |
| F1          | ~0.25       | ~0.20       |

## Reference Scores

- **MAGMA** (Kumiho paper): Recall@5 ~0.67 on LoCoMo-10
- **Kumiho**: Recall@5 ~0.72 on LoCoMo-10
- **MemPalace**: Not evaluated on LoCoMo
