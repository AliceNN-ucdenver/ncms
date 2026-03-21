<p align="center">
  <img src="docs/assets/hero-banner.svg" alt="NCMS - NeMo Cognitive Memory System" width="100%">
</p>

<p align="center">
  <a href="#see-it-working">See It Working</a> &bull;
  <a href="#how-it-works">How It Works</a> &bull;
  <a href="#benchmarks">Benchmarks</a> &bull;
  <a href="docs/quickstart.md">Quickstart Guide</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/badge/vectors-none_needed-purple" alt="No Vectors">
  <img src="https://img.shields.io/badge/external_deps-zero-orange" alt="Zero External Deps">
  <img src="https://img.shields.io/badge/tests-719_passing-brightgreen" alt="719 Tests Passing">
</p>

---

**Your AI agents forget everything between sessions.** Every conversation starts from zero. Every insight, every architectural decision, every hard-won debugging breakthrough &mdash; gone.

NCMS fixes this. Permanently.

```bash
pip install ncms
```

```python
from ncms.interfaces.mcp.server import create_ncms_services, create_mcp_server

memory, bus, snapshots, consolidation = await create_ncms_services()
server = create_mcp_server(memory, bus, snapshots, consolidation)
```

Three lines. Your agents now have persistent, searchable, shared memory with cognitive scoring &mdash; a system that learns while it sleeps, tracks how knowledge evolves, and lets agents share what they know like Neo downloading kung fu. No vector database. No embedding pipeline. No external services.

## What Makes NCMS Different

| Problem | Traditional Approach | NCMS |
|---------|---------------------|------|
| Memory retrieval | Dense vector similarity (lossy) | **BM25 + SPLADE + graph expansion + cross-encoder + structured recall** (precise) |
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

NCMS organizes agent memory into a **Hierarchical Temporal Memory Graph (HTMG)** &mdash; a four-level structure where raw facts crystallize into tracked states, states cluster into temporal episodes, and episodes consolidate into strategic insights. Think of it as giving your agents not just storage, but the ability to *understand* their knowledge. ([V1 architecture](docs/ncms_v1.md))

### NCMS Architecture (HTMG)

<p align="center">
  <img src="docs/assets/htmg-brain.svg" alt="HTMG - Hierarchical Temporal Memory Graph" width="100%">
</p>

Every memory enters through an **admission gate** that routes it &mdash; like a bouncer deciding who gets into the club. Raw facts become `ATOMIC` nodes. State changes ("`Redis upgraded to v7.4`") become `ENTITY_STATE` nodes with bitemporal validity tracking. Related events cluster into `EPISODE` nodes via a 7-signal hybrid linker. And overnight, **dream cycles** consolidate episodes into `ABSTRACT` insights &mdash; the system literally learns while it sleeps.

### Retrieval Pipeline

Traditional memory systems compress documents into dense vectors, losing precision. NCMS uses complementary mechanisms that work together without a single embedding:

<p align="center">
  <img src="docs/assets/retrieval-pipeline.svg" alt="Retrieval Pipeline" width="100%">
</p>

**Tier 0 &mdash; Intent Classification.** Before retrieval begins, the query is classified into one of 7 intent types (fact lookup, current state, historical, event reconstruction, change detection, pattern, strategic reflection) via a BM25 exemplar index. This shapes which memory types receive a scoring bonus downstream &mdash; asking "what changed?" boosts entity states, while "what patterns emerged?" boosts abstracts.

**Tier 1 &mdash; BM25 + SPLADE Hybrid Search.** BM25 via Tantivy (Rust) provides exact lexical matching. SPLADE adds learned sparse neural retrieval &mdash; expanding "API specification" to also match "endpoint", "schema", "contract". Results are fused via Reciprocal Rank Fusion (RRF).

**Tier 1.5 &mdash; Graph-Expanded Discovery.** Entity relationships in the knowledge graph discover related memories that search missed lexically. A query matching "connection pooling" also finds memories about "PostgreSQL replication" &mdash; because both share the `PostgreSQL` entity in the graph.

**Tier 2 &mdash; ACT-R Cognitive Scoring.** Every memory has an activation level computed from access recency, frequency, and contextual relevance &mdash; the same math that models human memory in cognitive science. Dream-learned association strengths weight entity connections, and reconciliation penalties demote superseded or conflicted states.

**Tier 2.5 &mdash; Score Normalization.** Per-query min-max normalization brings all signals (BM25, SPLADE, Graph) to [0,1] scale before combining. Without this, SPLADE (5-200 range) would dominate BM25 (1-15 range) despite lower configured weights.

**Tier 3 &mdash; Selective Cross-Encoder Reranking.** A 22M-parameter cross-encoder (ms-marco-MiniLM-L-6-v2) reranks candidates &mdash; but only for fact lookup, pattern, and strategic reflection queries. State and temporal queries skip reranking to preserve chronological and causal ordering.

**Tier 4 &mdash; Structured Recall.** The `recall()` method wraps the full pipeline and layers structured context on top: entity state snapshots, episode membership with sibling expansion, and causal chains from the HTMG. Episode siblings are appended *after* the primary ranked results, expanding the retrieval set without displacing BM25's ranking. One call returns what currently takes 5+ tool calls.

```
activation(m) = base_level(m) + spreading_activation(m, query) + noise
                - supersession_penalty - conflict_penalty + hierarchy_bonus
base_level(m) = ln( sum( (time_since_access)^(-decay) ) )
spreading(m)  = sum( learned_PMI_weight(entity) )     ← dream-learned associations
combined(m)   = bm25 * w_bm25 + splade * w_splade + activation * w_actr + graph * w_graph
```

### Entity Extraction & Memory Enrichment

Entities are automatically extracted at store-time and search-time, feeding the knowledge graph for spreading activation and graph expansion:

<p align="center">
  <img src="docs/assets/entity-extraction.svg" alt="Entity Extraction Pipeline" width="100%">
</p>

**GLiNER NER** &mdash; Zero-shot Named Entity Recognition using a 209M-parameter [DeBERTa](https://github.com/urchade/GLiNER) model. Extracts entities across any domain with per-domain label customization via `ncms topics` CLI.

**Admission Scoring** &mdash; An 8-feature heuristic gate (novelty, utility, reliability, temporal salience, persistence, redundancy, episode affinity, state change signal) routes incoming memories to the right level of the hierarchy: discard, ephemeral cache, atomic fact, entity state update, or episode fragment. Not everything deserves to be remembered &mdash; like in the Matrix, you want to download kung fu, not every email you've ever read.

**State Reconciliation** &mdash; When a new entity state arrives ("Redis upgraded to v7.4"), NCMS classifies its relationship to existing states (supports, refines, supersedes, conflicts) and applies bitemporal truth maintenance. Superseded states get `is_current=False` with validity closure. Stale knowledge is automatically penalized in retrieval &mdash; you always get the current truth first.

**Episode Formation** &mdash; Related memories are automatically grouped into temporal episodes via a 7-signal hybrid linker (BM25, SPLADE, entity overlap, domain match, temporal proximity, source agent, structured anchors like JIRA tickets). Episodes give structure to "what happened during the API v2 migration" without requiring anyone to manually organize knowledge.

**Contradiction Detection** (opt-in) &mdash; New memories are compared against existing related memories via LLM to detect factual contradictions, with bidirectional annotation so stale knowledge is surfaced during retrieval.

**Knowledge Consolidation** (opt-in) &mdash; Clusters memories by shared entities, then uses LLM synthesis to discover emergent cross-memory patterns stored as searchable insights.

### Dream Cycles (Project Oracle)

<p align="center">
  <img src="docs/assets/project-oracle.svg" alt="Project Oracle — Dream Cycle Architecture" width="100%">
</p>

The keyword bridge [catastrophic failure](docs/ncms_v1.md#negative-results-keyword-bridges) and ACT-R's underperformance on static benchmarks revealed a deeper insight: **ACT-R has the right mechanism but needs learned weights.** On static IR benchmarks, every document has identical access history &mdash; so `ln(sum(t^-d))` produces uniform scores that contribute only noise. Dream cycles fix this by creating *differential* access patterns offline, teaching the system what matters through its own cognitive architecture.

Like biological sleep consolidation &mdash; where the brain replays and strengthens important memories overnight &mdash; NCMS runs three non-LLM passes during "sleep":

- **Dream Rehearsal** &mdash; Selects high-value memories via a 5-signal weighted score (PageRank centrality 0.40, staleness 0.30, importance 0.20, access frequency 0.05, recency 0.05) and injects synthetic access records. These memories get stronger `B_i = ln(sum(t^-d))` scores without changing the formula &mdash; the system practices remembering what matters.

- **Association Learning** &mdash; Computes pointwise mutual information (PMI) from entity co-access patterns in the search log. When "Redis" and "caching" consistently appear together in search results, their learned association strength feeds into `spreading_activation()` &mdash; replacing uniform 1.0 weights with data-driven connections. This is what keyword bridges *tried* to do, but learned from actual usage instead of LLM-extracted generics.

- **Importance Drift** &mdash; Compares recent access rates against older rates and adjusts `memory.importance` within bounded limits. Frequently accessed memories rise; neglected ones gracefully decay. The system develops its own sense of what's important, based on how agents actually use knowledge.

### Knowledge Bus & Agent Sleep/Wake

Agents don't poll for updates. They don't call each other directly. Knowledge flows through domain-routed channels &mdash; osmotic knowledge transfer, like the Matrix's construct programs where knowledge loads instantly from anywhere in the network.

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

**Ask/Respond** &mdash; Non-blocking queries routed by domain. Any agent can ask any domain and get answers from whoever knows.
**Announce/Subscribe** &mdash; Fire-and-forget broadcasts to interested agents. Breaking changes propagate instantly.
**Surrogate Response** &mdash; When agents go offline, they publish knowledge snapshots. Other agents can still ask them questions &mdash; the snapshot answers on their behalf using keyword matching, like leaving a well-organized notebook for your replacement.

<p align="center">
  <img src="docs/assets/sleep-wake-cycle.svg" alt="Sleep/Wake/Surrogate Response Cycle" width="100%">
</p>

The agent lifecycle (`start → work → sleep → wake → shutdown`) ensures knowledge persists across sessions. An agent that goes offline at 5pm can still answer questions at 3am through its surrogate &mdash; and when it wakes up, it picks up exactly where it left off.

---

## Benchmarks

NCMS achieves **nDCG@10 = 0.7206 on SciFact** — the BEIR dataset most aligned with factual knowledge retrieval — exceeding published ColBERTv2 (0.693, +4.0%) and SPLADE++ (0.710, +1.5%) without dense vectors or LLM at query time. Cross-domain validation on NFCorpus (biomedical) shows consistent improvement: **+10.0% over BM25** (0.3188 → 0.3506). Weight tuning across 108 ranking configs confirmed optimal weights (BM25=0.6, SPLADE=0.3, Graph=0.3, ACT-R=0.0), with ACT-R deferred to post-dream-cycle activation. The keyword bridge catastrophic failure (nDCG@10: 0.690 → 0.032) directly motivated the HTMG architecture.

On **SWE-bench Django** (503 documents, 170 test queries), structured recall achieves **Recall AR nDCG@10 = 0.2032**, exceeding search-only AR (0.1759) by **+15.5%** &mdash; demonstrating that episode sibling expansion surfaces relevant documents that BM25 alone misses. Dream cycle rehearsal (1x) provides a reproducible **+0.9% AR improvement** (0.1774), with TTL accuracy at **65.3%** through pure retrieval. [Full SWE-bench results](docs/paper.md#69-swe-bench-django-pre-tuning-baseline-results) in the paper.

See the [full ablation study, weight tuning results, and completed milestones](docs/ncms_v1.md#v1-ablation-study) for methodology, per-dataset metrics, and development history.

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

NCMS LLM features (contradiction detection, knowledge consolidation) can be accelerated with an [NVIDIA DGX Spark](https://www.nvidia.com/en-us/products/workstations/dgx-spark/) running [vLLM](https://docs.vllm.ai/) via the [NGC vLLM container](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/vllm).

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
# Contradiction detection + knowledge consolidation via DGX Spark
NCMS_CONTRADICTION_DETECTION_ENABLED=true \
NCMS_LLM_MODEL=openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
NCMS_LLM_API_BASE=http://spark-ee7d.local:8000/v1 \
NCMS_CONSOLIDATION_KNOWLEDGE_ENABLED=true \
NCMS_CONSOLIDATION_KNOWLEDGE_MODEL=openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
NCMS_CONSOLIDATION_KNOWLEDGE_API_BASE=http://spark-ee7d.local:8000/v1 \
uv run ncms serve
```

The Nemotron 3 Nano (30B total, 3B active MoE) fits entirely in the Spark's 128GB unified memory with room to spare, delivering sub-second LLM inference &mdash; orders of magnitude faster than CPU-based inference.

## Roadmap

**Evaluation**
- [x] Oracle ablation &mdash; dream-cycle-enhanced ACT-R evaluation (SWE-bench Django: Recall AR 0.2032, +15.5% over search)

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

*See [completed milestones and V1 ablation results](docs/ncms_v1.md#completed-milestones-v1-to-project-oracle) for development history.*

## Acknowledgments

- **[GLiNER](https://github.com/urchade/GLiNER)** &mdash; Zero-shot NER by [Zaratiana et al. (NAACL 2024)](https://arxiv.org/abs/2311.08526)
- **[SPLADE](https://github.com/naver/splade)** &mdash; Sparse neural retrieval by [Formal et al. (SIGIR 2021)](https://arxiv.org/abs/2107.05720), powered by [sentence-transformers](https://www.sbert.net/) SparseEncoder
- **[Tantivy](https://github.com/quickwit-oss/tantivy)** &mdash; Rust-based full-text search engine
- **[ACT-R](https://en.wikipedia.org/wiki/ACT-R)** &mdash; Cognitive architecture by John R. Anderson
- **[BEIR](https://github.com/beir-cellar/beir)** &mdash; Heterogeneous IR benchmark by [Thakur et al. (NeurIPS 2021)](https://arxiv.org/abs/2104.08663)
- **[NetworkX](https://networkx.org/)** &mdash; Graph library powering the knowledge graph
- **[litellm](https://github.com/BerriAI/litellm)** &mdash; Universal LLM API proxy
- **[aiosqlite](https://github.com/omnilib/aiosqlite)** &mdash; Async SQLite wrapper

## License

MIT

---

<p align="center">
  <strong>Built for agents that remember.</strong><br>
  <sub>By Shawn McCarthy / Chief Archeologist</sub>
</p>
