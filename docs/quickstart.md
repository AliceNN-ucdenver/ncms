# NCMS Quickstart Guide

Everything you need to install, configure, and integrate NCMS into your agent workflow.

## Option A: Docker

Run the full NCMS stack — dashboard, HTTP API, and NemoClaw demo — with all ML models pre-baked in a single image.

### Prerequisites

- Docker Desktop installed
- HuggingFace account with access to naver/splade-v3 (gated model — accept license at https://huggingface.co/naver/splade-v3)
- HuggingFace token (https://huggingface.co/settings/tokens)

### Build

```bash
# Create .env with your HF token (for gated SPLADE model download)
echo "HF_TOKEN=hf_your_token_here" > .env

# Build the image (~789 MB of ML models baked in)
docker build -f deployment/nemoclaw/Dockerfile.allinone \
  --secret id=env,src=.env \
  -t ncms-nemoclaw:latest .
```

### Run

```bash
# With DGX Spark (default LLM endpoint)
docker run -p 8420:8420 -p 8080:8080 ncms-nemoclaw:latest

# With Ollama on host Mac
docker run -p 8420:8420 -p 8080:8080 \
  -e NCMS_LLM_MODEL=ollama_chat/qwen3.5:35b-a3b \
  -e NCMS_LLM_API_BASE=http://host.docker.internal:11434 \
  ncms-nemoclaw:latest

# With OpenAI
docker run -p 8420:8420 -p 8080:8080 \
  -e NCMS_LLM_MODEL=openai/gpt-4o-mini \
  -e OPENAI_API_KEY=sk-xxxx \
  ncms-nemoclaw:latest
```

### Access

- **Dashboard**: http://localhost:8420
- **MCP Server**: http://localhost:8080

### Note about secrets

All NCMS configuration has sensible defaults baked into the image. You only need environment variables for:
- `HF_TOKEN` — build-time only (SPLADE gated model)
- `OPENAI_API_KEY` — only if using OpenAI models
- `NCMS_LLM_MODEL` / `NCMS_LLM_API_BASE` — to override the default LLM endpoint

Other run modes:

```bash
docker run -p 8080:8080 ncms-nemoclaw:latest api        # HTTP API only
docker run -i ncms-nemoclaw:latest mcp                   # MCP server (stdio)
docker run -p 8420:8420 ncms-nemoclaw:latest dashboard   # Dashboard only
```

---

## Option B: Local Development

### Installation

```bash
# With uv (recommended)
uv add ncms

# With pip
pip install ncms

# With rich document support (DOCX, PPTX, PDF, XLSX)
pip install "ncms[docs]"

# With dashboard
pip install "ncms[dashboard]"
```

### Run the Demo

```bash
uv run ncms demo
```

The demo runs three collaborative agents through a complete lifecycle:

```
  Phase 0  Download architecture knowledge ("I know kung fu.")
  Phase 1  Three agents store domain knowledge
  Phase 2  Frontend agent asks API agent for endpoint specs (live response)
  Phase 3  API agent goes to sleep, frontend gets surrogate response from snapshot
  Phase 4  Database agent announces a breaking schema change
  Phase 5  Memory search shows ACT-R activation scoring in action
```

All in-process. All in-memory. Zero external dependencies. Under 10 seconds.

## Observability Dashboard

```bash
pip install "ncms[dashboard]"
uv run ncms dashboard
```

Opens a real-time web dashboard at `http://localhost:8420`:

- **Architecture Diagram Layout** &mdash; Central Knowledge Bus backbone with agent cards connected by animated flow lines
- **Per-Agent Activity Feeds** &mdash; Real-time SSE stream of asks, responses, announcements, and surrogate dispatches
- **Conversation Threading** &mdash; Click any activity item to see the full ask/response thread with confidence scores
- **Snapshot Badges** &mdash; Surrogate responses are visually distinguished from live agent responses

Use `--no-demo` for a blank canvas that observes your own agents.

---

## MCP Server

Start the MCP server:

```bash
uv run ncms serve
```

### Claude Code

Add to your `.claude/settings.json`:

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

### Claude Code Hooks

Add NCMS hooks to `.claude/settings.json` for automatic knowledge persistence:

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

### GitHub Copilot

Add to your `.github/copilot-hooks.json` (or workspace `copilot-hooks.json`):

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

**What happens:**
- `sessionStart`: Previous session knowledge loaded automatically (same as Claude Code)
- `sessionEnd`: Knowledge from the completed session committed to NCMS
- `postToolUse`: Incremental knowledge capture after each tool invocation

### Any MCP-Compatible Agent (Cursor, etc.)

Just connect to the MCP server:

```bash
uv run ncms serve
```

---

## MCP Tools & Resources

NCMS exposes 18 tools (+ 1 optional) and browsable resources via the Model Context Protocol:

| Tool | Description |
|------|-------------|
| `search_memory` | BM25 + SPLADE + Graph + CE scored search |
| `recall_memory` | Structured recall with episode/entity/causal context |
| `store_memory` | Store knowledge with automatic entity extraction and indexing |
| `delete_memory` | Remove a memory from store and all indexes |
| `ask_knowledge` | Non-blocking ask routed to live agents |
| `ask_knowledge_sync` | Blocking ask with surrogate fallback |
| `announce_knowledge` | Broadcast changes to subscribed agents |
| `commit_knowledge` | Store knowledge from a coding session |
| `get_provenance` | Trace a memory's origin and access history |
| `list_domains` | List all knowledge domains and providers |
| `get_snapshot` | Retrieve an agent's Knowledge Snapshot |
| `load_knowledge` | Import files into memory (Matrix download) |
| `get_current_state` | Look up current state of an entity |
| `get_state_history` | View temporal chain of state transitions |
| `list_episodes` | List open/closed memory episodes |
| `get_episode` | View episode with all member fragments |
| `watch_directory` | Watch directory for file changes with auto-domain classification |
| `stop_watch` | Stop a filesystem watcher |
| `run_consolidation` | *(optional)* Execute dream cycle + consolidation pass |

**Resources:** `ncms://status`, `ncms://domains`, `ncms://agents`, `ncms://graph/entities`

---

## NeMo Agent Integration

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

The `KnowledgeAgent` base class is designed to plug into NeMo Agent Toolkit's `MemoryEditor` and `MemoryManager` interfaces.

---

## Matrix-Style Knowledge Download

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

**Rich document formats** (install `ncms[docs]`): DOCX, PPTX, PDF, XLSX &mdash; powered by Microsoft's [MarkItDown](https://github.com/microsoft/markitdown).

```bash
# CLI version
uv run ncms load docs/architecture.md --domains arch platform
uv run ncms load design-deck.pptx --domains design
```

---

## Configuration Reference

All settings via environment variables with `NCMS_` prefix:

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `NCMS_DB_PATH` | `~/.ncms/ncms.db` | SQLite database path |
| `NCMS_INDEX_PATH` | `~/.ncms/index` | Tantivy index directory |
| `NCMS_MODEL_CACHE_DIR` | *(HF cache)* | Directory for downloaded models (GLiNER, SPLADE) |
| `NCMS_BUS_ASK_TIMEOUT_MS` | `5000` | Knowledge Bus ask timeout |
| `NCMS_SNAPSHOT_TTL_HOURS` | `168` | Snapshot expiry (default 7 days) |

### Retrieval & Scoring

| Variable | Default | Description |
|----------|---------|-------------|
| `NCMS_ACTR_DECAY` | `0.5` | Memory decay rate |
| `NCMS_ACTR_NOISE` | `0.25` | Activation noise (sigma) |
| `NCMS_ACTR_THRESHOLD` | `-2.0` | Retrieval activation threshold |
| `NCMS_SCORING_WEIGHT_BM25` | `0.6` | BM25 weight in combined score (tuned on SciFact) |
| `NCMS_SCORING_WEIGHT_ACTR` | `0.0` | ACT-R weight (activates after dream cycles) |
| `NCMS_SCORING_WEIGHT_SPLADE` | `0.3` | SPLADE weight in combined score (tuned on SciFact) |
| `NCMS_SCORING_WEIGHT_GRAPH` | `0.3` | Graph spreading activation weight |
| `NCMS_SCORING_WEIGHT_CE` | `0.7` | Cross-encoder weight when reranker active |

### Entity Extraction

| Variable | Default | Description |
|----------|---------|-------------|
| `NCMS_GLINER_MODEL` | `urchade/gliner_medium-v2.1` | GLiNER model for entity extraction |
| `NCMS_GLINER_THRESHOLD` | `0.3` | Minimum confidence score for entities |
| `NCMS_LABEL_DETECTION_MODEL` | `openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16` | LLM model for `ncms topics detect` (legacy — prefer the intent-slot SLM's topic head) |
| `NCMS_LABEL_DETECTION_API_BASE` | `http://spark-ee7d.local:8000/v1` | vLLM/OpenAI-compatible endpoint |

### Intent-Slot SLM (P2, ingest-side classifier)

Replaces the regex admission scorer, the state-change regex, the LLM topic labeller, and manual `Memory.domains` tagging with a single LoRA multi-head BERT classifier.  See [docs/p2-plan.md](p2-plan.md) and [docs/intent-slot-sprint-4-findings.md](intent-slot-sprint-4-findings.md) for the full design.

| Variable | Default | Description |
|----------|---------|-------------|
| `NCMS_DEFAULT_ADAPTER_DOMAIN` | *(none)* | 5-head SLM activation.  Set to a deployed adapter name (`software_dev` / `conversational` / `clinical`) to load the LoRA chain at startup so its admission / state_change / topic heads replace the regex paths at ingest time.  Unset → SLM stays dark. |
| `NCMS_SLM_CHECKPOINT_DIR` | *(none)* | Adapter artifact path override (e.g. `~/.ncms/adapters/software_dev/v9/`).  Three reference adapters ship pre-trained; train your own with `ncms adapters train --domain X --version v9`. |
| `NCMS_SLM_CONFIDENCE_THRESHOLD` | `0.3` | Per-head confidence floor.  Below this value the chain falls through to the next backend for that head. |
| `NCMS_SLM_POPULATE_DOMAINS` | `true` | Auto-append the topic head's output to `Memory.domains` — replaces manual domain tagging. |
| `NCMS_SLM_E5_FALLBACK_ENABLED` | `true` | Include the E5-small-v2 zero-shot classifier as a cold-start fallback when no adapter is available. |
| `NCMS_INTENT_SLOT_DEVICE` | *(auto)* | Device override for the SLM forward pass — `cuda` / `mps` / `cpu`.  Defaults to `NCMS_DEVICE` then auto-detect. |
| `NCMS_SLM_LATENCY_BUDGET_MS` | `200.0` | Soft latency budget; exceeding it logs a warning but does not block ingest. |

### SPLADE Neural Retrieval

| Variable | Default | Description |
|----------|---------|-------------|
| `NCMS_SPLADE_ENABLED` | `false` | Enable SPLADE sparse neural retrieval |
| `NCMS_SPLADE_MODEL` | `naver/splade-v3` | SPLADE model (sentence-transformers SparseEncoder) |
| `NCMS_SPLADE_TOP_K` | `50` | Number of SPLADE candidates per search |
| `NCMS_RERANKER_ENABLED` | `false` | Enable cross-encoder reranking (Phase 10) |
| `NCMS_RERANKER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Cross-encoder model |

### LLM Features (Opt-in)

| Variable | Default | Description |
|----------|---------|-------------|
| `NCMS_LLM_MODEL` | `openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16` | Model for contradiction detection |
| `NCMS_LLM_API_BASE` | `http://spark-ee7d.local:8000/v1` | vLLM/OpenAI-compatible endpoint |
| `NCMS_CONTRADICTION_DETECTION_ENABLED` | `false` | Enable contradiction detection at ingest |
| `NCMS_CONTRADICTION_CANDIDATE_LIMIT` | `5` | Max memories to check for contradictions |
| `NCMS_CONSOLIDATION_KNOWLEDGE_ENABLED` | `false` | Enable knowledge consolidation |
| `NCMS_CONSOLIDATION_KNOWLEDGE_MIN_CLUSTER_SIZE` | `3` | Min memories per entity cluster |
| `NCMS_CONSOLIDATION_KNOWLEDGE_MODEL` | `openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16` | LLM model for insight synthesis |
| `NCMS_CONSOLIDATION_KNOWLEDGE_API_BASE` | `http://spark-ee7d.local:8000/v1` | vLLM/OpenAI-compatible endpoint |
| `NCMS_CONSOLIDATION_KNOWLEDGE_MAX_INSIGHTS_PER_RUN` | `5` | Max insights per consolidation run |

---

## Local LLM Inference

All LLM features run against local models via [litellm](https://docs.litellm.ai/). Each feature has its own `MODEL` + optional `API_BASE` pair, so you can mix local and remote models.

### macOS with Ollama (Development)

```bash
brew install ollama
brew services start ollama
ollama pull qwen3.5:35b-a3b    # 35B MoE, 3B active — runs on 32GB+ Mac
```

```bash
export NCMS_LLM_MODEL=ollama_chat/qwen3.5:35b-a3b
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
export NCMS_CONSOLIDATION_KNOWLEDGE_API_BASE=http://localhost:8000/v1
export NCMS_CONSOLIDATION_KNOWLEDGE_MODEL=openai/meta-llama/Llama-3.2-3B-Instruct
```

vLLM requires Linux + CUDA. Use `api_base` to point at any OpenAI-compatible endpoint.

---

## Architecture

```
src/ncms/
  domain/           Pure models, protocols, scoring, entity extraction (zero deps)
  application/      Memory service, bus service, snapshot service, graph service, loader
  infrastructure/   SQLite, Tantivy, NetworkX, AsyncIO bus, LLM utilities
  interfaces/       MCP server, CLI, HTTP dashboard, agent base class, hooks
```

**Clean Architecture.** Domain layer has zero infrastructure dependencies. Every infrastructure component implements a Protocol interface. Swap SQLite for Postgres, NetworkX for Neo4j, or AsyncIO for Redis Pub/Sub &mdash; no application code changes.

**Embedded First.** Everything runs in-process with `pip install ncms`. No Redis. No vector database. Docker available for all-in-one deployment with pre-baked models. Scale up when you need to, not before.
