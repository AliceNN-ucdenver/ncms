# NCMS - Project Guide

## What Is This?

NeMo Cognitive Memory System (NCMS) — a Python library providing persistent cognitive memory for AI agents. Hybrid retrieval via BM25 + SPLADE v3 sparse neural + graph spreading activation, an embedded knowledge bus for agent coordination, and snapshot-based surrogate responses when agents go offline.

## Toolchain

- **Runtime**: Python 3.12+ (managed by `uv`, auto-downloads)
- **Package manager**: `uv` (at `/Users/shawnmccarthy/.local/bin/uv`)
- **Build backend**: `uv_build` (declared in pyproject.toml)
- **Linting**: `ruff` (line-length 100, target py312)
- **Testing**: `pytest` + `pytest-asyncio` (asyncio_mode = "auto")
- **Type checking**: `mypy` (`mypy_path = src`, `explicit_package_bases = true` — fixes duplicate module resolution)

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
uv run ncms state get <entity_id> [--key KEY]  # Show current entity state
uv run ncms state history <entity_id> [--key KEY]  # Show state transitions
uv run ncms state list            # List entities with state nodes
uv run ncms episodes list [--closed]  # List open/closed episodes
uv run ncms episodes show <id>    # Show episode with member fragments
uv run pytest tests/ -v          # Run all tests
uv run pytest tests/unit/ -v     # Unit tests only
uv run pytest tests/integration/ # Integration tests only
uv run ruff check src/           # Lint
uv run mypy src/                 # Type check

# Benchmarks (retrieval ablation study)
uv sync --group bench                                         # Install benchmark deps
./benchmarks/run.sh                                           # Sequential (all datasets)
./benchmarks/run.sh scifact                                   # Single dataset
./benchmarks/run_parallel.sh                                  # All 3 datasets in parallel
./benchmarks/run_parallel.sh scifact nfcorpus                 # Specific datasets in parallel
./benchmarks/run_parallel.sh scifact nfcorpus arguana -v      # With debug logging
tail -f benchmarks/results/ablation_latest.log                # Monitor sequential run
tail -f benchmarks/results/*/ablation_latest.log              # Monitor parallel runs

# Benchmarks (dream cycle / consolidation experiment — requires LLM endpoint)
./benchmarks/run_dream.sh                                     # Sequential (all datasets)
./benchmarks/run_dream.sh scifact                             # Single dataset
./benchmarks/run_dream_parallel.sh                            # All 3 datasets in parallel
./benchmarks/run_dream_parallel.sh scifact nfcorpus           # Specific datasets
./benchmarks/run_dream_test13.sh                              # Smoke test (13 docs, ~2-3min)
tail -f benchmarks/results/dream/dream_latest.log             # Monitor sequential
tail -f benchmarks/results/dream/*/dream_latest.log           # Monitor parallel
# Override LLM (default: DGX Spark Nemotron):
LLM_MODEL=ollama_chat/qwen3.5:35b-a3b LLM_API_BASE="" ./benchmarks/run_dream.sh scifact

uv run ruff check benchmarks/                                 # Lint benchmark code
```

## Architecture (Clean Architecture)

```
src/ncms/
├── domain/           # Pure models, protocols, scoring — ZERO infrastructure deps
│   ├── models.py     # All Pydantic models (Memory, MemoryNode, GraphEdge, RelationType, ScoredMemory, etc.)
│   ├── protocols.py  # Protocol interfaces (MemoryStore, MemoryNodeStore + temporal queries, etc.)
│   ├── scoring.py    # ACT-R activation + admission scoring + reconciliation penalties (pure fns)
│   ├── intent.py     # Intent taxonomy, exemplar queries, keyword fallback classifier (Phase 4)
│   ├── entity_extraction.py # Entity label constants + label resolution (domain-agnostic)
│   └── exceptions.py # Typed exception hierarchy
├── application/      # Use cases — orchestration logic
│   ├── memory_service.py        # Store/search/recall pipeline (index + graph + scorer)
│   ├── admission_service.py     # Admission scoring: 4-feature text heuristics + routing (Phase 1)
│   ├── reconciliation_service.py # State reconciliation: classify + apply (supports/refines/supersedes/conflicts)
│   ├── episode_service.py       # Hybrid episode linker: BM25/SPLADE/entity scoring (Phase 3)
│   ├── bus_service.py           # Knowledge Bus lifecycle, ask routing, surrogate dispatch
│   ├── snapshot_service.py      # Sleep/wake/surrogate cycle
│   ├── graph_service.py         # Entity resolution, subgraph extraction, graph rebuild
│   ├── consolidation_service.py # Decay pass + knowledge consolidation + hierarchical abstraction (Phase 5)
│   ├── index_worker.py          # Background indexing queue + worker pool (BM25/SPLADE/GLiNER async)
│   └── knowledge_loader.py      # "Matrix download" — import files into memory
├── infrastructure/   # Concrete implementations of domain protocols
│   ├── storage/sqlite_store.py  # aiosqlite — 10 tables, WAL mode, parameterized SQL
│   ├── storage/migrations.py    # DDL for schema creation and versioning (V1 base + V2 HTMG + V3 bitemporal)
│   ├── indexing/tantivy_engine.py # BM25 search via tantivy-py (Rust)
│   ├── indexing/splade_engine.py  # SPLADE v3 sparse neural retrieval (sentence-transformers)
│   ├── indexing/exemplar_intent_index.py # BM25 exemplar intent classifier (Phase 4)
│   ├── graph/networkx_store.py  # NetworkX DiGraph knowledge graph + O(1) name index
│   ├── bus/async_bus.py         # AsyncIO in-process event bus
│   ├── llm/caller.py            # Shared LLM calling utility (litellm + thinking mode)
│   ├── llm/json_utils.py        # Shared LLM JSON output parsing + repair
│   ├── llm/contradiction_detector.py # LLM contradiction detection at ingest
│   ├── llm/intent_classifier_llm.py  # LLM intent classification fallback (Phase 4)
│   ├── llm/episode_linker_llm.py     # LLM episode linking fallback (Phase 3)
│   ├── text/chunking.py         # Sentence-boundary text chunking (shared by GLiNER + SPLADE)
│   ├── extraction/gliner_extractor.py  # GLiNER zero-shot NER (required dependency)
│   ├── extraction/label_detector.py   # LLM-based domain label detection
│   ├── consolidation/clusterer.py  # Entity co-occurrence clustering
│   ├── consolidation/synthesizer.py # LLM insight synthesis
│   ├── consolidation/abstract_synthesizer.py # LLM episode/trajectory/pattern synthesis (Phase 5)
│   ├── reranking/cross_encoder_reranker.py  # Cross-encoder reranking (Phase 10, ms-marco-MiniLM-L-6-v2)
│   └── observability/event_log.py # Ring buffer event log + NullEventLog + SSE subscribers
├── interfaces/       # External-facing boundaries
│   ├── mcp/server.py           # FastMCP composition root
│   ├── mcp/tools.py            # 15 MCP tools (+ run_consolidation)
│   ├── mcp/resources.py        # 5 MCP resources (ncms://...)
│   ├── http/dashboard.py       # Starlette dashboard server (SSE + REST + entity/episode APIs)
│   ├── http/demo_runner.py     # Dashboard demo scenario runner
│   ├── http/static/index.html  # SPA frontend (D3 graph, SSE event feed)
│   ├── cli/main.py             # Click CLI: ncms serve|demo|dashboard|info|load|topics|state|episodes
│   ├── cli/commit_hook.py      # ncms-commit-hook for Claude Code/Copilot
│   ├── cli/context_loader.py   # ncms-context-loader for session start
│   └── agent/base.py           # KnowledgeAgent ABC (start/sleep/wake/shutdown)
└── demo/             # Interactive demo
    ├── run_demo.py              # 6-phase demo orchestrator (Rich terminal output)
    └── agents/                  # base_demo.py + api_agent.py, frontend_agent.py, database_agent.py
```

## Key Design Decisions

1. **No dense vectors** — BM25 via Tantivy (Rust) for lexical precision. SPLADE v3 sparse neural retrieval via sentence-transformers SparseEncoder (asymmetric encoding: `encode_document()` for indexing, `encode_query()` for search) with MPS/CUDA auto-detection. Rich document loading (DOCX/PPTX/PDF/XLSX) optional via `ncms[docs]` (markitdown).
2. **Three-signal retrieval**: BM25 (0.6) + SPLADE (0.3) + Graph spreading activation (0.3) — tuned via grid search on SciFact BEIR (nDCG@10=0.7206, +3.3% over BM25+SPLADE baseline, exceeds published ColBERTv2 and SPLADE++ on SciFact). Zero LLM at query time. The graph signal uses real BFS traversal (`graph_spreading_activation()` in scoring.py) with per-hop decay through PMI-weighted co-occurrence edges and IDF-weighted entity matching (rare entities contribute more than common ones). Cleanly separated from ACT-R: `spread` (Jaccard overlap) feeds only ACT-R's `total_activation`; `graph_spread` (graph traversal + IDF + PMI) feeds only `w_graph`. Reconciliation penalties applied directly to combined score (not just via ACT-R), so they work even with w_actr=0.0.
3. **ACT-R scoring**: `activation(m) = ln(sum(t^-d)) + jaccard_spread + noise`. Spreading activation uses Jaccard normalization (`|overlap| / |union|` instead of `|overlap| / |context|`). Weight defaults to 0.0 — grid search showed ACT-R hurts on cold corpora with no access history; designed to activate after dream cycles build differential access patterns.
4. **Protocol-based DI** — Domain layer has zero infrastructure deps. Swap SQLite → Postgres, NetworkX → Neo4j, AsyncIO → Redis without changing application code.
5. **AsyncIO in-process bus** — Zero deps, <1ms latency. Protocol interface allows Redis/NATS swap later.
6. **Raw SQL via aiosqlite** — 12 tables don't need an ORM. WAL mode for concurrent reads.
7. **Surrogate via keyword matching** — Fast, deterministic, traceable (no LLM synthesis for surrogates).
8. **Embedded first** — Everything runs in-process with `pip install ncms`. No Docker, no Redis, no vector DB.
9. **Automatic text chunking** — GLiNER (1,200 char chunks) and SPLADE v3 (2,000 char chunks) automatically split long text at sentence boundaries, merging results (entity dedup / max-pool) to avoid silent truncation from underlying model token limits.
10. **Hybrid episode linker** — Episodes group related fragments via incremental multi-signal matching (no LLM). Each episode maintains a compact profile (entities + domains + anchors) indexed in BM25/SPLADE. New fragments scored against candidates using 7 weighted signals: BM25 lexical match, SPLADE semantic match, entity overlap coefficient, domain overlap, temporal proximity, source agent, and structured anchor bonus. Weights auto-redistribute when SPLADE is disabled.
10. **Selective admission scoring** (Phase 1) — 4 pure text heuristic features (utility, persistence, state_change_signal, temporal_salience) implement a 3-way quality gate: discard / ephemeral_cache / persist. No index or LLM dependency — runs identically in sync and async indexing modes. Content-hash dedup (SHA-256) handles exact duplicates. State change signal ≥ 0.35 auto-promotes to persist for entity state capture. Every persisted memory gets an L1 atomic node; content with state change signal ≥ 0.35 or structured state declaration (`Entity: key = value`) additionally gets an L2 entity_state node with a DERIVED_FROM edge to L1; episodes link to L1 atomic nodes. `importance >= 8.0` bypasses admission entirely (force-store for agents). Feature-flagged off by default (`NCMS_ADMISSION_ENABLED=false`).
11. **Heuristic state reconciliation** (Phase 2) — Entity state nodes are compared via 5 relation types (supports, refines, supersedes, conflicts, unrelated). Superseded states get `is_current=False` + `valid_to` closure + bidirectional edges. Superseded/conflicted memories receive ACT-R mismatch penalties in retrieval scoring. Bitemporal fields (`observed_at`, `ingested_at`) enable point-in-time queries. Feature-flagged off by default (`NCMS_RECONCILIATION_ENABLED=false`).
12. **Intent-aware retrieval** (Phase 4) — BM25 exemplar index classifies queries into 7 intent classes (fact_lookup, current_state_lookup, historical_lookup, event_reconstruction, change_detection, pattern_lookup, strategic_reflection). ~70 exemplar queries indexed in a small in-memory Tantivy index; BM25 scoring aggregated per intent replaces hardcoded keyword patterns. Keyword fallback used when index unavailable. Matching node types receive an additive hierarchy bonus in scoring. Two-toggle safety: classification can be enabled for observability without affecting ranking (scoring_weight_hierarchy defaults to 0.0). Supplementary candidates (entity states, episode members, state history) injected based on classified intent. Batch node preload eliminates N+1 queries.
13. **Hierarchical consolidation** (Phase 5) — Three batch consolidation passes generate abstract memories from lower-level traces. Episode summaries (5A) synthesize closed episodes into searchable narratives via LLM. State trajectories (5B) generate temporal progression narratives for entities with ≥N state transitions. Recurring patterns (5C) cluster episode summaries by topic_entities Jaccard overlap, with stability-based promotion (`min(1.0, cluster_size/5) * confidence`) to `strategic_insight` above 0.7 threshold. Each abstract creates dual storage: `Memory(type="insight")` for Tantivy/SPLADE indexing + `MemoryNode(node_type=ABSTRACT)` for HTMG hierarchy. Staleness tracking via `refresh_due_at` metadata enables re-synthesis. All three sub-phases feature-flagged off by default.
14. **Dream cycles** (Phase 8) — Offline rehearsal creating differential access patterns so ACT-R provides meaningful signal. Three non-LLM passes: (8A) search logging captures query→result associations, learned entity co-occurrence strengths via PMI feed into graph spreading activation (PMI-weighted edges traversed during BFS, rare co-occurrences get high weight); (8B) dream rehearsal selects top memories by 5-signal weighted score (centrality 0.40, staleness 0.30, importance 0.20, access_count 0.05, recency 0.05) and injects synthetic access records; importance drift compares recent vs older access rates and adjusts importance within ±drift_rate. Co-occurrence edge generation capped at 12 entities per memory (`cooccurrence_max_entities`) to limit hub-node inflation from generic entities. Integrated into consolidation pass. Feature-flagged off by default (`NCMS_DREAM_CYCLE_ENABLED=false`).
15. **Per-query score normalization** (Phase 9) — Min-max normalization of all retrieval signals (BM25, SPLADE, Graph, CE) to [0,1] per query before combining. Fixes fundamental scale mismatch where SPLADE (5-200 range) dominated BM25 (1-15 range) despite lower configured weights. Applied in the scoring loop after all raw scores are collected.
16. **Selective cross-encoder reranking** (Phase 10) — Cross-encoder model (`cross-encoder/ms-marco-MiniLM-L-6-v2`, 22M params) reranks RRF candidates. Applied selectively based on classified intent: enabled for `fact_lookup`, `pattern_lookup`, `strategic_reflection` where textual relevance helps; disabled for `current_state_lookup`, `historical_lookup`, `change_detection` where it destroys temporal ordering (CR -4.2%) and long-range connections (LRU -28%). Config: `NCMS_RERANKER_ENABLED`, `NCMS_RERANKER_MODEL`, `NCMS_RERANKER_TOP_K=50`, `NCMS_RERANKER_OUTPUT_K=20`, `NCMS_SCORING_WEIGHT_CE=0.7`.
17. **Structured recall** (Phase 11) — `recall()` method wraps full `search()` pipeline and layers intent-specific structured context. Returns `RecallResult` with: (a) entity state snapshots (current values for all entities in the memory), (b) episode context (membership, sibling IDs, LLM summary), (c) causal chains (supersedes/superseded_by/derived_from/supports/conflicts_with edges). Episode siblings appended *after* primary results to expand retrieval set without displacing BM25 ranking. Achieves Recall AR nDCG@10=0.2032 on SWE-bench Django (+15.5% over search AR 0.1759). MCP tool: `recall_memory`. Models: `RecallResult`, `RecallContext`, `EntityStateSnapshot`, `EpisodeContext`, `CausalChain` in `domain/models.py`.

## Text Chunking & LLM Prompt Limits

Models with fixed token windows silently truncate long text. NCMS auto-chunks where possible and caps LLM prompts to fit the configured context window.

### Auto-Chunked (no information loss)

| Component | Token Limit | Chunk Size | Overlap | Merge Strategy |
|-----------|-------------|-----------|---------|----------------|
| GLiNER NER | 384 tokens | 1,200 chars | 100 chars | Entity dedup (lowercase, first wins) |
| SPLADE v3 embedding | 512 tokens | 2,000 chars | 100 chars | Max-pool per vocab index |

### LLM Prompt Truncation (hardcoded, fits 32K context)

| LLM Caller | File | Truncation | Worst-Case Tokens |
|------------|------|------------|-------------------|
| Contradiction Detector (new) | `contradiction_detector.py` | 8,000 chars | ~2K tokens |
| Contradiction Detector (existing) | `contradiction_detector.py` | 2,000 chars/memory × 5 | ~2.5K tokens |
| Consolidation Synthesizer | `synthesizer.py` | 2,000 chars/memory × cluster | ~3K tokens |
| Label Detector | `label_detector.py` | 500 chars/sample × 10 | ~1.5K tokens |
| Abstract Synthesizer (episode) | `abstract_synthesizer.py` | 2,000 chars/member × 20 | ~10K tokens |
| Abstract Synthesizer (trajectory) | `abstract_synthesizer.py` | 500 chars/state × 20 | ~2.5K tokens |
| Abstract Synthesizer (pattern) | `abstract_synthesizer.py` | 1,000 chars/summary × 10 | ~2.5K tokens |

### Display-Only Truncation (no data loss, cosmetic)

Dashboard (`content[:200]`), event logs (`[:200]`), demo output (`[:200]`), CLI (`[:500]`), log previews (`[:120]`) — all display/logging only.

## Data Flow

1. **Store**: Content → [content-hash dedup] → [admission scoring (4 text heuristics) → 3-way quality gate: discard/ephemeral/persist] → Memory model → SQLite (persist) → return Memory to caller (~2ms). Background indexing queue → workers process: Tantivy (BM25) + SPLADE + GLiNER (parallel) → entity linking → NetworkX (graph) → L1 atomic node → [L2 entity_state node if state change/declaration detected, DERIVED_FROM edge to L1] → [reconcile entity states] → [hybrid episode linker] → [contradiction detection]
2. **Search**: Query → [intent classification] → BM25 candidates → SPLADE fusion → graph expansion → [batch node preload + intent supplementary candidates] → per-query min-max normalization → weighted scoring (BM25 0.6 + SPLADE 0.3 + Graph 0.3 via BFS traversal with IDF-weighted entity matching and PMI-weighted edges) [+ ACT-R Jaccard spread if enabled + hierarchy bonus − reconciliation penalty on combined score] → [selective cross-encoder reranking for fact/pattern/reflection intents] → ranked results → [search log for dream cycle PMI]
3. **Recall**: Query → full search pipeline (step 2) → intent classification → entity extraction → wrap results as RecallResults → [state/episode/change bonus results for non-fact intents] → merge (base first, bonus appended) → enrich all results with entity states + episode context + causal chains → RecallResult list
4. **Ask**: Question → Knowledge Bus → domain routing → live agent handlers → inbox/response
5. **Surrogate**: Question → no live agent → snapshot lookup → keyword matching → warm response
6. **Announce**: Event → Knowledge Bus → subscription matching → fan-out to subscriber inboxes

## Database Schema (12 tables)

**V1 (base):**
- `memories` — Core knowledge storage
- `entities` — Knowledge graph nodes
- `relationships` — Knowledge graph edges
- `memory_entities` — Memory-to-entity links
- `access_log` — Access history for ACT-R base-level computation
- `snapshots` — Agent knowledge snapshots (JSON entries)
- `consolidation_state` — Key-value state for maintenance tasks

**V2 (Phase 1 — HTMG + Admission):**
- `memory_nodes` — Typed HTMG nodes (atomic, entity_state, episode, abstract)
- `graph_edges` — Typed directed edges in the HTMG
- `ephemeral_cache` — Short-lived entries below atomic admission threshold

**V3 (Phase 2 — Bitemporal):**
- `memory_nodes` + `observed_at` (when source says event happened) + `ingested_at` (when NCMS stored it)

**V4 (Phase 8 — Dream Cycles):**
- `search_log` — Query → returned memory ID associations for PMI computation
- `association_strengths` — Learned entity-pair co-occurrence strengths (PMI-based)

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
| `NCMS_SCORING_WEIGHT_BM25` | `0.6` | BM25 weight in combined score (tuned on SciFact) |
| `NCMS_SCORING_WEIGHT_ACTR` | `0.0` | ACT-R weight (0.0 default; activates after dream cycles) |
| `NCMS_BUS_ASK_TIMEOUT_MS` | `5000` | Bus ask timeout |
| `NCMS_LLM_MODEL` | `openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16` | LLM model for contradiction detection |
| `NCMS_LLM_API_BASE` | `http://spark-ee7d.local:8000/v1` | vLLM endpoint on DGX Spark |
| `NCMS_MODEL_CACHE_DIR` | *(none)* | Directory for downloaded models (GLiNER, SPLADE, sentence-transformers). Defaults to HuggingFace cache |
| `NCMS_SPLADE_ENABLED` | `false` | Enable SPLADE sparse neural retrieval (required dep) |
| `NCMS_SPLADE_MODEL` | `naver/splade-v3` | SPLADE model (sentence-transformers SparseEncoder) |
| `NCMS_SPLADE_TOP_K` | `50` | SPLADE candidates per search |
| `NCMS_SCORING_WEIGHT_SPLADE` | `0.3` | SPLADE weight in combined score (tuned on SciFact) |
| `NCMS_SCORING_WEIGHT_GRAPH` | `0.3` | Graph spreading activation weight (BFS traversal + IDF + PMI, tuned on SciFact) |
| `NCMS_GRAPH_HOP_DECAY` | `0.5` | Activation multiplier per hop in graph BFS traversal |
| `NCMS_GRAPH_SPREADING_MAX_HOPS` | `2` | Maximum hops for graph spreading activation BFS |
| `NCMS_COOCCURRENCE_MAX_ENTITIES` | `12` | Max entities per memory for co-occurrence edge generation (caps clique inflation) |
| `NCMS_CONTRADICTION_DETECTION_ENABLED` | `false` | Enable contradiction detection at ingest |
| `NCMS_CONTRADICTION_CANDIDATE_LIMIT` | `5` | Max memories to check for contradictions |
| `NCMS_CONSOLIDATION_KNOWLEDGE_ENABLED` | `false` | Enable knowledge consolidation |
| `NCMS_CONSOLIDATION_KNOWLEDGE_MODEL` | `openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16` | LLM for insight synthesis |
| `NCMS_CONSOLIDATION_KNOWLEDGE_API_BASE` | `http://spark-ee7d.local:8000/v1` | vLLM endpoint on DGX Spark |
| `NCMS_GLINER_MODEL` | `urchade/gliner_medium-v2.1` | GLiNER model for NER (required dep) |
| `NCMS_GLINER_THRESHOLD` | `0.3` | Minimum confidence for entity extraction |
| `NCMS_LABEL_DETECTION_MODEL` | `openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16` | LLM model for `ncms topics detect` |
| `NCMS_LABEL_DETECTION_API_BASE` | `http://spark-ee7d.local:8000/v1` | vLLM endpoint on DGX Spark |
| `NCMS_ADMISSION_ENABLED` | `false` | Enable admission scoring for incoming memories (Phase 1) |
| `NCMS_ADMISSION_EPHEMERAL_TTL_SECONDS` | `3600` | TTL for ephemeral cache entries (1 hour) |
| `NCMS_RECONCILIATION_ENABLED` | `false` | Enable state reconciliation (Phase 2, requires admission) |
| `NCMS_RECONCILIATION_IMPORTANCE_BOOST` | `0.5` | Importance boost for SUPPORTS relations |
| `NCMS_RECONCILIATION_SUPERSESSION_PENALTY` | `0.3` | ACT-R mismatch penalty for superseded states |
| `NCMS_RECONCILIATION_CONFLICT_PENALTY` | `0.15` | ACT-R mismatch penalty for conflicted states |
| `NCMS_EPISODES_ENABLED` | `false` | Enable hybrid episode linker (Phase 3) |
| `NCMS_EPISODE_WINDOW_MINUTES` | `1440` | Temporal proximity window for episode signals (24h) |
| `NCMS_EPISODE_CLOSE_MINUTES` | `1440` | Auto-close episodes with no activity past this (24h) |
| `NCMS_EPISODE_MATCH_THRESHOLD` | `0.30` | Weighted score threshold for joining an episode |
| `NCMS_EPISODE_CREATE_MIN_ENTITIES` | `2` | Min entities to create a new episode |
| `NCMS_EPISODE_CANDIDATE_LIMIT` | `10` | BM25/SPLADE candidate limit per search |
| `NCMS_EPISODE_WEIGHT_BM25` | `0.20` | BM25 lexical match weight |
| `NCMS_EPISODE_WEIGHT_SPLADE` | `0.20` | SPLADE semantic match weight (redistributed when disabled) |
| `NCMS_EPISODE_WEIGHT_ENTITY_OVERLAP` | `0.25` | Entity overlap coefficient weight |
| `NCMS_EPISODE_WEIGHT_DOMAIN` | `0.15` | Domain overlap weight |
| `NCMS_EPISODE_WEIGHT_TEMPORAL` | `0.10` | Temporal proximity weight |
| `NCMS_EPISODE_WEIGHT_AGENT` | `0.05` | Source agent match weight |
| `NCMS_EPISODE_WEIGHT_ANCHOR` | `0.05` | Structured anchor match weight |
| `NCMS_INTENT_CLASSIFICATION_ENABLED` | `false` | Enable heuristic query intent classification (Phase 4) |
| `NCMS_INTENT_CONFIDENCE_THRESHOLD` | `0.6` | Minimum confidence to apply classified intent (falls back to fact_lookup) |
| `NCMS_INTENT_HIERARCHY_BONUS` | `0.5` | Raw bonus value for node type match before weight |
| `NCMS_SCORING_WEIGHT_HIERARCHY` | `0.0` | Additive weight for hierarchy bonus (0 = no effect) |
| `NCMS_INTENT_SUPPLEMENT_MAX` | `20` | Max supplementary candidates from intent-specific stores |
| `NCMS_INTENT_LLM_FALLBACK_ENABLED` | `false` | LLM fallback when BM25 exemplar confidence low |
| `NCMS_EPISODE_LLM_FALLBACK_ENABLED` | `false` | LLM fallback when no episode matches heuristic scoring |
| `NCMS_EPISODE_CONSOLIDATION_ENABLED` | `false` | Enable episode summary generation (Phase 5A) |
| `NCMS_TRAJECTORY_CONSOLIDATION_ENABLED` | `false` | Enable state trajectory narratives (Phase 5B) |
| `NCMS_PATTERN_CONSOLIDATION_ENABLED` | `false` | Enable recurring pattern detection (Phase 5C) |
| `NCMS_TRAJECTORY_MIN_TRANSITIONS` | `3` | Min state transitions for trajectory generation |
| `NCMS_PATTERN_MIN_EPISODES` | `3` | Min episodes for pattern cluster |
| `NCMS_PATTERN_ENTITY_OVERLAP_THRESHOLD` | `0.3` | Jaccard threshold for episode clustering |
| `NCMS_PATTERN_STABILITY_THRESHOLD` | `0.7` | Promote to strategic_insight above this |
| `NCMS_ABSTRACT_REFRESH_DAYS` | `7` | Staleness window for re-synthesis |
| `NCMS_CONSOLIDATION_MAX_ABSTRACTS_PER_RUN` | `10` | Cap abstracts per consolidation pass |
| `NCMS_DREAM_CYCLE_ENABLED` | `false` | Enable dream cycle (rehearsal + associations + drift) |
| `NCMS_DREAM_REHEARSAL_FRACTION` | `0.10` | Top fraction of eligible memories to rehearse |
| `NCMS_DREAM_STALENESS_DAYS` | `7` | Memory considered stale after N days |
| `NCMS_DREAM_MIN_ACCESS_COUNT` | `3` | Minimum accesses before eligible for rehearsal |
| `NCMS_DREAM_REHEARSAL_WEIGHT_CENTRALITY` | `0.40` | PageRank centrality weight in rehearsal selector |
| `NCMS_DREAM_REHEARSAL_WEIGHT_STALENESS` | `0.30` | Staleness weight in rehearsal selector |
| `NCMS_DREAM_REHEARSAL_WEIGHT_IMPORTANCE` | `0.20` | Importance weight in rehearsal selector |
| `NCMS_DREAM_REHEARSAL_WEIGHT_ACCESS_COUNT` | `0.05` | Access count weight in rehearsal selector |
| `NCMS_DREAM_REHEARSAL_WEIGHT_RECENCY` | `0.05` | Recency weight in rehearsal selector |
| `NCMS_DREAM_IMPORTANCE_DRIFT_WINDOW_DAYS` | `14` | Window for access rate comparison |
| `NCMS_DREAM_IMPORTANCE_DRIFT_RATE` | `0.1` | Max importance adjustment per cycle |
| `NCMS_RERANKER_ENABLED` | `false` | Enable cross-encoder reranking (Phase 10) |
| `NCMS_RERANKER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Cross-encoder model for reranking |
| `NCMS_RERANKER_TOP_K` | `50` | Number of RRF candidates to rerank |
| `NCMS_RERANKER_OUTPUT_K` | `20` | Number of results after reranking |
| `NCMS_SCORING_WEIGHT_CE` | `0.7` | Cross-encoder weight when reranker active |
| `NCMS_ASYNC_INDEXING_ENABLED` | `true` | Background indexing (store returns in ~2ms, indexing async) |
| `NCMS_INDEX_WORKERS` | `3` | Background indexing worker count |
| `NCMS_INDEX_QUEUE_SIZE` | `1000` | Max pending index tasks before backpressure (falls back to inline) |
| `NCMS_INDEX_MAX_RETRIES` | `3` | Retry attempts per failed index task |
| `NCMS_INDEX_DRAIN_TIMEOUT_SECONDS` | `30` | Shutdown drain timeout |
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

# Contradiction Detection
NCMS_CONTRADICTION_DETECTION_ENABLED=true \
NCMS_LLM_MODEL=ollama_chat/qwen3.5:35b-a3b \
uv run ncms demo
```

**Ollama model prefix:** Use `ollama_chat/` prefix with litellm (no api_base needed).
Thinking mode is auto-disabled for `ollama` models to get clean JSON output.

**vLLM (production):** Use `openai/` prefix + `api_base` on Linux + NVIDIA GPU.

### DGX Spark (vLLM)

A DGX Spark at `spark-ee7d.local` (128GB) serves Nemotron 3 Nano via NGC vLLM container:

```bash
# Download the Nemotron Nano reasoning parser plugin (enables thinking mode)
sudo wget -O /root/nano_v3_reasoning_parser.py \
  https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16/resolve/main/nano_v3_reasoning_parser.py

# Deploy on Spark (via Portainer or SSH — use sudo if not in docker group)
sudo docker run -d --gpus all --ipc=host --restart unless-stopped \
  --name vllm-nemotron-nano \
  -p 8000:8000 \
  -e VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 \
  -v /root/.cache/huggingface:/root/.cache/huggingface \
  -v /root/nano_v3_reasoning_parser.py:/app/nano_v3_reasoning_parser.py \
  nvcr.io/nvidia/vllm:26.01-py3 \
  vllm serve nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
    --host 0.0.0.0 \
    --port 8000 \
    --trust-remote-code \
    --max-model-len 524288 \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_coder \
    --reasoning-parser-plugin /app/nano_v3_reasoning_parser.py \
    --reasoning-parser nano_v3

# Verify serving
curl http://spark-ee7d.local:8000/v1/models

# Test tool calling
curl http://spark-ee7d.local:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 50}'
```

**vLLM flags:**
- `--max-model-len 524288` — 512K context window (requires `VLLM_ALLOW_LONG_MAX_MODEL_LEN=1` env var; model ~60GB, KV cache <0.5% on 128GB Spark). Model's max_position_embeddings is 262144 but NVIDIA documents support up to 1M via RoPE scaling.
- `--enable-auto-tool-choice` — required for structured tool calling via OpenAI-compatible API
- `--tool-call-parser qwen3_coder` — Nemotron Nano uses `<tool_call><function=name>` format, parsed by `qwen3_coder` (NOT `hermes`). Only activates when `tools` param present in request; regular chat completions unaffected
- `--reasoning-parser-plugin /app/nano_v3_reasoning_parser.py` + `--reasoning-parser nano_v3` — The plugin registers the `nano_v3` parser (extends DeepSeek R1 parser for Nemotron's `<think>` tags). The `--reasoning-parser-plugin` loads the module, `--reasoning-parser nano_v3` activates it. Separates thinking tokens into `reasoning_content` field instead of leaking them into output content. Downloaded from the model's HuggingFace repo.

Enable LLM features via Spark:

```bash
# Consolidation + Contradiction Detection via DGX Spark
NCMS_CONSOLIDATION_KNOWLEDGE_ENABLED=true \
NCMS_CONSOLIDATION_KNOWLEDGE_MODEL=openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
NCMS_CONSOLIDATION_KNOWLEDGE_API_BASE=http://spark-ee7d.local:8000/v1 \
NCMS_CONTRADICTION_DETECTION_ENABLED=true \
NCMS_LLM_MODEL=openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
NCMS_LLM_API_BASE=http://spark-ee7d.local:8000/v1 \
uv run ncms demo

# Benchmarks with LLM configs
uv run python -m benchmarks.run_ablation --datasets scifact \
  --llm-model openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
```

**vLLM model prefix:** Use `openai/` prefix with litellm + `api_base` pointing to Spark.

## Important Patterns

- **All SQL is parameterized** — never string-interpolate user input into queries
- **Pydantic models at every boundary** — validation happens automatically
- **model_dump(mode="json")** — required when serializing models containing datetimes to JSON
- **Domain protocols** — all infrastructure contracts defined in `domain/protocols.py`
- **Agent lifecycle**: `start()` → work → `sleep()` (publish snapshot) → `wake()` (restore) → `shutdown()`
- **LLM calls are non-fatal** — all LLM features (judge, keywords, consolidation, contradiction) degrade gracefully on error
- **litellm kwargs pattern** — build a `kwargs` dict, optionally add `api_base`, add `think=False` for Ollama models
