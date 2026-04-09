# MemoryAgentBench Benchmark

## What It Tests

Evaluates NCMS across 4 memory competencies defined by the MemoryAgentBench benchmark:

- **AR (Accurate Retrieval)** -- Standard retrieval with relevance judgments (nDCG@10)
- **TTL (Test-Time Learning)** -- Classification from retrieved context (accuracy)
- **LRU (Long-Range Understanding)** -- Cross-topic connection queries (nDCG@10)
- **SF (Selective Forgetting)** -- Outdated/superseded memory filtering (forgetting accuracy, temporal MRR)

Runs with phases 1-3 enabled (admission, reconciliation, episodes) and intent classification.

## Dataset

| Split | Content Type        | Evaluation           |
|-------|--------------------|-----------------------|
| AR    | Scientific documents | IR metrics (nDCG@10) |
| TTL   | Movie recommendation dialogues | Classification accuracy |
| LRU   | Literature passages  | Cross-topic IR metrics |
| SF    | Factual knowledge (versioned) | Forgetting accuracy |

Source: [MemoryAgentBench](https://huggingface.co/datasets/ai-hyz/MemoryAgentBench)

## GLiNER Topic Labels (Replace Mode)

**Domain**: `mab`

Labels (10):
`person`, `organization`, `location`, `concept`, `event`, `work_title`, `scientific_term`, `date`, `technology`, `topic`

Rationale: The dataset spans scientific documents (AR), movie dialogues (TTL), literature passages (LRU), and factual knowledge (CR/SF). Labels balance coverage across these diverse content types without over-specializing on any single split. "work_title" covers movies and literary works in TTL/LRU splits, while "scientific_term" covers AR's scientific content.

## How to Run

```bash
# Full benchmark (all 4 competencies)
uv run python -m benchmarks.memoryagentbench.run_mab

# Specific competencies only
uv run python -m benchmarks.memoryagentbench.run_mab --competencies ar,ttl

# Quick test mode
uv run python -m benchmarks.memoryagentbench.run_mab --test

# Custom cache directory
uv run python -m benchmarks.memoryagentbench.run_mab --cache-dir /tmp/mab
```

## Expected Metrics

| Competency | Metric              | NCMS     |
|-----------|---------------------|----------|
| AR        | nDCG@10             | ~0.35    |
| TTL       | accuracy            | ~0.30    |
| LRU       | nDCG@10             | ~0.20    |
| SF        | forgetting_accuracy | ~0.50    |
| SF        | temporal_mrr        | ~0.40    |

## Reference Scores

- **MAGMA**: AR nDCG@10 ~0.45, TTL acc ~0.40, LRU nDCG@10 ~0.25, SF ~0.60
- **Kumiho**: Not evaluated on MemoryAgentBench
- **MemPalace**: Not evaluated on MemoryAgentBench
