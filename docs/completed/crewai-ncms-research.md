# NCMS + NeMo Agent Toolkit + NemoClaw Integration Research

> **Status: COMPLETE** (research concluded March 2026). Decision: NAT over CrewAI. Findings motivated the current nvidia-nat-ncms package and NemoClaw blueprint deployment.

Research conducted 2026-03-28 across multiple investigations.

**Decision: NeMo Agent Toolkit (NAT) over pure CrewAI** — NAT's `MemoryEditor` takes `query: str` (3 methods) vs CrewAI's `StorageBackend` takes `query_embedding: list[float]` (14 methods). NAT eliminates the embedding mismatch problem entirely and provides A2A protocol, profiling, MCP client/server, and NemoClaw-native integration.

Investigations:
1. CrewAI StorageBackend protocol (for comparison)
2. NemoClaw inference routing inside sandboxes
3. CrewAI + NemoClaw deployment patterns
4. NeMo Agent Toolkit (NAT) — memory, retriever, MCP, A2A, auto_memory_wrapper

---

## 1. CrewAI StorageBackend Protocol

**Module path:** `crewai.memory.storage.backend`

### The Protocol (exact source from crewAIInc/crewAI repo)

```python
from __future__ import annotations
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from crewai.memory.types import MemoryRecord, ScopeInfo

@runtime_checkable
class StorageBackend(Protocol):
    """Protocol for pluggable memory storage backends."""

    def save(self, records: list[MemoryRecord]) -> None: ...

    def search(
        self,
        query_embedding: list[float],
        scope_prefix: str | None = None,
        categories: list[str] | None = None,
        metadata_filter: dict[str, Any] | None = None,
        limit: int = 10,
        min_score: float = 0.0,
    ) -> list[tuple[MemoryRecord, float]]: ...

    def delete(
        self,
        scope_prefix: str | None = None,
        categories: list[str] | None = None,
        record_ids: list[str] | None = None,
        older_than: datetime | None = None,
        metadata_filter: dict[str, Any] | None = None,
    ) -> int: ...

    def update(self, record: MemoryRecord) -> None: ...
    def get_record(self, record_id: str) -> MemoryRecord | None: ...

    def list_records(
        self,
        scope_prefix: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[MemoryRecord]: ...

    def get_scope_info(self, scope: str) -> ScopeInfo: ...
    def list_scopes(self, parent: str = "/") -> list[str]: ...
    def list_categories(self, scope_prefix: str | None = None) -> dict[str, int]: ...
    def count(self, scope_prefix: str | None = None) -> int: ...
    def reset(self, scope_prefix: str | None = None) -> None: ...

    # Async variants
    async def asave(self, records: list[MemoryRecord]) -> None: ...

    async def asearch(
        self,
        query_embedding: list[float],
        scope_prefix: str | None = None,
        categories: list[str] | None = None,
        metadata_filter: dict[str, Any] | None = None,
        limit: int = 10,
        min_score: float = 0.0,
    ) -> list[tuple[MemoryRecord, float]]: ...

    async def adelete(
        self,
        scope_prefix: str | None = None,
        categories: list[str] | None = None,
        record_ids: list[str] | None = None,
        older_than: datetime | None = None,
        metadata_filter: dict[str, Any] | None = None,
    ) -> int: ...
```

### MemoryRecord Data Type

```python
class MemoryRecord:
    id: str                          # default: uuid4
    content: str                     # the actual text
    scope: str                       # hierarchical path like "/company/team/user" (default: "/")
    categories: list[str]            # tags
    metadata: dict[str, Any]         # arbitrary key-value
    importance: float                # 0.0-1.0 (default: 0.5)
    created_at: datetime
    last_accessed: datetime
    embedding: list[float] | None    # computed on save if not provided
    source: str | None               # provenance tracking
    private: bool                    # default: False
```

### ScopeInfo Data Type

```python
class ScopeInfo:
    path: str
    record_count: int
    categories: list[str]
    oldest_record: datetime | None
    newest_record: datetime | None
    child_scopes: list[str]
```

### How Embeddings Are Handled

**CrewAI handles embedding, NOT the backend.** The `Memory` class owns an embedder (default: OpenAI `text-embedding-3-small`, 1536 dims). The `EncodingFlow` pipeline embeds content in batch before calling `storage.save()`. The `MemoryRecord.embedding` field is populated before it reaches the backend.

For `search()`, the `Memory.recall()` method embeds the query and passes `query_embedding: list[float]` to the backend. The backend does pure vector similarity search — it never calls an embedder itself.

### How Memory Calls Into StorageBackend

From `unified_memory.py`, the `Memory` class stores the backend in `_storage: StorageBackend`:

1. **Save path:** `Memory.remember()` → `_encode_batch()` → `EncodingFlow` → `storage.save(records)` / `storage.update(record)` / `storage.delete(record_ids=...)`
2. **Recall path:** `Memory.recall()` → `storage.search(embedding, scope_prefix=..., categories=..., limit=..., min_score=0.0)`
3. **Delete path:** `Memory.forget()` → `storage.delete(scope_prefix=..., categories=..., record_ids=..., older_than=..., metadata_filter=...)`
4. **Update path:** `Memory.update()` → `storage.get_record(id)` then `storage.update(record)`
5. **Browse path:** `Memory.list_scopes()` → `storage.list_scopes()`, `Memory.info()` → `storage.get_scope_info()`, etc.
6. **Reset path:** `Memory.reset()` → `storage.reset(scope_prefix=...)`
7. **Last-accessed touch** (optional): `storage.touch_records([ids])` — called via `getattr` with fallback

### Optional/Non-Protocol Methods

The LanceDB implementation adds methods NOT in the protocol:
- `touch_records(record_ids: list[str])` — batch update `last_accessed`
- `optimize()` — compact LanceDB table
- `close()` — checked via `hasattr` in `Memory.close()`

### Lifecycle

No explicit `init`/`close` in the protocol. `Memory.close()` checks `hasattr(self._storage, "close")` and calls it if present.

### Instantiation

In `Memory.model_post_init()`, storage is resolved from the `storage` field:
- `"lancedb"` (default) → `LanceDBStorage()`
- `"qdrant-edge"` → `QdrantEdgeStorage()`
- Any other string → `LanceDBStorage(path=that_string)`
- Any object → used directly as the backend

### Existing Implementations

1. **LanceDBStorage** — default, vector DB
2. **QdrantEdgeStorage** — Qdrant with write-local/sync-central pattern

### Summary: All 14 Protocol Methods

| Method | Sync/Async | Return Type |
|--------|-----------|-------------|
| `save(records)` | sync | `None` |
| `search(query_embedding, ...)` | sync | `list[tuple[MemoryRecord, float]]` |
| `delete(...)` | sync | `int` (count deleted) |
| `update(record)` | sync | `None` |
| `get_record(record_id)` | sync | `MemoryRecord \| None` |
| `list_records(scope_prefix, limit, offset)` | sync | `list[MemoryRecord]` |
| `get_scope_info(scope)` | sync | `ScopeInfo` |
| `list_scopes(parent)` | sync | `list[str]` |
| `list_categories(scope_prefix)` | sync | `dict[str, int]` |
| `count(scope_prefix)` | sync | `int` |
| `reset(scope_prefix)` | sync | `None` |
| `asave(records)` | async | `None` |
| `asearch(query_embedding, ...)` | async | `list[tuple[MemoryRecord, float]]` |
| `adelete(...)` | async | `int` |

### Design Challenge: Dense Vectors vs BM25+SPLADE

CrewAI's `search()` takes `query_embedding: list[float]` — a dense vector. NCMS doesn't use dense vectors (BM25 + SPLADE sparse neural + graph spreading activation).

Options:
1. **Subclass `Memory`** to override `recall()` — intercept raw query text before embedding, pass to NCMS
2. **Store query text alongside** — use `metadata_filter` to pass original text
3. **Ignore embedding entirely** — accept it in `search()` but use NCMS retrieval internally

Option 1 is cleanest — NCMS's retrieval (BM25+SPLADE+Graph+ACT-R) is dramatically richer than vector similarity.

---

## 2. NemoClaw Inference Routing Inside Sandboxes

### The Core Mechanism: `inference.local`

Inside an OpenShell sandbox, all inference traffic goes to:

**`https://inference.local/v1`**

OpenShell intercepts this at the network layer and proxies to whichever upstream provider was configured on the host. The sandbox never sees raw API keys — credentials are stored on the host at `~/.nemoclaw/credentials.json`.

### `openshell inference set` Command

Host-side command that configures routing (can be changed at runtime):

```bash
openshell inference set --provider <provider-name> --model <model-name>
```

Examples:
```bash
openshell inference set --provider dgx-spark --model nvidia/nemotron-3-nano-30b-a3b
openshell inference set --provider nvidia-prod --model nvidia/nemotron-3-super-120b-a12b
openshell inference set --provider openai-api --model gpt-5.4
```

### Provider Registration

```bash
openshell provider create \
  --name "dgx-spark" \
  --type "openai" \
  --config "OPENAI_BASE_URL=http://spark-ee7d.local:8000/v1" \
  --credential "OPENAI_API_KEY=dummy"
```

Parameters:
- `--name`: Arbitrary identifier (e.g., `dgx-spark`, `ollama-local`)
- `--type`: Protocol (`openai`, `nvidia`, `anthropic`)
- `--config "OPENAI_BASE_URL=<url>"`: Upstream endpoint
- `--credential "OPENAI_API_KEY=<key>"`: API key (stays on host, never enters sandbox)

### Supported Provider Profiles

| Profile | Type | Endpoint | Credential | Experimental |
|---------|------|----------|------------|-------------|
| DGX Spark (vLLM) | `openai` | `http://spark-ee7d.local:8000/v1` | `OPENAI_API_KEY` (dummy) | Yes |
| Ollama | `openai` | `http://host.docker.internal:11434` | `OPENAI_API_KEY` (dummy) | No |
| NVIDIA NIM (cloud) | `nvidia` | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` | No |
| Local vLLM | `openai` | `http://localhost:8000/v1` | `OPENAI_API_KEY` (dummy) | Yes |

Experimental providers require `NEMOCLAW_EXPERIMENTAL=1`.

### The `host.openshell.internal` Hostname

For local inference (Ollama, vLLM), `localhost` resolves inside the sandbox's network namespace. OpenShell provides:

**`host.openshell.internal`** → resolves to the host machine's IP from inside the sandbox (192.168.65.254)

### Configuring Python Frameworks Inside a Sandbox

Any OpenAI-compatible client just points to `inference.local`:

**For litellm (which NCMS uses):**
```python
import litellm
response = litellm.completion(
    model="openai/nvidia/nemotron-3-nano-30b-a3b",
    api_base="https://inference.local/v1",
    api_key="dummy",
)
```

**For CrewAI (uses litellm internally):**
```python
from crewai import LLM
llm = LLM(
    model="openai/nvidia/nemotron-3-nano-30b-a3b",
    base_url="https://inference.local/v1",
    api_key="dummy",
)
```

**For LangChain:**
```python
from langchain_openai import ChatOpenAI
llm = ChatOpenAI(
    model="nvidia/nemotron-3-nano-30b-a3b",
    base_url="https://inference.local/v1",
    api_key="dummy",
)
```

The `api_key` can be `"dummy"` because the OpenShell gateway strips it and injects the real credential from the host.

### OpenClaw Configuration

OpenClaw reads `openclaw.json` inside the sandbox:

```json
{
  "models": {
    "providers": {
      "custom-local": {
        "baseUrl": "https://inference.local/v1",
        "apiKey": "${CUSTOM_LOCAL_API_KEY}",
        "api": "openai-completions",
        "models": [{
          "id": "your-model-id",
          "contextWindow": 128000,
          "maxTokens": 32000
        }]
      }
    }
  }
}
```

**Important:** If `ANTHROPIC_API_KEY` is present in the sandbox environment, OpenClaw silently defaults to Claude regardless of the configured model.

### Litellm Prefix Stripping

OpenShell uses bare model names (e.g., `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16`) while litellm requires provider prefixes (`openai/nvidia/...`). The entrypoint strips it:

```bash
CLEAN_MODEL=$(echo "$INFERENCE_MODEL" | sed 's|^openai/||; s|^ollama_chat/||')
```

### Known Issues

- **403 on POST**: Sandbox proxy blocks non-TLS POST to internal hosts even when policy allows. GET succeeds but POST returns 403. (Issue #314)
- **Missing routing**: `openshell inference set` validates config but may not create DNS/hosts/iptables routing in sandbox. (Issue #326)
- **WSL2**: Windows-hosted Ollama unreachable from NemoClaw sandboxes. (Issue #336)

---

## 3. CrewAI + NemoClaw Deployment

### Architecture Overview

CrewAI handles orchestration, NemoClaw provides infrastructure-level governance:
- **Flows**: Long-running process coordination
- **Crews**: Agent collaboration within a task
- **NemoClaw Sandboxes**: Security at the kernel level via Landlock, seccomp, netns

CrewAI developers can run their Crews inside NemoClaw sandboxes without code changes.

### Supervisor-Worker Pattern (Recommended)

- **Supervisor** receives task, decomposes into subtasks, dispatches to **workers**
- Each worker runs in its **own sandbox with its own YAML policy**
- Supervisor has read access to outputs but cannot access workers' tools
- All inter-agent communication routes through supervisor
- Compatible with both LangGraph and CrewAI

Mapping: Flow = process orchestration, Crew = team of agents, Agent = sandbox

### Single Sandbox vs Per-Agent

**Per-agent sandbox recommended for production.** Running multiple agents in one sandbox means shared policies — a compromised agent inherits all permissions (OWASP ASI01).

However, a single CrewAI Crew (multiple agents) in one sandbox is feasible for dev/testing since it runs as a single Python process.

### NeMo Agent Toolkit v1.5.0

Native integrations with 5 frameworks: LangChain, LlamaIndex, CrewAI, Semantic Kernel, Google ADK.

- **Package**: `nvidia-nat-crewai` (pip installable)
- **Agent Performance Primitives (APP)**: parallel execution, speculative branching, priority routing
- **Callback handlers**: profiler + monitoring for CrewAI workflows
- Works alongside existing frameworks — adds sandbox isolation without rewriting orchestration

### Key Repositories

- **[NVIDIA/NemoClaw](https://github.com/NVIDIA/NemoClaw)** — blueprints, policies, presets
- **[NVIDIA/NeMo-Agent-Toolkit](https://github.com/NVIDIA/NeMo-Agent-Toolkit)** — framework integrations (`agentiq_crewai`)
- **[crewAIInc/nvidia-demo](https://github.com/crewAIInc/nvidia-demo)** — official CrewAI + NVIDIA demo
- **[VoltAgent/awesome-nemoclaw](https://github.com/VoltAgent/awesome-nemoclaw)** — community presets/recipes
- **[NVIDIA NIM CrewAI Blueprint](https://build.nvidia.com/crewai/code-documentation-for-software-development)** — Code Documentation blueprint

### Deployment Path A: Native OpenShell

1. `openshell sandbox create --from openclaw --name <name> --policy <policy.yaml>`
2. Upload skills via `openshell sandbox upload`
3. Start NCMS bus agent sidecar inside sandbox
4. Configure inference via `openshell provider create` + `openshell inference set`

### Deployment Path B: Docker Compose (Fallback)

1. Hub container runs NCMS (API + Dashboard + Knowledge Bus)
2. Agent containers run bus-agent sidecars
3. Agents connect via Docker networking

### Installing CrewAI in Sandboxes

**Approach A: Bake into Docker image (reproducible):**
```dockerfile
RUN uv pip install crewai crewai-tools nvidia-nat-crewai
```

**Approach B: Install at runtime inside sandbox:**
```bash
sandbox_run "$sandbox_name" "pip install crewai crewai-tools nvidia-nat-crewai"
```

Requires PyPI policy endpoints (`pypi.org`, `files.pythonhosted.org`) in `openclaw-sandbox.yaml`.

---

## 4. CrewAI MCP Support (Native)

CrewAI has first-class MCP support. Two approaches:

### Approach 1: `mcps` field on Agent (recommended)

```python
from crewai.mcp import MCPServerHTTP, MCPServerStdio, MCPServerSSE

agent = Agent(
    role="Builder",
    mcps=[
        MCPServerHTTP(url="http://host.docker.internal:9080/mcp"),
        MCPServerStdio(command="uv", args=["run", "ncms", "serve"]),
        "https://some-mcp-server.com/mcp",  # string shorthand
    ]
)
```

### Approach 2: MCPServerAdapter (manual lifecycle)

```python
from crewai_tools import MCPServerAdapter
from mcp import StdioServerParameters

server_params = StdioServerParameters(command="python3", args=["servers/your_server.py"])
with MCPServerAdapter(server_params) as mcp_tools:
    agent = Agent(role="Worker", tools=mcp_tools)
```

### Tool Filtering

```python
from crewai.mcp import create_static_tool_filter

# Static
MCPServerHTTP(
    url="...",
    tool_filter=create_static_tool_filter(
        allowed_tool_names=["recall_memory", "store_memory"],
        blocked_tool_names=["reset_memory"]
    )
)

# Dynamic (context-aware)
def dynamic_filter(context, tool):
    if context.agent.role == "Builder":
        if "delete" in tool.get("name", "").lower():
            return False
    return True
```

### CrewBase Integration

```python
@CrewBase
class NCMSCrew:
    mcp_server_params = [
        {"url": "http://host.docker.internal:9080/mcp", "transport": "streamable-http"},
    ]
    mcp_connect_timeout = 60

    @agent
    def builder(self):
        return Agent(config=self.agents_config["builder"], tools=self.get_mcp_tools())

    @agent
    def architect(self):
        return Agent(config=self.agents_config["architect"],
                     tools=self.get_mcp_tools("recall_memory", "ask_knowledge_sync"))
```

**Key details:** Auto tool discovery, server-name-prefixed tool names prevent collisions, graceful degradation on connection failure, 30s default timeout. Limitation: only MCP tools are adapted, not prompts or resources.

Install: `uv add mcp` for DSL, `uv pip install 'crewai-tools[mcp]'` for MCPServerAdapter.

---

## 5. CrewAI Memory System

### Unified Memory API

```python
from crewai import Memory

memory = Memory(
    llm="gpt-4o-mini",
    embedder={"provider": "openai", "config": {"model_name": "text-embedding-3-small"}},
    recency_weight=0.3,
    semantic_weight=0.5,
    importance_weight=0.2,
    recency_half_life_days=30,
    consolidation_threshold=0.85,
    storage="lancedb",  # or StorageBackend instance
)

memory.remember("We chose PostgreSQL.")
matches = memory.recall("What database did we choose?")
memory.forget(scope="/project/old")
```

### Composite Scoring

```
composite = semantic_weight * similarity + recency_weight * decay + importance_weight * importance
```

Where decay is exponential with configurable half-life.

### Hierarchical Scopes

```python
agent_mem = memory.scope("/agent/researcher")
agent_mem.remember("Found papers")      # stored under /agent/researcher
agent_mem.recall("papers")              # searches only that subtree

view = memory.slice(scopes=["/agent/researcher", "/company/knowledge"], read_only=True)
matches = view.recall("security policies")  # searches both branches
```

### Integration with Crews

```python
crew = Crew(agents=[...], tasks=[...], memory=True)
# or
crew = Crew(agents=[...], tasks=[...], memory=Memory(storage=NCMSStorageBackend(...)))
```

### Events Emitted

`MemoryQueryStartedEvent`, `MemoryQueryCompletedEvent`, `MemorySaveStartedEvent`, `MemorySaveCompletedEvent`

---

## 6. CrewAI Inter-Agent Communication

CrewAI uses hub-and-spoke delegation, NOT peer-to-peer bus:

- **Within a Crew**: Task context passing (output of task A feeds into task B)
- **Across Crews (Flows)**: Shared `FlowState` mutations between `@listen` steps
- **Delegation**: Agents with `allow_delegation=True` hand tasks to other agents

This is fundamentally different from NCMS Knowledge Bus (async pub/sub with domain routing). For real-time notifications (architect announces breaking change), CrewAI has no native mechanism.

### Custom Tool Pattern for Bus Integration

```python
from crewai.tools import BaseTool

class AskKnowledgeTool(BaseTool):
    name = "ask_knowledge"
    description = "Ask a question to another agent domain"

    def _run(self, question: str, domains: list[str]) -> str:
        response = httpx.post(f"{hub_url}/api/v1/bus/ask", json={
            "from_agent": self.agent_id,
            "question": question,
            "domains": domains,
        })
        return response.json()["content"]
```

---

## 7. CrewAI Custom Memory Backend (Community Examples)

Community implementations of custom StorageBackend:
- **SurrealStorage** — SurrealDB v2 extending RAGStorage
- **QdrantStorage** — Qdrant vector DB
- **crewai-soul** — Markdown-based memory with semantic search
- **Mem0Storage** — External memory service integration

GitHub Issue #2278 (Custom Memory Storage) was closed as completed with PR #2280.

---

## 8. Network Policy Details (NemoClaw)

### Baseline Policy (from docs)

All endpoints use TLS (port 443). Deny-by-default:

| Policy | Endpoints | Binaries | Rules |
|--------|-----------|----------|-------|
| `claude_code` | `api.anthropic.com:443`, `statsig.anthropic.com:443`, `sentry.io:443` | `/usr/local/bin/claude` | All methods |
| `nvidia` | `integrate.api.nvidia.com:443`, `inference-api.nvidia.com:443` | `/usr/local/bin/claude`, `/usr/local/bin/openclaw` | All methods |
| `github` | `github.com:443` | `/usr/bin/gh`, `/usr/bin/git` | All methods |
| `github_rest_api` | `api.github.com:443` | `/usr/bin/gh` | GET, POST, PATCH, PUT, DELETE |
| `npm_registry` | `registry.npmjs.org:443` | `/usr/local/bin/openclaw`, `/usr/local/bin/npm` | GET only |
| `telegram` | `api.telegram.org:443` | Any binary | GET, POST on `/bot*/**` |

### Filesystem Policy

| Path | Access |
|------|--------|
| `/sandbox`, `/tmp`, `/dev/null` | Read-write |
| `/usr`, `/lib`, `/proc`, `/dev/urandom`, `/app`, `/etc`, `/var/log` | Read-only |

### Dynamic Policy Updates

- `openshell policy set <sandbox> --policy <file>` — hot-reload network policy
- `openshell term` — TUI for runtime approval of pending requests
- Approved rules auto-persist for that sandbox session

### Our Approved Rules (from testing 2026-03-22)

```
allow_host_docker_internal_9080 (private IP)
  host.docker.internal:9080
  Allowed IPs: 192.168.65.254
  Binaries: /sandbox/.uv/python/cpython-3.13.12-linux-aarch64-gnu/bin/python3.13, /usr/bin/curl
```

---

## 9. Verified Architecture (from testing 2026-03-22)

Successfully demonstrated:

- ✅ NCMS Hub as Docker container, agents in NemoClaw sandboxes
- ✅ Proxy policy for `host.docker.internal:9080` works (operator approval on first use)
- ✅ SSE long-lived connections flow through proxy
- ✅ Bus ask/respond end-to-end across sandbox boundaries
- ✅ 117 knowledge files loaded into hub memory
- ✅ 3/3 agents connected (architect, security, builder)
- ❌ `inference.local` routing not verified (known issue #326)
- ❌ OpenClaw used Anthropic API, not Spark (needed `openclaw` TUI, not `claude`)
- ❌ No MCP config for Claude/OpenClaw to use NCMS tools (fell back to curl)

---

---

## 10. NeMo Agent Toolkit (NAT) — The Chosen Path

### Why NAT Over Pure CrewAI

| Concern | CrewAI StorageBackend | NAT MemoryEditor |
|---------|----------------------|------------------|
| Methods to implement | 14 (sync + async) | 3 (`add_items`, `search`, `remove_items`) |
| Search input | `query_embedding: list[float]` (dense vector) | `query: str` (raw text) |
| Embedding handling | Backend must do vector similarity | Backend receives text — use any retrieval |
| Registration | Subclass Protocol, pass to Memory() | `@register_memory` decorator + YAML `_type` |
| Agent framework | CrewAI only | LangChain, CrewAI, LlamaIndex, Semantic Kernel, Google ADK |
| Inter-agent comms | None (sidecar needed) | A2A Protocol (Linux Foundation standard) |
| Observability | None built in | Profiler with Phoenix/OTel/Langfuse export |
| NemoClaw integration | Manual | Native (OpenShell runtime is part of NAT) |
| MCP support | `mcps=[]` on Agent | Bidirectional: MCP client + `nat mcp` server |

### NAT MemoryEditor Interface (from source)

```python
# packages/nvidia_nat_core/src/nat/memory/interfaces.py

class MemoryEditor(ABC):
    @abstractmethod
    async def add_items(self, items: list[MemoryItem]) -> None:
        """Insert multiple MemoryItems into memory."""

    @abstractmethod
    async def search(self, query: str, top_k: int = 5, **kwargs) -> list[MemoryItem]:
        """Retrieve items relevant to query. Takes RAW TEXT, not embeddings."""

    @abstractmethod
    async def remove_items(self, **kwargs) -> None:
        """Remove items by criteria."""
```

### NAT MemoryItem Model (from source)

```python
# packages/nvidia_nat_core/src/nat/memory/models.py

class MemoryItem(BaseModel):
    conversation: list[dict[str, str]] | None = None  # user/assistant messages
    tags: list[str] = []
    metadata: dict[str, Any] = {}
    user_id: str                    # required
    memory: str | None = None       # optional textual memory (used by auto_memory_wrapper)
    similarity_score: float | None = None
```

### NAT MemoryManager Interface (maps to NCMS consolidation)

```python
class MemoryManager(ABC):
    async def summarize(self) -> None: ...    # → NCMS episode consolidation (Phase 5A)
    async def reflect(self) -> None: ...      # → NCMS pattern detection (Phase 5C)
    async def forget(self, criteria) -> None: # → NCMS decay pass (Phase 8)
    async def merge(self, criteria) -> None:  # → NCMS reconciliation (Phase 2)
```

### NAT MemoryReader / MemoryWriter

```python
class MemoryReader(MemoryIOBase):
    async def retrieve(self, context: str, top_k: int = 5) -> list[MemoryItem]: ...

class MemoryWriter(MemoryIOBase):
    async def write(self, observation: str, context: str | None = None) -> list[MemoryItem]: ...
```

### Config Registration Pattern

```python
from nat.data_models.memory import MemoryBaseConfig

class NCMSMemoryConfig(MemoryBaseConfig, name="ncms_memory"):
    hub_url: str = "http://host.docker.internal:9080"
    agent_id: str = "default"
    domains: list[str] = []
```

### Factory Registration Pattern (from Redis implementation)

```python
from nat.cli.register_workflow import register_memory

@register_memory(config_type=NCMSMemoryConfig)
async def ncms_memory_client(config: NCMSMemoryConfig, builder: Builder):
    editor = NCMSMemoryEditor(config)
    # SSE connection, bus registration, etc.
    yield editor
    # Cleanup: deregister from bus, close SSE
```

### Auto-Memory Wrapper (wraps any agent with automatic memory)

The `auto_memory_agent` wraps any NAT agent (ReAct, ReWOO, Tool Calling, Router) to provide automatic memory without LLM tool invocation:

**Flow:** `user message → capture_user_message → memory_retrieve → inner_agent → capture_ai_response`

- `capture_user_message`: stores user input via `memory_editor.add_items()`
- `memory_retrieve`: calls `memory_editor.search(query=user_message)`, injects results as `SystemMessage`
- `inner_agent`: black-box agent (receives ChatRequest with memory context injected)
- `capture_ai_response`: stores agent output via `memory_editor.add_items()`

Config:
```yaml
memory:
  ncms_store:
    _type: ncms_memory
    hub_url: "http://host.docker.internal:9080"

functions:
  builder_agent:
    _type: react_agent
    llm_name: spark_llm
    tool_names: [ask_knowledge, announce_knowledge]

workflow:
  _type: auto_memory_agent
  inner_agent_name: builder_agent
  memory_name: ncms_store
  llm_name: spark_llm
  search_params:
    top_k: 10
```

Feature flags:
- `save_user_messages_to_memory: true` — auto-save user messages
- `retrieve_memory_for_every_response: true` — auto-retrieve context
- `save_ai_messages_to_memory: true` — auto-save AI responses

Multi-tenant isolation via `user_id` extracted from runtime context.

### NAT MCP Support (Bidirectional)

**MCP Client** — consume external MCP servers as NAT functions:
```yaml
functions:
  ncms_tools:
    _type: mcp_tool_wrapper
    uri: "http://host.docker.internal:9080/mcp"
```

**MCP Server** — publish NAT workflows as MCP tools:
```bash
nat mcp --config_file config.yml
```

### NAT A2A Protocol (Inter-Agent Communication)

Linux Foundation standard for distributed agent communication:
- **Agent Cards**: JSON metadata (capabilities, skills, content types)
- **A2A Client**: function group for invoking remote agent skills
- **A2A Server**: `nat a2a serve` publishes workflows as discoverable agents
- **Authentication**: OAuth 2.1 with JWT validation

Replaces NCMS Knowledge Bus for cross-sandbox communication. Within a single process, Knowledge Bus remains valuable for low-latency in-process pub/sub.

### NAT Profiler / Observability

- Token-level profiling from workflow → tool/agent calls
- Subject-Observer pattern with async telemetry exporters
- Integrations: Phoenix, Weave, Langfuse, OpenTelemetry, Elasticsearch
- Built-in sensitive data redaction
- `nvidia-nat-crewai` provides CrewAI-specific callback handlers

---

## 11. Recommended Architecture

```
NemoClaw Sandbox (per agent)
+------------------------------------------+
|  NeMo Agent Toolkit (NAT)                |
|  +------------------------------------+  |
|  |  Agent (ReAct / CrewAI / Custom)   |  |
|  |  auto_memory_agent wrapper         |  |
|  +------------------------------------+  |
|        |              |                   |
|  NCMSMemoryEditor   NAT MCP Client       |
|  (MemoryEditor)     (NCMS tools)         |
|        |              |                   |
|        +------+-------+                  |
|               |                           |
|     HTTP to NCMS Hub                      |
|     (host.docker.internal:9080)           |
|                                           |
|     LLM: inference.local → DGX Spark     |
|     A2A: cross-sandbox agent delegation   |
|     Profiler: Phoenix / OTel             |
+------------------------------------------+

NCMS Hub (Docker container, outside NemoClaw)
+------------------------------------------+
|  NCMS HTTP API (:9080)                   |
|  Dashboard (:8420)                       |
|  BM25 + SPLADE + Graph retrieval         |
|  Knowledge Bus (HttpBusTransport + SSE)  |
|  Entity extraction (GLiNER)              |
|  Episode linking, Consolidation          |
|  Dream cycles                            |
+------------------------------------------+
```

### Three Integration Paths (can combine)

1. **MCP bridge (zero plugin code)**: NAT MCP client → NCMS MCP server. All 15 NCMS tools available instantly via YAML config.

2. **Custom memory plugin (`nvidia-nat-ncms`)**: Implement `MemoryEditor` backed by NCMS `recall()`/`store()`. Gives native auto_memory_wrapper integration.

3. **Custom retriever plugin**: Implement NAT retriever backed by NCMS `search()`. For RAG workflows that need NCMS as the retrieval backend.

**Recommended: Path 2 (memory plugin) + Path 1 (MCP bridge for bus tools)**

- Memory plugin provides seamless auto_memory_wrapper integration
- MCP bridge provides `ask_knowledge_sync`, `announce_knowledge` as agent tools
- No custom sidecar needed — NCMSMemoryEditor handles SSE + HTTP internally

---

## 12. Resolved Design Questions

1. ~~**StorageBackend embedding mismatch**~~: **Resolved.** NAT MemoryEditor takes `query: str`, not `query_embedding`. No mismatch.
2. ~~**Sidecar role**~~: **Resolved.** NCMSMemoryEditor IS the sidecar. One class handles HTTP to hub + SSE notifications + bus registration.
3. **Notification latency**: SSE notifications stored as memories → agent discovers on next `search()` call from auto_memory_wrapper. For `retrieve_memory_for_every_response: true`, this is every turn. Acceptable for most use cases.
4. ~~**OpenClaw vs CrewAI**~~: **Resolved.** NAT supports both + more. Use ReAct agent or CrewAI agent inside NAT. Inference via `inference.local` → Spark.
5. **`inference.local` reliability**: Known issues (#326, #314). Fallback: use `host.docker.internal` with approved proxy rules (verified working 2026-03-22).
