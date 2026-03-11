<p align="center">
  <img src="docs/assets/hero-banner.svg" alt="NCMS - NeMo Cognitive Memory System" width="100%">
</p>

<p align="center">
  <a href="#see-it-working">See It Working</a> &bull;
  <a href="#how-it-works">How It Works</a> &bull;
  <a href="#mcp-server">MCP Server</a> &bull;
  <a href="#nemo-agent-quickstart">NeMo Agents</a> &bull;
  <a href="#coding-agent-quickstart">Coding Agents</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/badge/vectors-none_needed-purple" alt="No Vectors">
  <img src="https://img.shields.io/badge/external_deps-zero-orange" alt="Zero External Deps">
  <img src="https://img.shields.io/badge/tests-297_passing-brightgreen" alt="297 Tests Passing">
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
cd ncms
uv sync
uv run ncms demo
```

The demo runs three collaborative agents through a complete lifecycle including a "Matrix-style" knowledge download:

```
  Phase 0  Download architecture knowledge ("I know kung fu." -- Neo)
  Phase 1  Three agents store domain knowledge
  Phase 2  Frontend agent asks API agent for endpoint specs (live response)
  Phase 3  API agent goes to sleep, frontend gets surrogate response from snapshot
  Phase 4  Database agent announces a breaking schema change
  Phase 5  Memory search shows ACT-R activation scoring in action
```

All in-process. All in-memory. Zero external dependencies. Under 10 seconds.

### Observability Dashboard

```bash
pip install "ncms[dashboard]"
uv run ncms dashboard
```

Opens a real-time web dashboard at `http://localhost:8420` showing:

- **Architecture Diagram Layout** &mdash; Central Knowledge Bus backbone with agent cards arranged around it, connected by animated flow lines
- **Per-Agent Activity Feeds** &mdash; Real-time SSE stream of asks, responses, announcements, and surrogate dispatches scoped to each agent
- **Conversation Threading** &mdash; Click any activity item to see the full ask/response thread, including answer text, confidence scores, and source mode (live vs. snapshot)
- **Snapshot Badges** &mdash; Surrogate responses are visually distinguished from live agent responses

The dashboard auto-runs demo agents by default. Use `--no-demo` for a blank canvas that observes your own agents.

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

**Tier 1 &mdash; BM25 + SPLADE Hybrid Search.** BM25 via Tantivy (Rust) provides exact lexical matching. SPLADE adds learned sparse neural retrieval &mdash; expanding "API specification" to also match "endpoint", "schema", "contract". Results are fused via Reciprocal Rank Fusion (RRF). Enable with `NCMS_SPLADE_ENABLED=true`.

**Tier 1.5 &mdash; Graph-Expanded Discovery.** Entity relationships in the knowledge graph discover related memories search missed lexically. A query matching "connection pooling" also finds memories about "PostgreSQL replication" &mdash; because both share the `PostgreSQL` entity in the graph.

**Tier 2 &mdash; ACT-R Cognitive Scoring.** Every memory has an activation level computed from access recency, frequency, and contextual relevance &mdash; the same math that models human memory in cognitive science.

```
activation(m) = base_level(m) + spreading_activation(m, query) + noise
base_level(m) = ln( sum( (time_since_access)^(-decay) ) )
combined(m)   = bm25 * w_bm25 + splade * w_splade + activation * w_actr
```

Graph-discovered memories enter scoring with `bm25_score = 0.0` and rank purely on cognitive activation. A frequently-accessed graph memory can outrank a weak BM25 hit. Weights are configurable (`NCMS_SCORING_WEIGHT_BM25`, `NCMS_SCORING_WEIGHT_SPLADE`, `NCMS_SCORING_WEIGHT_ACTR`).

**Tier 3 &mdash; LLM-as-Judge Reranking** (optional). Enable `NCMS_LLM_JUDGE_ENABLED=true` to send the top-k candidates to an LLM for relevance scoring. Judge scores are blended with activation scores for final ranking.

### Entity Extraction & Knowledge Enrichment

Entities are automatically extracted at store-time and search-time, feeding the knowledge graph for spreading activation and graph expansion:

<p align="center">
  <img src="docs/assets/entity-extraction.svg" alt="Entity Extraction Pipeline" width="100%">
</p>

**GLiNER NER** (built-in, required dependency) &mdash; zero-shot Named Entity Recognition using a 209M-parameter [DeBERTa](https://github.com/urchade/GLiNER) model. Extracts entities across any domain: people, organizations, technologies, concepts, and more. Uses domain-agnostic universal labels by default, with per-domain label customization via `ncms topics` CLI commands.

**Keyword Bridges** (optional) &mdash; semantic keywords extracted via LLM that connect otherwise disconnected subgraphs. "JWT validation" and "role-based access" share no entities, but both relate to the keyword "security". Enable with `NCMS_KEYWORD_BRIDGE_ENABLED=true`.

**Contradiction Detection** (optional) &mdash; at ingest time, new memories are compared against existing related memories via LLM to detect factual contradictions. Both the new and existing memories are annotated bidirectionally, so stale knowledge is surfaced during retrieval. Enable with `NCMS_CONTRADICTION_DETECTION_ENABLED=true`.

**Knowledge Consolidation** (optional) &mdash; background process that clusters memories by shared entities in the knowledge graph, then uses LLM synthesis to discover emergent cross-memory patterns. Insights are stored as searchable `Memory(type="insight")` records. Enable with `NCMS_CONSOLIDATION_KNOWLEDGE_ENABLED=true`.

### Knowledge Bus: Osmotic Agent Coordination

Agents don't poll for updates. They don't call each other directly. Knowledge flows through domain-routed channels:

<p align="center">
  <img src="docs/assets/knowledge-bus.svg" alt="Knowledge Bus Architecture" width="100%">
</p>

```python
# API agent announces a change
await agent.announce_knowledge(
    event="breaking-change",
    domains=["api:user-service"],
    content="GET /users now returns role field",
    breaking=True,
)

# Frontend agent subscribed to "api:*" gets it automatically
# Next time it checks its inbox, the knowledge is already there
```

**Ask/Respond** &mdash; Non-blocking queries routed by domain, not by agent name.
**Announce/Subscribe** &mdash; Fire-and-forget broadcasts to all interested agents.
**Broadcast Domain (`*`)** &mdash; Every agent auto-subscribes to the `*` channel on registration. Announcements with empty domains (or `domains=["*"]`) reach all agents. Domain-specific announcements still require explicit subscriptions, so filtering is preserved.
**Inbox** &mdash; Responses queue up. Agents process them between task steps.

### Snapshot Surrogate Response

When an agent goes offline, its knowledge doesn't disappear:

<p align="center">
  <img src="docs/assets/sleep-wake-cycle.svg" alt="Sleep/Wake/Surrogate Response Cycle" width="100%">
</p>

A developer using Copilot at 2 AM gets answers from the API agent's snapshot even though that agent last ran during business hours. The response is marked as "warm" so consumers know it's from cache, with confidence automatically discounted.

### Matrix-Style Knowledge Download

Seed your agents with knowledge from any file format:

```python
from ncms.application.knowledge_loader import KnowledgeLoader

loader = KnowledgeLoader(memory_service)

# Load architecture docs, API specs, meeting notes
stats = await loader.load_file("docs/architecture.md", domains=["arch"])
stats = await loader.load_file("design-deck.pptx", domains=["design"])  # needs ncms[docs]
stats = await loader.load_directory("docs/", domains=["docs"])
stats = await loader.load_text(raw_text, domains=["platform"])
```

**Built-in formats** (no extra deps): Markdown, plain text, JSON, YAML, CSV, HTML, reStructuredText.

**Rich document formats** (install `ncms[docs]`): DOCX, PPTX, PDF, XLSX &mdash; powered by Microsoft's [MarkItDown](https://github.com/microsoft/markitdown). Documents are converted to Markdown, then chunked by semantic boundaries automatically.

```bash
# Install with document support
pip install "ncms[docs]"

# CLI version
uv run ncms load docs/architecture.md --domains arch platform
uv run ncms load design-deck.pptx --domains design
```

---

## Quick Start

### Install

```bash
# With uv (recommended)
uv add ncms

# With pip
pip install ncms

# With rich document support (DOCX, PPTX, PDF, XLSX)
pip install "ncms[docs]"
```

### Run the Demo

```bash
uv run ncms demo
```

### Start the MCP Server

```bash
uv run ncms serve
```

Add to your Claude Code MCP config:

```json
{
  "mcpServers": {
    "ncms": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/ncms", "ncms", "serve"]
    }
  }
}
```

## MCP Server

NCMS exposes 10 tools and browsable resources via the Model Context Protocol:

| Tool | Description |
|------|-------------|
| `search_memory` | BM25 + ACT-R scored search across all memories |
| `store_memory` | Store knowledge with automatic indexing |
| `ask_knowledge` | Non-blocking ask routed to live agents |
| `ask_knowledge_sync` | Blocking ask with surrogate fallback |
| `announce_knowledge` | Broadcast changes to subscribed agents |
| `commit_knowledge` | Store knowledge from a coding session |
| `get_provenance` | Trace a memory's origin and access history |
| `list_domains` | List all knowledge domains and providers |
| `get_snapshot` | Retrieve an agent's Knowledge Snapshot |
| `load_knowledge` | Import files into memory (Matrix download) |

**Resources:** `ncms://status`, `ncms://domains`, `ncms://agents`, `ncms://graph/entities`

## NeMo Agent Quickstart

Build knowledge-aware agents by extending `KnowledgeAgent`:

```python
from ncms.interfaces.agent.base import KnowledgeAgent
from ncms.domain.models import (
    KnowledgeAsk, KnowledgeResponse, KnowledgePayload,
    KnowledgeProvenance, SnapshotEntry,
)

class MyAgent(KnowledgeAgent):
    def declare_expertise(self) -> list[str]:
        return ["my-domain", "my-domain:subtopic"]

    def declare_subscriptions(self) -> list[str]:
        return ["other-domain"]

    async def on_ask(self, ask: KnowledgeAsk) -> KnowledgeResponse | None:
        results = await self._memory.search(ask.question, domain="my-domain")
        if results:
            return KnowledgeResponse(
                ask_id=ask.ask_id,
                from_agent=self.agent_id,
                confidence=0.9,
                knowledge=KnowledgePayload(
                    type="fact",
                    content=results[0].memory.content,
                ),
                provenance=KnowledgeProvenance(source="memory-store"),
            )
        return None

    async def collect_working_knowledge(self) -> list[SnapshotEntry]:
        memories = await self._memory.list_memories(agent_id=self.agent_id)
        return [
            SnapshotEntry(
                domain="my-domain",
                knowledge=KnowledgePayload(type=m.type, content=m.content),
            )
            for m in memories
        ]
```

**Lifecycle:**

```python
agent = MyAgent("my-agent", bus_service, memory_service, snapshot_service)
await agent.start()           # Register + restore from snapshot
await agent.store_knowledge("learned something important")
await agent.ask_knowledge("what do you know about X?", domains=["other-domain"])
snapshot = await agent.sleep() # Publish snapshot, go offline
await agent.wake()             # Restore from snapshot, go online
await agent.shutdown()         # Final snapshot + deregister
```

The `KnowledgeAgent` base class is designed to plug into NeMo Agent Toolkit's `MemoryEditor` and `MemoryManager` interfaces. The NAT adapter is on the [roadmap](#roadmap) &mdash; once complete, NCMS will be a drop-in replacement for Mem0, Zep, or Redis memory backends in any NAT agent type.

## Coding Agent Quickstart

### Claude Code

Add NCMS hooks to `.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "ncms-commit-hook --event stop"
      }]
    }],
    "PreCompact": [{
      "hooks": [{
        "type": "command",
        "command": "ncms-commit-hook --event pre-compact"
      }]
    }],
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "ncms-context-loader --project $CLAUDE_PROJECT_DIR"
      }]
    }]
  }
}
```

**What happens:**
- `SessionStart`: Previous session knowledge loaded automatically
- `Stop`: Knowledge from the completed task committed to NCMS
- `PreCompact`: Full context dump before window compaction (critical &mdash; compaction destroys context)

### GitHub Copilot (Planned)

> **Note:** Copilot hook integration is on the roadmap. The configuration below shows the intended shape once implemented.

```json
{
  "version": 1,
  "hooks": {
    "sessionStart": [{
      "type": "command",
      "bash": "ncms-context-loader --project $(pwd)",
      "cwd": ".", "timeoutSec": 15
    }],
    "sessionEnd": [{
      "type": "command",
      "bash": "ncms-commit-hook --event session-end",
      "cwd": ".", "timeoutSec": 30
    }],
    "postToolUse": [{
      "type": "command",
      "bash": "ncms-commit-hook --event post-tool",
      "cwd": ".", "timeoutSec": 10
    }]
  }
}
```

### Any MCP-Compatible Agent (Cursor, etc.)

Just connect to the MCP server:

```bash
uv run ncms serve
```

## Architecture

```
src/ncms/
  domain/           Pure models, protocols, scoring, entity extraction (zero deps)
  application/      Memory service, bus service, snapshot service, graph service, loader
  infrastructure/   SQLite, Tantivy, NetworkX, AsyncIO bus, LLM judge
  interfaces/       MCP server, CLI, HTTP dashboard, agent base class, hooks
```

**Clean Architecture.** Domain layer has zero infrastructure dependencies. Every infrastructure component implements a Protocol interface. Swap SQLite for Postgres, NetworkX for Neo4j, or AsyncIO for Redis Pub/Sub &mdash; no application code changes.

**Embedded First.** Everything runs in-process with `pip install ncms`. No Docker. No Redis. No vector database. Scale up when you need to, not before.

## Configuration

Environment variables with `NCMS_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `NCMS_DB_PATH` | `~/.ncms/ncms.db` | SQLite database path |
| `NCMS_INDEX_PATH` | `~/.ncms/index` | Tantivy index directory |
| `NCMS_ACTR_DECAY` | `0.5` | Memory decay rate |
| `NCMS_ACTR_NOISE` | `0.25` | Activation noise (sigma) |
| `NCMS_ACTR_THRESHOLD` | `-2.0` | Retrieval activation threshold |
| `NCMS_SCORING_WEIGHT_BM25` | `0.6` | BM25 weight in combined score |
| `NCMS_SCORING_WEIGHT_ACTR` | `0.4` | ACT-R weight in combined score |
| `NCMS_BUS_ASK_TIMEOUT_MS` | `5000` | Knowledge Bus ask timeout |
| `NCMS_LLM_JUDGE_ENABLED` | `false` | Enable LLM-as-judge reranking |
| `NCMS_LLM_MODEL` | `gpt-4o-mini` | Model for LLM-as-judge |
| `NCMS_SNAPSHOT_TTL_HOURS` | `168` | Snapshot expiry (default 7 days) |
| `NCMS_GLINER_MODEL` | `urchade/gliner_medium-v2.1` | GLiNER model for entity extraction (required dep) |
| `NCMS_GLINER_THRESHOLD` | `0.3` | Minimum confidence score for entity extraction |
| `NCMS_LABEL_DETECTION_MODEL` | `gpt-4o-mini` | LLM model for `ncms topics detect` auto-labeling |
| `NCMS_LABEL_DETECTION_API_BASE` | *(none)* | vLLM/OpenAI-compatible endpoint for label detection |
| `NCMS_KEYWORD_BRIDGE_ENABLED` | `false` | Enable LLM-extracted keyword bridge nodes |
| `NCMS_KEYWORD_MAX_PER_MEMORY` | `8` | Maximum keywords extracted per memory |
| `NCMS_KEYWORD_LLM_MODEL` | `gpt-4o-mini` | LLM model for keyword extraction |
| `NCMS_KEYWORD_LLM_API_BASE` | *(none)* | vLLM/OpenAI-compatible endpoint for keywords |
| `NCMS_LLM_API_BASE` | *(none)* | vLLM/OpenAI-compatible endpoint for LLM judge |
| `NCMS_MODEL_CACHE_DIR` | *(none)* | Directory for downloaded models (GLiNER, SPLADE). Defaults to HuggingFace cache |
| `NCMS_SPLADE_ENABLED` | `false` | Enable SPLADE sparse neural retrieval |
| `NCMS_SPLADE_MODEL` | `prithivida/Splade_PP_en_v1` | SPLADE model (ONNX via fastembed) |
| `NCMS_SPLADE_TOP_K` | `50` | Number of SPLADE candidates per search |
| `NCMS_SCORING_WEIGHT_SPLADE` | `0.0` | SPLADE weight in combined score |
| `NCMS_CONTRADICTION_DETECTION_ENABLED` | `false` | Enable contradiction detection at ingest |
| `NCMS_CONTRADICTION_CANDIDATE_LIMIT` | `5` | Max memories to check for contradictions |
| `NCMS_CONSOLIDATION_KNOWLEDGE_ENABLED` | `false` | Enable knowledge consolidation |
| `NCMS_CONSOLIDATION_KNOWLEDGE_MIN_CLUSTER_SIZE` | `3` | Min memories per entity cluster |
| `NCMS_CONSOLIDATION_KNOWLEDGE_MODEL` | `gpt-4o-mini` | LLM model for insight synthesis |
| `NCMS_CONSOLIDATION_KNOWLEDGE_API_BASE` | *(none)* | vLLM/OpenAI-compatible endpoint for consolidation |
| `NCMS_CONSOLIDATION_KNOWLEDGE_MAX_INSIGHTS_PER_RUN` | `5` | Max insights created per consolidation run |

## Local LLM Inference

All LLM features (judge, keywords, consolidation) run against local models. Each feature has its own `MODEL` + optional `API_BASE` pair, so you can mix local and remote models. All calls use [litellm](https://docs.litellm.ai/) under the hood.

### macOS with Ollama (Development)

```bash
brew install ollama
brew services start ollama
ollama pull qwen3.5:35b-a3b    # 35B MoE, 3B active — runs on 32GB+ Mac
```

```bash
export NCMS_LLM_MODEL=ollama_chat/qwen3.5:35b-a3b
export NCMS_KEYWORD_LLM_MODEL=ollama_chat/qwen3.5:35b-a3b
export NCMS_CONSOLIDATION_KNOWLEDGE_MODEL=ollama_chat/qwen3.5:35b-a3b
```

No `API_BASE` needed &mdash; litellm connects to Ollama automatically. Thinking mode is auto-disabled for `ollama` models to get clean JSON responses.

### Linux + NVIDIA GPU with vLLM (Production)

```bash
pip install vllm
vllm serve meta-llama/Llama-3.2-3B-Instruct --port 8000
```

```bash
export NCMS_LLM_API_BASE=http://localhost:8000/v1
export NCMS_LLM_MODEL=openai/meta-llama/Llama-3.2-3B-Instruct
export NCMS_KEYWORD_LLM_API_BASE=http://localhost:8000/v1
export NCMS_KEYWORD_LLM_MODEL=openai/meta-llama/Llama-3.2-3B-Instruct
export NCMS_CONSOLIDATION_KNOWLEDGE_API_BASE=http://localhost:8000/v1
export NCMS_CONSOLIDATION_KNOWLEDGE_MODEL=openai/meta-llama/Llama-3.2-3B-Instruct
```

vLLM requires Linux + CUDA. Use `api_base` to point at any OpenAI-compatible endpoint.

## Roadmap

**Retrieval & Scoring**
- [x] Graph-expanded retrieval (Tier 1.5) &mdash; entity-based cross-memory discovery
- [x] GLiNER entity extraction &mdash; required zero-shot NER with domain-agnostic universal labels + per-domain label customization via `ncms topics` CLI
- [x] Keyword bridge nodes &mdash; LLM-extracted semantic concept bridges
- [x] Knowledge consolidation &mdash; entity co-occurrence clustering + LLM insight synthesis
- [x] vLLM / local LLM support &mdash; `api_base` config for all LLM features
- [x] SPLADE sparse neural retrieval &mdash; required dependency, learned term expansion fused with BM25 via RRF
- [x] Contradiction detection &mdash; LLM-powered detection at ingest with bidirectional annotation

**Ingestion**
- [ ] Directory watcher &mdash; filesystem monitor that auto-imports new files via the load pipeline, using GLiNER or LLM to classify domains

**Knowledge Bus & Agents**
- [ ] Redis/NATS-backed Knowledge Bus transport for multi-process deployments
- [ ] Periodic snapshot scheduler with incremental delta publishing
- [ ] NeMo Agent Toolkit `MemoryEditor`/`MemoryManager` plugin adapter

**Infrastructure & Packaging**
- [ ] Distributed memory configured through NeMo Agent Toolkit `config.yaml`
- [ ] Neo4j / FalkorDB graph backend for production-scale knowledge graphs
- [ ] Docker container with Helm charts (NIM-compatible packaging)
- [ ] REST/gRPC API following NIM conventions

**Dashboard & Observability**
- [ ] Historical replay and time-travel debugging
- [ ] Prometheus metrics and OpenTelemetry traces

**Evaluation**
- [ ] Retrieval pipeline ablation study &mdash; BEIR benchmarks + synthetic ACT-R temporal data; includes dataset-specific topic seeding via `ncms topics set` to align GLiNER extraction labels with each benchmark's entity types ([design doc](docs/ablation-study-design.md))

## Acknowledgments

- **[GLiNER](https://github.com/urchade/GLiNER)** — Zero-shot Named Entity Recognition model by [Zaratiana et al. (NAACL 2024)](https://arxiv.org/abs/2311.08526). Required dependency for domain-agnostic entity extraction.
- **[SPLADE](https://github.com/naver/splade)** — Sparse neural retrieval via learned term expansion by [Formal et al. (SIGIR 2021)](https://arxiv.org/abs/2107.05720). Required dependency for hybrid BM25 + SPLADE search, powered by [fastembed](https://github.com/qdrant/fastembed).
- **[Tantivy](https://github.com/quickwit-oss/tantivy)** — Rust-based full-text search engine powering BM25 retrieval.
- **[ACT-R](https://en.wikipedia.org/wiki/ACT-R)** — Cognitive architecture by John R. Anderson providing the activation scoring model.

## License

MIT

---

<p align="center">
  <strong>Built for agents that remember.</strong><br>
  <sub>By Shawn McCarthy / Chief Archeologist</sub>
</p>
