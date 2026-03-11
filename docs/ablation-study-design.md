# NCMS Retrieval Pipeline Ablation Study

## Goal

Systematically evaluate how each retrieval pipeline component contributes to overall retrieval quality. The NCMS pipeline has five stages that can be independently enabled/disabled:

1. **BM25** (Tantivy) -- lexical retrieval baseline
2. **SPLADE** -- sparse neural retrieval fused with BM25 via RRF
3. **Graph Expansion** -- entity-based cross-memory discovery via NetworkX
4. **ACT-R Scoring** -- cognitive scoring (recency, frequency, spreading activation)
5. **LLM Judge** -- optional LLM-as-judge reranking

---

## Datasets

### Standard IR Benchmarks (via BEIR)

Use the [BEIR benchmark suite](https://github.com/beir-cellar/beir) for established baselines. Load via `ir_datasets` or the `beir` Python package.

| Dataset | Queries | Corpus | Why It Fits NCMS |
|---------|---------|--------|------------------|
| **NQ** | 3,452 | 2.68M | Factoid QA -- tests core BM25/SPLADE retrieval |
| **HotpotQA** | 7,405 | 5.23M | Multi-hop reasoning -- tests graph expansion |
| **FiQA-2018** | 648 | 57K | Domain-specific (finance) -- tests domain filtering |
| **SciFact** | 300 | 5K | Fact verification -- tests precision on small corpus |
| **DBPedia-Entity** | 400 | 4.63M | Entity retrieval -- directly tests graph/entity features |
| **NFCorpus** | 323 | 3.6K | Biomedical -- tests entity-rich content |

### Multi-Hop / Entity-Centric (for Graph Expansion)

| Dataset | Description | What It Tests |
|---------|-------------|---------------|
| **HotpotQA** (distractor) | 2-hop reasoning across Wikipedia | Whether graph expansion discovers the second hop BM25 misses |
| **MuSiQue** | 2-4 hop, anti-shortcut construction | Stress-tests graph traversal depth |
| **2WikiMultiHopQA** | Entity-bridged Wikipedia pairs | Entity linking as central to retrieval |

### Temporal / Cognitive (for ACT-R)

Standard IR datasets are static -- no access history, no temporal decay. Three approaches:

| Approach | Dataset / Method | What It Tests |
|----------|-----------------|---------------|
| **Synthetic augmentation** | Overlay Poisson access patterns on BEIR datasets | Recency/frequency decay effects in isolation |
| **Re2Bench** | Disentangles Relevance vs Recency vs Hybrid | ACT-R base-level activation alignment |
| **LoCoMo** | 300-turn dialogues over 35 sessions with temporal event graphs | Long-term agent memory with natural access patterns |
| **FiFA** | Cognitive memory benchmark with typed stores and timestamps | Forgetting policies (recency, frequency, importance) |
| **Custom NCMS scenarios** | Record access patterns from demo agent runs | Most authentic evaluation for NCMS's specific use case |

### Synthetic Access Pattern Augmentation (Recommended for ACT-R)

Take a BEIR dataset (e.g., NQ) and overlay access histories:

1. **Recency**: Assign documents creation times + simulate access events via Poisson process. Recent documents get more recent timestamps. Evaluate whether ACT-R's `ln(sum(t^-d))` formula correctly boosts recently accessed documents.
2. **Frequency**: Create "frequently accessed" (many events) vs "rarely accessed" (few events) populations with ground truth relevance. Test whether frequency component helps.
3. **Power-law**: Generate realistic access distributions (few memories accessed thousands of times, long tail accessed once).
4. **Controlled injection**: For a fixed query, inject a highly relevant document with (a) recent access, (b) stale access, (c) no access. Measure rank delta.

---

## Metrics

### Primary

| Metric | What It Measures | Use Case |
|--------|-----------------|----------|
| **nDCG@10** | Graded ranking quality | Primary metric (BEIR/TREC standard) |
| **nDCG@100** | Ranking at deeper cutoff | Whether graph expansion helps at deeper ranks |
| **MRR@10** | Position of first relevant result | Single-answer factoid queries |
| **Recall@k** (k=10,50,100) | Coverage of relevant documents | Critical for evaluating stage 1 ceiling |

### Per-Stage

| Metric | What It Measures |
|--------|-----------------|
| **Recall@k at each stage** | How coverage changes as each stage adds/filters candidates |
| **Latency (p50, p95, p99)** | Computational cost per stage |
| **Candidate count per stage** | Pipeline width at each point |

### Temporal (for ACT-R evaluation)

| Metric | Source | What It Measures |
|--------|--------|-----------------|
| **TimeVar@K** | Re2Bench | Temporal alignment tightness |
| **MFG@K** | Re2Bench | Mean Freshness Gap |

---

## Published Baselines (Target Scores)

Reference nDCG@10 scores from established retrieval systems on BEIR benchmarks.
These represent the scores NCMS's pipeline is measured against.

### SciFact (Primary Benchmark)

| Model | Type | nDCG@10 | Notes |
|-------|------|:-------:|-------|
| **BM25** | Lexical | 0.671 | Strong on scientific terminology |
| **SPLADE v2** | Learned sparse | 0.693 | Sparse neural — closest to NCMS approach |
| **ColBERT** | Late-interaction embedding | 0.693 | Expensive token-level interaction |
| **ANCE** | Dense embedding (ANN) | 0.507 | Underperforms BM25 out-of-domain |
| **TAS-B** | Dense embedding (distilled) | 0.502 | Distilled bi-encoder |
| **DPR** | Dense embedding | ~0.32 | Worst out-of-domain transfer |

**Key insight:** Dense embeddings (DPR, ANCE, TAS-B) *underperform* BM25 on SciFact.
This is the out-of-domain generalization gap that BEIR was designed to expose, and
validates NCMS's lexical-first architecture. SPLADE and ColBERT match at ~0.69.

### BEIR Aggregate (Average across 18 datasets)

| Model | Type | Avg nDCG@10 |
|-------|------|:-----------:|
| Voyage-Large-2 | Dense embedding | 0.548 |
| Cohere Embed v4 | Dense embedding | 0.537 |
| OpenAI text-3-large | Dense embedding | 0.519 |
| E5-Mistral-7B | Dense embedding (LLM) | 0.512 |
| **BM25** | Lexical | 0.412 |

**Key insight:** On aggregate, dense embeddings win (~0.52–0.55 vs 0.41 for BM25).
But per-dataset variance is high — BM25 is competitive or better on domain-specific
corpora (SciFact, NFCorpus) where exact term matching matters.

### NCMS Target Range

| NCMS Config | Expected nDCG@10 | Rationale |
|-------------|:-----------------:|-----------|
| BM25 only | 0.65–0.68 | Should match published BM25 baseline |
| + SPLADE | 0.68–0.71 | RRF fusion with sparse neural |
| + Graph + ACT-R | 0.68–0.72 | Entity expansion + cognitive rescoring |
| Full + LLM Judge | 0.70–0.75 | LLM reranking of top candidates |

### Sources

- [BEIR: A Heterogeneous Benchmark for Zero-shot Evaluation of IR Models (NeurIPS 2021)](https://arxiv.org/abs/2104.08663)
- [BEIR GitHub Repository — Official Baselines](https://github.com/beir-cellar/beir)
- [BEIR SciFact Benchmark — Model Comparison](https://www.emergentmind.com/topics/beir-scifact-benchmark)
- [BEIR 2.0 Leaderboard — Aggregate Scores](https://app.ailog.fr/en/blog/news/beir-benchmark-update)

---

## Ablation Configurations

### Subtractive (remove one component at a time)

| Config | BM25 | SPLADE | Graph | ACT-R | Judge | What It Isolates |
|--------|------|--------|-------|-------|-------|------------------|
| **Full** | Y | Y | Y | Y | Y | Upper bound |
| **-Judge** | Y | Y | Y | Y | N | Marginal value of LLM reranking |
| **-ACT-R** | Y | Y | Y | N | Y | Value of cognitive scoring |
| **-Graph** | Y | Y | N | Y | Y | Value of entity-based discovery |
| **-SPLADE** | Y | N | N | Y | Y | Value of sparse neural retrieval |
| **BM25 only** | Y | N | N | N | N | Lower bound baseline |

### Additive (build up from baseline)

| Config | Components | What It Shows |
|--------|-----------|---------------|
| **BM25** | BM25 only | Baseline |
| **+SPLADE** | BM25 + SPLADE + RRF | SPLADE lift |
| **+Graph** | BM25 + SPLADE + Graph | Graph expansion lift |
| **+ACT-R** | BM25 + SPLADE + Graph + ACT-R | Cognitive scoring lift |
| **+Judge** | Full pipeline | LLM judge lift |

### ACT-R Sub-Ablation

| Config | Base-Level | Spreading | Noise | What It Tests |
|--------|-----------|-----------|-------|---------------|
| **Full ACT-R** | Y | Y | Y | Full cognitive model |
| **-Spreading** | Y | N | Y | Value of entity-context overlap |
| **-Base Level** | N | Y | Y | Value of recency/frequency decay |
| **Spreading only** | N | Y | N | Pure graph-based activation |
| **Base only** | Y | N | N | Pure temporal decay |

### How to Toggle Each Component

All toggles map directly to existing NCMS config settings:

```python
# BM25 is always on (core)
config.splade_enabled = False              # -SPLADE
config.scoring_weight_splade = 0.0         # -SPLADE scoring
config.graph_expansion_enabled = False     # -Graph
config.scoring_weight_actr = 0.0           # -ACT-R
config.llm_judge_enabled = False           # -Judge
config.actr_max_spread = 0.0              # -Spreading activation
```

---

## Evaluation Harness Design

### Data Flow

```
BEIR Dataset
    |
    v
Seed domain-specific topics via `ncms topics set`
(align GLiNER labels with dataset entity types)
    |
    v
Load corpus into NCMS MemoryService (SQLite + Tantivy + NetworkX)
    |
    v
(Optional) Inject synthetic access patterns for ACT-R eval
    |
    v
For each ablation config:
    For each query:
        results = memory_service.search(query, limit=100)
        record ranked memory IDs + scores
    |
    v
Compute metrics against ground truth qrels
    |
    v
Output: table of (config x dataset x metric)
```

### Topic Seeding per Dataset

Before ingestion, set domain-specific entity labels matching each dataset's
entity types. This ensures GLiNER extracts entities relevant to the domain
rather than relying on universal fallback labels:

| Dataset | Domain | Suggested Topics |
|---------|--------|-----------------|
| **SciFact** | `science` | `claim, evidence, study, method, result, finding` |
| **NFCorpus** | `biomedical` | `disease, drug, protein, gene, symptom, treatment` |
| **FiQA** | `finance` | `company, stock, market, fund, indicator, regulation` |
| **DBPedia-Entity** | `encyclopedia` | `person, organization, location, event, concept, product` |
| **NQ / HotpotQA** | `general` | *(use UNIVERSAL_LABELS -- no seeding needed)* |
| **TREC-COVID** | `epidemiology` | `virus, vaccine, transmission, treatment, population, study` |
| **Touche-2020** | `argument` | `claim, premise, stance, topic, evidence, source` |

Seeding is done via the CLI or programmatically:
```bash
ncms topics set biomedical disease drug protein gene symptom treatment --db :memory:
```

Or programmatically in the harness:
```python
await store.set_consolidation_value(
    "entity_labels:biomedical",
    json.dumps(["disease", "drug", "protein", "gene", "symptom", "treatment"]),
)
```

### Key Implementation Notes

- Use in-memory backends (`:memory:` SQLite, ephemeral Tantivy) for speed
- **Topic seeding must occur BEFORE corpus ingestion** -- labels determine which entities GLiNER extracts
- Entity extraction runs at ingest time -- graph structure depends on content and labels
- Access patterns must be injected AFTER ingest but BEFORE search queries
- Each configuration requires a fresh service instance (configs affect ingest too)
- Track wall-clock time per stage via pipeline debug events

### Reporting

For each dataset, produce:
1. **Summary table**: rows = configs, columns = nDCG@10, MRR@10, Recall@100, latency
2. **Stage-level recall curve**: how Recall@k changes at each pipeline stage
3. **Statistical significance**: paired t-test or bootstrap confidence intervals across queries
4. **Efficiency plot**: nDCG@10 vs. latency scatter for each config
5. **ACT-R sensitivity**: sweep `actr_decay` (0.1-1.0) and `actr_max_spread` (0.0-2.0)

---

## Open Questions

1. **Corpus size**: BEIR corpora range from 5K to 5M docs. Should we subsample large corpora or run full-scale?
2. **Topic label selection**: Domain-specific labels vs universal labels will produce different graph structures. Should we ablate label strategies (universal-only vs dataset-specific topics)?
3. **Access pattern realism**: What distribution best models real agent access? Poisson? Power-law? Bursty?
4. **Cross-dataset aggregation**: Report per-dataset or aggregate (BEIR-style normalized)?
5. **Budget**: LLM Judge requires API calls. How many queries x candidates are feasible?
