## NCMS SWE-bench Dream Cycle Experiment Results

- **Documents**: 503
- **Episodes created**: 114
- **Ingestion time**: 577.2s
- **LLM model**: `openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16`
- **Total time**: 14229.4s

#### Multi-Split Retrieval Progression

| Stage | AR nDCG@10 | AR Δ% | TTL Acc | CR tMRR | LRU nDCG@10 | Time |
|-------|-----------|-------|---------|---------|-------------|------|
| Baseline (Phases 1-3) | 0.1757 | — | 0.6294 | 0.0947 | 0.4866 | 507s |
| + Episode Summaries (5A) | 0.1753 | -0.22% | 0.6294 | 0.0944 | 0.4432 | 1328s |
| + State Trajectories (5B) | 0.1744 | -0.74% | 0.6294 | 0.0948 | 0.4520 | 755s |
| + Pattern Detection (5C) | 0.1744 | -0.74% | 0.6294 | 0.0948 | 0.4520 | 708s |
| + Dream Cycle (1×) | 0.1754 | -0.18% | 0.6294 | 0.0921 | 0.4531 | 5447s |
| + Dream Cycle (3×) | 0.1749 | -0.45% | 0.6176 | 0.0881 | 0.4521 | 4907s |

#### Graph Connectivity Progression

| Stage | Entities | Edges | Density | Components | Largest | Degree mean | PR max |
|-------|----------|-------|---------|------------|---------|-------------|--------|
| Baseline (Phases 1-3) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.027455 |
| + Episode Summaries (5A) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.027455 |
| + State Trajectories (5B) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.027455 |
| + Pattern Detection (5C) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.027455 |
| + Dream Cycle (1×) | 3,396 | 45,926 | 0.0040 | 829 | 2,557 | 27.05 | 0.061520 |
| + Dream Cycle (3×) | 3,396 | 45,928 | 0.0040 | 829 | 2,557 | 27.05 | 0.104570 |

#### ACT-R Crossover Sweep (AR Split)

| Stage | actr_0.0 | actr_0.1 | actr_0.2 | actr_0.3 | actr_0.4 | Best |
|-------|---------|---------|---------|---------|---------|------|
| Baseline (Phases 1-3) | 0.1757 | 0.1514 | 0.1243 | 0.1124 | 0.1046 | 0.0 |
| + Episode Summaries (5A) | 0.1753 | 0.1486 | 0.1206 | 0.1078 | 0.1017 | 0.0 |
| + State Trajectories (5B) | 0.1744 | 0.1483 | 0.1198 | 0.1094 | 0.1016 | 0.0 |
| + Pattern Detection (5C) | 0.1744 | 0.1473 | 0.1194 | 0.1092 | 0.1022 | 0.0 |
| + Dream Cycle (1×) | 0.1754 | 0.1451 | 0.1177 | 0.1035 | 0.0960 | 0.0 |
| + Dream Cycle (3×) | 0.1749 | 0.1460 | 0.1164 | 0.1016 | 0.0924 | 0.0 |

#### SciFact vs SWE-bench Django Comparison

| Metric | SciFact (BEIR) | SWE-bench Django |
|--------|---------------|------------------|
| Entities | 51,357 | 3,396 |
| Edges | 0 | 45,928 |
| Components | 51,357 | 829 |
| Density | 0.0000 | 0.003984 |
| Dream cycle AR Δ | +0.04% | +-0.45% |