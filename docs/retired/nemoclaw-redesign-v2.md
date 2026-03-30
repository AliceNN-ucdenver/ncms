# NemoClaw + NCMS: Updated Design (v2)

**Date**: 2026-03-22
**Status**: Proposal
**Supersedes**: `nemoclaw-redesign-analysis.md` (Option C single-sandbox)

## Architecture: Three Sandboxes + NCMS Hub

Following the NemoClaw pattern (one sandbox per agent), we deploy **three isolated OpenClaw sandboxes** plus a shared **NCMS Hub** that provides cognitive memory and a real-time Knowledge Bus.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Host (Mac / Linux / DGX Spark)                                          │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  NCMS Hub Container                                                │  │
│  │                                                                    │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐ │  │
│  │  │ MCP HTTP API │  │ Bus HTTP API │  │ Dashboard                │ │  │
│  │  │ :8080        │  │ (same :8080) │  │ :8420                    │ │  │
│  │  │              │  │              │  │ (SSE + D3 graph + stats) │ │  │
│  │  │ store/search │  │ register     │  └──────────────────────────┘ │  │
│  │  │ recall/delete│  │ ask/respond  │                               │  │
│  │  │ announce     │  │ subscribe    │                               │  │
│  │  └──────┬───────┘  └──────┬───────┘                               │  │
│  │         │                 │                                        │  │
│  │  ┌──────▼─────────────────▼──────────────────────────────────────┐ │  │
│  │  │  NCMS Core                                                     │ │  │
│  │  │                                                                │ │  │
│  │  │  AsyncKnowledgeBus  ←── RemoteAskHandlers (SSE push + wait)   │ │  │
│  │  │  EventLog           ←── SSE subscribers (per agent + dashboard)│ │  │
│  │  │  MemoryService      ←── BM25 + SPLADE + Graph + SQLite        │ │  │
│  │  │  SnapshotService    ←── Surrogate fallback when agents sleep   │ │  │
│  │  │  EpisodeService     ←── Groups related memories across agents  │ │  │
│  │  │  ConsolidationSvc   ←── Dream cycles, abstracts (scheduled)    │ │  │
│  │  └────────────────────────────────────────────────────────────────┘ │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│         ▲ HTTP+SSE          ▲ HTTP+SSE          ▲ HTTP+SSE              │
│         │                   │                   │                        │
│  ┌──────┴──────┐     ┌──────┴──────┐     ┌──────┴──────┐               │
│  │  OpenShell   │     │  OpenShell   │     │  OpenShell   │              │
│  │  Sandbox     │     │  Sandbox     │     │  Sandbox     │              │
│  │              │     │              │     │              │              │
│  │  Architect   │     │  Security    │     │  Builder     │              │
│  │  Agent       │     │  Agent       │     │  Agent       │              │
│  │              │     │              │     │              │              │
│  │ ┌──────────┐│     │ ┌──────────┐│     │ ┌──────────┐│              │
│  │ │ OpenClaw ││     │ │ OpenClaw ││     │ │ OpenClaw ││              │
│  │ │ + skill  ││     │ │ + skill  ││     │ │ + skill  ││              │
│  │ └─────┬────┘│     │ └─────┬────┘│     │ └─────┬────┘│              │
│  │       │MCP  │     │       │MCP  │     │       │MCP  │              │
│  │ ┌─────▼────┐│     │ ┌─────▼────┐│     │ ┌─────▼────┐│              │
│  │ │Bus Agent ││     │ │Bus Agent ││     │ │Bus Agent ││              │
│  │ │(sidecar) ││     │ │(sidecar) ││     │ │(sidecar) ││              │
│  │ │ SSE ↔ Hub││     │ │ SSE ↔ Hub││     │ │ SSE ↔ Hub││              │
│  │ └──────────┘│     │ └──────────┘│     │ └──────────┘│              │
│  └─────────────┘     └─────────────┘     └─────────────┘               │
└──────────────────────────────────────────────────────────────────────────┘
```

## Why This Architecture

1. **One sandbox per agent** — The NemoClaw pattern. Each agent has isolated filesystem, network policy, and inference config. Architect can't touch Security's data. Builder can't bypass network rules.

2. **NCMS Hub is the shared fabric** — Replaces "shared volumes" from the NemoClaw article with a cognitive memory system + real-time event bus. Every agent reads/writes the same memory. The Knowledge Bus routes questions and announcements in real-time.

3. **Event-driven, not polling** — Agents maintain SSE connections to the Hub. Breaking changes propagate immediately. Questions route in real-time. No polling, no message board.

4. **Everything already exists** — `AsyncKnowledgeBus` has domain routing, ask handlers, announcement fanout, subscriptions, inboxes. `EventLog` has SSE subscriber support with `asyncio.Queue` per subscriber. We just need HTTP transport endpoints on the Hub and a thin bus sidecar in each sandbox.

## Component Details

### 1. NCMS Hub — HTTP Bus API (New Endpoints)

Added alongside existing MCP HTTP and Dashboard routes on `:8080`:

```
# Agent lifecycle
POST   /api/v1/bus/register          # Register agent + domains, establish SSE
POST   /api/v1/bus/deregister        # Agent going offline
POST   /api/v1/bus/availability      # Update status (online/sleeping)

# Real-time bus
GET    /api/v1/bus/subscribe          # SSE stream (questions + announcements for this agent)
POST   /api/v1/bus/ask                # Ask question (blocks until response or timeout)
POST   /api/v1/bus/respond            # Post response to a pending ask
POST   /api/v1/bus/announce           # Broadcast announcement

# Inbox (drain missed events)
GET    /api/v1/bus/inbox/{agent_id}          # Pending responses
GET    /api/v1/bus/announcements/{agent_id}  # Pending announcements
DELETE /api/v1/bus/inbox/{agent_id}          # Drain inbox
DELETE /api/v1/bus/announcements/{agent_id}  # Drain announcements
```

### 2. RemoteAskHandler — Bridge Bus to SSE

The key adapter. When a remote agent registers, the Hub creates a `RemoteAskHandler` and calls `bus.set_ask_handler(agent_id, handler)`. This handler is invoked by `AsyncKnowledgeBus.ask()` just like a local handler — the bus doesn't know the difference.

```python
class RemoteAskHandler:
    """Bridges AsyncKnowledgeBus ask to a remote agent via SSE + HTTP response."""

    def __init__(self, agent_id: str, sse_queue: asyncio.Queue[DashboardEvent]):
        self._agent_id = agent_id
        self._sse_queue = sse_queue
        self._pending: dict[str, asyncio.Future[KnowledgeResponse]] = {}

    async def __call__(self, ask: KnowledgeAsk) -> KnowledgeResponse | None:
        """Called by AsyncKnowledgeBus when a question routes to this agent."""
        # 1. Push question to agent's SSE stream
        self._sse_queue.put_nowait(DashboardEvent(
            type="bus.ask.routed",
            agent_id=self._agent_id,
            data={
                "ask_id": ask.ask_id,
                "from_agent": ask.from_agent,
                "question": ask.question,
                "domains": ask.domains,
            },
        ))

        # 2. Wait for agent to POST /bus/respond with matching ask_id
        future: asyncio.Future[KnowledgeResponse] = asyncio.get_event_loop().create_future()
        self._pending[ask.ask_id] = future
        try:
            return await asyncio.wait_for(future, timeout=ask.ttl_ms / 1000)
        except TimeoutError:
            return None  # Bus will try surrogate fallback
        finally:
            self._pending.pop(ask.ask_id, None)

    def resolve(self, ask_id: str, response: KnowledgeResponse) -> None:
        """Called when POST /api/v1/bus/respond arrives."""
        future = self._pending.get(ask_id)
        if future and not future.done():
            future.set_result(response)
```

**This is the only new abstraction.** Everything else — domain routing, subscription filtering, surrogate fallback, event logging — uses the existing `AsyncKnowledgeBus` and `EventLog` unchanged.

### 3. Bus Agent Sidecar — In Each Sandbox

A lightweight Python daemon (~200 lines) that runs alongside OpenClaw in each sandbox. Installed via `pip install ncms` (the package already exists).

```python
"""ncms-bus-agent — Knowledge Bus sidecar for OpenShell sandboxes.

Maintains SSE connection to NCMS Hub. Handles incoming questions
by searching NCMS memory + optional LLM synthesis. Receives
announcements and stores them into memory.

Run: ncms bus-agent --hub http://ncms-hub:8080 --agent-id architect --domains architecture,calm-model
"""

class BusAgentSidecar:
    def __init__(self, hub_url: str, agent_id: str, domains: list[str]):
        self.hub = hub_url
        self.agent_id = agent_id
        self.domains = domains

    async def run(self):
        # 1. Register with Hub
        await self._register()

        # 2. Connect SSE stream
        async for event in self._subscribe_sse():
            if event.type == "bus.ask.routed":
                await self._handle_question(event)
            elif event.type == "bus.announce":
                await self._handle_announcement(event)

    async def _handle_question(self, event: DashboardEvent):
        """Search NCMS memory + synthesize answer, POST response back."""
        ask_id = event.data["ask_id"]
        question = event.data["question"]

        # Search NCMS Hub for relevant knowledge
        results = await self._http_post(f"{self.hub}/api/v1/memories/recall", {
            "query": question,
            "domain": self.domains[0],
            "limit": 5,
        })

        # Synthesize answer (could use LLM, or just return top result)
        answer = self._synthesize(question, results)

        # POST response back to Hub — unblocks the asking agent
        await self._http_post(f"{self.hub}/api/v1/bus/respond", {
            "ask_id": ask_id,
            "from_agent": self.agent_id,
            "content": answer,
            "confidence": 0.8,
        })

    async def _handle_announcement(self, event: DashboardEvent):
        """Store announcement into NCMS memory for future recall."""
        content = event.data.get("content", "")
        domains = event.data.get("domains", [])

        await self._http_post(f"{self.hub}/api/v1/memories", {
            "content": f"[announcement from {event.data.get('from_agent')}] {content}",
            "memory_type": "event",
            "domains": domains,
            "source_agent": self.agent_id,
            "importance": 7.0,
        })
```

The sidecar runs as a background process (supervisor/systemd) in each sandbox. It's independent of OpenClaw — OpenClaw uses MCP tools for explicit store/recall, the sidecar handles the real-time bus.

### 4. OpenClaw MCP Config — Per Sandbox

Each sandbox has OpenClaw configured with NCMS MCP pointing to the Hub:

```json
{
  "mcpServers": {
    "ncms": {
      "type": "url",
      "url": "http://ncms-hub:8080/mcp"
    }
  }
}
```

OpenClaw uses MCP tools (`recall_memory`, `store_memory`, `search_memory`, `announce_knowledge`) for **explicit** agent-initiated operations. The bus sidecar handles **reactive** operations (incoming questions, announcements pushed to the agent).

### 5. Network Policy — Per Sandbox

Each sandbox only needs access to the NCMS Hub:

```yaml
version: 1

filesystem_policy:
  include_workdir: true
  read_only: [/usr, /lib, /proc, /dev/urandom, /app, /etc, /var/log]
  read_write: [/sandbox, /tmp, /dev/null]
  landlock:
    compatibility: best_effort

process:
  run_as_user: sandbox
  run_as_group: sandbox

network_policies:
  ncms_hub:
    name: ncms_hub
    endpoints:
      - host: ncms-hub          # Docker network name or host.docker.internal
        port: 8080
        protocol: rest
        enforcement: enforce
        access: full
    binaries:
      - path: /usr/local/bin/claude
      - path: /usr/bin/curl
      - path: /usr/bin/node
      - path: /usr/bin/python3   # For bus sidecar

  claude_code:
    name: claude_code
    endpoints:
      - host: api.anthropic.com
        port: 443
        protocol: rest
        tls: terminate
        enforcement: enforce
        access: full
    binaries:
      - path: /usr/local/bin/claude
      - path: /usr/bin/node

  inference:
    name: inference
    endpoints:
      - host: spark-ee7d.local
        port: 8000
        protocol: rest
        enforcement: enforce
        access: full
    binaries:
      - path: /usr/local/bin/claude
      - path: /usr/bin/curl
      - path: /usr/bin/node
```

## Data Flow: Complete Scenarios

### Scenario A: Builder Asks Architect a Question

```
Builder sandbox                    NCMS Hub                        Architect sandbox
     │                                │                                 │
     │ [OpenClaw reads builder.md]    │                                 │
     │ [Skill says: consult arch]     │                                 │
     │                                │                                 │
     │ MCP: ask_knowledge_sync        │                                 │
     │ q: "What auth pattern?"        │                                 │
     │ domains: ["architecture"]      │                                 │
     │──────── HTTP POST ───────────→ │                                 │
     │                                │                                 │
     │                                │ AsyncKnowledgeBus.ask()         │
     │                                │ → finds architect registered    │
     │                                │   for "architecture" domain     │
     │                                │ → calls RemoteAskHandler        │
     │                                │                                 │
     │                                │ ──── SSE push ────────────────→ │
     │                                │ {type: "bus.ask.routed",        │
     │                                │  ask_id: "q-123",              │
     │                                │  question: "What auth..."}     │
     │                                │                                 │
     │        (HTTP request held      │              [Bus sidecar       │
     │         open, waiting)         │               receives event]   │
     │                                │                                 │
     │                                │              [Calls recall_memory│
     │                                │               on NCMS Hub]      │
     │                                │ ←─── HTTP POST /memories/recall │
     │                                │ ───→ {results: [...ADR-003...]} │
     │                                │                                 │
     │                                │              [Synthesizes answer]│
     │                                │                                 │
     │                                │ ←── HTTP POST /bus/respond ──── │
     │                                │ {ask_id: "q-123",              │
     │                                │  answer: "Per ADR-003, use     │
     │                                │  JWT+RBAC with refresh tokens"} │
     │                                │                                 │
     │                                │ RemoteAskHandler.resolve()      │
     │                                │ → Future completes              │
     │                                │ → response returns to bus       │
     │                                │ → bus returns to BusService     │
     │                                │                                 │
     │ ← HTTP 200 ────────────────── │                                 │
     │ {answer: "Per ADR-003...",     │                                 │
     │  from_agent: "architect",      │                                 │
     │  source_mode: "live",          │                                 │
     │  confidence: 0.85}             │                                 │
     │                                │                                 │
     │ [OpenClaw stores decision]     │                                 │
     │ MCP: store_memory              │                                 │
     │──────── HTTP POST ───────────→ │                                 │
```

### Scenario B: Breaking Change Announcement

```
Builder sandbox                    NCMS Hub                     Architect + Security
     │                                │                              │
     │ MCP: announce_knowledge        │                              │
     │ "BREAKING: User API v1→v2"     │                              │
     │ domains: ["architecture",      │                              │
     │           "security"]          │                              │
     │──────── HTTP POST ───────────→ │                              │
     │                                │                              │
     │ ← 200 (ack)                    │ AsyncKnowledgeBus.announce() │
     │                                │ → subscription filter match  │
     │                                │ → fan-out to architect +     │
     │                                │   security inboxes           │
     │                                │                              │
     │                                │ ── SSE push ───────────────→ │ Architect sidecar
     │                                │ {type: "bus.announce",       │ receives, calls
     │                                │  from: "builder",            │ store_memory()
     │                                │  content: "BREAKING:..."}    │ to persist it
     │                                │                              │
     │                                │ ── SSE push ───────────────→ │ Security sidecar
     │                                │ {type: "bus.announce",       │ receives, calls
     │                                │  content: "BREAKING:..."}    │ store_memory(),
     │                                │                              │ triggers review
     │                                │                              │
     │                                │ EventLog emits:              │
     │                                │ "bus.announce" event         │
     │                                │ → Dashboard SSE → UI updates │
```

### Scenario C: Agent Goes to Sleep → Surrogate Response

```
Security sandbox                   NCMS Hub                     Builder sandbox
     │                                │                              │
     │ [Agent done, going to sleep]   │                              │
     │ MCP: sleep (publish snapshot)  │                              │
     │──────── HTTP POST ───────────→ │                              │
     │                                │ SnapshotService stores       │
     │                                │ BusService: status="sleeping"│
     │ [Sidecar disconnects SSE]      │ RemoteAskHandler removed     │
     │                                │                              │
     │                                │     ... time passes ...      │
     │                                │                              │
     │                                │ ←──── HTTP POST /bus/ask ─── │
     │                                │ "Is endpoint SOC2 compliant?"│
     │                                │ domains: ["security"]        │
     │                                │                              │
     │                                │ AsyncKnowledgeBus.ask()      │
     │                                │ → no live handler for        │
     │                                │   "security" domain          │
     │                                │ → returns no response        │
     │                                │                              │
     │                                │ BusService.ask_sync()        │
     │                                │ → timeout, no live response  │
     │                                │ → _try_surrogate()           │
     │                                │ → finds security snapshot    │
     │                                │ → keyword match on question  │
     │                                │                              │
     │                                │ ────── HTTP 200 ───────────→ │
     │                                │ {answer: "SOC2 requires...", │
     │                                │  source_mode: "warm",        │
     │                                │  from_agent: "security",     │
     │                                │  confidence: 0.6,            │
     │                                │  snapshot_age_seconds: 3600} │
```

## What We Need to Build

### Hub Side (NCMS Core)

| Component | What | Scope | Touches |
|-----------|------|-------|---------|
| `interfaces/http/bus_api.py` | HTTP Bus endpoints (register, ask, respond, announce, subscribe SSE) | ~300 lines, new file | Starlette routes |
| `infrastructure/bus/remote_handler.py` | `RemoteAskHandler` — SSE push + Future wait | ~80 lines, new file | None (standalone) |
| `interfaces/http/dashboard.py` | Mount bus_api routes alongside existing dashboard routes | ~10 lines edit | Route list |
| `interfaces/cli/main.py` | Ensure `ncms serve --transport http` starts both MCP + Bus API | ~5 lines edit | CLI |

**Zero changes to**: `AsyncKnowledgeBus`, `EventLog`, `BusService`, `MemoryService`, `SnapshotService`, domain models, protocols, scoring. The existing bus is transport-agnostic — we're adding a transport, not changing the bus.

### Sandbox Side

| Component | What | Scope |
|-----------|------|-------|
| `interfaces/cli/bus_agent.py` | `ncms bus-agent` CLI command — SSE client + question handler + announcement handler | ~200 lines, new file |
| Custom sandbox image | Dockerfile extending `openclaw:latest` with `pip install ncms` + sidecar as supervised process | ~40 lines |
| Sandbox policy | Network policy allowing only `ncms-hub:8080` + inference + Claude API | Already exists |

### Deployment

| Component | What | Scope |
|-----------|------|-------|
| `docker-compose.nemoclaw.yaml` | 4-service compose: ncms-hub + 3 sandbox containers | ~80 lines |
| `deployment/nemoclaw-blueprint/sandbox-image/Dockerfile` | OpenClaw + ncms + bus sidecar | ~30 lines |
| Updated skills | One skill per sandbox (already exist: architect.md, security.md, builder.md) | No change |
| Knowledge bootstrap | Load governance-mesh files into Hub at startup | Existing `ncms load` CLI |

## Docker Compose Layout

```yaml
services:
  # ── NCMS Hub ──────────────────────────────────────────
  ncms-hub:
    image: ncms:latest
    command: ncms serve --transport http --port 8080 --host 0.0.0.0
    environment:
      # Retrieval features
      NCMS_SPLADE_ENABLED: "true"
      NCMS_EPISODES_ENABLED: "true"
      NCMS_INTENT_CLASSIFICATION_ENABLED: "true"
      NCMS_RERANKER_ENABLED: "true"
      # LLM features → DGX Spark (fully local, no Anthropic)
      NCMS_LLM_MODEL: openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
      NCMS_LLM_API_BASE: http://spark-ee7d.local:8000/v1
      NCMS_CONSOLIDATION_KNOWLEDGE_ENABLED: "true"
      NCMS_CONSOLIDATION_KNOWLEDGE_MODEL: openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
      NCMS_CONSOLIDATION_KNOWLEDGE_API_BASE: http://spark-ee7d.local:8000/v1
      NCMS_CONTRADICTION_DETECTION_ENABLED: "true"
    ports:
      - "8080:8080"    # MCP + Bus API
      - "8420:8420"    # Dashboard
    volumes:
      - ncms-data:/app/data
      - ./knowledge:/app/knowledge  # Governance-mesh files
    healthcheck:
      test: curl -f http://localhost:8080/api/v1/health
      interval: 30s
      timeout: 10s
      retries: 5

  # ── Architect Agent ────────────────────────────────────
  architect:
    image: ncms-openclaw:latest
    environment:
      NCMS_HUB_URL: http://ncms-hub:8080
      NCMS_AGENT_ID: architect
      NCMS_AGENT_DOMAINS: "architecture,calm-model,quality,decisions"
      # Inference → DGX Spark (no Anthropic API key needed)
      INFERENCE_BASE_URL: http://spark-ee7d.local:8000/v1
      INFERENCE_MODEL: nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
    depends_on:
      ncms-hub:
        condition: service_healthy
    volumes:
      - ./skills/architect:/sandbox/.agents/skills/architect

  # ── Security Agent ─────────────────────────────────────
  security:
    image: ncms-openclaw:latest
    environment:
      NCMS_HUB_URL: http://ncms-hub:8080
      NCMS_AGENT_ID: security
      NCMS_AGENT_DOMAINS: "security,threats,compliance,controls"
      INFERENCE_BASE_URL: http://spark-ee7d.local:8000/v1
      INFERENCE_MODEL: nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
    depends_on:
      ncms-hub:
        condition: service_healthy
    volumes:
      - ./skills/security:/sandbox/.agents/skills/security

  # ── Builder Agent ──────────────────────────────────────
  builder:
    image: ncms-openclaw:latest
    environment:
      NCMS_HUB_URL: http://ncms-hub:8080
      NCMS_AGENT_ID: builder
      NCMS_AGENT_DOMAINS: "identity-service,implementation"
      INFERENCE_BASE_URL: http://spark-ee7d.local:8000/v1
      INFERENCE_MODEL: nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
    depends_on:
      ncms-hub:
        condition: service_healthy
    volumes:
      - ./skills/builder:/sandbox/.agents/skills/builder

volumes:
  ncms-data:
```

## Startup Sequence

```
1. ncms-hub starts
   ├── SQLite + Tantivy + SPLADE + GLiNER initialize
   ├── MCP HTTP server on :8080
   ├── Bus HTTP API on :8080/api/v1/bus/*
   ├── Dashboard on :8420
   └── Knowledge loaded (governance-mesh files)

2. architect sandbox starts
   ├── OpenClaw initializes with architect.md skill
   ├── Bus sidecar starts: POST /bus/register {agent_id: "architect", domains: [...]}
   ├── Hub creates RemoteAskHandler for architect
   ├── Sidecar connects SSE: GET /bus/subscribe?agent_id=architect
   └── Ready — architect can answer questions about architecture

3. security sandbox starts (same pattern)

4. builder sandbox starts
   ├── OpenClaw initializes with builder.md skill
   ├── Bus sidecar starts + registers
   └── Ready — user connects via: nemoclaw builder connect → openclaw tui

5. User triggers work:
   > "Design the imdb-identity-service"
   ├── OpenClaw reads builder.md skill
   ├── Skill says: consult Architecture first
   ├── MCP: ask_knowledge_sync(domains=["architecture"]) → NCMS Hub → SSE → Architect sidecar → response
   ├── Skill says: consult Security
   ├── MCP: ask_knowledge_sync(domains=["security"]) → NCMS Hub → SSE → Security sidecar → response
   ├── Agent makes design decisions, stores via MCP: store_memory
   └── Agent announces via MCP: announce_knowledge → Hub → SSE → all agents notified
```

## What Stays the Same

| Component | Status |
|-----------|--------|
| `AsyncKnowledgeBus` | **Unchanged** — domain routing, ask handlers, announce fanout, subscriptions, inboxes |
| `EventLog` | **Unchanged** — ring buffer, SSE subscribers, emit/subscribe, `to_sse()` |
| `BusService` | **Unchanged** — `ask_sync()` with surrogate fallback, `announce()`, registration |
| `MemoryService` | **Unchanged** — store/search/recall pipeline |
| `SnapshotService` | **Unchanged** — sleep/wake/surrogate cycle |
| MCP Tools | **Unchanged** — 15 tools already defined, work over HTTP transport |
| Dashboard | **Unchanged** — already consumes EventLog SSE, shows bus events, agent status |
| Skills | **Unchanged** — architect.md, security.md, builder.md already written |
| Domain models | **Unchanged** — KnowledgeAsk, KnowledgeResponse, KnowledgeAnnounce, AgentInfo |

## Inference: Fully Local, No Anthropic

A core design requirement: **zero outbound calls to Anthropic**. All LLM inference routes through the local DGX Spark running Nemotron 3 Nano via vLLM.

### Inference Topology

```
┌──────────────────────────────────────────────────────────────────────┐
│  DGX Spark (spark-ee7d.local)                                        │
│                                                                      │
│  vLLM serving Nemotron 3 Nano 30B (A3B, BF16)                       │
│  OpenAI-compatible API on :8000/v1                                   │
│  Context: 32K tokens                                                 │
│                                                                      │
│  Used by:                                                            │
│    ├── OpenClaw agents (reasoning, skill execution)                  │
│    ├── NCMS Hub (consolidation, contradiction detection)             │
│    └── Bus sidecar (answer synthesis for questions)                  │
└──────────────────────────────────────────────────────────────────────┘
```

### Configuration Per Component

**OpenClaw (in each sandbox)** — Uses `openshell inference set` or provider config:
```bash
# OpenShell inference provider pointing to DGX Spark
openshell inference set --provider openai --model nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
# Or via NemoClaw blueprint provider config
```

OpenClaw natively supports OpenAI-compatible endpoints. The DGX Spark vLLM server exposes exactly this API at `http://spark-ee7d.local:8000/v1`.

**NCMS Hub** — Environment variables (already supported):
```bash
NCMS_LLM_MODEL=openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
NCMS_LLM_API_BASE=http://spark-ee7d.local:8000/v1
NCMS_CONSOLIDATION_KNOWLEDGE_MODEL=openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
NCMS_CONSOLIDATION_KNOWLEDGE_API_BASE=http://spark-ee7d.local:8000/v1
```

**Bus sidecar (in each sandbox)** — When synthesizing answers to questions, calls the same endpoint via litellm (already a dependency of ncms).

### Inference Profiles (Switchable)

The Docker Compose supports multiple profiles. Switch by changing a single env var or compose profile:

| Profile | LLM Endpoint | Model | Use Case |
|---------|-------------|-------|----------|
| `spark` (default) | `http://spark-ee7d.local:8000/v1` | Nemotron 3 Nano 30B | Local DGX Spark |
| `ollama` | `http://host.docker.internal:11434` | `qwen3.5:35b-a3b` | Mac local (Ollama) |
| `nim-cloud` | `https://integrate.api.nvidia.com/v1` | Nemotron 3 Super 120B | NVIDIA NIM cloud |

All three profiles use **OpenAI-compatible APIs** — no Anthropic SDK, no `ANTHROPIC_API_KEY`. The `claude_code` network policy is only needed if you want to use Claude Code inside the sandbox as an alternative agent runtime; for NIM-only operation it can be removed.

### Network Policy (NIM-Only, No Anthropic)

```yaml
network_policies:
  ncms_hub:
    name: ncms_hub
    endpoints:
      - host: ncms-hub
        port: 8080
        protocol: rest
        enforcement: enforce
        access: full

  inference:
    name: inference
    endpoints:
      - host: spark-ee7d.local
        port: 8000
        protocol: rest
        enforcement: enforce
        access: full
```

Only two network policies needed: NCMS Hub for memory/bus, DGX Spark for inference. No Anthropic, no GitHub, no external calls.

## Build Order

| Phase | What | Effort | Depends On |
|-------|------|--------|------------|
| **1** | `RemoteAskHandler` + HTTP Bus API endpoints | 1-2 days | Nothing |
| **2** | `ncms bus-agent` CLI sidecar | 1-2 days | Phase 1 |
| **3** | Custom OpenClaw sandbox image (Dockerfile) | 1 day | Phase 2 |
| **4** | Docker Compose + startup orchestration | 1 day | Phase 3 |
| **5** | Integration test: 3 sandboxes + Hub end-to-end | 1 day | Phase 4 |
| **Total** | | **5-7 days** | |

Phase 1 can be tested immediately with curl/httpie against the Hub. Phase 2 can be tested with a simple Python script acting as a sandbox agent. Phases 3-4 bring in OpenShell/NemoClaw. Phase 5 is the full demo.

## Comparison to Previous Attempts

| Aspect | Previous (broken) | This Design |
|--------|-------------------|-------------|
| Containers | 1 monolith + K3s | 1 hub + 3 sandboxes (Docker Compose) |
| NCMS transport | Injected via crictl | HTTP + SSE (standard) |
| Agent runtime | Claude Code (no API key) | OpenClaw (handles auth) |
| Inter-agent comms | None (tried to trigger externally) | Knowledge Bus over SSE (real-time) |
| Bus architecture | Tried to bypass it | Uses existing AsyncKnowledgeBus unchanged |
| Event model | Existed but unused | EventLog SSE drives both dashboard + agents |
| Skills | Injected via crictl exec | Mounted as volumes |
| Breaking changes | Would be lost | SSE push to all subscribers immediately |
| Surrogate fallback | Not possible | Works — agent sleeps, snapshot responds |
| New code needed | ~1000 lines + fragile bash | ~600 lines (bus API + sidecar) |
