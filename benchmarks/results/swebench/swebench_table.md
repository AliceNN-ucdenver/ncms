## NCMS SWE-bench Dream Cycle Experiment Results

- **Documents**: 503
- **Episodes created**: 114
- **Ingestion time**: 961.5s
- **LLM model**: `openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16`
- **Total time**: 34455.5s

#### Multi-Split Retrieval Progression

| Stage | AR nDCG@10 | AR Δ% | TTL Acc | CR tMRR | LRU nDCG@10 | Time |
|-------|-----------|-------|---------|---------|-------------|------|
| Baseline (Phases 1-3) | 0.1759 | — | 0.6529 | 0.0947 | 0.3523 | 2229s |
| + Episode Summaries (5A) | 0.1752 | -0.38% | 0.6471 | 0.0944 | 0.3496 | 3238s |
| + State Trajectories (5B) | 0.1753 | -0.32% | 0.6471 | 0.0948 | 0.3488 | 2823s |
| + Pattern Detection (5C) | 0.1753 | -0.32% | 0.6471 | 0.0948 | 0.3488 | 3191s |
| + Dream Cycle (1×) | 0.1774 | +0.86% | 0.6471 | 0.0947 | 0.3488 | 12895s |
| + Dream Cycle (3×) | 0.1754 | -0.28% | 0.6471 | 0.0907 | 0.3488 | 9114s |

#### Graph Connectivity Progression

| Stage | Entities | Edges | Density | Components | Largest | Degree mean | PR max |
|-------|----------|-------|---------|------------|---------|-------------|--------|
| Baseline (Phases 1-3) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.027455 |
| + Episode Summaries (5A) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.027455 |
| + State Trajectories (5B) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.027455 |
| + Pattern Detection (5C) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.027455 |
| + Dream Cycle (1×) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.060006 |
| + Dream Cycle (3×) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.098943 |

#### ACT-R Crossover Sweep (AR Split)

| Stage | actr_0.0 | actr_0.1 | actr_0.2 | actr_0.3 | actr_0.4 | Best |
|-------|---------|---------|---------|---------|---------|------|
| Baseline (Phases 1-3) | 0.1759 | 0.1472 | 0.1248 | 0.1112 | 0.1051 | 0.0 |
| + Episode Summaries (5A) | 0.1752 | 0.1458 | 0.1210 | 0.1063 | 0.1011 | 0.0 |
| + State Trajectories (5B) | 0.1753 | 0.1466 | 0.1225 | 0.1077 | 0.1008 | 0.0 |
| + Pattern Detection (5C) | 0.1753 | 0.1466 | 0.1225 | 0.1082 | 0.1015 | 0.0 |
| + Dream Cycle (1×) | 0.1774 | 0.1447 | 0.1195 | 0.1034 | 0.0950 | 0.0 |
| + Dream Cycle (3×) | 0.1754 | 0.1422 | 0.1172 | 0.1022 | 0.0948 | 0.0 |

#### SciFact vs SWE-bench Django Comparison

| Metric | SciFact (BEIR) | SWE-bench Django |
|--------|---------------|------------------|
| Entities | 51,357 | 3,396 |
| Edges | 0 | 45,926 |
| Components | 51,357 | 829 |
| Density | 0.0000 | 0.003983 |
| Dream cycle AR Δ | +0.04% | +-0.28% |