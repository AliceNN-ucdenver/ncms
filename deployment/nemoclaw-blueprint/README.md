# NCMS NemoClaw Blueprint

NemoClaw Blueprint that deploys NCMS Cognitive Memory System with 3 agent skills inside an OpenClaw sandbox.

## Prerequisites

- Docker (with BuildKit support)
- Access to an LLM endpoint: DGX Spark (default), Ollama (local), or NVIDIA NIM (cloud)
- Optional: HuggingFace token for SPLADE v3 model

## Quick Start

```bash
cd deployment/nemoclaw-blueprint

# Preview what will be deployed
python -m orchestrator.runner plan

# Build image and start container
python -m orchestrator.runner apply

# Check status
python -m orchestrator.runner status

# Access services
open http://localhost:8420    # Dashboard
curl http://localhost:8080/api/v1/health  # MCP HTTP API
```

## Profile Switching

Three inference profiles are available:

```bash
# DGX Spark — Nemotron 3 Nano 30B (default)
python -m orchestrator.runner apply --profile default

# Local Ollama — Qwen 3.5 35B MoE
python -m orchestrator.runner apply --profile ollama

# NVIDIA NIM — Llama 3.1 Nemotron 70B (cloud, requires NVIDIA_API_KEY)
python -m orchestrator.runner apply --profile nim
```

## Skills

Three agent skills are included, each defined as a Markdown file with YAML frontmatter:

| Skill | File | Purpose |
|-------|------|---------|
| Architect | `skills/architect/architect.md` | CALM models, ADRs, quality attributes, fitness functions |
| Security | `skills/security/security.md` | STRIDE threats, OWASP Top 10, NIST controls, compliance |
| Builder | `skills/builder/builder.md` | Drives the design work loop, consults Architect and Security |

Skills declare which MCP tools they use and which knowledge domains they operate in. The Builder agent orchestrates by consulting Architect and Security via the Knowledge Bus (`ask_knowledge_sync`).

## MCP Tools Available

The NCMS MCP server exposes these tools to agents:

| Tool | Description |
|------|-------------|
| `store_memory` | Persist knowledge with entity extraction |
| `search_memory` | BM25 + SPLADE + Graph hybrid search |
| `recall_memory` | Search with full context enrichment (episodes, states, causal chains) |
| `ask_knowledge_sync` | Blocking question to live agents or surrogates |
| `announce_knowledge` | Broadcast updates to all subscribed agents |
| `commit_knowledge` | Store session learnings |
| `get_provenance` | Trace memory origin and history |
| `list_domains` | Show registered knowledge domains |
| `get_snapshot` | Retrieve agent knowledge snapshots |
| `get_current_state` | Look up entity state |
| `get_state_history` | Temporal state transitions |
| `list_episodes` | List open/closed episodes |
| `get_episode` | Episode details with members |
| `delete_memory` | Remove a memory |
| `run_consolidation` | Trigger consolidation pass |

## Rollback

```bash
python -m orchestrator.runner rollback
```

## Building with SPLADE

To include the SPLADE v3 model (gated, requires HuggingFace license acceptance):

```bash
docker build -f Dockerfile --build-arg HF_TOKEN=hf_xxxx -t ncms-nemoclaw:latest ../..
```
