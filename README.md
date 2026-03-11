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

**Keyword Bridges** (opt-in) &mdash; LLM-extracted semantic keywords that connect otherwise disconnected subgraphs. "JWT validation" and "role-based access" share no entities, but both relate to the keyword "security".

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

<!-- Results table and chart will be inserted here after benchmark completes -->

*Results pending &mdash; benchmark in progress.*

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

## Roadmap

**Retrieval & Scoring**
- [x] Graph-expanded retrieval (Tier 1.5) &mdash; entity-based cross-memory discovery
- [x] GLiNER entity extraction &mdash; zero-shot NER with per-domain label customization
- [x] Keyword bridge nodes &mdash; LLM-extracted semantic concept bridges
- [x] Knowledge consolidation &mdash; entity clustering + LLM insight synthesis
- [x] SPLADE sparse neural retrieval &mdash; learned term expansion fused with BM25 via RRF
- [x] Contradiction detection &mdash; LLM-powered detection with bidirectional annotation
- [x] vLLM / local LLM support &mdash; `api_base` config for all LLM features

**Evaluation**
- [x] Retrieval pipeline ablation study &mdash; BEIR benchmarks with dataset-specific topic seeding ([design doc](docs/ablation-study-design.md))

**Ingestion**
- [ ] Directory watcher &mdash; filesystem monitor with auto-domain classification

**Knowledge Bus & Agents**
- [ ] Redis/NATS-backed transport for multi-process deployments
- [ ] NeMo Agent Toolkit `MemoryEditor`/`MemoryManager` adapter

**Infrastructure**
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
