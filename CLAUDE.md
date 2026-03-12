# NCMS - Project Guide

## What Is This?

NeMo Cognitive Memory System (NCMS) — a Python library providing persistent cognitive memory for AI agents. Vector-free retrieval via BM25 + ACT-R scoring, an embedded knowledge bus for agent coordination, and snapshot-based surrogate responses when agents go offline.

## Toolchain

- **Runtime**: Python 3.12+ (managed by `uv`, auto-downloads)
- **Package manager**: `uv` (at `/Users/shawnmccarthy/.local/bin/uv`)
- **Build backend**: `uv_build` (declared in pyproject.toml)
- **Linting**: `ruff` (line-length 100, target py312)
- **Testing**: `pytest` + `pytest-asyncio` (asyncio_mode = "auto")
- **Type checking**: `mypy` (strict mode)

## Commands

```bash
uv sync                          # Install all deps
uv sync --extra docs             # Install with document support (DOCX/PPTX/PDF/XLSX)
uv run ncms demo                 # Run interactive demo (in-memory, no side effects)
uv run ncms serve                # Start MCP server
uv run ncms dashboard            # Start observability dashboard (web UI)
uv run ncms dashboard --no-demo  # Dashboard without auto-starting demo agents
uv run ncms info                 # Show system info
uv run ncms load <file>          # Load knowledge from file into memory store
uv run ncms topics set <domain> <labels...>  # Set entity labels for a domain
uv run ncms topics list [domain] # Show cached entity labels
uv run ncms topics detect <domain> <paths...>  # Auto-detect labels via LLM
uv run ncms topics clear <domain>  # Clear cached labels for a domain
uv run pytest tests/ -v          # Run all tests
uv run pytest tests/unit/ -v     # Unit tests only
uv run pytest tests/integration/ # Integration tests only
uv run ruff check src/           # Lint
uv run mypy src/                 # Type check

# Benchmarks (ablation study)
uv sync --group bench                                         # Install benchmark deps
uv run python -m benchmarks.run_ablation                      # Full ablation (~2h)
uv run python -m benchmarks.run_ablation --datasets scifact   # Single dataset (~30min)
uv run python -m benchmarks.run_ablation --llm-model ollama_chat/qwen3.5:35b-a3b  # + LLM configs
uv run ruff check benchmarks/                                 # Lint benchmark code
```

## Architecture (Clean Architecture)

```
src/ncms/
├── domain/           # Pure models, protocols, scoring — ZERO infrastructure deps
│   ├── models.py     # All Pydantic models (Memory, KnowledgeAsk, KnowledgeResponse, etc.)
│   ├── protocols.py  # Protocol interfaces (MemoryStore, IndexEngine, GraphEngine, etc.)
│   ├── scoring.py    # ACT-R activation math (pure functions, no I/O)
│   ├── entity_extraction.py # Entity label constants + label resolution (domain-agnostic)
│   └── exceptions.py # Typed exception hierarchy
├── application/      # Use cases — orchestration logic
│   ├── memory_service.py        # Store/search/recall pipeline (index + graph + scorer)
│   ├── bus_service.py           # Knowledge Bus lifecycle, ask routing, surrogate dispatch
│   ├── snapshot_service.py      # Sleep/wake/surrogate cycle
│   ├── graph_service.py         # Entity resolution, subgraph extraction, graph rebuild
│   ├── consolidation_service.py # Decay pass + knowledge consolidation
│   └── knowledge_loader.py      # "Matrix download" — import files into memory
├── infrastructure/   # Concrete implementations of domain protocols
│   ├── storage/sqlite_store.py  # aiosqlite — 7 tables, WAL mode, parameterized SQL
│   ├── storage/migrations.py    # DDL for schema creation and versioning
│   ├── indexing/tantivy_engine.py # BM25 search via tantivy-py (Rust)
│   ├── indexing/splade_engine.py  # SPLADE sparse neural retrieval (fastembed)
│   ├── graph/networkx_store.py  # NetworkX DiGraph knowledge graph + O(1) name index
│   ├── bus/async_bus.py         # AsyncIO in-process event bus
│   ├── llm/judge.py            # Optional LLM-as-judge via litellm
│   ├── llm/contradiction_detector.py # LLM contradiction detection at ingest
│   ├── extraction/gliner_extractor.py  # GLiNER zero-shot NER (required dependency)
│   ├── extraction/keyword_extractor.py # LLM keyword bridge extraction
│   ├── extraction/label_detector.py   # LLM-based domain label detection
│   ├── consolidation/clusterer.py  # Entity co-occurrence clustering
│   ├── consolidation/synthesizer.py # LLM insight synthesis
│   └── observability/event_log.py # Ring buffer event log + SSE subscriber support
├── interfaces/       # External-facing boundaries
│   ├── mcp/server.py           # FastMCP composition root
│   ├── mcp/tools.py            # 10 MCP tools
│   ├── mcp/resources.py        # 4 MCP resources (ncms://...)
│   ├── http/dashboard.py       # Starlette dashboard server (SSE + REST)
│   ├── http/demo_runner.py     # Dashboard demo scenario runner
│   ├── http/static/index.html  # SPA frontend (D3 graph, SSE event feed)
│   ├── cli/main.py             # Click CLI: ncms serve|demo|dashboard|info|load|topics
│   ├── cli/commit_hook.py      # ncms-commit-hook for Claude Code/Copilot
│   ├── cli/context_loader.py   # ncms-context-loader for session start
│   └── agent/base.py           # KnowledgeAgent ABC (start/sleep/wake/shutdown)
└── demo/             # Interactive demo
    ├── run_demo.py              # 6-phase demo orchestrator (Rich terminal output)
    └── agents/                  # api_agent.py, frontend_agent.py, database_agent.py
```

## Key Design Decisions

1. **No vectors** — BM25 via Tantivy (Rust) for lexical precision. SPLADE sparse neural retrieval via fastembed (required, disabled by default). Rich document loading (DOCX/PPTX/PDF/XLSX) optional via `ncms[docs]` (markitdown).
2. **Three-tier retrieval**: BM25 candidates → ACT-R cognitive rescoring → optional LLM-as-judge.
3. **ACT-R scoring**: `activation(m) = ln(sum(t^-d)) + spreading_activation + noise`. Recency and frequency modeled with cognitive science math.
4. **Protocol-based DI** — Domain layer has zero infrastructure deps. Swap SQLite → Postgres, NetworkX → Neo4j, AsyncIO → Redis without changing application code.
5. **AsyncIO in-process bus** — Zero deps, <1ms latency. Protocol interface allows Redis/NATS swap later.
6. **Raw SQL via aiosqlite** — 7 tables don't need an ORM. WAL mode for concurrent reads.
7. **Surrogate via keyword matching** — Fast, deterministic, traceable (no LLM synthesis for surrogates).
8. **Embedded first** — Everything runs in-process with `pip install ncms`. No Docker, no Redis, no vector DB.

## Data Flow

1. **Store**: Content → Memory model → SQLite (persist) + Tantivy (index) + NetworkX (graph)
2. **Search**: Query → BM25 candidates → SPLADE fusion → graph expansion → ACT-R + graph scoring → ranked results
3. **Ask**: Question → Knowledge Bus → domain routing → live agent handlers → inbox/response
4. **Surrogate**: Question → no live agent → snapshot lookup → keyword matching → warm response
5. **Announce**: Event → Knowledge Bus → subscription matching → fan-out to subscriber inboxes

## Database Schema (7 tables)

- `memories` — Core knowledge storage
- `entities` — Knowledge graph nodes
- `relationships` — Knowledge graph edges
- `memory_entities` — Memory-to-entity links
- `access_log` — Access history for ACT-R base-level computation
- `snapshots` — Agent knowledge snapshots (JSON entries)
- `consolidation_state` — Key-value state for maintenance tasks

## Testing Conventions

- All tests use in-memory backends (`:memory:` SQLite, ephemeral Tantivy/NetworkX)
- Fixtures in `tests/conftest.py` provide `memory_service`, `bus_service`, `snapshot_service`
- No hardcoded expected values — use formula-computed expectations or relative assertions
- Integration tests use real service compositions (not mocks)
- Test files mirror source structure: `tests/unit/domain/`, `tests/unit/infrastructure/`, `tests/integration/`

## Configuration

All settings via environment variables with `NCMS_` prefix (Pydantic Settings):

| Variable | Default | Purpose |
|----------|---------|---------|
| `NCMS_DB_PATH` | `~/.ncms/ncms.db` | SQLite database |
| `NCMS_INDEX_PATH` | `~/.ncms/index` | Tantivy index directory |
| `NCMS_ACTR_DECAY` | `0.5` | Memory decay rate (d parameter) |
| `NCMS_ACTR_NOISE` | `0.25` | Activation noise (sigma) |
| `NCMS_ACTR_THRESHOLD` | `-2.0` | Retrieval threshold |
| `NCMS_BUS_ASK_TIMEOUT_MS` | `5000` | Bus ask timeout |
| `NCMS_LLM_JUDGE_ENABLED` | `false` | Enable LLM-as-judge tier |
| `NCMS_LLM_MODEL` | `gpt-4o-mini` | LLM model for judge |
| `NCMS_LLM_API_BASE` | *(none)* | vLLM/OpenAI-compatible endpoint for judge |
| `NCMS_KEYWORD_BRIDGE_ENABLED` | `false` | Enable LLM keyword bridge nodes |
| `NCMS_KEYWORD_LLM_MODEL` | `gpt-4o-mini` | LLM model for keywords |
| `NCMS_KEYWORD_LLM_API_BASE` | *(none)* | vLLM/OpenAI-compatible endpoint for keywords |
| `NCMS_MODEL_CACHE_DIR` | *(none)* | Directory for downloaded models (GLiNER, SPLADE). Defaults to HuggingFace cache |
| `NCMS_SPLADE_ENABLED` | `false` | Enable SPLADE sparse neural retrieval (required dep) |
| `NCMS_SPLADE_MODEL` | `prithivida/Splade_PP_en_v1` | SPLADE model (ONNX via fastembed) |
| `NCMS_SPLADE_TOP_K` | `50` | SPLADE candidates per search |
| `NCMS_SCORING_WEIGHT_SPLADE` | `0.0` | SPLADE weight in combined score |
| `NCMS_SCORING_WEIGHT_GRAPH` | `0.0` | Graph expansion entity-overlap weight (spreading activation) |
| `NCMS_CONTRADICTION_DETECTION_ENABLED` | `false` | Enable contradiction detection at ingest |
| `NCMS_CONTRADICTION_CANDIDATE_LIMIT` | `5` | Max memories to check for contradictions |
| `NCMS_CONSOLIDATION_KNOWLEDGE_ENABLED` | `false` | Enable knowledge consolidation |
| `NCMS_CONSOLIDATION_KNOWLEDGE_MODEL` | `gpt-4o-mini` | LLM for insight synthesis |
| `NCMS_CONSOLIDATION_KNOWLEDGE_API_BASE` | *(none)* | vLLM/OpenAI-compatible endpoint |
| `NCMS_GLINER_MODEL` | `urchade/gliner_medium-v2.1` | GLiNER model for NER (required dep) |
| `NCMS_GLINER_THRESHOLD` | `0.3` | Minimum confidence for entity extraction |
| `NCMS_LABEL_DETECTION_MODEL` | `gpt-4o-mini` | LLM model for `ncms topics detect` |
| `NCMS_LABEL_DETECTION_API_BASE` | *(none)* | vLLM/OpenAI-compatible endpoint for label detection |
| `NCMS_PIPELINE_DEBUG` | `false` | Emit candidate details in pipeline events |
| `NCMS_SNAPSHOT_TTL_HOURS` | `168` | Snapshot expiry (7 days) |

## Local LLM Development

Ollama is installed via Homebrew with Qwen3.5:35b-a3b (35B MoE, 3B active, 256 experts):

```bash
brew services start ollama           # Start Ollama service
ollama list                          # Verify qwen3.5:35b-a3b is available
```

Enable LLM features for local testing:

```bash
# Consolidation
NCMS_CONSOLIDATION_KNOWLEDGE_ENABLED=true \
NCMS_CONSOLIDATION_KNOWLEDGE_MODEL=ollama_chat/qwen3.5:35b-a3b \
uv run ncms demo

# Keywords
NCMS_KEYWORD_BRIDGE_ENABLED=true \
NCMS_KEYWORD_LLM_MODEL=ollama_chat/qwen3.5:35b-a3b \
uv run ncms demo

# Contradiction Detection
NCMS_CONTRADICTION_DETECTION_ENABLED=true \
NCMS_LLM_MODEL=ollama_chat/qwen3.5:35b-a3b \
uv run ncms demo

# LLM Judge (Tier 3)
NCMS_LLM_JUDGE_ENABLED=true \
NCMS_LLM_MODEL=ollama_chat/qwen3.5:35b-a3b \
uv run ncms demo
```

**Ollama model prefix:** Use `ollama_chat/` prefix with litellm (no api_base needed).
Thinking mode is auto-disabled for `ollama` models to get clean JSON output.

**vLLM (production):** Use `openai/` prefix + `api_base` on Linux + NVIDIA GPU.

## Important Patterns

- **All SQL is parameterized** — never string-interpolate user input into queries
- **Pydantic models at every boundary** — validation happens automatically
- **model_dump(mode="json")** — required when serializing models containing datetimes to JSON
- **Domain protocols** — all infrastructure contracts defined in `domain/protocols.py`
- **Agent lifecycle**: `start()` → work → `sleep()` (publish snapshot) → `wake()` (restore) → `shutdown()`
- **LLM calls are non-fatal** — all LLM features (judge, keywords, consolidation, contradiction) degrade gracefully on error
- **litellm kwargs pattern** — build a `kwargs` dict, optionally add `api_base`, add `think=False` for Ollama models
