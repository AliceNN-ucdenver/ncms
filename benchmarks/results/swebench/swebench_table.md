## NCMS SWE-bench Dream Cycle Experiment Results

- **Documents**: 503
- **Episodes created**: 114
- **Ingestion time**: 553.7s
- **LLM model**: `openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16`
- **Total time**: 24140.9s

#### Multi-Split Retrieval Progression

| Stage | AR nDCG@10 | AR Δ% | TTL Acc | CR tMRR | LRU nDCG@10 | Time |
|-------|-----------|-------|---------|---------|-------------|------|
| Baseline (Phases 1-3) | 0.1534 | — | 0.5706 | 0.0815 | 0.4842 | 2317s |
| + Episode Summaries (5A) | 0.1532 | -0.14% | 0.5765 | 0.0827 | 0.4532 | 3068s |
| + State Trajectories (5B) | 0.1523 | -0.73% | 0.5765 | 0.0862 | 0.4531 | 2515s |
| + Pattern Detection (5C) | 0.1523 | -0.73% | 0.5765 | 0.0862 | 0.4531 | 2463s |
| + Dream Cycle (1×) | 0.1523 | -0.73% | 0.5765 | 0.0862 | 0.4531 | 7295s |
| + Dream Cycle (3×) | 0.1523 | -0.73% | 0.5765 | 0.0862 | 0.4531 | 5930s |

#### Graph Connectivity Progression

| Stage | Entities | Edges | Density | Components | Largest | Degree mean | PR max |
|-------|----------|-------|---------|------------|---------|-------------|--------|
| Baseline (Phases 1-3) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.003493 |
| + Episode Summaries (5A) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.003493 |
| + State Trajectories (5B) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.003493 |
| + Pattern Detection (5C) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.003493 |
| + Dream Cycle (1×) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.003493 |
| + Dream Cycle (3×) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.003493 |

#### ACT-R Crossover Sweep (AR Split)

| Stage | actr_0.0 | actr_0.1 | actr_0.2 | actr_0.3 | actr_0.4 | Best |
|-------|---------|---------|---------|---------|---------|------|
| Baseline (Phases 1-3) | 0.1534 | 0.1535 | 0.1537 | 0.1521 | 0.1512 | 0.2 |
| + Episode Summaries (5A) | 0.1532 | 0.1521 | 0.1519 | 0.1519 | 0.1516 | 0.0 |
| + State Trajectories (5B) | 0.1523 | 0.1523 | 0.1510 | 0.1513 | 0.1513 | 0.0 |
| + Pattern Detection (5C) | 0.1523 | 0.1523 | 0.1510 | 0.1515 | 0.1513 | 0.0 |
| + Dream Cycle (1×) | 0.1523 | 0.1518 | 0.1510 | 0.1515 | 0.1512 | 0.0 |
| + Dream Cycle (3×) | 0.1523 | 0.1518 | 0.1510 | 0.1515 | 0.1512 | 0.0 |

#### SciFact vs SWE-bench Django Comparison

| Metric | SciFact (BEIR) | SWE-bench Django |
|--------|---------------|------------------|
| Entities | 51,357 | 3,396 |
| Edges | 0 | 45,926 |
| Components | 51,357 | 829 |
| Density | 0.0000 | 0.003983 |
| Dream cycle AR Δ | +0.04% | +-0.73% |