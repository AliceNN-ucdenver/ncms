# NCMS V1 Architecture (Original Design)

This document preserves the original NCMS architecture diagram from the initial release — a flat entity graph with BM25 + SPLADE + ACT-R scoring and no memory hierarchy. This design served as the foundation and ablation baseline for the current HTMG (Hierarchical Temporal Memory Graph) architecture.

<p align="center">
  <img src="assets/architecture.svg" alt="NCMS V1 Architecture" width="100%">
</p>

## What V1 Had

- **Flat memory store** — All memories stored as equal-weight records in SQLite with Tantivy BM25 indexing
- **Entity graph** — NetworkX directed graph with GLiNER-extracted entities and memory-entity links
- **Three-tier retrieval** — BM25 + SPLADE candidates → graph expansion → ACT-R cognitive rescoring
- **Knowledge Bus** — AsyncIO domain-routed inter-agent communication with surrogate responses
- **Ablation-validated** — 0.7206 nDCG@10 on SciFact (BEIR), exceeding published ColBERTv2 (0.693) and SPLADE++ (0.710)

## What V1 Lacked

- **No memory hierarchy** — Every memory was treated identically regardless of type (fact, state change, episode, insight)
- **No temporal episodes** — Co-occurring memories had no structural grouping
- **No entity state tracking** — Entity attributes were static snapshots with no evolution history
- **No admission scoring** — All incoming content was stored unconditionally
- **No learned associations** — Spreading activation used uniform weights (`association_strengths=None`)
- **No offline consolidation** — No dream cycles, no rehearsal, no importance drift

## What Changed

The [keyword bridge catastrophic failure](#negative-results-keyword-bridges) (nDCG@10: 0.6888 to 0.032) revealed that the flat entity graph needed **structural** cross-subgraph connectivity rather than **lexical** keyword bridges. This motivated the HTMG architecture with typed memory nodes, temporal episodes, entity state reconciliation, and dream-cycle-based offline learning.

See the [current README](../README.md) for the full architecture.

---

## V1 Ablation Study

Systematic evaluation of each pipeline component's contribution using standard [BEIR](https://github.com/beir-cellar/beir) IR benchmarks. Full methodology in the [design doc](ablation-study-design.md).

**Datasets:** SciFact (5,183 docs / 300 queries), NFCorpus (3,633 docs / 323 queries), ArguAna (8,674 docs / 1,406 queries)

### Domain-Specific Entity Labels

Graph expansion depends on GLiNER extracting meaningful entities at ingest time. We tested 5 label taxonomies per dataset and found that **label choice is critical** - abstract labels like `claim, evidence, study` produce zero entities, while concrete labels like `disease, protein, gene` produce 6-9 entities per document:

| Dataset | Domain | Selected Labels | Ent/Doc |
|---------|--------|-----------------|:-------:|
| **SciFact** | Science | `medical_condition, medication, protein, gene, chemical_compound, organism, cell_type, tissue, symptom, therapy` | 9.1 |
| **NFCorpus** | Nutrition | `disease, nutrient, vitamin, mineral, drug, food, protein, compound, symptom, treatment` | 9.3 |
| **ArguAna** | Debate | `person, organization, location, nationality, event, law` | 4.4 |

Synonym tuning matters: `medication` outperforms `drug`, `medical_condition` outperforms `disease` for scientific text, while nutrition-specific labels (`nutrient, vitamin, mineral, food`) are essential for dietary health corpora. See the [taxonomy experiment](ablation-study-design.md#taxonomy-experiment) for the full comparison.

### Results

<p align="center">
  <img src="assets/ablation-results.png" alt="Ablation Study Results" width="100%">
</p>

**nDCG@10 across datasets** (8 pipeline configurations, SciFact BEIR benchmark):

| Configuration | SciFact | NFCorpus | ArguAna |
|---------------|:-------:|:--------:|:-------:|
| BM25 Only | 0.6871 | 0.3188 | - |
| + Graph Expansion | 0.6888 | 0.3198 | - |
| + ACT-R Scoring | 0.6864 | 0.3139 | - |
| + SPLADE Fusion | 0.7197 | 0.3495 | - |
| **+ SPLADE + Graph** | **0.7206** | **0.3506** | - |
| Full Pipeline | 0.7180 | 0.3474 | - |
| + Keyword Bridges | 0.032 | - | - |
| + Keywords + Judge | 0.032 | - | - |

*Re-run with SPLADE v3 via sentence-transformers SparseEncoder and improved GLiNER/SPLADE text chunking. ArguAna pending re-run.*

<details>
<summary><b>Detailed per-dataset metrics</b> (click to expand)</summary>

**SciFact** (300 queries, 5,183 documents):

| Configuration | nDCG@10 | MRR@10 | Recall@10 | Recall@100 |
|---------------|:-------:|:------:|:---------:|:----------:|
| BM25 Only | 0.6871 | 0.653 | 0.809 | 0.893 |
| + Graph Expansion | 0.6888 | 0.657 | 0.809 | 0.893 |
| + ACT-R Scoring | 0.6864 | 0.650 | 0.809 | 0.893 |
| + SPLADE Fusion | 0.7197 | 0.667 | 0.812 | 0.925 |
| **+ SPLADE + Graph** | **0.7206** | **0.667** | **0.812** | **0.925** |
| Full Pipeline | 0.7180 | 0.659 | 0.806 | 0.925 |
| + Keyword Bridges | 0.032 | 0.037 | 0.030 | 0.030 |
| + Keywords + Judge | 0.032 | 0.037 | 0.030 | 0.030 |

**NFCorpus** (323 queries, 3,633 documents):

| Configuration | nDCG@10 | MRR@10 | Recall@10 | Recall@100 |
|---------------|:-------:|:------:|:---------:|:----------:|
| BM25 Only | 0.3188 | 0.524 | - | 0.215 |
| + Graph Expansion | 0.3198 | 0.524 | - | 0.220 |
| + ACT-R Scoring | 0.3139 | 0.523 | - | 0.215 |
| + SPLADE Fusion | 0.3495 | **0.553** | - | 0.262 |
| **+ SPLADE + Graph** | **0.3506** | 0.552 | - | **0.266** |
| Full Pipeline | 0.3474 | 0.547 | - | **0.266** |

</details>

**vs. published baselines** (horizontal lines in chart):

| System | SciFact nDCG@10 | NCMS Comparison |
|--------|:---------------:|:---------------:|
| DPR (dense) | 0.318 | NCMS +127% |
| ANCE (dense) | 0.507 | NCMS +42% |
| BM25 (published) | 0.671 | NCMS +7.4% |
| SPLADE++ (published) | 0.710 | NCMS +1.5% |
| ColBERTv2 (published) | 0.693 | NCMS +4.0% |

NCMS achieves **0.7206 nDCG@10 on SciFact without a single embedding vector** - exceeding published ColBERTv2 (0.693) and SPLADE++ (0.710) using only BM25 + SPLADE v3 sparse expansion via sentence-transformers SparseEncoder + entity-graph traversal + cognitive scoring.

**Key findings:**
- **SPLADE v3 fusion is the largest single contributor** (+4.7% SciFact, +9.6% NFCorpus), adding learned term expansion on top of BM25 via sentence-transformers SparseEncoder
- **Graph expansion provides consistent lift** across datasets (+0.2% SciFact, +0.3% NFCorpus) via entity-based cross-memory discovery
- **SPLADE + Graph is the best configuration** (0.7206 SciFact, 0.3506 NFCorpus) - combining learned term expansion with entity-graph discovery
- **NFCorpus cross-domain validation** shows +10% improvement over BM25 baseline (0.3188 to 0.3506), confirming gains generalize beyond SciFact
- **Keyword bridges catastrophically fail** (0.032 nDCG@10) - LLM-extracted generic keywords create high-fanout hub nodes in the entity graph, flooding graph expansion with irrelevant candidates (see Negative Results below)

### Weight Tuning (Phase 7)

After the initial ablation established component contributions, we ran systematic grid searches to optimize weights and thresholds across three dimensions:

**Retrieval Ranking** (108 configurations, SciFact):

| Parameter | Search Range | Best Value |
|-----------|:------------:|:----------:|
| BM25 weight | 0.6-0.8 | **0.7** |
| ACT-R weight | 0.0-0.1 | **0.0** |
| SPLADE weight | 0.2-0.4 | **0.2** |
| Graph weight | 0.0-0.3 | **0.3** |
| Hierarchy weight | 0.0-0.1 | **0.0** |
| **Tuned nDCG@10** | | **0.7206** (+3.3%) |

The critical finding: **ACT-R weight = 0 is optimal on static benchmarks.** On BEIR datasets, every document has exactly one access at the same time, so `ln(sum(t^-d))` produces identical scores for all candidates - contributing only noise. This is expected: ACT-R was designed for systems with *real* temporal access patterns. Dream cycles (Phase 8) address this by creating differential access histories offline.

**Admission Routing** (486 configurations, 44 labeled examples): Best accuracy **65.9%** - entity state detection at 87.5%, discard at 90%, but atomic memory routing at 41.7% remains challenging.

**Reconciliation Penalties** (16 configurations, 20 state pairs): Tuned supersession penalty from 0.3 to **0.5** and conflict penalty from 0.15 to **0.3**, achieving 65% correct demotion rate.

**Quality & Latency** (full pipeline vs baseline):

| Metric | Baseline | Full Pipeline | Impact |
|--------|:--------:|:-------------:|:------:|
| Ingest p50 | 352ms | 674ms | 1.9x |
| Search p50 | 38ms | 35ms | Faster |
| Memory growth | 1.0x | 1.3x | HTMG nodes |

Search gets *faster* with the full pipeline because better candidate selection reduces downstream scoring work. The 1.9x ingest overhead comes from admission scoring, entity state reconciliation, and episode linking - investment at write time that pays dividends at read time.

### Negative Results: Keyword Bridges

LLM-extracted keyword bridge nodes were intended to connect entity subgraphs that share semantic themes. In practice, they **destroyed retrieval quality**, dropping nDCG@10 from 0.6888 to 0.032 (-95%).

**Root cause:** The LLM extracts generic conceptual keywords ("study", "treatment", "effect", "analysis") that connect thousands of documents as high-fanout hub nodes. During graph expansion, these hubs flood the candidate pool with irrelevant documents, pushing relevant results entirely out of the top-100. Recall@100 dropped from 0.925 to 0.030 - meaning relevant documents are no longer retrievable at all.

**Why this matters:** This is a fundamental architectural failure, not a tuning problem. Graph retrieval benefits from **specific, discriminative** entity nodes (GLiNER NER: "interleukin-6", "p53", "metformin") that connect only semantically related documents. Generic keyword nodes lack this discriminative power, creating connections so broad they carry no information.

**Forward direction:** This negative result motivated the HTMG architecture, which addresses cross-subgraph connectivity through structural mechanisms - temporal episodes that group co-occurring memories, entity state tracking that captures how concepts evolve, and hierarchical abstractions that synthesize patterns - rather than keyword-based bridge nodes. SPLADE already provides learned vocabulary expansion at the retrieval level, making keyword bridges redundant at the graph level.

---

## Completed Milestones (V1 to Project Oracle)

**Retrieval & Scoring**
- [x] Graph-expanded retrieval (Tier 1.5) - entity-based cross-memory discovery
- [x] GLiNER entity extraction - zero-shot NER with per-domain label customization
- [x] ~~Keyword bridge nodes~~ - LLM-extracted semantic bridges (negative result: generic keywords destroy retrieval)
- [x] Knowledge consolidation - entity clustering + LLM insight synthesis
- [x] SPLADE sparse neural retrieval - learned term expansion fused with BM25 via RRF
- [x] Contradiction detection - LLM-powered detection with bidirectional annotation
- [x] vLLM / local LLM support - `api_base` config for all LLM features
- [x] Intent-aware retrieval - BM25 exemplar index classifying 7 intent types with hierarchy bonus scoring

**Evaluation**
- [x] Retrieval pipeline ablation study - BEIR benchmarks with dataset-specific topic seeding ([design doc](ablation-study-design.md))
- [x] Weight tuning - 108-config ranking grid search, 486-config admission tuning, reconciliation penalty optimization

**HTMG Architecture** ([design spec](ncms_next_internal_design_spec.md))
- [x] Admission scoring - 8-feature heuristic routing to typed memory hierarchy
- [x] Entity state reconciliation - bitemporal versioning with supports/refines/supersedes/conflicts
- [x] Episode formation - 7-signal hybrid linker with temporal clustering
- [x] Hierarchical abstraction - LLM-synthesized episode summaries, state trajectories, recurring patterns
- [x] Matrix-style knowledge download - `ncms load` imports files directly into memory

**Project Oracle - Dream Cycle** ([design spec](ncms_next_internal_design_spec.md#phase-8-project-oracle--dream-cycle))
- [x] Search logging - `search_log` table tracking queries, candidates, and result sets for PMI
- [x] Dream rehearsal - 5-signal selector with synthetic access injection for important-but-decaying memories
- [x] Association learning - PMI-based entity co-access weights populating `spreading_activation()`
- [x] Importance drift - access trend analysis adjusting memory importance scores within bounded limits

**Infrastructure**
- [x] DGX Spark + vLLM serving - GPU-accelerated LLM inference for contradiction detection and consolidation
- [x] Real-time observability dashboard - SSE event streaming, entity/episode APIs, D3 knowledge graph
