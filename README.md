<p align="center">
  <img src="docs/assets/hero-banner.svg" alt="NCMS - NeMo Cognitive Memory System" width="100%">
</p>

<p align="center">
  <a href="#see-it-working">See It Working</a> &bull;
  <a href="#how-it-works">How It Works</a> &bull;
  <a href="#ablation-study">Benchmarks</a> &bull;
  <a href="docs/quickstart.md">Quickstart Guide</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/badge/vectors-none_needed-purple" alt="No Vectors">
  <img src="https://img.shields.io/badge/external_deps-zero-orange" alt="Zero External Deps">
  <img src="https://img.shields.io/badge/tests-332_passing-brightgreen" alt="332 Tests Passing">
</p>

---

**Your AI agents forget everything between sessions.** Every conversation starts from zero. Every insight, every architectural decision, every hard-won debugging breakthrough &mdash; gone.

NCMS fixes this. Permanently.

```bash
pip install ncms
```

```python
from ncms.interfaces.mcp.server import create_ncms_services, create_mcp_server

memory, bus, snapshots = await create_ncms_services()
server = create_mcp_server(memory, bus, snapshots)
```

Three lines. Your agents now have persistent, searchable, shared memory with cognitive scoring. No vector database. No embedding pipeline. No external services.

## What Makes NCMS Different

| Problem | Traditional Approach | NCMS |
|---------|---------------------|------|
| Memory retrieval | Dense vector similarity (lossy) | **BM25 + SPLADE + graph expansion + ACT-R cognitive scoring** (precise) |
| Agent coordination | Polling shared files, explicit tool calls | **Embedded Knowledge Bus** (osmotic) |
| Agent goes offline | Knowledge lost until restart | **Snapshot surrogate response** (always available) |
| Dependencies | Vector DB + graph DB + message broker | **Zero. Single `pip install`.** |
| Setup time | Hours of infrastructure | **3 seconds to first query** |

## See It Working

```bash
git clone https://github.com/AliceNN-ucdenver/ncms.git
cd ncms && uv sync
uv run ncms demo
```

Three collaborative agents run through a complete lifecycle &mdash; storing knowledge, asking questions, going offline with surrogate responses, and announcing breaking changes &mdash; all in-memory, under 10 seconds.

```bash
uv run ncms dashboard    # Real-time observability at http://localhost:8420
```

---

## How It Works

<p align="center">
  <img src="docs/assets/architecture.svg" alt="NCMS Architecture" width="100%">
</p>

### Retrieval Pipeline

Traditional memory systems compress documents into dense vectors, losing precision. NCMS uses complementary mechanisms that work together without a single embedding:

<p align="center">
  <img src="docs/assets/retrieval-pipeline.svg" alt="Retrieval Pipeline" width="100%">
</p>

**Tier 1 &mdash; BM25 + SPLADE Hybrid Search.** BM25 via Tantivy (Rust) provides exact lexical matching. SPLADE adds learned sparse neural retrieval &mdash; expanding "API specification" to also match "endpoint", "schema", "contract". Results are fused via Reciprocal Rank Fusion (RRF).

**Tier 1.5 &mdash; Graph-Expanded Discovery.** Entity relationships in the knowledge graph discover related memories that search missed lexically. A query matching "connection pooling" also finds memories about "PostgreSQL replication" &mdash; because both share the `PostgreSQL` entity in the graph.

**Tier 2 &mdash; ACT-R Cognitive Scoring.** Every memory has an activation level computed from access recency, frequency, and contextual relevance &mdash; the same math that models human memory in cognitive science.

```
activation(m) = base_level(m) + spreading_activation(m, query) + noise
base_level(m) = ln( sum( (time_since_access)^(-decay) ) )
combined(m)   = bm25 * w_bm25 + splade * w_splade + activation * w_actr
```

**Tier 3 &mdash; LLM-as-Judge Reranking** (opt-in). Top-k candidates are sent to an LLM for relevance scoring, blended with activation scores for final ranking.

### Entity Extraction & Knowledge Enrichment

Entities are automatically extracted at store-time and search-time, feeding the knowledge graph for spreading activation and graph expansion:

<p align="center">
  <img src="docs/assets/entity-extraction.svg" alt="Entity Extraction Pipeline" width="100%">
</p>

**GLiNER NER** &mdash; Zero-shot Named Entity Recognition using a 209M-parameter [DeBERTa](https://github.com/urchade/GLiNER) model. Extracts entities across any domain with per-domain label customization via `ncms topics` CLI.

**Keyword Bridges** (opt-in, [negative result](#negative-results-keyword-bridges)) &mdash; LLM-extracted semantic keywords intended to connect entity subgraphs. Ablation study showed these destroy retrieval quality by creating high-fanout hub nodes that flood graph expansion with irrelevant candidates.

**Contradiction Detection** (opt-in) &mdash; New memories are compared against existing related memories via LLM to detect factual contradictions, with bidirectional annotation so stale knowledge is surfaced during retrieval.

**Knowledge Consolidation** (opt-in) &mdash; Clusters memories by shared entities, then uses LLM synthesis to discover emergent cross-memory patterns stored as searchable insights.

### Knowledge Bus

Agents don't poll for updates. They don't call each other directly. Knowledge flows through domain-routed channels:

<p align="center">
  <img src="docs/assets/knowledge-bus.svg" alt="Knowledge Bus Architecture" width="100%">
</p>

```python
# API agent announces a change — frontend agent gets it automatically
await agent.announce_knowledge(
    event="breaking-change",
    domains=["api:user-service"],
    content="GET /users now returns role field",
    breaking=True,
)
```

**Ask/Respond** &mdash; Non-blocking queries routed by domain.
**Announce/Subscribe** &mdash; Fire-and-forget broadcasts to interested agents.
**Surrogate Response** &mdash; When agents go offline, their snapshots answer on their behalf.

<p align="center">
  <img src="docs/assets/sleep-wake-cycle.svg" alt="Sleep/Wake/Surrogate Response Cycle" width="100%">
</p>

---

## Ablation Study

Systematic evaluation of each pipeline component's contribution using standard [BEIR](https://github.com/beir-cellar/beir) IR benchmarks. Full methodology in the [design doc](docs/ablation-study-design.md).

**Datasets:** SciFact (5,183 docs / 300 queries), NFCorpus (3,633 docs / 323 queries), ArguAna (8,674 docs / 1,406 queries)

### Domain-Specific Entity Labels

Graph expansion depends on GLiNER extracting meaningful entities at ingest time. We tested 5 label taxonomies per dataset and found that **label choice is critical** &mdash; abstract labels like `claim, evidence, study` produce zero entities, while concrete labels like `disease, protein, gene` produce 6&ndash;9 entities per document:

| Dataset | Domain | Selected Labels | Ent/Doc |
|---------|--------|-----------------|:-------:|
| **SciFact** | Science | `medical_condition, medication, protein, gene, chemical_compound, organism, cell_type, tissue, symptom, therapy` | 9.1 |
| **NFCorpus** | Nutrition | `disease, nutrient, vitamin, mineral, drug, food, protein, compound, symptom, treatment` | 9.3 |
| **ArguAna** | Debate | `person, organization, location, nationality, event, law` | 4.4 |

Synonym tuning matters: `medication` outperforms `drug`, `medical_condition` outperforms `disease` for scientific text, while nutrition-specific labels (`nutrient, vitamin, mineral, food`) are essential for dietary health corpora. See the [taxonomy experiment](docs/ablation-study-design.md#taxonomy-experiment) for the full comparison.

### Results

<p align="center">
  <img src="docs/assets/ablation-results.png" alt="Ablation Study Results" width="100%">
</p>

**nDCG@10 across datasets** (8 pipeline configurations, SciFact BEIR benchmark):

| Configuration | SciFact | NFCorpus | ArguAna |
|---------------|:-------:|:--------:|:-------:|
| BM25 Only | 0.687 | 0.319 | &mdash; |
| + Graph Expansion | 0.690 | **0.321** | &mdash; |
| + ACT-R Scoring | 0.686 | 0.317 | &mdash; |
| + SPLADE Fusion | 0.697 | **0.339** | &mdash; |
| **+ SPLADE + Graph** | **0.698** | 0.338 | &mdash; |
| Full Pipeline | 0.690 | 0.337 | &mdash; |
| + Keyword Bridges | 0.032 | &mdash; | &mdash; |
| + Keywords + Judge | 0.032 | &mdash; | &mdash; |

*SciFact re-run with improved GLiNER/SPLADE text chunking. NFCorpus/ArguAna pending re-run.*

<details>
<summary><b>Detailed per-dataset metrics</b> (click to expand)</summary>

**SciFact** (300 queries, 5,183 documents):

| Configuration | nDCG@10 | MRR@10 | Recall@10 | Recall@100 |
|---------------|:-------:|:------:|:---------:|:----------:|
| BM25 Only | 0.687 | 0.653 | 0.809 | 0.893 |
| + Graph Expansion | 0.690 | 0.657 | 0.809 | 0.893 |
| + ACT-R Scoring | 0.686 | 0.650 | 0.809 | 0.893 |
| + SPLADE Fusion | 0.697 | 0.667 | 0.812 | 0.925 |
| **+ SPLADE + Graph** | **0.698** | **0.667** | **0.812** | **0.925** |
| Full Pipeline | 0.690 | 0.659 | 0.806 | 0.925 |
| + Keyword Bridges | 0.032 | 0.037 | 0.030 | 0.030 |
| + Keywords + Judge | 0.032 | 0.037 | 0.030 | 0.030 |

**NFCorpus** (323 queries, 3,633 documents):

| Configuration | nDCG@10 | MRR@10 | Recall@10 | Recall@100 |
|---------------|:-------:|:------:|:---------:|:----------:|
| BM25 Only | 0.319 | 0.524 | &mdash; | 0.215 |
| + Graph Expansion | 0.321 | 0.524 | &mdash; | 0.220 |
| + ACT-R Scoring | 0.317 | 0.523 | &mdash; | 0.215 |
| + SPLADE Fusion | **0.339** | **0.553** | &mdash; | 0.262 |
| + SPLADE + Graph | 0.338 | 0.552 | &mdash; | **0.266** |
| Full Pipeline | 0.337 | 0.547 | &mdash; | **0.266** |

</details>

**vs. published baselines** (horizontal lines in chart):

| System | SciFact nDCG@10 | NCMS Comparison |
|--------|:---------------:|:---------------:|
| DPR (dense) | 0.318 | NCMS +120% |
| ANCE (dense) | 0.507 | NCMS +38% |
| BM25 (published) | 0.671 | NCMS +4.0% |
| SPLADE v2 / ColBERT v2 | 0.693 | NCMS +0.7% |

NCMS achieves **0.698 nDCG@10 on SciFact without a single embedding vector** &mdash; outperforming published dense and sparse neural retrieval baselines using only BM25 + SPLADE sparse expansion + entity-graph traversal + cognitive scoring.

**Key findings:**
- **SPLADE fusion is the largest single contributor** (+1.5% SciFact, +6.2% NFCorpus), adding learned term expansion on top of BM25
- **Graph expansion provides consistent lift** across datasets (+0.4% SciFact, +0.6% NFCorpus) via entity-based cross-memory discovery
- **SPLADE + Graph is the best configuration** (0.698 SciFact) &mdash; combining learned term expansion with entity-graph discovery
- **Keyword bridges catastrophically fail** (0.032 nDCG@10) &mdash; LLM-extracted generic keywords create high-fanout hub nodes in the entity graph, flooding graph expansion with irrelevant candidates (see Negative Results below)

### Negative Results: Keyword Bridges

LLM-extracted keyword bridge nodes were intended to connect entity subgraphs that share semantic themes. In practice, they **destroyed retrieval quality**, dropping nDCG@10 from 0.690 to 0.032 (&minus;95%).

**Root cause:** The LLM extracts generic conceptual keywords ("study", "treatment", "effect", "analysis") that connect thousands of documents as high-fanout hub nodes. During graph expansion, these hubs flood the candidate pool with irrelevant documents, pushing relevant results entirely out of the top-100. Recall@100 dropped from 0.925 to 0.030 &mdash; meaning relevant documents are no longer retrievable at all.

**Why this matters:** This is a fundamental architectural failure, not a tuning problem. Graph retrieval benefits from **specific, discriminative** entity nodes (GLiNER NER: "interleukin-6", "p53", "metformin") that connect only semantically related documents. Generic keyword nodes lack this discriminative power, creating connections so broad they carry no information.

**Forward direction:** This negative result motivates the [HTMG architecture](docs/ncms_next_internal_design_spec.md) (Hierarchical Temporal Memory Graph), which addresses cross-subgraph connectivity through structural mechanisms &mdash; temporal episodes that group co-occurring memories, entity state tracking that captures how concepts evolve, and hierarchical abstractions that synthesize patterns &mdash; rather than keyword-based bridge nodes. SPLADE already provides learned vocabulary expansion at the retrieval level, making keyword bridges redundant at the graph level.

### Research Direction: Project Oracle

<p align="center">
  <img src="docs/assets/project-oracle.svg" alt="Project Oracle — Dream Cycle Architecture" width="100%">
</p>

The keyword bridge failure and ACT-R underperformance on static benchmarks point to a deeper insight: **ACT-R's spreading activation has the right mechanism but no learned weights.** The `association_strengths` parameter in `spreading_activation()` exists but is always `None` &mdash; every entity pair contributes equally, regardless of actual co-relevance.

**Project Oracle** is a non-LLM offline consolidation system that teaches ACT-R through its own mechanism:

- **Dream Rehearsal** &mdash; Nightly pass injects synthetic access records for high-importance memories showing decay, boosting their base-level activation `B_i = ln(sum(t^-d))` without changing the formula
- **Association Learning** &mdash; Computes PMI (pointwise mutual information) from entity co-access patterns in `search_log`, populating the `association_strengths` parameter so spreading activation uses learned weights instead of uniform 1.0
- **Importance Drift** &mdash; Adjusts `memory.importance` based on 30-day access trends, letting frequently-accessed memories rise and neglected ones gracefully decay

This eliminates the need for LLM keyword bridges (replaced by learned PMI weights) and LLM-as-judge reranking (replaced by dream-tuned activation scores), keeping the entire retrieval pipeline LLM-free at query time. See the [design spec](docs/ncms_next_internal_design_spec.md#phase-8-project-oracle--dream-cycle) for the full implementation plan.

---

## Get Started

```bash
pip install ncms                    # Core install
pip install "ncms[docs]"            # + rich document support (DOCX/PPTX/PDF/XLSX)
pip install "ncms[dashboard]"       # + observability dashboard
```

```bash
uv run ncms demo                    # See it in action
uv run ncms serve                   # Start MCP server
uv run ncms dashboard               # Real-time dashboard
uv run ncms load file.md --domains arch  # Matrix-style knowledge download
```

**[Quickstart Guide](docs/quickstart.md)** &mdash; MCP server setup, Claude Code hooks, NeMo agent integration, configuration reference, and local LLM inference.

## GPU-Accelerated LLM Inference

NCMS LLM features (keyword bridges, LLM-as-judge, contradiction detection, knowledge consolidation) can be accelerated with an [NVIDIA DGX Spark](https://www.nvidia.com/en-us/products/workstations/dgx-spark/) running [vLLM](https://docs.vllm.ai/) via the [NGC vLLM container](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/vllm).

**Deploy Nemotron on DGX Spark:**

```bash
docker run -d --gpus all --ipc=host --restart unless-stopped \
  -p 8000:8000 \
  -v /root/.cache/huggingface:/root/.cache/huggingface \
  nvcr.io/nvidia/vllm:26.01-py3 \
  vllm serve nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
    --host 0.0.0.0 \
    --port 8000 \
    --trust-remote-code \
    --max-model-len 32768
```

**Point NCMS at the Spark:**

```bash
# All LLM features via DGX Spark
NCMS_LLM_MODEL=openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
NCMS_LLM_API_BASE=http://spark-ee7d.local:8000/v1 \
NCMS_KEYWORD_BRIDGE_ENABLED=true \
NCMS_KEYWORD_LLM_MODEL=openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
NCMS_KEYWORD_LLM_API_BASE=http://spark-ee7d.local:8000/v1 \
uv run ncms serve
```

The Nemotron 3 Nano (30B total, 3B active MoE) fits entirely in the Spark's 128GB unified memory with room to spare, delivering sub-second keyword extraction &mdash; orders of magnitude faster than CPU-based inference.

## Roadmap

**Retrieval & Scoring**
- [x] Graph-expanded retrieval (Tier 1.5) &mdash; entity-based cross-memory discovery
- [x] GLiNER entity extraction &mdash; zero-shot NER with per-domain label customization
- [x] ~~Keyword bridge nodes~~ &mdash; LLM-extracted semantic bridges ([negative result](#negative-results-keyword-bridges): generic keywords destroy retrieval)
- [x] Knowledge consolidation &mdash; entity clustering + LLM insight synthesis
- [x] SPLADE sparse neural retrieval &mdash; learned term expansion fused with BM25 via RRF
- [x] Contradiction detection &mdash; LLM-powered detection with bidirectional annotation
- [x] vLLM / local LLM support &mdash; `api_base` config for all LLM features

**Evaluation**
- [x] Retrieval pipeline ablation study &mdash; BEIR benchmarks with dataset-specific topic seeding ([design doc](docs/ablation-study-design.md))

**Next Architecture &mdash; HTMG** ([design spec](docs/ncms_next_internal_design_spec.md))
- [ ] Episode formation &mdash; temporal clustering of co-occurring memories into coherent episodes
- [ ] Entity state tracking &mdash; bitemporal versioning of entity attributes (valid-time + system-time)
- [ ] Hierarchical abstraction &mdash; LLM-synthesized higher-order patterns from episode clusters
- [ ] Temporal retrieval &mdash; time-window and "as-of" queries across the memory graph

**Project Oracle &mdash; Dream Cycle** ([design spec](docs/ncms_next_internal_design_spec.md#phase-8-project-oracle--dream-cycle))
- [ ] Search logging &mdash; `search_log` table tracking queries, candidates, and user selections
- [ ] Dream rehearsal &mdash; nightly synthetic access injection for important-but-decaying memories
- [ ] Association learning &mdash; PMI-based entity co-access weights populating `spreading_activation()`
- [ ] Importance drift &mdash; 30-day access trend analysis adjusting memory importance scores
- [ ] Oracle ablation &mdash; before/after benchmarking on temporal retrieval tasks

**Ingestion**
- [ ] Directory watcher &mdash; filesystem monitor with auto-domain classification

**Knowledge Bus & Agents**
- [ ] Redis/NATS-backed transport for multi-process deployments
- [ ] NeMo Agent Toolkit `MemoryEditor`/`MemoryManager` adapter

**Infrastructure**
- [x] DGX Spark + vLLM serving &mdash; GPU-accelerated LLM inference for keyword bridges, judge, and consolidation
- [ ] Neo4j / FalkorDB graph backend for production-scale knowledge graphs
- [ ] Docker container with Helm charts (NIM-compatible packaging)

**Dashboard**
- [ ] Historical replay and time-travel debugging
- [ ] Prometheus metrics and OpenTelemetry traces

## Acknowledgments

- **[GLiNER](https://github.com/urchade/GLiNER)** &mdash; Zero-shot NER by [Zaratiana et al. (NAACL 2024)](https://arxiv.org/abs/2311.08526)
- **[SPLADE](https://github.com/naver/splade)** &mdash; Sparse neural retrieval by [Formal et al. (SIGIR 2021)](https://arxiv.org/abs/2107.05720), powered by [fastembed](https://github.com/qdrant/fastembed)
- **[Tantivy](https://github.com/quickwit-oss/tantivy)** &mdash; Rust-based full-text search engine
- **[ACT-R](https://en.wikipedia.org/wiki/ACT-R)** &mdash; Cognitive architecture by John R. Anderson

## License

MIT

---

<p align="center">
  <strong>Built for agents that remember.</strong><br>
  <sub>By Shawn McCarthy / Chief Archeologist</sub>
</p>
