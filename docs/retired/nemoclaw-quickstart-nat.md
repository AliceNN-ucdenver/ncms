> **DEPRECATED** — This was an early version of the NAT integration guide.
> For the current multi-agent deployment with 5 agents, Tavily web search, native tool calling,
> and the PRD→Design document pipeline, see **[nemoclaw-nat-quickstart.md](nemoclaw-nat-quickstart.md)**.

# NCMS + NemoClaw + NeMo Agent Toolkit Quickstart (Archived)

Deploy a multi-agent system with persistent cognitive memory using NCMS as the shared memory backend, NemoClaw for sandbox isolation, and NVIDIA NeMo Agent Toolkit (NAT) for agent reasoning.

## Architecture

```
Host Machine
+--------------------------------------------------+
|  Docker                                          |
|  +--------------------------------------------+  |
|  | ncms-hub (:9080 API, :8420 Dashboard)      |  |
|  | BM25 + SPLADE retrieval, Knowledge Bus     |  |
|  +--------------------------------------------+  |
|  | phoenix (:6006 Tracing UI)                 |  |
|  +--------------------------------------------+  |
+--------------------------------------------------+

NemoClaw Sandboxes (OpenShell k3s)
+--------------------------------------------------+
| ncms-architect                                   |
|   NAT react_agent + auto_memory_agent            |
|   Loads: ADRs, CALM model, architecture docs     |
|   LLM: inference.local -> DGX Spark              |
+--------------------------------------------------+
| ncms-security                                    |
|   NAT react_agent + auto_memory_agent            |
|   Loads: OWASP, STRIDE, compliance docs          |
|   LLM: inference.local -> DGX Spark              |
+--------------------------------------------------+
| ncms-builder                                     |
|   NAT react_agent + auto_memory_agent            |
|   Tools: ask_knowledge, announce_knowledge       |
|   Consults architect + security before designing  |
|   LLM: inference.local -> DGX Spark              |
+--------------------------------------------------+
```

## Prerequisites

1. **Docker** installed and running
2. **NemoClaw** installed and onboarded:
   ```bash
   curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash
   ```
3. **Inference provider** configured (DGX Spark or Ollama):
   ```bash
   openshell provider create --name dgx-spark --type openai \
     --credential "OPENAI_API_KEY=dummy" \
     --config "OPENAI_BASE_URL=http://spark-ee7d.local:8000/v1"
   openshell inference set --no-verify --provider dgx-spark \
     --model nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
   ```
4. **HF_TOKEN** set for gated model downloads (SPLADE v3)

## Quick Start

```bash
cd deployment/nemoclaw-blueprint
./setup_nemoclaw.sh
```

This creates:
- NCMS Hub + Phoenix as Docker containers
- 3 NemoClaw sandboxes with NAT agents
- Each agent loads its domain knowledge into the shared NCMS Hub

## What the Setup Does

### Step 1: NCMS Hub (Docker)
- Builds and starts `ncms-hub` container (API :9080 + Dashboard :8420)
- Starts Phoenix tracing container (:6006)
- Hub provides BM25 + SPLADE retrieval and Knowledge Bus

### Step 2-4: Agent Sandboxes (NemoClaw)
For each agent (architect, security, builder):
1. Creates an OpenShell sandbox with network policy
2. Uploads NCMS source + NAT plugin + agent config + knowledge files
3. Installs dependencies: `uv sync` + `nvidia-nat-core` + `nvidia-nat-langchain`
4. Starts NAT agent: `nat start fastapi --config_file /sandbox/configs/<agent>.yml`

### Agent Startup Flow
When a NAT agent starts:
1. `NCMSMemoryEditor` registers with the NCMS Hub Knowledge Bus
2. Loads knowledge files from `/sandbox/knowledge/` into Hub memory via `store_memory()`
3. Starts SSE listener for bus announcements and routed questions
4. NAT FastAPI server starts on port 8000

## Network Policy

NemoClaw sandboxes are fully network-isolated. The policy at `policies/openclaw-sandbox.yaml` allows:

| Endpoint | Purpose | Format |
|----------|---------|--------|
| `host.docker.internal:9080` | NCMS Hub API + Bus | `access: full` + `allowed_ips` |
| `spark-ee7d.local:8000` | DGX Spark LLM inference | `access: full` + `allowed_ips` |
| `host.docker.internal:6006` | Phoenix tracing | `access: full` + `allowed_ips` |
| `pypi.org:443` | Python packages | `protocol: rest` + TLS |
| `huggingface.co:443` | Model downloads | `protocol: rest` + TLS |

**Important:** Private IP endpoints must use `access: full` + `allowed_ips` (not `protocol: rest`). This was the fix from NemoClaw issue #314.

**First run:** The `ncms-architect` sandbox may require one manual approval in `openshell term` for `host.docker.internal:9080`. Security and builder auto-approve.

## Agent Configurations

### Architect (`configs/architect.yml`)
- **Role:** Architecture expert, owns ADRs and CALM model
- **Domains:** architecture, calm-model, quality, decisions
- **Knowledge:** `/sandbox/knowledge/architecture/` (ADRs, fitness functions, quality attributes)
- **Tools:** announce_knowledge

### Security (`configs/security.yml`)
- **Role:** Security expert, owns OWASP and STRIDE threat models
- **Domains:** security, threats, compliance, controls
- **Knowledge:** `/sandbox/knowledge/security/` (threat model, security controls, compliance)
- **Tools:** announce_knowledge

### Builder (`configs/builder.yml`)
- **Role:** Builder, designs services by consulting experts
- **Domains:** identity-service, implementation
- **Knowledge:** None (learns by asking)
- **Tools:** ask_knowledge, announce_knowledge
- **Config:** `retrieve_memory_for_every_response: false` (forces tool use)

## Usage

### Dashboard
Open http://localhost:8420 to see:
- Agent status (3 connected agents)
- Memory count (loaded knowledge)
- Event feed (bus activity)
- **Agent Chat** — select an agent and ask questions

### Trigger Builder Design
Select **Builder** from the chat dropdown:
```
Design the imdb-identity-service. Consult the architecture and security agents before making any decisions.
```

### Direct API
```bash
# Ask via bus (routes to domain handler)
curl -X POST http://localhost:9080/api/v1/bus/ask \
  -H 'Content-Type: application/json' \
  -d '{"from_agent":"user","question":"What auth pattern should we use?","domains":["architecture"]}'

# Direct NAT agent call (full LLM reasoning)
ssh openshell-ncms-builder 'curl -s -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d "{\"input_message\": \"Design the imdb-identity-service.\"}"'
```

### Status & Teardown
```bash
./setup_nemoclaw.sh --status     # Show all services
./setup_nemoclaw.sh --teardown   # Remove everything
./setup_nemoclaw.sh --rebuild    # Teardown + full setup
```

## NCMS NAT Plugin (`nvidia-nat-ncms`)

The plugin at `packages/nvidia-nat-ncms/` provides:

| Component | Purpose |
|-----------|---------|
| `NCMSMemoryConfig` | Config: hub URL, agent ID, domains, knowledge paths |
| `NCMSMemoryEditor` | NAT MemoryEditor backed by NCMS recall/store |
| `NCMSHttpClient` | Async HTTP client for all NCMS Hub API endpoints |
| `sse_listener` | Background SSE consumer for bus questions + announcements |
| `ask_knowledge` | NAT tool: ask domain experts via Knowledge Bus |
| `announce_knowledge` | NAT tool: broadcast to subscribed agents |

### Plugin Registration
The plugin registers via entry points (`nat.plugins` group). NAT discovers it automatically when installed.

### Namespace Packages
The plugin uses implicit namespace packages. `src/nat/__init__.py` and `src/nat/plugins/__init__.py` must NOT exist — they would shadow the core `nat` package from `nvidia-nat-core`.

## Known Issues

1. **Architect sandbox approval** — first sandbox created may need manual approval in `openshell term`. Gateway restart clears stale state.
2. **Agent reconnect** — agents don't auto-re-register when hub restarts. Manual restart of NAT agents required.
3. **ReAct tool format** — Nemotron model sometimes outputs raw JSON instead of ReAct format, causing tool calls to be treated as direct answers.
4. **`nat serve` crash** — use `nat start fastapi` instead (dask_client error).
5. **MPS not available in Docker** — SPLADE runs on CPU only inside the hub container.
