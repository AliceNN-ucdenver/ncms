# NCMS Benchmark Suite

Comprehensive evaluation suite for the NeMo Cognitive Memory System. Each benchmark tests different aspects of the retrieval pipeline across diverse domains.

## Benchmark Overview

| Benchmark | Domain | What It Tests | Primary Metric |
|-----------|--------|--------------|----------------|
| [BEIR](beir/) | Science, Biomedical, Arguments | Retrieval ablation study (component contribution) | nDCG@10 |
| [Dream](dream/) | Same as BEIR | LLM consolidation impact on retrieval | nDCG@10 delta |
| [SWE-bench](swebench/) | Django issues | 4-competency dream cycle (AR/TTL/LRU/CR) | nDCG@10, accuracy, temporal MRR |
| [LoCoMo](locomo/) | Personal conversations | Long-context conversational memory QA | Recall@5 |
| [LongMemEval](longmemeval/) | User-assistant dialogues | Long-term interactive memory | Recall@5 |
| [MemoryAgentBench](memoryagentbench/) | Mixed (science, movies, literature, facts) | 4-competency memory evaluation (AR/TTL/LRU/SF) | nDCG@10, accuracy |
| [Hub Replay](hub_replay/) | Software architecture | Operational health (latency, entity quality) | Ingest/search latency |

## GLiNER Topic Label Strategy

All benchmarks use domain-specific GLiNER labels in **replace mode** (domain labels replace universal labels, not merge). This keeps the label set small (~10 per dataset) for optimal entity extraction quality.

Key finding from taxonomy testing:
- 10 domain-specific labels: 9.1 entities/doc, 181 unique entities
- 20 generic labels: degraded extraction quality

Labels are defined in `benchmarks/core/datasets.py` and seeded into the NCMS store before ingestion via `set_consolidation_value("entity_labels:{domain}", ...)`.

## Quick Start

```bash
# Install benchmark dependencies
uv sync --group bench

# Run BEIR ablation (fastest, no LLM needed)
./benchmarks/run.sh scifact

# Run all BEIR datasets in parallel
./benchmarks/run_parallel.sh

# Run dream cycle experiment (requires LLM endpoint)
./benchmarks/run_dream.sh scifact

# Run LoCoMo conversational memory benchmark
uv run python -m benchmarks.locomo.run_locomo --test

# Run LongMemEval
uv run python -m benchmarks.longmemeval.run_longmemeval --test

# Run MemoryAgentBench
uv run python -m benchmarks.memoryagentbench.run_mab --test

# Run Hub Replay operational benchmark
uv run python -m benchmarks.hub_replay.run_hub_replay

# Run SWE-bench dream experiment (requires LLM endpoint)
uv run python -m benchmarks.swebench.run_swebench \
    --llm-model openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
    --llm-api-base http://spark-ee7d.local:8000/v1
```

## Architecture

```
benchmarks/
├── core/              # Shared infrastructure
│   ├── configs.py     # Ablation configurations
│   ├── datasets.py    # Dataset loading + topic label definitions
│   ├── metrics.py     # IR metrics (nDCG, MRR, Recall)
│   ├── qa_metrics.py  # QA metrics (F1, Contains, LLM judge)
│   └── runner.py      # Common CLI runner utilities
├── beir/              # BEIR retrieval ablation
├── dream/             # Dream cycle / consolidation
├── swebench/          # SWE-bench Django multi-split
├── locomo/            # LoCoMo conversational memory
├── longmemeval/       # LongMemEval long-term memory
├── memoryagentbench/  # MemoryAgentBench 4-competency
├── hub_replay/        # Hub replay operational health
├── tuning/            # Hyperparameter tuning utilities
└── results/           # Output directory (gitignored)
```

## 4-Competency Evaluation Framework

Multiple benchmarks share a common 4-competency framework for evaluating memory systems:

| Competency | Abbrev | What It Measures | Key Metric |
|------------|--------|-----------------|------------|
| Associative Recall | AR | Can the system find relevant memories? | nDCG@10 |
| Time-To-Live | TTL | Does knowledge expire correctly? | Accuracy |
| Change Resolution | CR | Are superseded facts demoted? | Temporal MRR |
| Least Recently Used | LRU | Do access patterns affect retrieval? | nDCG@10 |

SWE-bench and MemoryAgentBench both implement this framework. MemoryAgentBench adds a 5th competency: **Selective Forgetting** (SF) — can the system actively forget irrelevant knowledge?

## Current Baseline Scores

| Benchmark | Key Metric | NCMS Score | Notes |
|-----------|-----------|------------|-------|
| SciFact (BEIR) | nDCG@10 | 0.7206 | Exceeds ColBERTv2 (+4.0%), SPLADE++ (+1.5%) |
| SWE-bench Django | AR nDCG@10 | 0.2032 (recall) | +15.5% over search-only |
| LongMemEval | Recall@5 | 0.4680 | 500 questions, 6 categories |

## Results

Benchmark results are written to `benchmarks/results/` with timestamped JSON files and `_latest` symlinks for convenience. Monitor running benchmarks with:

```bash
tail -f benchmarks/results/ablation_latest.log          # BEIR
tail -f benchmarks/results/dream/dream_latest.log       # Dream
tail -f benchmarks/results/*/ablation_latest.log        # Parallel runs
```

## Lint

```bash
uv run ruff check benchmarks/ --exclude benchmarks/results
```
