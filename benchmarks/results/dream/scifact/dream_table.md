## NCMS Dream Cycle Experiment Results

### scifact

- **Documents**: 4967
- **Episodes created**: 2438
- **Ingestion time**: 27232.1s
- **LLM model**: `openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16`
- **Total time**: 57312.7s

#### Retrieval Progression

| Stage | nDCG@10 | Δ% | MRR@10 | Recall@100 | Insights | Memories | Time |
|-------|---------|------|--------|------------|----------|----------|------|
| Baseline (Phases 1-3) | 0.6841 | — | 0.6577 | 0.8928 | 0 | 7693 | 3779.4s |
| + Episode Summaries (5A) | 0.6843 | +0.04% | 0.6579 | 0.8928 | 100 | 7793 | 4683.8s |
| + State Trajectories (5B) | 0.6843 | +0.04% | 0.6579 | 0.8928 | 161 | 7854 | 4608.4s |
| + Pattern Detection (5C) | 0.6843 | +0.04% | 0.6579 | 0.8928 | 161 | 7854 | 4508.2s |
| + Dream Cycle (1×) | 0.6843 | +0.04% | 0.6579 | 0.8928 | 161 | 7854 | 7261.2s |
| + Dream Cycle (3×) | 0.6843 | +0.04% | 0.6579 | 0.8928 | 161 | 7854 | 5239.0s |

#### Structural Diagnostics

| Stage | Entities | Edges | Density | Components | PR max | Assoc Pairs | Abstracts |
|-------|----------|-------|---------|------------|--------|-------------|-----------|
| Baseline (Phases 1-3) | 51357 | 0 | 0.0000 | 51357 | 0.0000 | 0 | 0 |
| + Episode Summaries (5A) | 51357 | 0 | 0.0000 | 51357 | 0.0000 | 0 | 100 |
| + State Trajectories (5B) | 51357 | 0 | 0.0000 | 51357 | 0.0000 | 0 | 161 |
| + Pattern Detection (5C) | 51357 | 0 | 0.0000 | 51357 | 0.0000 | 0 | 161 |
| + Dream Cycle (1×) | 51357 | 0 | 0.0000 | 51357 | 0.0000 | 0 | 161 |
| + Dream Cycle (3×) | 51357 | 0 | 0.0000 | 51357 | 0.0000 | 0 | 161 |

#### Cognitive Diagnostics

| Stage | ACT-R mean | Above thr | Import mean | Spread mean | Insight top-10 | Improved | Degraded |
|-------|------------|-----------|-------------|-------------|----------------|----------|----------|
| Baseline (Phases 1-3) | 0.437 | 7278 | 5.00 | 0.0558 | 0 | — | — |
| + Episode Summaries (5A) | 0.671 | 7382 | 5.06 | 0.0557 | 33 | 1 | 0 |
| + State Trajectories (5B) | 0.812 | 7444 | 5.09 | 0.0557 | 33 | 1 | 0 |
| + Pattern Detection (5C) | 0.944 | 7444 | 5.09 | 0.0557 | 33 | 1 | 0 |
| + Dream Cycle (1×) | 1.073 | 7444 | 5.19 | 0.0557 | 33 | 1 | 0 |
| + Dream Cycle (3×) | 1.147 | 7444 | 5.47 | 0.0557 | 33 | 1 | 0 |

#### ACT-R Crossover Sweep

| Stage | actr_0.0 | actr_0.1 | actr_0.2 | actr_0.3 | actr_0.4 | Best |
|-------|--------|--------|--------|--------|--------|------|
| Baseline (Phases 1-3) | 0.6841 | 0.6837 | 0.6836 | 0.6822 | 0.6818 | 0.0 |
| + Episode Summaries (5A) | 0.6843 | 0.6836 | 0.6836 | 0.6820 | 0.6808 | 0.0 |
| + State Trajectories (5B) | 0.6843 | 0.6837 | 0.6837 | 0.6833 | 0.6809 | 0.0 |
| + Pattern Detection (5C) | 0.6843 | 0.6837 | 0.6837 | 0.6833 | 0.6809 | 0.0 |
| + Dream Cycle (1×) | 0.6843 | 0.6837 | 0.6837 | 0.6832 | 0.6809 | 0.0 |
| + Dream Cycle (3×) | 0.6843 | 0.6837 | 0.6837 | 0.6832 | 0.6809 | 0.0 |

