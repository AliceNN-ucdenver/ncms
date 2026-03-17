# NCMS × NemoClaw Integration Design

**Date**: 2026-03-16
**Status**: Draft
**Authors**: Shawn McCarthy, Claude

## 1. Background

### What Is NemoClaw?

NemoClaw is NVIDIA's open-source orchestration layer (Apache 2.0, alpha) that deploys **OpenClaw** (an autonomous AI agent framework) inside **OpenShell** (a sandboxed runtime with policy enforcement). The stack is:

- **OpenClaw** — The agent framework (TypeScript). Gateway + Pi agent runtime + plugin system + memory + skills. This is where the agent logic lives.
- **OpenShell** — Sandboxed runtime (Rust + K3s). 4-layer security: filesystem (Landlock), network (L7 policy), process (seccomp), inference routing (gateway proxy). Agents cannot make unauthorized network calls or filesystem writes.
- **NemoClaw** — Thin orchestration glue (TypeScript plugin + Python blueprint) that deploys OpenClaw inside OpenShell with NVIDIA inference routing (Nemotron 3 Super 120B default).

### Why Integrate?

OpenClaw's built-in memory is basic: LanceDB vectors + BM25 (FTS5), 70/30 blend, exponential decay. No knowledge graph, no entity extraction, no episode formation, no state reconciliation, no multi-agent knowledge bus.

NCMS provides exactly the cognitive memory capabilities OpenClaw lacks:
- **Hybrid retrieval**: BM25 + SPLADE v3 + graph spreading activation (tuned nDCG@10=0.7206)
- **Knowledge graph**: Entity extraction (GLiNER), co-occurrence edges, spreading activation
- **Temporal reasoning**: ACT-R scoring, dream cycles, importance drift
- **Multi-agent coordination**: Knowledge bus, surrogate responses, snapshot lifecycle
- **Structured memory**: Admission scoring, state reconciliation, episode formation, hierarchical consolidation

## 2. Integration Paths

### Path A: NCMS as MCP Server (Recommended)

OpenClaw has native MCP support. NCMS already exposes 14 MCP tools via `ncms serve`. This is the path of least resistance.

**How it works:**
1. NCMS MCP server runs inside the OpenShell sandbox as a subprocess (stdio transport)
2. OpenClaw discovers NCMS tools via MCP protocol
3. Agent uses NCMS tools for store/search/recall alongside OpenClaw's native capabilities
4. No code changes to OpenClaw or NemoClaw required

**Configuration** (`openclaw.json`):
```json
{
  "mcp": {
    "servers": {
      "ncms": {
        "command": "ncms",
        "args": ["serve"],
        "env": {
          "NCMS_DB_PATH": "/sandbox/.ncms/ncms.db",
          "NCMS_INDEX_PATH": "/sandbox/.ncms/index",
          "NCMS_SPLADE_ENABLED": "true",
          "NCMS_COOCCURRENCE_EDGES_ENABLED": "true"
        }
      }
    }
  }
}
```

**Pros:**
- Zero OpenClaw modifications — works with any OpenClaw version
- NCMS runs as a standard MCP server (already built and tested)
- All 14 NCMS tools immediately available to the agent
- Sandbox filesystem isolation naturally scopes the NCMS database per agent
- stdio transport means no network policy changes needed

**Cons:**
- No auto-recall/auto-capture lifecycle hooks (agent must explicitly call tools)
- Two memory systems coexist (OpenClaw's native + NCMS) — potential confusion
- No integration with OpenClaw's `<relevant-memories>` context injection
- Agent must learn when to use NCMS vs native memory tools

**Sandbox constraints:**
- NCMS DB and index must live under `/sandbox/` (the only writable path)
- GLiNER and SPLADE models need to be pre-loaded in the container image or cached under `/sandbox/`
- LLM calls for consolidation/contradiction go through OpenShell's inference gateway (not direct to vLLM)

### Path B: NCMS as OpenClaw Memory Plugin (Deeper Integration)

Replace OpenClaw's `memory-lancedb` plugin entirely with an NCMS-backed plugin that occupies the exclusive memory slot.

**How it works:**
1. Build a TypeScript OpenClaw plugin (`extensions/memory-ncms/`) that wraps NCMS
2. NCMS runs as an embedded Python subprocess, communicating via MCP stdio
3. Plugin registers tools (`memory_recall`, `memory_store`, `memory_forget`) that delegate to NCMS
4. Plugin hooks into OpenClaw's agent lifecycle for auto-recall/auto-capture
5. Plugin occupies the `plugins.slots.memory` exclusive slot

**Plugin structure:**
```
extensions/memory-ncms/
├── openclaw.plugin.json    # Manifest + config schema
├── index.ts                # Plugin registration
├── ncms-bridge.ts          # MCP client → NCMS subprocess
├── auto-recall.ts          # before_agent_start hook
├── auto-capture.ts         # agent_end hook
└── package.json
```

**Key integration hooks:**

```typescript
// auto-recall: inject relevant memories into system context
api.on("before_agent_start", async (ctx) => {
  const results = await ncmsBridge.search(ctx.userMessage, { limit: 5 });
  if (results.length > 0) {
    const memories = results.map(r =>
      `<memory relevance="${r.score}">${escapeForPrompt(r.content)}</memory>`
    ).join("\n");
    ctx.prependSystemContext(`<relevant-memories>\n${memories}\n</relevant-memories>`);
  }
});

// auto-capture: extract and store knowledge from conversations
api.on("agent_end", async (ctx) => {
  const content = extractStorableContent(ctx.messages);
  if (content) {
    await ncmsBridge.store(content, {
      source_agent: ctx.agentId,
      domains: [ctx.channel],
    });
  }
});
```

**Pros:**
- Seamless integration — agent uses one unified memory system
- Auto-recall injects NCMS results into every agent turn automatically
- Auto-capture persists knowledge without explicit agent action
- Replaces weaker LanceDB memory with full cognitive pipeline
- Knowledge bus enables multi-agent memory sharing across sessions
- Surrogate responses work when agents are offline

**Cons:**
- Requires building and maintaining a TypeScript plugin
- Tighter coupling to OpenClaw's plugin API (alpha, breaking changes expected)
- Must match OpenClaw's expected tool signatures (`memory_recall`, `memory_store`)
- More complex deployment (NCMS subprocess management inside sandbox)

### Path C: Hybrid (Recommended for Production)

Combine both paths:
1. **Phase 1**: Deploy as MCP server (Path A) — immediate value, no OpenClaw changes
2. **Phase 2**: Build the memory plugin (Path B) — deeper integration once OpenClaw stabilizes
3. **Phase 3**: Add knowledge bus integration for multi-agent NemoClaw deployments

## 3. Recommended Architecture (Hybrid Path C)

```
┌─────────────────────────────────────────────────────────────┐
│  OpenShell Sandbox                                          │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  OpenClaw Agent                                       │  │
│  │                                                       │  │
│  │  ┌─────────────┐  ┌──────────────┐  ┌─────────────┐  │  │
│  │  │ Pi Runtime   │  │ Skills       │  │ Channels    │  │  │
│  │  │ (agent loop) │  │ (.md files)  │  │ (ws/http)   │  │  │
│  │  └──────┬───────┘  └──────────────┘  └─────────────┘  │  │
│  │         │                                              │  │
│  │  ┌──────▼───────┐                                      │  │
│  │  │ memory-ncms  │  (Phase 2: OpenClaw plugin)          │  │
│  │  │ plugin       │                                      │  │
│  │  │ ┌──────────┐ │  - auto-recall (before_agent_start)  │  │
│  │  │ │ MCP      │ │  - auto-capture (agent_end)          │  │
│  │  │ │ client   │ │  - memory_recall / _store / _forget   │  │
│  │  │ └────┬─────┘ │                                      │  │
│  │  └──────┼───────┘                                      │  │
│  │         │ stdio                                        │  │
│  │  ┌──────▼───────────────────────────────────────────┐  │  │
│  │  │  NCMS MCP Server  (Phase 1: standalone)          │  │  │
│  │  │                                                   │  │  │
│  │  │  ┌─────────┐ ┌────────┐ ┌──────────┐ ┌────────┐ │  │  │
│  │  │  │ BM25    │ │ SPLADE │ │ Graph    │ │ ACT-R  │ │  │  │
│  │  │  │ Tantivy │ │ v3     │ │ NetworkX │ │ Scoring│ │  │  │
│  │  │  └─────────┘ └────────┘ └──────────┘ └────────┘ │  │  │
│  │  │  ┌─────────┐ ┌────────┐ ┌──────────┐            │  │  │
│  │  │  │ SQLite  │ │ GLiNER │ │ Knowledge│            │  │  │
│  │  │  │ (WAL)   │ │ NER    │ │ Bus      │            │  │  │
│  │  │  └─────────┘ └────────┘ └──────────┘            │  │  │
│  │  └───────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌───────────────────────────┐                              │
│  │ OpenShell Gateway         │  Inference routing           │
│  │ (intercepts LLM calls)    │  Network policy enforcement  │
│  └───────────────────────────┘                              │
└─────────────────────────────────────────────────────────────┘
```

## 4. Implementation Plan

### Phase 1: MCP Server Integration (1-2 days)

No NCMS code changes needed. Configuration and deployment only.

1. **Create NemoClaw blueprint overlay** that includes NCMS:
   - Custom Dockerfile extending `ghcr.io/nvidia/openshell-community/sandboxes/openclaw`
   - Pre-install NCMS: `pip install ncms` (or mount from host)
   - Pre-download GLiNER model to avoid runtime download inside sandbox
   - Set NCMS environment variables for sandbox paths

2. **Configure OpenClaw MCP** (`openclaw.json`):
   - Add NCMS as an MCP server (stdio transport)
   - Map NCMS tools to agent capabilities

3. **Update OpenShell network policy** (if NCMS needs external LLM):
   - Allowlist DGX Spark endpoint for consolidation/contradiction
   - Or configure NCMS to use OpenShell's inference gateway

4. **Write an NCMS skill for OpenClaw** (`skills/ncms-memory.md`):
   - Teach the agent when to use NCMS tools vs native memory
   - Provide examples of store/search/recall patterns

### Phase 2: OpenClaw Memory Plugin (1-2 weeks)

Build `memory-ncms` TypeScript plugin:

1. **NCMS Bridge** (`ncms-bridge.ts`):
   - Spawn NCMS MCP server as subprocess
   - MCP client for tool invocation
   - Health checking and restart logic

2. **Tool Registration**:
   - `memory_recall` → NCMS `search_memory` with result formatting
   - `memory_store` → NCMS `store_memory` with metadata extraction
   - `memory_forget` → NCMS delete (if/when supported)
   - Additional NCMS-specific tools: `memory_episodes`, `memory_graph`, `memory_consolidate`

3. **Lifecycle Hooks**:
   - `before_agent_start`: Auto-recall top-5 relevant memories, inject as system context
   - `agent_end`: Extract key facts/decisions/entities, auto-capture to NCMS
   - Map OpenClaw's `MemoryCategory` to NCMS memory types and domains

4. **Configuration Schema** (`openclaw.plugin.json`):
   ```json
   {
     "id": "memory-ncms",
     "configSchema": {
       "type": "object",
       "properties": {
         "spladeEnabled": { "type": "boolean", "default": true },
         "autoRecall": { "type": "boolean", "default": true },
         "autoRecallLimit": { "type": "number", "default": 5 },
         "autoCapture": { "type": "boolean", "default": true },
         "consolidationEnabled": { "type": "boolean", "default": false },
         "dreamCycleEnabled": { "type": "boolean", "default": false }
       }
     }
   }
   ```

### Phase 3: Multi-Agent Knowledge Bus (2-4 weeks)

This is the highest-value integration — it gives NemoClaw something no other agent memory system provides: **real-time multi-agent knowledge sharing with offline surrogates**. A team of specialized agents (code agent, docs agent, ops agent, security agent) can share observations, answer each other's questions, and maintain continuity even when individual agents are asleep.

#### Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  OpenShell Host                                                      │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │  NCMS Hub (shared instance, outside sandboxes)                  │ │
│  │                                                                  │ │
│  │  ┌──────────┐  ┌──────────┐  ┌───────────┐  ┌───────────────┐  │ │
│  │  │ HTTP/SSE │  │ SQLite   │  │ Knowledge │  │ Consolidation │  │ │
│  │  │ Server   │  │ (WAL) or │  │ Bus       │  │ + Dream       │  │ │
│  │  │ :8080    │  │ Postgres │  │ (pub/sub) │  │ Cycles        │  │ │
│  │  └──────────┘  └──────────┘  └───────────┘  └───────────────┘  │ │
│  │       ▲               ▲            ▲                ▲           │ │
│  └───────┼───────────────┼────────────┼────────────────┼───────────┘ │
│          │               │            │                │             │
│  ┌───────┼───────┐ ┌────┼────────┐ ┌─┼──────────┐ ┌───┼──────────┐ │
│  │ Sandbox A     │ │ Sandbox B   │ │ Sandbox C  │ │ Heartbeat    │ │
│  │ Code Agent    │ │ Docs Agent  │ │ Ops Agent  │ │ Daemon       │ │
│  │               │ │             │ │            │ │              │ │
│  │ memory-ncms   │ │ memory-ncms │ │ memory-ncms│ │ Triggers:    │ │
│  │ plugin ──────►│ │ plugin ────►│ │ plugin ───►│ │ - dream()    │ │
│  │  HTTP client  │ │ HTTP client │ │ HTTP client│ │ - consolidate│ │
│  │  to Hub:8080  │ │ to Hub:8080 │ │ to Hub:8080│ │ - decay pass │ │
│  └───────────────┘ └─────────────┘ └────────────┘ └──────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

#### 3a. NCMS Hub Server

A shared NCMS instance running outside the sandboxes, accessible via HTTP. This is the single source of truth for all agents' knowledge.

**New NCMS component**: `ncms serve --transport http --port 8080`

The hub exposes the same 14 MCP tools via HTTP REST + SSE for real-time events:

```
POST /api/v1/store          # store_memory (with source_agent field)
POST /api/v1/search         # search_memory
POST /api/v1/ask            # ask on knowledge bus (routes to live agents or surrogates)
POST /api/v1/announce       # publish event to knowledge bus subscribers
GET  /api/v1/events         # SSE stream for bus events (subscribe by domain)
POST /api/v1/agent/sleep    # agent publishes snapshot before going offline
POST /api/v1/agent/wake     # agent restores from snapshot on startup
GET  /api/v1/agent/status   # which agents are live, sleeping, or offline
```

**Storage upgrade**: For concurrent multi-agent writes, replace SQLite with PostgreSQL (NCMS's protocol-based DI makes this a storage adapter swap — the `MemoryStore` protocol is already defined in `domain/protocols.py`). SQLite WAL mode works for 2-3 agents but won't scale to 10+.

#### 3b. Knowledge Bus Integration

The knowledge bus is NCMS's most unique feature for multi-agent systems. Each agent registers domains of expertise and can:

**Ask**: Route questions to the most qualified agent.
```
Code Agent → ask("What's the current auth middleware stack?", domain="security")
  → Knowledge Bus routes to Security Agent (live) → direct answer
  → Or if Security Agent is sleeping → surrogate response from snapshot
```

**Announce**: Broadcast observations to interested agents.
```
Ops Agent → announce("Deployment failed: auth-service OOM at 2GB", domain="ops")
  → Knowledge Bus fans out to Code Agent (subscribed to "ops")
  → Code Agent auto-stores the observation, updates its context
```

**Subscribe**: Register interest in specific domains.
```
Code Agent subscribes to: ["ops", "security", "architecture"]
Docs Agent subscribes to: ["api", "architecture", "releases"]
Ops Agent subscribes to: ["ops", "monitoring", "incidents"]
```

**Implementation in OpenClaw**: Each agent's `memory-ncms` plugin:
- On `before_agent_start`: Check bus inbox for new announcements since last wake
- On `agent_end`: Announce any significant observations to relevant domains
- Register a background service (`api.registerService`) that maintains the SSE connection for real-time bus events

#### 3c. Surrogate Responses (Offline Agent Continuity)

When an agent goes to sleep (session ends, sandbox stops), it publishes a **snapshot** — a compressed summary of its current knowledge state. When another agent asks a question routed to the sleeping agent, NCMS's surrogate system responds using keyword matching against the snapshot.

**Agent lifecycle in NemoClaw:**

```
Agent Start (sandbox create)
  → memory-ncms plugin calls POST /api/v1/agent/wake
  → NCMS restores agent's snapshot, marks agent as "live"
  → Agent processes inbox (bus messages received while sleeping)

Agent Running
  → Stores memories, searches, asks questions via bus
  → Receives real-time announcements via SSE

Agent Sleep (sandbox stop / heartbeat idle)
  → memory-ncms plugin calls POST /api/v1/agent/sleep
  → NCMS publishes snapshot (top-K memories by importance per domain)
  → Agent marked as "sleeping" — surrogates active

Agent Offline (no sandbox)
  → Other agents can still ask questions routed to this agent
  → NCMS responds with surrogate (keyword match against snapshot)
  → Response tagged with "surrogate: true" so caller knows it's not live
```

**Why this matters**: In a NemoClaw deployment with 5 specialized agents, typically only 1-2 are actively running at any time (sandboxes are expensive). The knowledge bus + surrogates ensure continuity — the ops agent can ask about code architecture even when the code agent is offline.

#### 3d. Cross-Agent Dream Cycles

Dream cycles become dramatically more valuable with multiple agents:

- **Search log aggregation**: All agents' search queries feed into the shared search log. PMI association strengths reflect the collective access patterns of the entire team.
- **Cross-agent spreading activation**: Entity co-occurrences from the code agent's queries strengthen associations used by the ops agent's retrievals.
- **Rehearsal selection**: The dream cycle rehearses memories that are central to the shared knowledge graph — high PageRank nodes that multiple agents reference.
- **Importance drift**: Memories accessed by multiple agents drift upward in importance; memories only one agent ever accessed drift down over time.

**Scheduling**: OpenClaw's heartbeat daemon (configurable interval, default 30min) triggers dream cycles:

```yaml
# HEARTBEAT.md for NCMS consolidation agent
- [ ] Run NCMS dream cycle (every 6 hours)
- [ ] Run consolidation pass (every 12 hours)
- [ ] Report stale knowledge (weekly)
```

Or via OpenShell's cron-like scheduling for a dedicated consolidation sandbox.

#### 3e. OpenShell Network Policy for Hub

Each agent sandbox needs network access to the NCMS Hub:

```yaml
# openclaw-sandbox.yaml (NemoClaw policy)
network:
  dynamic:
    allow:
      - host: "ncms-hub.local"
        port: 8080
        protocol: "http"
        reason: "NCMS shared memory hub"
```

#### 3f. NCMS Changes Required

| Component | Change | Scope |
|-----------|--------|-------|
| `interfaces/http/` | New HTTP REST server (Starlette) wrapping MCP tools | New file, ~300 lines |
| `interfaces/http/` | SSE endpoint for bus event streaming | Extend dashboard.py or new |
| `infrastructure/storage/` | PostgreSQL adapter implementing `MemoryStore` protocol | New file, ~400 lines |
| `application/bus_service.py` | HTTP-aware bus that routes across processes | Extend existing |
| `application/snapshot_service.py` | HTTP endpoints for sleep/wake/status | Extend existing |
| `config.py` | `transport: str = "stdio"`, `hub_port: int = 8080` | 2 lines |
| `cli/main.py` | `ncms serve --transport http --port 8080` | Extend existing |

#### 3g. Competitive Advantage

No existing agent memory system provides this combination:
- **LangMem / Mem0**: Single-agent memory, no bus, no surrogates
- **Zep**: Multi-user but not multi-agent, no knowledge bus
- **OpenClaw native**: Per-agent LanceDB, no cross-agent sharing
- **MemGPT/Letta**: Single-agent with tiered memory, no graph

NCMS + NemoClaw would be the first system offering **cognitive multi-agent memory** with knowledge bus, surrogate responses, graph-based retrieval, and dream cycle consolidation — all inside a sandboxed, policy-governed runtime.

## 5. OpenClaw ↔ NCMS Feature Mapping

| OpenClaw Feature | NCMS Equivalent | Enhancement |
|-----------------|-----------------|-------------|
| LanceDB vectors (dense) | BM25 + SPLADE v3 (sparse) | Better precision on technical text, no embedding model dependency |
| FTS5 BM25 | Tantivy BM25 (Rust) | Same paradigm, faster engine |
| 70/30 vector/BM25 | 0.6/0.3/0.3 BM25/SPLADE/Graph | Three-signal retrieval with graph expansion |
| Exponential decay (30d half-life) | ACT-R cognitive decay + dream rehearsal | Biologically-inspired, differential access patterns |
| MMR re-ranking | Spreading activation + episode scoring | Graph-aware diversity, not just embedding distance |
| `memory_recall` (vector search) | `search_memory` (hybrid multi-signal) | Entity overlap, intent-aware supplementary candidates |
| `memory_store` (with dedup at 0.95) | `store_memory` (with admission scoring) | 8-feature quality gate, entity extraction, episode linking |
| Categories (preference/fact/decision) | Node types (atomic/entity_state/episode/abstract) | Hierarchical typed memory graph |
| Per-agent SQLite | Shared SQLite + knowledge bus | Multi-agent knowledge sharing and surrogates |
| None | State reconciliation | Tracks entity state changes, supersession, conflicts |
| None | Episode formation | Groups related memories into coherent narratives |
| None | Dream cycles | Offline rehearsal creating differential access patterns |
| None | Consolidation (5A/5B/5C) | Episode summaries, state trajectories, recurring patterns |

## 6. Sandbox Constraints and Mitigations

| Constraint | Impact | Mitigation |
|-----------|--------|------------|
| Writable only: `/sandbox/`, `/tmp/` | NCMS DB + index path | Set `NCMS_DB_PATH=/sandbox/.ncms/ncms.db`, `NCMS_INDEX_PATH=/sandbox/.ncms/index` |
| Network deny-by-default | LLM calls for consolidation blocked | Route through OpenShell inference gateway, or use local Ollama inside sandbox |
| No GPU passthrough (by default) | SPLADE/GLiNER need CPU inference | GLiNER (209M) runs fine on CPU (~50ms/chunk). SPLADE v3 CPU is slower but viable |
| Container image size | GLiNER + SPLADE models add ~1GB | Pre-bake models into custom sandbox image, or lazy-download to `/sandbox/` |
| Process isolation (seccomp) | Tantivy mmap may be restricted | Test mmap syscall allowance; fallback to file I/O if blocked |
| Inference routing intercepts all LLM calls | NCMS litellm calls get intercepted | Configure litellm to use OpenShell gateway URL as api_base |

## 7. DGX Spark Integration

For NCMS features requiring LLM (consolidation, contradiction detection), the DGX Spark at `spark-ee7d.local` running Nemotron 3 Nano can be:

1. **Direct** (if policy allows): Allowlist `spark-ee7d.local:8000` in OpenShell network policy
2. **Via OpenShell gateway**: Configure a custom inference provider in NemoClaw blueprint pointing to Spark
3. **Replaced by NemoClaw default**: Use Nemotron 3 Super 120B (cloud) via NemoClaw's built-in routing

Option 2 is most aligned with the NemoClaw model — all inference goes through the gateway.

## 8. Testing Strategy

1. **Unit tests**: Mock MCP bridge, verify tool mapping and lifecycle hook behavior
2. **Integration tests**: NCMS MCP server inside a local OpenShell sandbox, end-to-end store/search
3. **Benchmark**: Compare OpenClaw memory-lancedb vs memory-ncms on retrieval quality using agent conversation traces
4. **Multi-agent test**: Two agents sharing knowledge via NCMS knowledge bus inside separate sandboxes

## 9. Open Questions

1. **OpenClaw stability**: Alpha software with expected breaking changes. When does the plugin API stabilize?
2. **MCP server discovery**: Does OpenClaw auto-discover MCP tools or require explicit configuration?
3. **Inference routing transparency**: Can NCMS detect that LLM calls are being routed through OpenShell and adjust timeouts accordingly?
4. **Model caching**: Can GLiNER/SPLADE models be shared across sandbox instances via a read-only volume mount?
5. **Knowledge bus transport**: For Phase 3 multi-agent, would Redis/NATS be available inside sandboxes, or should the bus use HTTP?

## 10. References

- [NVIDIA NemoClaw Documentation](https://docs.nvidia.com/nemoclaw/latest/index.html)
- [NVIDIA OpenShell GitHub](https://github.com/NVIDIA/OpenShell)
- [NVIDIA NemoClaw GitHub](https://github.com/NVIDIA/NemoClaw)
- [OpenClaw Plugin Documentation](https://docs.openclaw.ai/tools/plugin)
- [OpenClaw Memory System](https://docs.openclaw.ai/concepts/memory)
