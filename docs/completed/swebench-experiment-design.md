# SWE-bench Dream Cycle Experiment Design

> **Status: COMPLETE** (experiment designed and executed). 4-competency framework (AR, TTL, CR, LRU) validated. Dream cycle showed flat retrieval gain on SciFact but established methodology. See `ncms-resilience-update.md` Section 8.4 for LongMemEval as next benchmark target.

## Motivation: Lessons from SciFact

The SciFact dream cycle experiment (March 2026) revealed a fundamental mismatch between BEIR-style IR benchmarks and NCMS's cognitive architecture:

| Metric | SciFact (BEIR) | Expected (Agent Workload) |
|--------|---------------|--------------------------|
| Unique entities | 51,357 | ~2,000-5,000 |
| Graph edges | **0** | Thousands |
| Connected components | 51,357 | < 100 |
| PageRank max | 0.0000 (uniform) | >> uniform (hub entities) |
| Spreading activation mean | 0.0558 | >> 0.1 |
| ACT-R crossover | None (best = 0.0) | Expected at 0.1-0.2 |
| Dream cycle delta | +0.04% (1 query) | Measurable improvement |

**Root cause**: BEIR documents are independent scientific abstracts. No shared entities across documents, no temporal ordering, no causal chains. The knowledge graph is completely disconnected — 51,357 isolated nodes with zero edges.

NCMS's cognitive architecture (ACT-R activation, spreading activation, episode grouping, state reconciliation, dream rehearsal) requires **relational structure** between memories: shared entities that create graph connectivity, temporal sequences that enable state tracking, and recurring patterns that consolidation can synthesize.

**Key insight**: NCMS is not a general-purpose IR system. It is a **cognitive memory system for AI agents**, where knowledge accumulates incrementally, entities recur across interactions, and state evolves over time. The benchmark must reflect this usage pattern.

## Experiment Design

### Dataset: SWE-bench Django Subset

[SWE-bench](https://www.swebench.com/) contains 2,294 real GitHub issue-to-PR pairs from 12 Python repositories. We use the Django subset (850 instances) because:

1. **Dense entity overlap**: Django class names (`QuerySet`, `Model`, `CharField`, `URLPattern`, `Middleware`), module paths (`django.db.models`, `django.contrib.auth`), and function names recur across dozens of issues
2. **Temporal ordering**: Issues span 2012-2023 with `created_at` timestamps, enabling chronological ingestion and state tracking
3. **Causal chains**: Bug reports reference prior fixes, regressions reference prior changes, features build on prior implementations
4. **Natural subsystem clustering**: Issues group by Django module (ORM, auth, forms, templates, admin, URLs, middleware)
5. **Sufficient scale**: 850 issues is large enough for meaningful IR metrics, small enough for reasonable experiment runtime

### Memory Competency Framework

Following [MemoryAgentBench](https://github.com/HUST-AI-HYZ/MemoryAgentBench) (ICLR 2026), we evaluate four memory competencies:

#### 1. Accurate Retrieval (AR)

**What**: Given a new Django issue, retrieve the most relevant past issues.

**Ground truth**: Auto-generated from patch file overlap. Two issues are relevant if their patches modify overlapping files. Graded relevance: Jaccard >= 0.5 = highly relevant (grade 2), Jaccard > 0 = relevant (grade 1).

**Split**: 80/20 chronological split — 680 train (corpus), 170 test (queries). Chronological ensures no data leakage.

**Metrics**: nDCG@10, MRR@10, Recall@100

**Hypothesis**: Graph expansion and spreading activation should significantly improve AR over BM25-only, because issues sharing code entities (same Django module) will have connected graph neighborhoods. Dream cycles should make ACT-R positive by creating differential access patterns on hub entities.

#### 2. Test-Time Learning (TTL)

**What**: After ingesting the corpus, can the system classify new issues by Django subsystem?

**Ground truth**: Each issue is labeled by primary subsystem derived from its patch file paths:
- `django/db/` → ORM
- `django/contrib/auth/` → Auth
- `django/forms/` → Forms
- `django/template/` → Templates
- `django/contrib/admin/` → Admin
- `django/urls/` → URLs
- `django/http/` → HTTP
- Other paths → Other

**Evaluation**: For each query issue, retrieve top-5 results. Majority subsystem label among retrieved issues = predicted label. Compare to ground truth.

**Metrics**: Classification accuracy

**Hypothesis**: Pattern detection (Phase 5C) should improve TTL by clustering episodes that share subsystem entities, creating searchable pattern insights that group by module.

#### 3. Long-Range Understanding (LRU)

**What**: Synthesize knowledge across many issues to answer holistic questions about Django modules.

**Queries**: 20-30 template-generated holistic questions:
- "What are the recurring problems in Django's ORM?"
- "How has Django's authentication module evolved over time?"
- "What patterns emerge across Django form validation issues?"

**Ground truth**: Entity coverage — do retrieved results (especially consolidated insights) contain entities from the queried subsystem?

**Metrics**: Entity coverage F1

**Hypothesis**: Episode summaries (Phase 5A) should dramatically improve LRU by creating searchable narrative summaries that span multiple related issues. State trajectories (5B) should add temporal evolution context.

#### 4. Conflict Resolution (CR)

**What**: When multiple issues reference the same code entity with evolving information, does the system track the current state correctly?

**Ground truth**: For entities appearing in 3+ issues across different Django versions, the most recent issue (by `created_at`) represents the current state. The system should rank it highest.

**Queries**: "What is the current behavior of {entity_name}?" for entities with temporal state evolution.

**Metrics**: Temporal MRR (MRR where the target is the most recent document for that entity)

**Hypothesis**: State trajectories (Phase 5B) should improve CR by tracking entity state evolution. Reconciliation penalties should suppress superseded states. Dream cycles may further help by rehearsing recent states more than old ones.

### Experiment Stages

Same 6-stage progression as the BEIR experiment, measuring all 4 splits at each stage:

| Stage | Phase | What happens |
|-------|-------|-------------|
| 1 | Baseline (1-3) | Ingest with admission, reconciliation, episodes |
| 2 | + Episode Summaries (5A) | LLM synthesizes closed episode summaries |
| 3 | + State Trajectories (5B) | LLM generates temporal progression narratives |
| 4 | + Pattern Detection (5C) | Cluster episodes, detect recurring patterns |
| 5 | + Dream Cycle (1x) | Search logging, dream rehearsal, importance drift |
| 6 | + Dream Cycle (3x) | Three dream cycles for stronger access patterns |

At each stage: ACT-R crossover sweep (weights 0.0 to 0.4) for the AR split.

### Expected Outcomes

| Metric | SciFact (observed) | SWE-bench Django (predicted) |
|--------|-------------------|------------------------------|
| Graph edges | 0 | 5,000-20,000 |
| Connected components | 51,357 | < 100 |
| Entity overlap mean Jaccard | ~0 | > 0.05 |
| ACT-R crossover point | None | 0.1-0.2 after dream cycles |
| Dream cycle AR delta | +0.04% | > +1% |
| Episode summary LRU delta | negligible | > +5% |
| Trajectory CR delta | negligible | > +3% |
| Pattern TTL delta | negligible | > +2% |

### Structural Validation

Before running the full experiment, a data analysis script validates the dataset suitability:

1. Entity overlap matrix (GLiNER on 100 issue sample)
2. File overlap matrix (all 850 issues)
3. Predicted graph density and connectivity
4. Subsystem distribution balance
5. Temporal distribution coverage

**Validation gate**: If entity overlap mean Jaccard < 0.05 or predicted connected components > 500, the dataset is insufficient.

## Implementation

### Files

| File | Purpose |
|------|---------|
| `benchmarks/swebench_analysis.py` | Pre-experiment structural analysis |
| `benchmarks/swebench_loader.py` | Dataset loading, filtering, corpus/query/qrel construction |
| `benchmarks/swebench_qrels.py` | Ground truth for all 4 competency splits |
| `benchmarks/swebench_queries.py` | Query generation for AR, TTL, LRU, CR |
| `benchmarks/swebench_harness.py` | Multi-split dream harness |
| `benchmarks/swebench_report.py` | Per-split metric tables and comparison reports |
| `benchmarks/swebench_configs.py` | SWE-bench-specific configuration |
| `benchmarks/run_swebench.py` | CLI entry point |
| `benchmarks/run_swebench.sh` | Shell runner with durable logging |

### Running

```bash
# Phase 1: Structural analysis (no LLM needed)
uv run python -m benchmarks.swebench_analysis

# Full experiment (requires LLM for consolidation)
./benchmarks/run_swebench.sh

# With Ollama
LLM_MODEL=ollama_chat/qwen3.5:35b-a3b LLM_API_BASE="" ./benchmarks/run_swebench.sh

# With DGX Spark
./benchmarks/run_swebench.sh  # uses defaults from env
```

## References

- [SWE-bench: Can Language Models Resolve Real-World GitHub Issues?](https://arxiv.org/abs/2310.06770)
- [MemoryAgentBench: Evaluating Memory in LLM Agents via Incremental Multi-Turn Interactions](https://arxiv.org/abs/2507.05257) (ICLR 2026)
- [NCMS SciFact Dream Cycle Results](../benchmarks/results/dream/scifact/dream_table.md)
