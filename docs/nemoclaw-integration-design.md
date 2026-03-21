# NCMS × NemoClaw + NVIDIA Agent Toolkit Integration Design

**Date**: 2026-03-16 (revised 2026-03-21)
**Status**: Active
**Authors**: Shawn McCarthy, Claude

## 1. Background

### The NVIDIA Agent Stack

NVIDIA's agent infrastructure is a layered stack. Understanding each layer clarifies where NCMS fits:

```
┌─────────────────────────────────────────────────────────────┐
│  OpenClaw Agent                                              │
│  Autonomous agent platform (TypeScript). Gateway + Pi        │
│  runtime + plugin system + skills. The agent logic lives     │
│  here. Built-in memory: LanceDB vectors + FTS5 BM25.        │
└──────────────────────┬───────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────┐
│  NemoClaw                                                     │
│  Orchestration layer (TypeScript plugin + Python blueprint).  │
│  Deploys OpenClaw inside OpenShell. Configures inference      │
│  routing (Nemotron 3 Super 120B default). CLI: `nemoclaw`.    │
└──────────────────────┬───────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────┐
│  OpenShell                                                    │
│  Sandboxed runtime (Rust + K3s). 4-layer security:            │
│  filesystem (Landlock LSM), network (L7 policy), process      │
│  (seccomp), inference routing (gateway proxy). YAML policies. │
└──────────────────────┬───────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────┐
│  NVIDIA Agent Toolkit (NeMo Agent Toolkit)                    │
│  Modular framework for building agent teams. Components:      │
│  - Agent Performance Primitives (APP)                         │
│  - A2A Protocol (agent-to-agent, HTTP JSON-RPC 2.0)          │
│  - Memory Module (MemoryEditor/Reader/Writer + providers)     │
│  - MCP integration (tool discovery)                           │
│  - Observability (Phoenix, Weave, OpenTelemetry)              │
│  - Evaluation system                                          │
│  - Nemotron models                                            │
│  Works with LangChain, LlamaIndex, CrewAI, Google ADK.        │
└──────────────────────────────────────────────────────────────┘
```

### Why Integrate?

The NVIDIA stack has two memory systems, both basic:

**OpenClaw native memory**: LanceDB vectors + BM25 (FTS5), 70/30 blend, exponential decay. No knowledge graph, no entity extraction, no episode formation, no state reconciliation.

**Agent Toolkit Memory Module**: `MemoryEditor`/`MemoryReader`/`MemoryWriter` interfaces with pluggable providers (Mem0, Redis, Zep). Short-term session state + long-term vector DB. No cognitive scoring, no graph traversal, no multi-agent knowledge bus.

NCMS replaces both with a unified cognitive memory system:

| Capability | OpenClaw Native | Agent Toolkit (Zep) | NCMS |
|-----------|----------------|--------------------|----|
| Lexical search | FTS5 BM25 | — | Tantivy BM25 (Rust) |
| Semantic search | LanceDB vectors | Zep vectors | SPLADE v3 sparse neural |
| Knowledge graph | — | — | GLiNER NER + NetworkX + spreading activation |
| Temporal reasoning | 30-day exponential decay | — | ACT-R cognitive decay + dream cycles |
| Multi-agent coordination | — | A2A Protocol | Knowledge Bus + A2A bridge |
| Offline continuity | — | — | Snapshot surrogates |
| Memory quality | Dedup at 0.95 | — | 8-feature admission scoring |
| State tracking | — | — | Bitemporal reconciliation (supports/refines/supersedes/conflicts) |
| Memory organization | Categories (pref/fact/decision) | Thread-based | HTMG (atomic → entity_state → episode → abstract) |
| Consolidation | — | — | Episode summaries, state trajectories, recurring patterns |
| Retrieval accuracy | Unknown | Unknown | nDCG@10=0.7206 (SciFact), beats Mem0 +31%, Letta +44% (SWE-bench) |

### Key Insight: No TypeScript Required

OpenClaw has native MCP support. NCMS already exposes 15 MCP tools via `ncms serve`. The Agent Toolkit Memory Module uses Python interfaces. **Every integration point is achievable in Python** — no TypeScript plugin needed:

- **MCP tools** → OpenClaw discovers NCMS tools via standard MCP protocol (stdio)
- **Agent Toolkit memory provider** → Python `MemoryEditor` implementation wrapping NCMS
- **A2A Protocol** → Python HTTP JSON-RPC 2.0 server bridging to NCMS Knowledge Bus
- **Agent behavior** → OpenClaw skill file (Markdown) teaches the agent when/how to use NCMS

## 2. Architecture

### Single-Agent (Phase 1-2)

```
┌─────────────────────────────────────────────────────────────┐
│  OpenShell Sandbox                                           │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  OpenClaw Agent                                        │  │
│  │                                                        │  │
│  │  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐  │  │
│  │  │ Pi Runtime   │  │ Skills       │  │ NAT Memory   │  │  │
│  │  │ (agent loop) │  │ ncms.md      │  │ Provider     │  │  │
│  │  └──────┬───────┘  └──────────────┘  │ (Python)     │  │  │
│  │         │                             └──────┬───────┘  │  │
│  │         │ MCP (stdio)                        │ Python   │  │
│  │  ┌──────▼────────────────────────────────────▼───────┐  │  │
│  │  │  NCMS                                              │  │  │
│  │  │                                                    │  │  │
│  │  │  ┌─────────┐ ┌────────┐ ┌──────────┐ ┌─────────┐  │  │  │
│  │  │  │ BM25    │ │ SPLADE │ │ Entity   │ │ ACT-R   │  │  │  │
│  │  │  │ Tantivy │ │ v3     │ │ Graph    │ │ Scoring │  │  │  │
│  │  │  └─────────┘ └────────┘ └──────────┘ └─────────┘  │  │  │
│  │  │  ┌─────────┐ ┌────────┐ ┌──────────┐ ┌─────────┐  │  │  │
│  │  │  │ SQLite  │ │ GLiNER │ │ Episodes │ │ Cross-  │  │  │  │
│  │  │  │ (WAL)   │ │ NER    │ │ + States │ │ Encoder │  │  │  │
│  │  │  └─────────┘ └────────┘ └──────────┘ └─────────┘  │  │  │
│  │  └────────────────────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌────────────────────────┐                                  │
│  │ OpenShell Gateway      │  Inference routing               │
│  │ (intercepts LLM calls) │  Network policy enforcement      │
│  └────────────────────────┘                                  │
└─────────────────────────────────────────────────────────────┘
```

### Multi-Agent (Phase 3-4)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  OpenShell Host                                                          │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  NCMS Hub (shared instance, outside sandboxes)                     │  │
│  │                                                                    │  │
│  │  ┌───────────┐ ┌───────────┐ ┌──────────┐ ┌───────────────────┐   │  │
│  │  │ HTTP API  │ │ A2A       │ │ Knowledge│ │ Consolidation     │   │  │
│  │  │ REST+SSE  │ │ JSON-RPC  │ │ Bus      │ │ + Dream Cycles    │   │  │
│  │  │ :8080     │ │ Server    │ │ (pub/sub)│ │ (scheduled)       │   │  │
│  │  └─────┬─────┘ └─────┬────┘ └────┬─────┘ └─────────┬─────────┘   │  │
│  │        │              │           │                  │             │  │
│  │  ┌─────▼──────────────▼───────────▼──────────────────▼──────────┐  │  │
│  │  │  NCMS Core (BM25 + SPLADE + Graph + SQLite/Postgres)        │  │  │
│  │  │  Snapshots │ Surrogates │ Entity States │ Episodes           │  │  │
│  │  └─────────────────────────────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│         ▲                ▲                ▲                              │
│         │ HTTP           │ HTTP           │ HTTP                        │
│  ┌──────┴──────┐  ┌──────┴──────┐  ┌──────┴──────┐                     │
│  │ Sandbox A   │  │ Sandbox B   │  │ Sandbox C   │                     │
│  │ Code Agent  │  │ Docs Agent  │  │ Ops Agent   │                     │
│  │             │  │             │  │             │                     │
│  │ NCMS client │  │ NCMS client │  │ NCMS client │                     │
│  │ (MCP+HTTP)  │  │ (MCP+HTTP)  │  │ (MCP+HTTP)  │                     │
│  └─────────────┘  └─────────────┘  └─────────────┘                     │
└──────────────────────────────────────────────────────────────────────────┘
```

## 3. Implementation Plan

### Phase 1: MCP Server Integration (immediate, no NCMS code changes)

NCMS already works as an MCP server. This phase is configuration and deployment artifacts only.

#### 1a. OpenClaw MCP Configuration

`openclaw.json`:
```json
{
  "mcp": {
    "servers": {
      "ncms": {
        "command": "ncms",
        "args": ["serve"],
        "transport": "stdio",
        "env": {
          "NCMS_DB_PATH": "/sandbox/.ncms/ncms.db",
          "NCMS_INDEX_PATH": "/sandbox/.ncms/index",
          "NCMS_SPLADE_ENABLED": "true",
          "NCMS_EPISODES_ENABLED": "true",
          "NCMS_INTENT_CLASSIFICATION_ENABLED": "true",
          "NCMS_RERANKER_ENABLED": "true",
          "NCMS_MODEL_CACHE_DIR": "/sandbox/.ncms/models"
        }
      }
    }
  }
}
```

#### 1b. OpenClaw Skill File

`skills/ncms-memory/SKILL.md`:
```yaml
---
name: ncms-memory
description: Cognitive memory system with hybrid retrieval, knowledge graph, and structured recall
version: 1.0.0
metadata:
  openclaw:
    always: true
    emoji: "🧠"
    requires:
      bins: ["ncms"]
---
```

```markdown
# NCMS Cognitive Memory

You have access to NCMS, a cognitive memory system. Use it to persist and retrieve knowledge across sessions.

## When to Store

Call `store_memory` when you:
- Learn a new fact, decision, or observation
- Complete a task (store the outcome)
- Discover a relationship between concepts
- Receive information that may be useful later

For structured state changes, include the `structured` parameter:
```json
{"entity": "auth-service", "key": "status", "value": "deployed v2.3"}
```

## When to Search

Call `recall_memory` (preferred) or `search_memory` when you:
- Need context about a topic before starting work
- Want to check if something was already discussed or decided
- Need to understand the history of a component or decision

`recall_memory` returns richer context: episode membership, entity states, and causal chains.
`search_memory` returns flat ranked results (faster, simpler).

## When to Use the Knowledge Bus

Call `ask_knowledge_sync` to ask other agents (or their surrogates) questions:
- "What's the current deployment status?" → routes to ops agent
- "What auth middleware is in use?" → routes to security agent

Call `announce_knowledge` to broadcast observations:
- "API latency increased 3x after deploy" → fans out to subscribed agents

## Domains

Tag memories with domains to organize knowledge: `["backend", "auth", "ops"]`.
Use `list_domains` to see what domains exist and which agents provide them.

## At Session Start

1. Call `recall_memory` with a summary of your current task to load relevant context
2. Check for announcements: `list_domains` to see if other agents have shared updates

## At Session End

Store any important findings or decisions before the session ends.
```

#### 1c. Dockerfile for Sandbox Image

`deployment/nemoclaw/Dockerfile`:
```dockerfile
FROM ghcr.io/nvidia/openshell-community/sandboxes/openclaw:latest

# Install NCMS
RUN pip install ncms

# Pre-download models to avoid runtime network calls
RUN python -c "from gliner import GLiNER; GLiNER.from_pretrained('urchade/gliner_medium-v2.1')"
RUN python -c "from sentence_transformers import SparseEncoder; SparseEncoder('naver/splade-v3')"
RUN python -c "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

# Copy skill file
COPY skills/ncms-memory/ /sandbox/skills/ncms-memory/

# Copy OpenClaw config
COPY openclaw.json /sandbox/.openclaw/openclaw.json
```

#### 1d. OpenShell Network Policy

`deployment/nemoclaw/openclaw-sandbox.yaml`:
```yaml
version: 1

filesystem_policy:
  read_write:
    - /sandbox
    - /tmp
  read_only:
    - /usr/local/lib/python3.12

network_policies:
  # Inference through OpenShell gateway (default, no extra policy needed)
  # DGX Spark direct access (optional, for consolidation/contradiction LLM)
  dgx_spark:
    endpoints:
      - host: "spark-ee7d.local"
        port: 8000
        protocol: rest
        tls: passthrough
    authorized_binaries:
      - python3
```

### Phase 2: NVIDIA Agent Toolkit Memory Provider (Python, ~200 lines)

Register NCMS as a NAT memory provider, replacing Zep/Mem0. This is a Python package that implements NAT's `MemoryEditor` interface and delegates to NCMS.

#### 2a. NAT Memory Provider

`src/ncms/integrations/nat_memory.py`:

```python
"""NVIDIA Agent Toolkit memory provider backed by NCMS.

Replaces Zep/Mem0/Redis as the NAT memory backend, providing:
- Hybrid retrieval (BM25 + SPLADE + Graph) instead of vector search
- Entity extraction and knowledge graph
- Episode formation and state reconciliation
- Structured recall with context enrichment

Installation:
    pip install ncms nvidia-nat

Registration:
    from ncms.integrations.nat_memory import NCMSMemoryEditor
    builder.register_memory("ncms", NCMSMemoryEditor)

Usage:
    memory = builder.get_memory_client("ncms")
    await memory.add(items=[MemoryItem(text="API uses OAuth2", metadata={...})])
    results = await memory.search(query="authentication method", limit=5)
"""
```

The provider implements three NAT interfaces:
- `MemoryEditor.add()` → delegates to `MemoryService.store()`
- `MemoryEditor.search()` → delegates to `MemoryService.recall()` (preferred) or `search()`
- `MemoryEditor.remove()` → delegates to `MemoryService.delete()` (new method needed)

NAT's `conversation_id` maps to NCMS domains. NAT's `MemoryItem` maps to NCMS `Memory` model.

#### 2b. NCMS Changes Required

| Component | Change | Scope |
|-----------|--------|-------|
| `application/memory_service.py` | Add `delete()` method | ~15 lines |
| `interfaces/mcp/tools.py` | Add `delete_memory` MCP tool | ~20 lines |
| `integrations/nat_memory.py` | NAT MemoryEditor implementation | New file, ~200 lines |
| `pyproject.toml` | Add `nat` optional dependency group | 2 lines |

### Phase 3: HTTP API Server (Starlette, ~400 lines)

Add HTTP REST transport to NCMS, enabling multi-agent deployments where sandboxed agents connect to a shared NCMS hub over HTTP.

#### 3a. REST API Endpoints

```
# Memory operations
POST   /api/v1/memories              # store_memory
GET    /api/v1/memories/search       # search_memory
GET    /api/v1/memories/recall       # recall_memory
DELETE /api/v1/memories/{id}         # delete_memory
GET    /api/v1/memories/{id}         # get_provenance

# Knowledge Bus
POST   /api/v1/bus/ask               # ask_knowledge_sync
POST   /api/v1/bus/announce          # announce_knowledge
GET    /api/v1/bus/events            # SSE stream (subscribe by domain)
GET    /api/v1/bus/domains           # list_domains

# Agent lifecycle
POST   /api/v1/agents/{id}/wake      # restore snapshot, mark live
POST   /api/v1/agents/{id}/sleep     # publish snapshot, mark sleeping
GET    /api/v1/agents/{id}/snapshot   # get_snapshot
GET    /api/v1/agents                 # list agents with status

# Entity state & episodes
GET    /api/v1/entities/{id}/state    # get_current_state
GET    /api/v1/entities/{id}/history  # get_state_history
GET    /api/v1/episodes              # list_episodes
GET    /api/v1/episodes/{id}         # get_episode

# Operations
POST   /api/v1/consolidation/run     # run_consolidation
GET    /api/v1/health                # health check (for OpenShell liveness probes)
```

#### 3b. CLI Extension

```
ncms serve                           # MCP server (stdio, existing)
ncms serve --transport http          # HTTP REST server
ncms serve --transport http --port 8080 --host 0.0.0.0
ncms serve --transport http --auth-token <token>  # Token auth for multi-agent
```

#### 3c. Authentication

Simple bearer token authentication for multi-agent deployments:

```
Authorization: Bearer <NCMS_AUTH_TOKEN>
X-Agent-ID: code-agent
```

Each request includes an `X-Agent-ID` header identifying the calling agent. The auth token is shared across all agents in a deployment (not per-agent — agents are trusted within the OpenShell perimeter).

#### 3d. SSE Bus Events

```
GET /api/v1/bus/events?domains=ops,security

data: {"type": "announcement", "from": "ops-agent", "domain": "ops", "content": "Deploy failed: OOM at 2GB"}
data: {"type": "ask", "from": "code-agent", "domain": "security", "question": "What auth middleware?"}
data: {"type": "response", "to": "code-agent", "source_mode": "surrogate", "content": "OAuth2 + JWT"}
```

#### 3e. NCMS Changes Required

| Component | Change | Scope |
|-----------|--------|-------|
| `interfaces/http/api.py` | New HTTP REST server (Starlette) | New file, ~400 lines |
| `interfaces/cli/main.py` | `--transport http` flag, `--port`, `--host`, `--auth-token` | ~30 lines |
| `config.py` | `http_port`, `http_host`, `auth_token` settings | ~10 lines |

### Phase 4: A2A Protocol Bridge (~300 lines)

Bridge NCMS Knowledge Bus to the NVIDIA Agent Toolkit A2A Protocol (HTTP JSON-RPC 2.0), enabling NCMS agents to communicate with any A2A-compatible agent (LangChain, CrewAI, Google ADK).

#### 4a. Agent Card

NCMS publishes an A2A Agent Card describing its capabilities:

```json
{
  "name": "ncms-memory-hub",
  "version": "1.0.0",
  "description": "Cognitive memory system with knowledge bus and surrogate responses",
  "skills": [
    {
      "name": "memory_store",
      "description": "Store knowledge with entity extraction and episode linking"
    },
    {
      "name": "memory_recall",
      "description": "Search memory with structured context enrichment"
    },
    {
      "name": "knowledge_ask",
      "description": "Route questions to domain experts or their surrogates"
    },
    {
      "name": "knowledge_announce",
      "description": "Broadcast observations to subscribed agents"
    }
  ],
  "capabilities": {
    "streaming": true,
    "pushNotifications": false
  },
  "interfaces": [
    {
      "protocol": "jsonrpc",
      "url": "http://ncms-hub.local:8080/a2a"
    }
  ]
}
```

#### 4b. A2A ↔ Knowledge Bus Mapping

| A2A Operation | NCMS Knowledge Bus |
|---------------|-------------------|
| `tasks/send` (skill: memory_store) | `MemoryService.store()` |
| `tasks/send` (skill: memory_recall) | `MemoryService.recall()` |
| `tasks/send` (skill: knowledge_ask) | `BusService.ask_sync()` |
| `tasks/send` (skill: knowledge_announce) | `BusService.announce()` |
| Agent Card discovery | `BusService.list_domains()` |

#### 4c. Bidirectional Bridge

NCMS agents can ask questions to external A2A agents:

```python
# NCMS Knowledge Bus ask → no local handler → A2A fallback
# BusService checks registered A2A agents for matching domain
# Forwards question via A2A tasks/send to external agent
# Response flows back through Knowledge Bus to requesting agent
```

External A2A agents can store/search NCMS memory:

```python
# External LangChain agent → A2A tasks/send (skill: memory_recall)
# NCMS A2A server receives JSON-RPC request
# Delegates to MemoryService.recall()
# Returns results via A2A tasks/sendSubscribe streaming
```

#### 4d. NCMS Changes Required

| Component | Change | Scope |
|-----------|--------|-------|
| `interfaces/a2a/server.py` | A2A JSON-RPC server | New file, ~200 lines |
| `interfaces/a2a/client.py` | A2A client for outbound requests | New file, ~100 lines |
| `application/bus_service.py` | A2A fallback when no local handler | ~30 lines |
| `config.py` | `a2a_enabled`, `a2a_port` settings | ~5 lines |

### Phase 5: Production Deployment

#### 5a. NemoClaw Blueprint Overlay

`deployment/nemoclaw/blueprint.yaml`:
```yaml
version: 1
min_openshell_version: "0.5.0"
min_openclaw_version: "0.8.0"
profiles:
  - name: ncms-single
    description: Single-agent with NCMS cognitive memory (MCP)
  - name: ncms-hub
    description: Multi-agent with shared NCMS hub (HTTP + A2A)
```

#### 5b. Docker Compose for Multi-Agent

`deployment/nemoclaw/docker-compose.yaml`:
```yaml
services:
  ncms-hub:
    image: ncms:latest
    command: ncms serve --transport http --port 8080 --host 0.0.0.0
    environment:
      NCMS_DB_PATH: /data/ncms.db
      NCMS_INDEX_PATH: /data/index
      NCMS_SPLADE_ENABLED: "true"
      NCMS_EPISODES_ENABLED: "true"
      NCMS_DREAM_CYCLE_ENABLED: "true"
      NCMS_AUTH_TOKEN: "${NCMS_AUTH_TOKEN}"
    volumes:
      - ncms-data:/data
    ports:
      - "8080:8080"

  code-agent:
    image: ncms-openclaw:latest
    environment:
      NCMS_HUB_URL: http://ncms-hub:8080
      NCMS_AUTH_TOKEN: "${NCMS_AUTH_TOKEN}"
    depends_on:
      - ncms-hub

  docs-agent:
    image: ncms-openclaw:latest
    environment:
      NCMS_HUB_URL: http://ncms-hub:8080
      NCMS_AUTH_TOKEN: "${NCMS_AUTH_TOKEN}"
    depends_on:
      - ncms-hub

volumes:
  ncms-data:
```

#### 5c. Heartbeat-Driven Consolidation

For scheduled dream cycles and consolidation in multi-agent deployments:

```
# HEARTBEAT.md for NCMS maintenance
- [ ] Run dream cycle (every 6 hours)
- [ ] Run consolidation pass (every 12 hours)
- [ ] Report stale knowledge (weekly)
```

Or via the NCMS Hub's built-in scheduler:

```bash
ncms serve --transport http --dream-interval 6h --consolidation-interval 12h
```

## 4. Feature Mapping

### OpenClaw ↔ NCMS

| OpenClaw Feature | NCMS Equivalent | Enhancement |
|-----------------|-----------------|-------------|
| LanceDB vectors (dense) | BM25 + SPLADE v3 (sparse) | Better precision, no embedding dependency |
| FTS5 BM25 | Tantivy BM25 (Rust) | Same paradigm, faster engine |
| 70/30 vector/BM25 | 0.6/0.3/0.3 BM25/SPLADE/Graph | Three-signal retrieval with graph expansion |
| Exponential decay (30d) | ACT-R decay + dream rehearsal | Biologically-inspired, differential access |
| MMR re-ranking | Cross-encoder + spreading activation | Graph-aware, intent-selective |
| `memory_recall` (vector) | `recall_memory` (structured) | Episode context, entity states, causal chains |
| `memory_store` (dedup 0.95) | `store_memory` (admission) | 8-feature quality gate + entity extraction |
| Categories (pref/fact/decision) | HTMG nodes (atomic/state/episode/abstract) | Hierarchical typed memory graph |
| Per-agent SQLite | Shared hub + knowledge bus | Multi-agent sharing + surrogates |
| — | State reconciliation | Tracks entity changes, supersession, conflicts |
| — | Episode formation | Groups related memories into narratives |
| — | Dream cycles | Offline rehearsal for differential access |
| — | Consolidation (5A/5B/5C) | Summaries, trajectories, patterns |

### NVIDIA Agent Toolkit ↔ NCMS

| NAT Feature | NCMS Equivalent | Enhancement |
|------------|-----------------|-------------|
| MemoryEditor (add/search/remove) | MemoryService (store/recall/delete) | Hybrid retrieval, graph enrichment |
| Zep thread-based memory | Domain-scoped memory + episodes | Structural organization, not just threads |
| Mem0 provider (vector search) | BM25 + SPLADE + Graph | 31% better AR, 6.3x better CR (benchmarked) |
| Redis provider (key-value) | SQLite + entity state store | Bitemporal state with reconciliation |
| A2A Protocol (agent-to-agent) | Knowledge Bus + A2A bridge | Surrogate responses when agents offline |
| Short-term memory (session) | Access log + ACT-R recency | Cognitive decay, not just TTL |
| Long-term memory (vector DB) | SQLite + Tantivy + SPLADE | No external vector DB dependency |
| Observability (Phoenix/Weave) | Event log + SSE + dashboard | Real-time pipeline visibility |

## 5. Sandbox Constraints and Mitigations

| Constraint | Impact | Mitigation |
|-----------|--------|------------|
| Writable only: `/sandbox/`, `/tmp/` | NCMS DB + index path | `NCMS_DB_PATH=/sandbox/.ncms/ncms.db` |
| Network deny-by-default | LLM calls for consolidation | Route through OpenShell gateway |
| No GPU passthrough (default) | SPLADE/GLiNER need CPU | GLiNER ~50ms/chunk on CPU; SPLADE slower but viable |
| Container image size | Models add ~1GB | Pre-bake in custom image |
| Process isolation (seccomp) | Tantivy mmap may be restricted | Test mmap allowance; file I/O fallback |
| Inference routing intercepts LLM | NCMS litellm calls intercepted | Use OpenShell gateway as `api_base` |

## 6. DGX Spark Integration

For NCMS features requiring LLM (consolidation, contradiction detection):

1. **Via OpenShell gateway** (recommended): Configure inference provider in blueprint pointing to Spark. All calls routed through gateway.
2. **Direct** (if policy allows): Allowlist `spark-ee7d.local:8000` in network policy.
3. **NemoClaw default**: Use Nemotron 3 Super 120B (cloud) via built-in routing.

Option 1 is most aligned with the NemoClaw model — all inference goes through the gateway.

## 7. Testing Strategy

| Level | What | How |
|-------|------|-----|
| Unit | NAT MemoryEditor interface | Mock NCMS services, verify add/search/remove |
| Unit | HTTP API endpoints | TestClient against Starlette app |
| Unit | A2A JSON-RPC protocol | Mock bus, verify request/response encoding |
| Integration | MCP in OpenShell sandbox | End-to-end store/search inside container |
| Integration | HTTP hub with 2 agents | Two agent processes sharing memory via HTTP |
| Integration | A2A bridge | External A2A agent → NCMS hub → response |
| Benchmark | vs OpenClaw LanceDB | Retrieval quality on agent conversation traces |
| Benchmark | Multi-agent knowledge sharing | Cross-agent recall accuracy with surrogates |

## 8. Competitive Advantage

No existing agent memory system provides this combination:

| System | Graph | Multi-Agent Bus | Surrogates | Structured Recall | A2A |
|--------|-------|----------------|------------|-------------------|-----|
| Mem0 | — | — | — | — | — |
| Zep | — | — | — | — | — |
| Letta (MemGPT) | — | — | — | — | — |
| LangMem | — | — | — | — | — |
| OpenClaw native | — | — | — | — | — |
| **NCMS** | **✓** | **✓** | **✓** | **✓** | **✓** |

NCMS + NemoClaw is the first system offering **cognitive multi-agent memory** with knowledge bus, surrogate responses, graph-based retrieval, dream cycle consolidation, and A2A interoperability — all inside a sandboxed, policy-governed runtime.

### Phase 6: NemoClaw Live Demo Dashboard

A complete working demo that launches three NeMo Agent Toolkit agents communicating through the NCMS Knowledge Bus, with the existing NCMS dashboard providing real-time visualization of all memory operations, bus events, entity graph evolution, and agent lifecycle transitions.

#### 6a. Demo Agents

Three domain-specialized agents modeled on a realistic software team, built as NAT workflows:

| Agent | Domain Expertise | Subscriptions | Personality |
|-------|-----------------|---------------|-------------|
| **Code Agent** | `backend`, `api`, `architecture` | `ops`, `security`, `architecture` | Builds APIs, reviews PRs, tracks technical debt |
| **Ops Agent** | `ops`, `monitoring`, `incidents` | `backend`, `api`, `releases` | Deploys, monitors, responds to incidents |
| **Security Agent** | `security`, `auth`, `compliance` | `api`, `ops`, `architecture` | Audits, enforces policies, reviews access patterns |

#### 6b. Demo Scenario (8 Phases)

The demo orchestrates a realistic multi-agent workflow that exercises every NCMS feature:

**Phase 1 — Agent Registration & Wake**
- Each agent wakes via HTTP API, restores snapshot (if any), registers domains
- Dashboard shows: 3 agents come online, domain map populates

**Phase 2 — Knowledge Seeding**
- Code Agent stores API specs, architecture decisions, tech debt items
- Ops Agent stores deployment history, monitoring alerts, runbook entries
- Security Agent stores auth policies, compliance requirements, audit findings
- Dashboard shows: memories appearing, entity graph growing, episodes forming

**Phase 3 — Cross-Agent Collaboration**
- Code Agent asks Security Agent: "What auth middleware should the new endpoint use?"
- Security Agent responds with current policy (live response)
- Ops Agent announces: "Deploy of auth-service v2.3 completed successfully"
- Code Agent and Security Agent receive the announcement, store it
- Dashboard shows: bus messages flowing, ask/response pairs, entity state updates

**Phase 4 — Breaking Change Propagation**
- Code Agent announces: "Breaking: User API response format changed from v1 to v2"
- Ops Agent receives, stores, updates monitoring config
- Security Agent receives, reviews for compliance impact
- Entity states updated: `user-api.version` supersedes `v1` with `v2`
- Dashboard shows: state reconciliation, SUPERSEDES edges, conflict detection

**Phase 5 — Agent Sleep & Surrogate**
- Security Agent goes to sleep (publishes snapshot, marked sleeping)
- Code Agent asks: "Is the new endpoint compliant with SOC2?"
- NCMS responds with surrogate (keyword match against Security Agent's snapshot)
- Response tagged `source_mode: "warm"` with staleness warning
- Dashboard shows: agent status change, surrogate response path

**Phase 6 — Dream Cycle**
- Trigger dream cycle on the hub
- Rehearsal selects high-value memories (cross-agent PageRank)
- PMI associations computed from search logs
- Importance drift adjusts based on multi-agent access patterns
- Dashboard shows: dream rehearsal events, association strength updates

**Phase 7 — Structured Recall**
- Ops Agent recalls: "What happened with the auth service?"
- NCMS returns RecallResults with: episode context (deploy + breaking change grouped), entity states (current versions), causal chains (v1 SUPERSEDED_BY v2)
- Dashboard shows: recall retrieval path, episode expansion, context enrichment

**Phase 8 — Security Agent Wakes**
- Security Agent wakes, restores snapshot, processes inbox
- Inbox contains: the breaking change announcement from Phase 4
- Agent reviews and stores compliance assessment
- Dashboard shows: wake lifecycle, inbox drain, new memories stored

#### 6c. Implementation

The demo reuses the existing NCMS dashboard infrastructure (Starlette + SSE + D3 graph visualization) and extends the demo runner pattern from `demo/run_demo.py`:

```
src/ncms/demo/
├── run_demo.py                    # Existing 6-phase demo (in-process)
├── run_nemoclaw_demo.py           # NEW: NemoClaw 8-phase demo
├── agents/
│   ├── base_demo.py               # Existing base agent
│   ├── api_agent.py               # Existing → rename to code_agent.py
│   ├── frontend_agent.py          # Existing → repurpose as ops_agent.py
│   ├── database_agent.py          # Existing → repurpose as security_agent.py
│   ├── nat_code_agent.py          # NEW: NAT workflow-based code agent
│   ├── nat_ops_agent.py           # NEW: NAT workflow-based ops agent
│   └── nat_security_agent.py      # NEW: NAT workflow-based security agent
```

**Two demo modes:**

1. **`ncms demo`** (existing) — In-process demo with the existing 3 agents (api/frontend/db). No changes.
2. **`ncms demo --nemoclaw`** — NemoClaw demo with NAT agents communicating via HTTP API. Requires Phase 3 (HTTP API) to be complete. Launches the NCMS hub, then runs the 8-phase scenario against it.

Alternatively, for environments without NAT installed:

3. **`ncms demo --multi-agent`** — Same 8-phase scenario using the existing `DemoAgent` base class over HTTP API (no NAT dependency). Demonstrates the same Knowledge Bus, surrogate, and dream cycle features.

#### 6d. Dashboard Enhancements

The existing dashboard SPA (`interfaces/http/static/index.html`) gets minor extensions:

| Enhancement | What | Scope |
|------------|------|-------|
| Agent lifecycle panel | Show agent status (live/sleeping/offline) with transition timestamps | ~50 lines HTML/JS |
| Bus message feed | Separate SSE feed showing ask/response/announce events | ~40 lines |
| Surrogate indicator | Visual flag when a response came from surrogate vs live agent | ~10 lines |
| Episode timeline | Horizontal timeline showing episode membership and state transitions | ~80 lines |

#### 6e. CLI

```bash
# Quick start — runs everything locally, opens dashboard in browser
ncms demo --nemoclaw

# Manual — start hub, then demo separately
ncms serve --transport http --port 8080 &
ncms demo --nemoclaw --hub-url http://localhost:8080

# Without NAT — same scenario, pure NCMS agents
ncms demo --multi-agent
```

#### 6f. NCMS Changes Required

| Component | Change | Scope |
|-----------|--------|-------|
| `demo/run_nemoclaw_demo.py` | 8-phase demo orchestrator | New file, ~400 lines |
| `demo/agents/nat_*.py` | 3 NAT agent wrappers (or DemoAgent-based fallback) | 3 new files, ~150 lines each |
| `interfaces/http/static/index.html` | Agent lifecycle + bus feed + episode timeline UI | ~180 lines additions |
| `interfaces/http/dashboard.py` | SSE bus event forwarding endpoint | ~30 lines |
| `interfaces/cli/main.py` | `--nemoclaw` and `--multi-agent` flags | ~20 lines |

## 9. Implementation Summary

| Phase | Deliverable | NCMS Code Changes | Effort |
|-------|------------|-------------------|--------|
| 1 | MCP + Skill + Dockerfile + Policy | None | 1 day |
| 2 | NAT Memory Provider | `delete()` method + `nat_memory.py` | 1-2 days |
| 3 | HTTP REST API | `api.py` + CLI flags + config | 2-3 days |
| 4 | A2A Protocol Bridge | `a2a/server.py` + `a2a/client.py` + bus fallback | 2-3 days |
| 5 | Production Deployment | Blueprint + Docker Compose + scheduling | 1-2 days |
| 6 | NemoClaw Live Demo | Demo orchestrator + agents + dashboard enhancements | 2-3 days |

**Total: ~10-15 days**

## 10. References

- [NVIDIA NemoClaw Documentation](https://docs.nvidia.com/nemoclaw/latest/index.html)
- [NVIDIA NemoClaw GitHub](https://github.com/NVIDIA/NemoClaw)
- [NVIDIA OpenShell Documentation](https://docs.nvidia.com/openshell/latest/)
- [NVIDIA OpenShell GitHub](https://github.com/NVIDIA/OpenShell)
- [NVIDIA NeMo Agent Toolkit Documentation](https://docs.nvidia.com/nemo/agent-toolkit/latest/)
- [NVIDIA NeMo Agent Toolkit GitHub](https://github.com/NVIDIA/NeMo-Agent-Toolkit)
- [A2A Protocol Specification](https://a2a-protocol.org/latest/specification/)
- [OpenClaw Skill Format](https://github.com/openclaw/clawhub/blob/main/docs/skill-format.md)
- [OpenClaw Plugin Documentation](https://docs.openclaw.ai/tools/plugin)
- [OpenShell Policy Schema Reference](https://docs.nvidia.com/openshell/latest/reference/policy-schema.html)
