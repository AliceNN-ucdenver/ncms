# NemoClaw + NCMS: Redesign Analysis

**Date**: 2026-03-22
**Status**: Proposal for morning review

## The Problem With Our Current Approach

We tried to run NCMS as a separate Docker container with OpenShell creating a K3s cluster container, then injecting skills and MCP config into the OpenClaw sandbox via `crictl exec`. This has several fundamental problems:

1. **Claude Code needs an ANTHROPIC_API_KEY** — The OpenClaw sandbox runs Claude Code as the agent runtime. Without an API key injected into the sandbox environment, Claude Code can't call the Anthropic API. We can't inject this via `crictl exec` (it needs to be an env var at container start).

2. **No autonomous trigger** — Even with the key, Claude Code's `-p` mode is one-shot: send a prompt, get a response, exit. There's no daemon mode. We were trying to manually trigger it from outside, which defeats the purpose.

3. **Rube Goldberg architecture** — Container A (ncms-nemoclaw) orchestrates Container B (K3s cluster) which runs Pod C (sandbox) which runs Claude Code, which calls back to Container A over `host.docker.internal`. Every layer adds fragility.

4. **NemoClaw is a deployment tool, not an orchestrator** — NemoClaw's job is: create a sandbox, configure inference, apply policies. It gives you a sandboxed environment with `openclaw tui` or `openclaw agent` inside. It doesn't orchestrate multi-agent workflows — that's the agent's job.

## What NemoClaw Actually Does (From the Docs)

NemoClaw is simple:
```
nemoclaw onboard          # One-time setup: create gateway + sandbox + inference
nemoclaw my-sandbox connect  # SSH into sandbox
# Inside:
openclaw tui              # Interactive chat (the primary interface)
openclaw agent -m "..."   # Single-shot CLI
```

The flow is: **human → `nemoclaw connect` → `openclaw tui` → chat with agent → agent uses skills/MCP tools**.

There IS a chat interface — that's by design. The quickstart literally says "use `openclaw tui`." Skills are triggered by the agent during conversation — the LLM reads skill descriptions and decides when to invoke their tools.

## The Right Design: Three Options

### Option A: NemoClaw Native (OpenClaw + Skills + NCMS MCP)
**Use NemoClaw as intended.** One sandbox, one OpenClaw agent, three skills.

```
┌─────────────────────────────────────────────────────────┐
│  OpenShell Sandbox (created by nemoclaw onboard)         │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  OpenClaw Agent (single LLM brain)                 │  │
│  │                                                    │  │
│  │  Skills:                                           │  │
│  │    /architect  → NCMS MCP: recall architecture     │  │
│  │    /security   → NCMS MCP: recall security         │  │
│  │    /builder    → NCMS MCP: recall + store + announce│  │
│  │                                                    │  │
│  │  MCP Server: ncms (stdio, in-process)              │  │
│  │    └── NCMS (SQLite + Tantivy + SPLADE + Graph)    │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  Inference: Nemotron via OpenShell gateway                │
└─────────────────────────────────────────────────────────┘

User interaction:
  nemoclaw ncms-assistant connect
  $ openclaw tui
  > "Design the identity service using /builder"
  (agent reads builder skill → consults /architect and /security → stores decisions → announces)
```

**Pros:**
- Uses NemoClaw exactly as NVIDIA intended
- Skills are the orchestration mechanism (skill markdown defines the work loop)
- NCMS runs in-process as stdio MCP (no HTTP, no network, no second container)
- Single sandbox, simple deployment
- `openclaw tui` is the interface — that's fine per user: "if we have to launch one of the interfaces and trigger a chat that is ok"

**Cons:**
- Single agent with multiple skills, not three separate agents
- No true multi-agent coordination (one LLM wearing three hats)
- Knowledge Bus not utilized (no separate agents to route to)
- Can't demonstrate surrogate responses or agent sleep/wake

**How it works:** Install `ncms` (pip) into the OpenClaw sandbox image. Configure NCMS as an MCP server in OpenClaw's config. Write three skills that teach the agent different personas. The agent reads the skill, calls NCMS MCP tools, and reasons over the governance-mesh knowledge.

### Option B: Python ND Agents (What Already Works) + NemoClaw Dashboard
**Keep the existing Python demo agents. Run them inside NemoClaw for the secure sandbox story.**

```
┌──────────────────────────────────────────────────────────────┐
│  OpenShell Sandbox                                            │
│                                                               │
│  ┌──────────────────────────────────────────────────────────┐│
│  │  NCMS (in-process Python)                                ││
│  │                                                          ││
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐               ││
│  │  │Architect │  │Security  │  │Builder   │               ││
│  │  │Agent     │  │Agent     │  │Agent     │               ││
│  │  │(Python)  │  │(Python)  │  │(Python)  │               ││
│  │  └────┬─────┘  └────┬─────┘  └────┬─────┘               ││
│  │       │              │              │                     ││
│  │  ┌────▼──────────────▼──────────────▼─────────────────┐  ││
│  │  │  Memory Service + Knowledge Bus + Snapshot Service  │  ││
│  │  │  (BM25 + SPLADE + Graph + SQLite + ACT-R)          │  ││
│  │  └────────────────────────────────────────────────────┘  ││
│  │                                                          ││
│  │  Dashboard: http://localhost:8420                         ││
│  │  MCP HTTP: http://localhost:8080                          ││
│  └──────────────────────────────────────────────────────────┘│
│                                                               │
│  Inference: Nemotron via OpenShell gateway                     │
└──────────────────────────────────────────────────────────────┘

User interaction:
  nemoclaw ncms-assistant connect
  $ uv run ncms demo --nemoclaw-nd
  (Builder autonomously consults Architect + Security via Knowledge Bus)
  (Dashboard shows real-time events at :8420)
```

**Pros:**
- Already works (`uv run ncms demo --nemoclaw-nd`)
- True multi-agent: 3 separate Python agents with independent LLM reasoning
- Full Knowledge Bus utilization (ask, respond, announce, surrogate)
- Dashboard shows real-time events
- Demonstrates every NCMS feature (episodes, entity states, reconciliation, surrogates)

**Cons:**
- Not using OpenClaw's agent runtime (Python agents, not OpenClaw skills)
- OpenClaw tui sits unused inside the sandbox
- The "NemoClaw" part is just the sandbox — agents are NCMS-native

**How it works:** Build a custom sandbox image with NCMS + Python deps installed. The entrypoint runs `uv run ncms demo --nemoclaw-nd` which starts all three agents, loads knowledge, runs the builder work loop, and shows results. The dashboard provides real-time visualization. OpenShell provides the security sandbox.

### Option C: Hybrid — OpenClaw TUI + NCMS MCP Hub (Recommended)
**OpenClaw for the chat interface + agent runtime. NCMS as shared MCP server. Three skills define agent personas. User triggers work via TUI.**

```
┌──────────────────────────────────────────────────────────────┐
│  OpenShell Sandbox                                            │
│                                                               │
│  ┌───────────────────────────────────────────────────────┐   │
│  │  OpenClaw Agent                                        │   │
│  │                                                        │   │
│  │  Skills (loaded from /sandbox/.agents/skills/):        │   │
│  │    architect.md — Architecture consultation persona     │   │
│  │    security.md  — Security review persona               │   │
│  │    builder.md   — Builder persona (drives work loop)    │   │
│  │                                                        │   │
│  │  MCP Servers:                                          │   │
│  │    ncms (stdio) → NCMS in-process                      │   │
│  │      Tools: recall_memory, store_memory, search_memory │   │
│  │              ask_knowledge_sync, announce_knowledge     │   │
│  │              list_domains, get_provenance               │   │
│  │                                                        │   │
│  │  Interface: openclaw tui                                │   │
│  │    User says: "Design the identity service"             │   │
│  │    Agent reads builder.md skill                         │   │
│  │    Agent calls recall_memory(domain="architecture")     │   │
│  │    Agent calls recall_memory(domain="security")         │   │
│  │    Agent calls store_memory(...) for decisions          │   │
│  │    Agent calls announce_knowledge(...) for final design │   │
│  └───────────────────────────────────────────────────────┘   │
│                                                               │
│  ┌───────────────────────────────────────────────────────┐   │
│  │  NCMS (in-process, stdio MCP)                          │   │
│  │  SQLite + Tantivy + SPLADE + GLiNER + NetworkX         │   │
│  │  Pre-loaded: governance-mesh knowledge (13 files)       │   │
│  └───────────────────────────────────────────────────────┘   │
│                                                               │
│  NCMS Dashboard: http://localhost:8420 (port-forwarded)       │
│  Inference: Nemotron via OpenShell gateway or local            │
└──────────────────────────────────────────────────────────────┘

User interaction:
  nemoclaw ncms-assistant connect
  $ openclaw tui
  > "Use the builder skill to design the imdb-identity-service.
     Consult architecture and security knowledge before making decisions."
```

**Pros:**
- Uses OpenClaw as NVIDIA intended (TUI is the interface)
- NCMS runs in-process (stdio MCP — no HTTP, no network hops, no second container)
- Skills teach the agent NCMS-aware behavior
- Knowledge is pre-loaded into NCMS at sandbox creation
- Dashboard can run alongside for observability
- Single sandbox, simple deployment
- User triggers work with a chat message — "that is ok" per your instruction
- Switchable inference providers via `openshell inference set`

**Cons:**
- Single LLM agent (not three separate agents with independent reasoning)
- Knowledge Bus ask/announce work but go to NCMS store, not to live separate agents
- No surrogate responses (no sleeping agents to fall back to)

**Trade-off:** Option C treats NCMS as a **cognitive memory backend** for OpenClaw rather than a multi-agent bus. The three "agents" are skill-defined personas, not independent processes. This is actually closer to how OpenClaw was designed — one agent, many skills.

## Recommendation: Start With C, Layer On B

### Phase 1: Option C (NemoClaw + OpenClaw + NCMS MCP)

Build a custom OpenClaw sandbox image that includes NCMS:

```dockerfile
FROM ghcr.io/nvidia/openshell-community/sandboxes/openclaw:latest

# Install NCMS
RUN pip install ncms

# Pre-download models (GLiNER, SPLADE, cross-encoder)
RUN python -c "from gliner import GLiNER; GLiNER.from_pretrained('urchade/gliner_medium-v2.1')"
RUN python -c "from sentence_transformers import SparseEncoder; SparseEncoder('naver/splade-v3')"
RUN python -c "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

# Copy skills
COPY skills/architect/ /sandbox/.agents/skills/architect/
COPY skills/security/ /sandbox/.agents/skills/security/
COPY skills/builder/ /sandbox/.agents/skills/builder/

# Copy knowledge files (pre-loaded into NCMS at first boot)
COPY knowledge/ /sandbox/.ncms/knowledge/

# OpenClaw MCP config: NCMS as stdio server
COPY openclaw-mcp.json /sandbox/.openclaw/mcp.json

# NCMS config
ENV NCMS_DB_PATH=/sandbox/.ncms/ncms.db
ENV NCMS_INDEX_PATH=/sandbox/.ncms/index
ENV NCMS_SPLADE_ENABLED=true
ENV NCMS_EPISODES_ENABLED=true
ENV NCMS_INTENT_CLASSIFICATION_ENABLED=true
ENV NCMS_RERANKER_ENABLED=true
```

The MCP config (`openclaw-mcp.json`):
```json
{
  "servers": {
    "ncms": {
      "command": "ncms",
      "args": ["serve"],
      "transport": "stdio"
    }
  }
}
```

Workflow:
1. `nemoclaw onboard` creates the sandbox with our custom image
2. `nemoclaw ncms-assistant connect` enters the sandbox
3. Knowledge auto-loads on first boot (or via `ncms load` commands)
4. `openclaw tui` — user chats with the agent
5. User says: "Design the identity service" — agent uses builder skill, calls NCMS MCP tools
6. Dashboard (port-forwarded) shows memory operations in real-time

### Phase 2: Option B as `ncms demo --nemoclaw-nd`

Keep the Python ND demo as a separate mode for demonstrating true multi-agent behavior (3 independent LLM agents, Knowledge Bus, surrogates). This can run inside the sandbox too:

```
nemoclaw ncms-assistant connect
$ uv run ncms demo --nemoclaw-nd
```

Or standalone without NemoClaw:
```
uv run ncms demo --nemoclaw-nd
```

This gives two demo paths:
- **Interactive**: `openclaw tui` → chat-driven, OpenClaw skills, NCMS MCP (Phase 1)
- **Autonomous**: `ncms demo --nemoclaw-nd` → headless, 3 Python agents, Knowledge Bus (Phase 2)

## Key Simplification: NCMS Runs INSIDE the Sandbox

The biggest mistake in our previous approach was running NCMS as a separate container and trying to connect via HTTP over `host.docker.internal`. The right answer from the design doc:

> **Phase 1: MCP Server Integration (immediate, no NCMS code changes)** — NCMS already works as an MCP server. This phase is configuration and deployment artifacts only.

NCMS installs via `pip install ncms`. It runs as an stdio MCP server. No Docker networking, no HTTP transport, no port mapping. Just:

```json
{"servers": {"ncms": {"command": "ncms", "args": ["serve"]}}}
```

The NCMS Dashboard can still run separately (port-forwarded from the sandbox) for observability, but the core memory system is in-process.

## What We Need to Build

### For Phase 1 (Option C):

1. **Custom sandbox image** — Dockerfile extending `openclaw:latest` with NCMS + models + skills + knowledge
2. **OpenClaw MCP config** — `mcp.json` declaring NCMS as stdio server
3. **Skills updated for OpenClaw format** — Add `metadata.openclaw` section with `always: true` and `requires.bins: ["ncms"]`
4. **Knowledge bootstrap script** — Runs `ncms load` for each governance-mesh file on first boot
5. **NemoClaw blueprint** — Blueprint YAML + policy for sandbox creation via `nemoclaw onboard`
6. **Dashboard port-forward** — Script or config to expose NCMS dashboard from sandbox

### For Phase 2 (existing, minor changes):

1. Already works: `uv run ncms demo --nemoclaw-nd`
2. Make it work inside sandbox: ensure Python deps available, paths correct
3. Optionally add dashboard integration so events flow to the port-forwarded dashboard

## Inference Provider Switching

Per the NemoClaw docs, inference switching is:
```
openshell inference set --provider nvidia-nim --model <model-id>
```

Our NCMS LLM features (consolidation, contradiction detection) should use the same inference routing. Inside the sandbox, we configure NCMS to use the OpenShell gateway as `api_base`:

```
NCMS_LLM_API_BASE=http://localhost:18789/v1  # OpenShell gateway
NCMS_LLM_MODEL=nvidia/nemotron-3-nano-30b-a3b
```

When the user switches inference providers via NemoClaw, both OpenClaw and NCMS automatically use the new model.

## Summary

| Aspect | Previous (broken) | Proposed (Option C) |
|--------|-------------------|---------------------|
| Architecture | 2 containers + K3s + crictl injection | 1 sandbox, NCMS in-process |
| NCMS transport | HTTP over host.docker.internal | stdio MCP (in-process) |
| Agent runtime | Claude Code (needs API key) | OpenClaw (handles auth) |
| Interface | None (tried to trigger externally) | `openclaw tui` (chat) |
| Skills | Injected via crictl exec | Baked into sandbox image |
| Knowledge | Loaded via CLI in entrypoint | Loaded at first boot or via CLI |
| Inference | Hardcoded provider config | NemoClaw inference switching |
| Dashboard | Separate container on :8420 | Port-forwarded from sandbox |
| Complexity | Very high | Low |
