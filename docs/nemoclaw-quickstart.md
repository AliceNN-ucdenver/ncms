# NCMS + NemoClaw Quick Start Guide

Get NCMS cognitive memory running inside NemoClaw in under 15 minutes.

## Quick Start: Blueprint Container (Recommended)

The NemoClaw Blueprint container includes NCMS + NemoClaw CLI + OpenShell + pre-downloaded models + 3 agent skills:

```bash
# Clone the repo
git clone https://github.com/AliceNN-ucdenver/ncms.git
cd ncms

# Build the blueprint image (~800 MB of ML models baked in)
docker build -f deployment/nemoclaw-blueprint/Dockerfile \
  -t ncms-nemoclaw:latest .

# Optional: include SPLADE v3 (gated model, needs HuggingFace token)
docker build -f deployment/nemoclaw-blueprint/Dockerfile \
  --build-arg HF_TOKEN=hf_xxxx -t ncms-nemoclaw:latest .
```

### Run with an LLM Backend

```bash
# DGX Spark (Nemotron 3 Nano 30B)
docker run -p 8420:8420 -p 8080:8080 \
  -e NCMS_LLM_MODEL=openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
  -e NCMS_LLM_API_BASE=http://spark-ee7d.local:8000/v1 \
  ncms-nemoclaw:latest

# Ollama on host Mac (Qwen 3.5 35B MoE)
docker run -p 8420:8420 -p 8080:8080 \
  -e NCMS_LLM_MODEL=ollama_chat/qwen3.5:35b-a3b \
  ncms-nemoclaw:latest

# NVIDIA NIM (cloud, needs API key)
docker run -p 8420:8420 -p 8080:8080 \
  -e NCMS_LLM_MODEL=openai/nvidia/llama-3.1-nemotron-70b-instruct \
  -e NCMS_LLM_API_BASE=https://integrate.api.nvidia.com/v1 \
  -e NVIDIA_API_KEY=nvapi-xxxx \
  ncms-nemoclaw:latest
```

- **Dashboard**: http://localhost:8420 (SSE events, D3 graph, time-travel replay)
- **MCP HTTP API**: http://localhost:8080

### Container Modes

```bash
# Default: MCP HTTP API + Dashboard
docker run -p 8420:8420 -p 8080:8080 ncms-nemoclaw:latest

# NemoClaw ND autonomous agent demo (3 LLM agents design imdb-identity-service)
docker run -it ncms-nemoclaw:latest demo

# MCP stdio server (pipe to OpenClaw)
docker run -i ncms-nemoclaw:latest mcp

# Interactive shell (NemoClaw + OpenClaw + NCMS all on PATH)
docker run -it -p 8420:8420 -p 8080:8080 ncms-nemoclaw:latest shell

# Inside the shell:
#   nemoclaw onboard               # NemoClaw setup wizard
#   openclaw tui                   # OpenClaw chat interface
#   uv run ncms demo --nemoclaw-nd # ND agent demo
```

### Blueprint Runner

Use the runner to manage the sandbox lifecycle (auto-detects OpenShell, falls back to Docker):

```bash
cd deployment/nemoclaw-blueprint

# Preview deployment plan
python orchestrator/runner.py plan --profile default

# Build + deploy container
python orchestrator/runner.py apply --profile default

# Check status
python orchestrator/runner.py status

# Rollback
python orchestrator/runner.py rollback --run-id <id>
```

### What's Inside

The blueprint image bundles:
- NCMS with all features enabled (BM25, SPLADE v3, GLiNER NER, cross-encoder reranking)
- Pre-downloaded models (~800 MB, no runtime network calls)
- NemoClaw CLI + OpenShell for sandbox management
- 3 OpenClaw agent skills (Architect, Security, Builder)
- HTTP REST API server (port 8080) with health endpoint
- Observability dashboard (port 8420) with SSE event stream + D3 graph + time-travel replay
- Knowledge Bus with surrogate responses for offline agents
- Security policy (filesystem + network allowlist)

### Persistent Data

Mount a volume to keep data across container restarts:

```bash
docker run -p 8420:8420 -p 8080:8080 \
  -v ncms-data:/app/data \
  ncms-nemoclaw:latest
```

---

## Alternative: All-in-One Docker (Legacy)

The simpler all-in-one image without NemoClaw/OpenShell:

```bash
docker build -f deployment/nemoclaw/Dockerfile.allinone -t ncms-allinone:latest .
docker run -p 8420:8420 -p 8080:8080 ncms-allinone:latest
```

---

## Manual Setup (Step by Step)

If you prefer to install each component individually, or want to integrate NCMS into an existing NemoClaw deployment:

## Prerequisites

### System Requirements

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 4 vCPU | 4+ vCPU |
| RAM | 8 GB | 16 GB |
| Disk | 20 GB free | 40 GB free |

> **Note:** The NemoClaw sandbox image is ~2.4 GB compressed. Systems with <8 GB RAM should configure at least 8 GB swap to avoid issues during image decompression.

### Software Dependencies

| Dependency | Version | Notes |
|-----------|---------|-------|
| Linux | Ubuntu 22.04+ | macOS via Colima/Docker Desktop; Windows via WSL |
| Node.js | 20+ | Required by NemoClaw/OpenClaw |
| npm | 10+ | |
| Python | 3.12+ | Required by NCMS |
| Docker | Latest | Or Colima on macOS |
| uv | Latest | Python package manager (`curl -LsSf https://astral.sh/uv/install.sh \| sh`) |

### Container Runtime

| Platform | Supported |
|----------|-----------|
| Linux | Docker |
| macOS (Apple Silicon) | Colima, Docker Desktop |
| macOS | Podman not yet supported |
| Windows WSL | Docker Desktop (WSL backend) |

## Step 1: Install NemoClaw

```bash
curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash
```

If using nvm or fnm, reload your shell:
```bash
source ~/.bashrc  # or source ~/.zshrc
```

Verify installation:
```bash
nemoclaw --version
```

## Step 2: Install NCMS

```bash
pip install ncms
# Or with uv:
uv pip install ncms
```

Verify:
```bash
ncms info
```

## Step 3: Configure NCMS as MCP Server

Create the OpenClaw MCP configuration to register NCMS:

```bash
mkdir -p ~/.openclaw
cat > ~/.openclaw/openclaw.json << 'EOF'
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
EOF
```

## Step 4: Add the NCMS Skill

The skill file teaches the OpenClaw agent how to use NCMS memory tools:

```bash
mkdir -p ~/.openclaw/skills/ncms-memory
cat > ~/.openclaw/skills/ncms-memory/SKILL.md << 'SKILLEOF'
---
name: ncms-memory
description: Cognitive memory system with hybrid retrieval, knowledge graph, and structured recall
version: 1.0.0
metadata:
  openclaw:
    always: true
    emoji: "\U0001F9E0"
    requires:
      bins: ["ncms"]
---

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
- "What's the current deployment status?" routes to ops agent
- "What auth middleware is in use?" routes to security agent

Call `announce_knowledge` to broadcast observations:
- "API latency increased 3x after deploy" fans out to subscribed agents

## Domains

Tag memories with domains to organize knowledge: `["backend", "auth", "ops"]`.
Use `list_domains` to see what domains exist and which agents provide them.

## At Session Start

1. Call `recall_memory` with a summary of your current task to load relevant context
2. Check for announcements with `list_domains`

## At Session End

Store any important findings or decisions before the session ends.
SKILLEOF
```

## Step 5: Create the Sandbox

Create a NemoClaw sandbox with NCMS pre-installed:

```bash
nemoclaw my-assistant connect
```

Inside the sandbox, verify NCMS is available:
```bash
sandbox@my-assistant:~$ ncms info
```

## Step 6: Start Using It

Launch the OpenClaw TUI:
```bash
sandbox@my-assistant:~$ openclaw tui
```

Or use the CLI:
```bash
sandbox@my-assistant:~$ openclaw agent --agent main --local \
  -m "Store this: Our API uses OAuth2 with JWT tokens for authentication" \
  --session-id test
```

The agent now has access to all 18 NCMS memory tools. Try asking it to recall something:
```bash
sandbox@my-assistant:~$ openclaw agent --agent main --local \
  -m "What do you remember about our authentication setup?" \
  --session-id test
```

## Quick Verification

Test NCMS tools directly (outside the sandbox, for debugging):

```bash
# Store a memory
ncms demo

# Or start the MCP server manually
ncms serve
```

## What's Available

Once configured, the agent has access to these NCMS tools:

| Tool | Purpose |
|------|---------|
| `store_memory` | Store knowledge with entity extraction and episode linking |
| `search_memory` | BM25 + SPLADE + Graph hybrid search |
| `recall_memory` | Structured recall with episode context, entity states, causal chains |
| `ask_knowledge_sync` | Ask other agents questions (live or surrogate response) |
| `announce_knowledge` | Broadcast observations to subscribed agents |
| `commit_knowledge` | Store knowledge learned during coding sessions |
| `get_provenance` | Trace a memory's origin and modification history |
| `list_domains` | Show registered knowledge domains and providers |
| `get_current_state` | Look up current state of an entity |
| `get_state_history` | View temporal chain of state transitions |
| `list_episodes` | List open/closed memory episodes |
| `get_episode` | View episode with all member fragments |
| `get_snapshot` | Retrieve agent's knowledge snapshot |
| `load_knowledge` | Import knowledge from files |
| `run_consolidation` | Execute dream cycle + consolidation pass |

## Optional: Custom Sandbox Image

For production deployments, pre-bake models into the sandbox image to avoid runtime downloads:

```dockerfile
FROM ghcr.io/nvidia/openshell-community/sandboxes/openclaw:latest

# Install NCMS
RUN pip install ncms

# Pre-download models (~1 GB total)
RUN python -c "from gliner import GLiNER; GLiNER.from_pretrained('urchade/gliner_medium-v2.1')"
RUN python -c "from sentence_transformers import SparseEncoder; SparseEncoder('naver/splade-v3')"
RUN python -c "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

# Copy config
COPY openclaw.json /sandbox/.openclaw/openclaw.json
COPY skills/ncms-memory/ /sandbox/skills/ncms-memory/
```

Build and use:
```bash
docker build -t ncms-openclaw:latest .
# Update NemoClaw blueprint to use custom image
```

## Optional: DGX Spark for LLM Features

NCMS features like consolidation and contradiction detection require an LLM. To use a DGX Spark:

Add to your OpenShell network policy (`openclaw-sandbox.yaml`):
```yaml
network_policies:
  dgx_spark:
    endpoints:
      - host: "spark-ee7d.local"
        port: 8000
        protocol: rest
        tls: passthrough
    authorized_binaries:
      - python3
```

Set NCMS environment variables:
```json
{
  "env": {
    "NCMS_CONSOLIDATION_KNOWLEDGE_ENABLED": "true",
    "NCMS_CONSOLIDATION_KNOWLEDGE_MODEL": "openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16",
    "NCMS_CONSOLIDATION_KNOWLEDGE_API_BASE": "http://spark-ee7d.local:8000/v1",
    "NCMS_DREAM_CYCLE_ENABLED": "true"
  }
}
```

Or route through OpenShell's inference gateway (recommended for production):
```json
{
  "env": {
    "NCMS_CONSOLIDATION_KNOWLEDGE_MODEL": "openai/nvidia/nemotron-3-super-120b",
    "NCMS_CONSOLIDATION_KNOWLEDGE_API_BASE": "http://localhost:18789/v1"
  }
}
```

## Optional: Multi-Agent Hub

For deployments with multiple agents sharing knowledge (requires Phase 3 HTTP API):

```bash
# Start the shared NCMS hub
ncms serve --transport http --port 8080 --host 0.0.0.0

# Each sandbox agent connects via HTTP
export NCMS_HUB_URL=http://ncms-hub.local:8080
```

Add network policy for hub access:
```yaml
network_policies:
  ncms_hub:
    endpoints:
      - host: "ncms-hub.local"
        port: 8080
        protocol: rest
    authorized_binaries:
      - python3
```

## Optional: Live Demo Dashboard

See all three agents collaborating in real-time:

```bash
# Launch the NemoClaw demo with dashboard
ncms demo --nemoclaw

# Or without NAT dependency
ncms demo --multi-agent
```

Open http://localhost:8095 to see:
- Agent lifecycle (live/sleeping/offline)
- Knowledge Bus messages flowing between agents
- Entity graph growing in real-time
- Episode formation and state reconciliation
- Surrogate responses when agents are offline
- Dream cycle rehearsal events

## Troubleshooting

### NCMS not found in sandbox
Ensure `ncms` is installed in the sandbox's Python environment. Check `which ncms` inside the sandbox.

### Models downloading at runtime
Pre-bake models into the sandbox image (see Custom Sandbox Image above) or set `NCMS_MODEL_CACHE_DIR=/sandbox/.ncms/models` and ensure network access for first-run download.

### Tantivy mmap errors
If seccomp blocks mmap syscalls, Tantivy may fail. Test inside the sandbox:
```bash
python -c "import tantivy; print('OK')"
```
If blocked, file an OpenShell issue — mmap is required for Tantivy's index.

### LLM calls timing out
Consolidation/contradiction LLM calls go through OpenShell's inference gateway by default. If using direct access to DGX Spark, ensure the network policy allows it. Increase timeout with `NCMS_LLM_TIMEOUT=60`.

### Slow SPLADE on CPU
SPLADE v3 runs on CPU inside sandboxes (~200ms/query vs ~50ms on GPU). For latency-sensitive deployments, disable SPLADE (`NCMS_SPLADE_ENABLED=false`) — BM25 + Graph still provides strong retrieval (nDCG@10=0.67 on SciFact).

## Next Steps

- Read the [Integration Design](nemoclaw-integration-design.md) for architecture details
- See the [NCMS paper](paper.md) for retrieval pipeline internals
- Explore [NVIDIA NemoClaw docs](https://docs.nvidia.com/nemoclaw/latest/) for sandbox configuration
- Review [NVIDIA Agent Toolkit docs](https://docs.nvidia.com/nemo/agent-toolkit/latest/) for A2A protocol and memory module integration

## Uninstall

Remove NemoClaw:
```bash
curl -fsSL https://raw.githubusercontent.com/NVIDIA/NemoClaw/refs/heads/main/uninstall.sh | bash
```

Flags: `--yes` (skip confirmation), `--keep-openshell` (retain OpenShell), `--delete-models` (remove Ollama models).

Remove NCMS:
```bash
pip uninstall ncms
rm -rf ~/.ncms  # Remove database and index
```
