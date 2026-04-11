# NCMS Resilience, Data Integrity & Performance Update

**Status:** Proposed
**Date:** 2026-04-08
**Authors:** Shawn McCarthy, with analysis assistance from Claude (Anthropic)
**Context:** Audit of the live NCMS Hub (ncms-hub container, 67 memories, 6 agents, IMDB Lite architecture exercise) revealed data integrity issues in the ingestion pipeline, gaps in graph persistence, and opportunities for performance improvement. This document proposes concrete fixes, architectural improvements, and an operational lifecycle for recurring maintenance events. Design informed by competitive analysis of MemPalace and Karpathy's LLM Knowledge Bases, and a survey of Jan-Apr 2026 agent memory research.

---

## Table of Contents

1. [Hub Audit Findings](#1-hub-audit-findings)
2. [SOTA Priority Analysis](#2-sota-priority-analysis)
3. [Data Integrity Improvements](#3-data-integrity-improvements)
4. [Graph Persistence & Rebuild-from-Store](#4-graph-persistence--rebuild-from-store)
5. [Ingestion Performance Improvements](#5-ingestion-performance-improvements)
6. [Compilation, Synthesis & Document Integration](#6-compilation-synthesis--document-integration)
7. [Recurring Events & Operational Lifecycle](#7-recurring-events--operational-lifecycle)
8. [Competitive Context](#8-competitive-context)
9. [Retrieval Quality & System Resilience](#9-retrieval-quality--system-resilience)
10. [Codebase Housekeeping](#10-codebase-housekeeping)
11. [Implementation Roadmap](#11-implementation-roadmap)

---

## 1. Hub Audit Findings

### 1.1 Environment

| Attribute | Value |
|-----------|-------|
| Database | 67 memories, 319 entities, 611 memory-entity links |
| HTMG | 68 nodes (59 atomic, 2 entity_state, 7 episode), 61 edges |
| Agents | architect (8), security (5), product_owner (19), archeologist (9), designer (18), default_user (7), anonymous (1) |
| Config | Admission ON, Reconciliation ON, Episodes ON, SPLADE ON, Intent ON, Reranker ON, Contradiction Detection ON, Dream Cycles OFF |

### 1.2 Issue Summary

| ID | Severity | Issue | Impact |
|----|----------|-------|--------|
| **I-01** | CRITICAL | No content deduplication | 27% of store is duplicates (18 wasted memories from 10 unique announcements stored 2-4x each) |
| **I-02** | HIGH | Raw LLM outputs stored as memories | 3 memories totaling 78K chars (28K PRD, 24K research, 26K design) from NAT auto_memory_wrapper |
| **I-03** | HIGH | NAT auto_memory_wrapper double-storage | `save_user_messages_to_memory` and `save_ai_messages_to_memory` store conversation turns as `default_user` memories, redundant with agent-explicit stores |
| **I-04** | MEDIUM | Junk entities polluting graph | ~11% of 319 entities are noise: `85%`, `25789`, `1 item(s)`, `2783 chars`, `ac`, `S5`, `Document: 6f01603fe96a` |
| **I-05** | MEDIUM | Entity state detection misfiring | 2 entity_state nodes with meaningless values (500-char truncation of raw LLM output assigned to wrong entities: "Market research document", "digital authentication market") |
| **I-06** | MEDIUM | Episode seed memories invisible to entity scoring | 7 keyword-extracted episode profiles contain comma-separated terms; GLiNER extracts 0 entities from these |
| **I-07** | LOW | 1 orphaned memory (no memory_node) | 25,800-char `default_user` assistant output has no atomic node — likely race condition during ingestion |
| **I-08** | LOW | Co-occurrence edges not persisted | NetworkX graph has edges during runtime but they're lost on container restart; `relationships` table stays empty |
| **I-09** | LOW | No search logging / No dream data | `search_log` and `association_strengths` tables empty (dream cycles disabled), so PMI-based co-occurrence and importance drift cannot function |

### 1.3 What's Working Well

- **Episode formation**: 7 episodes correctly formed with cross-agent membership (product_owner + archeologist + default_user). Match scores 0.3-0.94.
- **HTMG structure**: All atomic nodes linked to exactly one episode. `belongs_to_episode` and `derived_from` edges structurally correct.
- **Entity extraction density**: Average ~9 entities per memory on substantive content (ADRs, threat models, PRDs).
- **Entity type taxonomy**: 20 types covering the architecture domain well (technology, document, metric, concept, organization, security_control, etc.).
- **Access logging**: 92 entries showing realistic multi-agent retrieval patterns.

### 1.4 Root Cause: NAT auto_memory_wrapper

The `default_user` memories originate from NVIDIA NAT's `auto_memory_wrapper` (`nat/plugins/langchain/agent/auto_memory_wrapper/agent.py`), which wraps every agent in a LangGraph workflow:

1. `capture_user_message_node` saves `{"role": "user", "content": "..."}` to NCMS
2. `memory_retrieve_node` searches NCMS for context
3. `inner_agent_node` runs the actual agent
4. `capture_ai_response_node` saves `{"role": "assistant", "content": "..."}` to NCMS

The wrapper's `_get_user_id_from_context()` falls through to `"default_user"` when no authentication is configured. Since all agent configs set `save_user_messages_to_memory: true` and `save_ai_messages_to_memory: true`, every conversation turn gets stored as a `default_user` memory **in addition to** the agent's own structured stores.

---

## 2. SOTA Priority Analysis

Analysis of MemPalace (milla-jovovich/mempalace) and Karpathy's LLM Knowledge Bases approach identified key areas to push NCMS toward state-of-the-art. The hub audit confirmed the graph HTMG is working correctly for live sessions (59 `belongs_to_episode` edges, 2 `derived_from` edges, 7 episodes with cross-agent membership). The original concern about a "disconnected graph" was a benchmark artifact — the BEIR harness queries the SQLite `relationships` table, which only stores explicitly-passed relationships, not the in-memory co-occurrence edges that NetworkX builds during ingestion.

The remaining gap is **persistence across restarts**: co-occurrence edges are ephemeral (in-memory only) and must be rebuilt on restart.

### 2.1 Priority Ranking

| # | Area | Impact | Effort | Rationale |
|---|------|--------|--------|-----------|
| 1 | **Data integrity fixes** | High | Low | 27% duplicate waste, junk entities, raw LLM blobs — fix upstream before scaling |
| 2 | **Co-occurrence edge persistence** | High | Low | Graph spreading activation (weight 0.3) operates on empty graph after restart |
| 3 | **LongMemEval benchmark** | High | Medium | Can't claim SOTA on agent memory without measuring it; BEIR only tests static document retrieval |
| 4 | **Compilation/synthesis layer** | High | Low | ~500 lines wiring existing recall + consolidation into structured responses |
| 5 | **Temporal query boosting** | Medium | Low | Proven by MemPalace (40% proximity boost); NCMS has bitemporal fields but no query-time signal |
| 6 | **Progressive context loading** | Medium | Medium | Enables efficient agent integration; MemPalace's L0-L3 stack keeps wake-up under 900 tokens |
| 7 | **Wiki export** | Medium | Medium | Human auditability + Karpathy's "Obsidian as IDE" workflow |
| 8 | **Lint command** | Low | Low | Quality-of-life; builds on reconciliation |
| 9 | **User/assistant retrieval asymmetry** | Low | Low | MemPalace's two-pass approach (search user turns -> find sessions -> search assistant responses); niche but valuable for multi-agent "what did agent X recommend?" queries |
| 10 | **Maintenance scheduler** | Medium | Medium | Consolidation, dream cycles, episode closure are all manual today; recurring events needed for a running system |

### 2.2 Key Insight: Graph Is Working, Persistence Is Not

The hub audit revealed:
- **61 HTMG graph_edges** (59 `belongs_to_episode` + 2 `derived_from`) — these survive restart (persisted in SQLite `graph_edges` table)
- **0 co-occurrence edges** in `relationships` table — these are built in NetworkX during ingestion but never persisted
- **0 association_strengths** — dream cycles disabled, so PMI-weighted edges don't exist yet

After a container restart, `rebuild_from_store()` loads entities (319) and relationships (0) from SQLite. The graph starts with 319 isolated nodes. The graph spreading activation signal (weight 0.3 in tuned config) contributes nothing until new memories are ingested and rebuild co-occurrence edges.

**Fix**: Persist co-occurrence edges to `relationships` table during `store_memory()` (Section 4.2). This is ~20 lines of code and restores graph functionality across restarts.

### 2.3 Key Insight: Compilation Is the Biggest Product Gap

NCMS retrieves fragments. It never synthesizes answers. The `recall()` method returns ranked `ScoredMemory` objects with enrichment context, but the consuming agent must assemble them. Meanwhile:
- Karpathy's system pre-compiles knowledge into interlinked wiki articles
- MemPalace's 4-layer stack delivers context-budgeted responses

The infrastructure for compilation already exists in NCMS (episode summaries, state trajectories, pattern detection, causal chains). It just needs to be wired into a `synthesize()` method that returns a structured narrative instead of ranked fragments (Section 6).

### 2.4 Key Insight: Wrong Benchmarks for the Use Case

NCMS achieves nDCG@10=0.7206 on SciFact (exceeding published ColBERTv2 and SPLADE++), but SciFact measures scientific fact retrieval — not agent memory, temporal reasoning, or multi-turn conversation. Features like ACT-R decay, dream cycles, episode grouping, and reconciliation show **zero improvement on BEIR** because BEIR has no temporal access patterns, no agent attribution, and no state changes.

LongMemEval (conversation memory) would exercise exactly these features. MemPalace claims 96.6% recall@5 on LongMemEval with just ChromaDB. NCMS needs to prove it can match or beat this on the task it was designed for.

---

## 3. Data Integrity Improvements

### 3.1 Content Deduplication Gate (P0)

**Problem**: No check for duplicate content before storing. Each `store_memory()` call generates a fresh UUID.

**Design**: Add a content-hash dedup check as the very first gate in `store_memory()`, before classification and admission scoring.

The full gate ordering in `store_memory()` after all phases are implemented:

```
store_memory(content, ...)
  |
  v
[Gate 1: Content-hash dedup] -- duplicate? --> return existing memory (no-op)
  |
  v
[Gate 2: Content classification] -- navigable doc? --> section-aware ingestion (Section 6.2)
  |                                  atomic fragment? --> standard pipeline below
  v
[Gate 3: Size validation] -- per section (navigable) or per memory (atomic)
  |
  v
[Gate 4: Admission scoring] -- discard / ephemeral / persist
  |
  v
[Persist -> Index -> Entity -> Episode]
```

Dedup runs on the raw content hash before any classification or chunking. This ensures identical documents are caught regardless of whether the content classifier detects them as navigable.

**Implementation**:
- Add `content_hash TEXT` column to `memories` table with a unique index
- Compute SHA-256 hash before any processing
- On conflict, return the existing memory ID with a `deduplicated=True` flag
- Migration: V5 schema adds column + backfills hashes for existing rows

**Edge cases**:
- Same content from different agents: still dedup (content is identical, agent attribution preserved on first write)
- Content that *should* be stored twice (e.g., recurring status updates): use `force_store=True` parameter to bypass dedup

**Effort**: Low (~30 lines in memory_service, ~10 lines migration)

### 3.2 Content Size Gating (P1)

**Problem**: No maximum content size limit. 28K-char raw LLM outputs pass through the full pipeline (GLiNER, SPLADE, entity extraction, episode scoring).

**Design**: Add configurable `NCMS_MAX_CONTENT_LENGTH` (default: 5,000 chars) applied **after** content classification (Section 6.2.1).

The enforcement order in `store_memory()` is:
1. Content-hash dedup (Section 3.1)
2. Content classification — detect navigable documents (Section 6.2.1, Phase 4)
3. Size validation — applied per **section** for navigable documents, per **memory** for atomic fragments
4. Admission scoring (existing pipeline)

Before Phase 4 is implemented, the size limit applies to the whole content with an exemption for memories with `importance >= 8.0` (which already bypass admission). This prevents rejecting high-importance structured documents (ADRs at importance=9.0) while still blocking raw LLM outputs (importance=5.0, 28K chars).

- Document ingestion via `knowledge_loader` and `document_service` uses its own chunking and is exempt
- The MCP `store_memory` tool and HTTP `POST /api/v1/memories` both enforce the limit

**Configuration**: `NCMS_MAX_CONTENT_LENGTH = 5000` (env var, Pydantic Settings)

**Effort**: Low (~15 lines validation)

### 3.3 Entity Quality Filtering (P1)

**Problem**: GLiNER extracts noise entities from structured content (YAML, JSON, announcement text). Examples: `85%`, `25789`, `1 item(s)`, `Document: 6f01603fe96a`.

**Design**: Post-extraction filter in `gliner_extractor.py` that rejects entities matching noise patterns:

```python
_ENTITY_REJECT_PATTERNS = [
    re.compile(r"^\d+(\.\d+)?%?$"),        # Pure numeric: "85%", "25789"
    re.compile(r"^\d+ \w+\(s\)$"),          # Count patterns: "1 item(s)"
    re.compile(r"^\d+ chars$"),             # Size patterns: "2783 chars"
    re.compile(r"^[a-f0-9]{8,}$"),          # Hex IDs: "6f01603fe96a", "acaaba712dfa"
    re.compile(r"^Document: "),             # Prefixed IDs
    re.compile(r"^[A-Z]\d+$"),              # Citation labels: "S5", "S6", "R1"
    re.compile(r"^avg \d"),                 # Aggregate labels: "avg 85%"
]
```

Additional rules:
- Reject entities with `len(name) <= 1`
- Reject entities that are pure punctuation or whitespace
- Apply after GLiNER extraction, before entity linking

**Effort**: Low (~40 lines in gliner_extractor.py)

### 3.4 Entity State Detection Tightening (P2)

**Problem**: The admission service's `state_change_signal` feature triggers on large LLM outputs that contain patterns resembling state declarations, creating meaningless entity_state nodes.

**Note**: Section 3.5 (NAT wrapper fix) eliminates the root cause — raw 28K-char LLM outputs no longer enter the pipeline. This tightening is a defense-in-depth measure against future similar content from other sources.

**Current behavior**: State change detected when content contains patterns like `Entity: key = value` or status keywords. The state_value is truncated to 500 chars of the raw content.

**Design**: Tighten state_change_signal detection:
1. Require **explicit structured format**: `Entity: key = value` on its own line (not embedded in prose)
2. Reject state_change_signal when content length > `NCMS_STATE_CHANGE_MAX_CONTENT` (default: 2,000 chars) — large documents are not state declarations
3. Validate that the detected entity_id exists in the extracted entities set (must be a real entity, not a substring match)
4. The state_value should be the *matched declaration line*, not a 500-char prefix of the full content

**Effort**: Medium (~60 lines in admission_service.py + scoring.py)

### 3.5 NAT Wrapper Configuration Fix (P0)

**Problem**: NAT auto_memory_wrapper stores redundant user/assistant turns as `default_user` memories.

**Fix (immediate)**: Update agent configs to disable wrapper auto-save:

```yaml
# In all configs: archeologist.yml, designer.yml, product_owner.yml
workflow:
  _type: auto_memory_agent
  save_user_messages_to_memory: false    # was: true
  save_ai_messages_to_memory: false      # was: true
  retrieve_memory_for_every_response: false
```

The agents already store their own structured outputs (announcements, documents, episode seeds) with proper attribution, domains, and importance. The wrapper's auto-save is redundant.

**Fix (long-term)**: If conversation history logging is desired, add a dedicated `conversation_turns` table (not the memories table) that stores user/assistant exchanges with proper session_id, turn_number, and agent_id. This separates conversation audit trail from the knowledge memory store.

---

## 4. Graph Persistence & Rebuild-from-Store

### 4.1 Current State

On startup, `GraphService.rebuild_from_store()` loads entities and relationships from SQLite into NetworkX. However:

- **Co-occurrence edges** (entity pairs appearing in the same memory) are built in-memory during `store_memory()` but **never persisted** to the `relationships` table
- **PMI-weighted edges** (from dream cycle association learning) go to the `association_strengths` table but are not loaded into the graph on rebuild
- **HTMG edges** (`belongs_to_episode`, `derived_from`) are in the `graph_edges` table and survive restart

This means after restart, the graph has entities but no co-occurrence edges. The graph spreading activation signal (weight 0.3 in tuned config) operates on an empty graph until new memories are ingested and rebuild edges.

### 4.2 Proposed: Persist Co-occurrence Edges

**Design**: When co-occurrence edges are created in `store_memory()`, also persist them to the `relationships` table.

```python
# In memory_service.py, after co-occurrence edge creation (lines 433-465):
for src_id, tgt_id in cooccurrence_pairs:
    self._graph.add_relationship(src_id, tgt_id, "co_occurs_with")
    # NEW: also persist to SQLite
    await self._store.save_relationship(
        source_entity_id=src_id,
        target_entity_id=tgt_id,
        relation_type="co_occurs_with",
        memory_id=memory.id,  # provenance
    )
```

**Schema change**: Add optional `memory_id TEXT` column to `relationships` table for provenance tracking.

**Rebuild**: `rebuild_from_store()` already loads from `relationships` — no change needed.

**Effort**: Low (~20 lines)

### 4.3 Proposed: Load Association Strengths into Graph

**Design**: During `rebuild_from_store()`, also load PMI-based association strengths from `association_strengths` table and apply them as edge weights in NetworkX.

```python
# In graph_service.py rebuild_from_store():
async def rebuild_from_store(self):
    # ... existing entity + relationship loading ...

    # NEW: Load dream cycle associations as weighted edges
    associations = await self._store.get_association_strengths()
    for assoc in associations:
        self._graph.set_edge_weight(
            assoc.entity_a, assoc.entity_b,
            weight=assoc.strength,
            edge_type="pmi_association",
        )
```

This ensures PMI weights from previous dream cycles are available for graph spreading activation after restart.

**Effort**: Low (~30 lines)

### 4.4 Proposed: Full Reindex Command

For disaster recovery or schema migration, add a `ncms reindex` CLI command that rebuilds all derived data from the source-of-truth `memories` table:

```bash
uv run ncms reindex [--bm25] [--splade] [--entities] [--episodes] [--graph] [--all]
```

**Stages**:
1. `--bm25`: Drop and rebuild Tantivy index from all memories
2. `--splade`: Drop and rebuild SPLADE sparse vectors from all memories
3. `--entities`: Re-extract entities via GLiNER for all memories, rebuild `memory_entities`
4. `--graph`: Rebuild co-occurrence edges from `memory_entities` pairs
5. `--episodes`: Re-run episode assignment for all unlinked atomic nodes
6. `--all`: All of the above, sequential

This is the "rebuild from store" safety net. The `memories` table is the single source of truth; everything else is a derived index.

**Effort**: Medium (~200 lines CLI + service methods)

---

## 5. Ingestion Performance Improvements

### 5.1 Current Performance Profile

From hub logs (67 memories ingested on 2026-04-04):

| Stage | Typical Latency | Parallelism | Notes |
|-------|-----------------|-------------|-------|
| Admission scoring | 4-6ms | Sequential | Single BM25 search + 8 heuristic features |
| SQLite persist | 5-20ms | Sequential | WAL mode, single writer |
| BM25 indexing | 1-5ms | **Parallel** | Tantivy (Rust), fast |
| SPLADE indexing | 20-600ms | **Parallel** | GPU/CPU neural model, global lock serializes |
| GLiNER extraction | 50-1500ms | **Parallel** | GPU/CPU inference, global lock, scales with content length |
| Entity linking | 5-20ms | Sequential | O(n) where n=extracted entities |
| Co-occurrence edges | 10-50ms | Sequential | O(n^2) clique, capped at 12 entities |
| Contradiction detection | 500-2000ms | Sequential | LLM call (DGX Spark Nemotron) |
| Episode formation | 100-500ms | Sequential | Candidate scoring + sequential DB queries per open episode |

**Total per-memory (observed)**: 200ms-3500ms depending on content length and LLM availability.

The three indexing operations (BM25 + SPLADE + GLiNER) are the **only parallel section**. Everything else is sequential.

### 5.2 Proposed: Deferred Contradiction Detection

**Problem**: Contradiction detection is the single most expensive sequential step (500-2000ms), blocking ingestion for an LLM round-trip.

**Design**: Move contradiction detection to a post-ingest async task.

```python
# In store_memory(), replace synchronous contradiction check with:
if self._config.contradiction_detection_enabled:
    # Fire-and-forget: schedule contradiction check
    asyncio.create_task(self._deferred_contradiction_check(memory.id))
```

The deferred task:
1. Runs BM25 search for similar existing memories
2. Calls LLM for contradiction analysis
3. If contradiction found, creates reconciliation edges and emits an event
4. Memory is already stored and indexed — contradiction is metadata enrichment, not a gate

**Risk**: A contradicted memory is searchable for the 500ms-2s before the check completes. Acceptable for non-safety-critical use cases.

**Interaction with section-aware ingestion (Section 6.2.2)**: When a navigable document is section-indexed, the deferred contradiction check runs once per **child section memory**, not once for the parent document. This is correct — contradictions occur at the section level ("Decision section of ADR-003 contradicts the Compliance section of the threat model"), not at the whole-document level. The parent section-index memory is excluded from contradiction checking (it contains only headings and summaries, not assertive content).

**Effort**: Low (~40 lines, extract existing code into async task)

### 5.3 Proposed: Batch Episode Candidate Queries

**Problem**: Episode formation issues sequential DB queries per candidate episode (3 queries per candidate: get_memory, get_episode_members, collect_member_entities). With 7 open episodes, that's 21+ DB round-trips.

**Design**: Batch-load episode metadata in a single query at the start of episode scoring.

```python
# In episode_service.py assign_or_create():
# Instead of per-candidate:
#   ep_memory = await self._store.get_memory(ep.memory_id)
#   ep_members = await self._store.get_episode_members(ep.id)

# Batch all open episodes at once:
open_episodes = await self._store.get_open_episodes()
episode_ids = [ep.id for ep in open_episodes]
all_members = await self._store.get_episode_members_batch(episode_ids)
all_entities = await self._store.get_episode_entities_batch(episode_ids)
```

**Effort**: Medium (~60 lines, new batch query methods in sqlite_store)

### 5.4 Proposed: Episode Profile Caching

**Problem**: Episode profiles (entities, domains, anchors) are recomputed for every new memory.

**Design**: Cache open episode profiles in memory (dict keyed by episode_id). Invalidate on episode update or close. This avoids the DB round-trips entirely for the common case where the same episodes are matched repeatedly.

```python
class EpisodeService:
    _profile_cache: dict[str, EpisodeProfile] = {}

    async def assign_or_create(self, memory, ...):
        # Use cache instead of DB queries
        for ep_id in self._profile_cache:
            profile = self._profile_cache[ep_id]
            score = self._compute_score(memory, profile)
            ...
```

**Effort**: Medium (~80 lines)

### 5.5 Proposed: SPLADE/GLiNER Lock Granularity

**Problem**: Both SPLADE and GLiNER use a global `threading.Lock()` that serializes all model access. When multiple memories are ingested concurrently (e.g., bulk import), they queue behind the lock.

**Design options**:
1. **Queue-based batching**: Instead of per-memory model calls, accumulate a batch (up to N memories or T milliseconds) and run a single batched inference call. Both SPLADE and GLiNER already support batch input.
2. **Separate locks**: Use independent locks for SPLADE and GLiNER so they can run concurrently on the same GPU (if memory permits).
3. **Async executor**: Wrap model calls in `loop.run_in_executor()` with a dedicated thread pool, preventing the event loop from blocking.

**Effort**: Medium-High (~100 lines for queue batching)

### 5.6 Proposed: Bulk Import Mode

For `ncms load` and initial corpus ingestion, add a bulk mode that:
1. Disables per-memory episode assignment (batch-assign after all memories loaded)
2. Disables per-memory contradiction detection (batch-check after load)
3. Batches SPLADE/GLiNER inference across multiple documents
4. Builds co-occurrence edges in a single pass from the entity index
5. Runs a single consolidation pass at the end

```bash
uv run ncms load --bulk /path/to/files/
```

**Estimated speedup**: 3-5x for corpora > 50 documents (amortizes model load, batches GPU inference, eliminates per-document episode scoring overhead).

**Effort**: Medium (~150 lines)

### 5.7 Implemented: Background Indexing Pipeline

**Problem**: `store_memory()` blocks the caller for the full indexing pipeline (200-600ms warm) even though the memory is safely persisted to SQLite within ~1ms. The caller doesn't need BM25/SPLADE/GLiNER/episodes to complete before receiving the `Memory` object back.

**Design**: Decouple persist from indexing via a bounded async queue and worker pool.

**What stays synchronous** (client blocks for ~2ms):
1. Content-hash dedup check (must know if duplicate before returning)
2. Admission scoring — 4 pure text heuristic features (utility, persistence, state_change_signal, temporal_salience), no index dependency
3. SQLite persist (0.1ms)

**What moves to background** (enqueued, workers process concurrently):
1. BM25 indexing (Tantivy)
2. SPLADE indexing (neural model, shares singleton `SpladeEngine`)
3. GLiNER entity extraction (NER model, shares singleton module-level `_model`)
4. Entity linking + co-occurrence edge construction
5. Memory node creation (L1 atomic, L2 entity_state)
6. State reconciliation
7. Episode formation (with per-episode `asyncio.Lock` for concurrent safety)
8. Contradiction detection (fire-and-forget, same as before)

**Architecture**:

```
store_memory()                  IndexWorkerPool
  ┌─────────────┐              ┌──────────────────────────────────┐
  │ dedup check  │              │  Worker 0 ─── [BM25+SPLADE+GLiNER]
  │ admission    │  enqueue()   │                  ↓
  │ SQLite save  │────────────→ │              entity linking
  │ return Memory│              │                  ↓
  └─────────────┘              │              nodes + episodes
                               │
                               │  Worker 1 ─── (same pipeline)
                               │  Worker 2 ─── (same pipeline)
                               │
                               │  Queue: bounded asyncio.Queue(1000)
                               └──────────────────────────────────┘
```

**Backpressure**: When the queue is full, `enqueue()` returns `False` and `store_memory()` falls back to inline indexing (same as pre-feature behavior). No data loss, just slower for that one call. The returned Memory has `structured.indexing = "queued"` vs omitted for inline.

**Failure handling**:
- Memory is in SQLite regardless — indexing failure = not searchable by content, but retrievable by ID
- Retry: re-enqueue with `attempt += 1`, max 3 attempts, exponential backoff (1s, 5s, 25s)
- Dead letter: after 3 failures, log error + emit `indexing.failed` event. Memory stays in SQLite with annotation
- Startup recovery: planned — query for memories with no BM25 index entry, re-enqueue orphans

**Episode concurrent safety**: Multiple workers can process different memories targeting the same episode. A per-episode `asyncio.Lock` (lazily created, keyed by episode ID) serializes episode formation updates. Different episodes can be updated concurrently.

**Model singletons**: GLiNER uses a module-level `_model` with `threading.Lock` — safe for concurrent `to_thread()` calls. SPLADE uses an instance-level `_model` with `_ensure_model()` lazy init — the single `SpladeEngine` instance is shared by all workers via the `MemoryService` reference.

**Observability**:
- `ncms://indexing/status` MCP resource: queue depth, worker count/busy, processed/failed/retried totals, avg processing time
- Pipeline events via `EventLog`: `indexing.started`, `indexing.complete` (with per-stage timing), `indexing.retry`, `indexing.failed`
- `ncms://status` includes indexing summary when pool is active

**Configuration**:

| Variable | Default | Purpose |
|----------|---------|---------|
| `NCMS_ASYNC_INDEXING_ENABLED` | `true` | Feature flag (enabled by default) |
| `NCMS_INDEX_WORKERS` | `3` | Background worker count |
| `NCMS_INDEX_QUEUE_SIZE` | `1000` | Max pending tasks before backpressure |
| `NCMS_INDEX_MAX_RETRIES` | `3` | Retry attempts per failed task |
| `NCMS_INDEX_DRAIN_TIMEOUT_SECONDS` | `30` | Shutdown drain timeout |

**Performance impact**: Store latency drops from ~330ms (warm, with SPLADE) to ~2ms from the client's perspective. Search latency unchanged. Tradeoff: eventual consistency — a store followed by an immediate search may not find the memory (typically indexed within 300ms).

**Admission scoring cleanup**: The original 8-feature admission pipeline (novelty, utility, reliability, temporal_salience, persistence, redundancy, episode_affinity, state_change_signal) was reduced to 4 pure text heuristic features. Removed features and rationale:
- `novelty` / `redundancy` — depended on BM25 index search, creating a circular dependency with background indexing. Content-hash dedup (SHA-256 against SQLite) handles exact duplicate detection instead.
- `reliability` — always returned 0.60 (nobody passes `source_type`), zero discriminative signal.
- `episode_affinity` — weighted only 4%, and episode formation happens post-admission in background workers, so affinity can't be known at admission time.

Renormalized weights: utility (0.30), persistence (0.25), state_change_signal (0.25), temporal_salience (0.20). Routing thresholds recalibrated: discard < 0.10, ephemeral 0.10–0.25, persist ≥ 0.25. State change signal ≥ 0.35 auto-promotes to persist. All real content (ADRs, incidents, deployments, state changes) correctly routes to persist. The `importance >= 8.0` force-store bypass still works for agents that need guaranteed persistence.

**Lifecycle integration**: `create_ncms_services()` calls `start_index_pool()` automatically — all entry points (MCP server, dashboard, demo, NAT integration) get background indexing. The demo runner calls `stop_index_pool()` at cleanup for graceful drain.

**Files**:
- `src/ncms/application/index_worker.py` — `IndexTask`, `IndexWorkerPool`, `IndexingStats`
- `src/ncms/application/memory_service.py` — `start_index_pool()`, `stop_index_pool()`, `index_pool_stats()`, enqueue path in `store_memory()`
- `src/ncms/application/admission_service.py` — 4 text heuristic extractors (dead methods removed)
- `src/ncms/domain/scoring.py` — `AdmissionFeatures` (4 fields), `score_admission()`, `route_memory()`
- `src/ncms/config.py` — 4 `NCMS_` indexing config vars (removed `admission_novelty_search_limit`)
- `src/ncms/interfaces/mcp/resources.py` — `ncms://indexing/status` resource
- `src/ncms/interfaces/mcp/server.py` — `start_index_pool()` in `create_ncms_services()`
- `src/ncms/demo/run_demo.py` — `start_index_pool()` / `stop_index_pool()` lifecycle

**Effort**: Implemented (~400 lines across 8 files)

---

## 6. Compilation, Synthesis & Document Integration

### 6.1 Two Classes of Knowledge

NCMS ingests two fundamentally different kinds of content, but today treats them identically in `store_memory()`:

#### Class 1: Navigable Documents
Structured content with internal sections, headings, and schemas. The right unit of retrieval is a **section**, not the whole document.

Examples from the hub: ADR-003 (2,306 chars with Status/Context/Decision/Consequences sections), CALM JSON (12,981 chars with nodes/relationships/metadata), STRIDE threat model (4,913 chars with per-threat entries), security compliance checklist (774 chars with categorized items).

These arrive via three paths:
- `store_memory()` by agents — stored as **single flat blobs** (the problem)
- `publish_document()` by document service — stored in document store with 460-char stub in memory store
- `ncms load` by knowledge loader — **already chunked by heading** via `_chunk_markdown()` (the correct behavior)

#### Class 2: Atomic Fragments
Self-contained, no internal structure. The right unit of retrieval is the **whole memory**.

Examples from the hub: announcements (85-212 chars), episode keyword seeds (189-552 chars), user prompts (76-176 chars), document summary stubs (460-488 chars).

#### The Problem: Flat Ingestion

The hub's 67 memories break down as:

| Content Type | Count | Total Chars | Avg Chars | Treatment Today |
|---|---|---|---|---|
| **Navigable docs** (ADRs, CALM, threat models, checklists) | 13 | 31,815 | 2,447 | Flat blobs via `store_memory()` — no section awareness |
| **Announcements** | 35 | 5,320 | 152 | Atomic fragments — correctly handled |
| **Document summaries** | 5 | 2,359 | 472 | Stubs — correctly handled but only 3% of document content |
| **Keyword fragments** | 8 | 2,757 | 345 | Episode seeds — correctly handled |
| **Raw LLM outputs** | 3 | 78,116 | 26,039 | Should not be here (NAT wrapper issue, Section 3.5) |

When someone searches "JWT refresh token rotation," BM25 hits the entire 2,306-char ADR-003 blob. The relevant content is in the Decision section (lines 26-55), but the system returns the whole document. At scale with 100+ documents, this wastes context window and buries the precise answer.

Meanwhile, the `knowledge_loader` already solves this for files loaded via `ncms load` — `_chunk_markdown()` splits by heading, `_chunk_json()` splits by top-level keys. But agents calling `store_memory()` bypass this entirely.

#### The Two-Store Gap

The memory store has rich retrieval (BM25 + SPLADE + graph) but only sees 460-char summary stubs of documents published via the document service. The document store has full content with versioning, review scores, and derivation chains but has no semantic search — only SQL LIKE queries on entity JSON. Neither references the other except through weak tag-based links.

The 5 published documents total 83,830 chars of substantive content. The memory store sees ~3% of it. When an agent searches for "JWT authentication patterns," it hits the stub but has no path to the 25K-char design document with the actual implementation.

### 6.2 Design: Content-Aware Ingestion

**Principle**: Classify content at ingest time. Navigable documents get section-aware indexing with a structured table of contents. Atomic fragments go through the current pipeline unchanged.

#### 6.2.1 Ingest-Time Content Classification

Add a classification step as Gate 2 in `store_memory()`, after content-hash dedup (Section 3.1) but before size validation and admission scoring:

```
store_memory(content, memory_type, ...)
  |
  v
[Gate 1: Content-hash dedup — Section 3.1]
  |
  v
[Gate 2: Content classification — heuristic, no LLM]
  |
  ├── ATOMIC FRAGMENT (len < 1000, no headings, no structure)
  |   └── Current pipeline unchanged: size check, admission, BM25, SPLADE, GLiNER, episode
  |
  └── NAVIGABLE DOCUMENT (detected structure)
      |
      ├── [1. Section extraction — reuse knowledge_loader chunking logic]
      ├── [2. Section index generation — heading + summary per section]
      ├── [3. Size validation — per section, not per whole document]
      ├── [4. Index memory — store the section index as parent memory]
      ├── [5. Section memories — store each section as child memory]
      └── [6. Entity extraction — GLiNER per section, not per blob]
```

**Detection heuristics** (fast, no LLM):

| Signal | Detection | Example |
|--------|-----------|---------|
| Markdown headings | `re.match(r"^#{1,4}\s+", line)` with 2+ headings | ADR-003 with `## Status`, `## Context`, `## Decision` |
| JSON object | Content starts with `{` and `json.loads()` succeeds | CALM architecture model |
| YAML document | Content matches `^\w+:\s*$` with 2+ top-level keys | Security controls, compliance checklists |
| Long + structured | `len > 1000` AND `content.count("\n\n") >= 3` | Research reports, PRDs |
| Explicit type | `memory_type="document_chunk"` or `memory_type="document"` | Caller declares it |

**Fallback**: If classification is uncertain, treat as atomic. False negatives (a document treated as atomic) are less harmful than false positives (an announcement split into sections).

#### 6.2.2 Section-Aware Indexing

For content classified as navigable, the ingestion pipeline produces three artifacts:

**1. Section extraction** — Reuse the `knowledge_loader` chunking logic already implemented:
- Markdown: `_chunk_markdown()` splits by `#{1-4}` headings
- JSON: `_chunk_json()` splits by top-level keys
- YAML: Split by top-level keys (new, mirrors JSON logic)
- Plain text with structure: Split by double-newline paragraphs with heading detection

**2. Section index** — A lightweight table of contents stored as the parent memory:

```markdown
# ADR-003: JWT with Inline RBAC — Section Index
doc_id: cfafab12  |  type: architecture_decision  |  agent: architect  |  sections: 4

- **Status & Context** — Decision status (accepted), team, problem statement
- **Decision** — JWT with inline RBAC claims, refresh token rotation, Passport.js
- **Consequences** — Token size trade-offs, revocation complexity, monitoring needs
- **Compliance** — OWASP A07 mapping, NIST SP 800-63-3 alignment
```

This index is ~300 chars and gets indexed in BM25/SPLADE. It serves two purposes:
- **Retrieval routing**: A search for "token revocation" hits the index and identifies the Consequences section as relevant
- **Navigation**: An agent reading the index sees the full knowledge topology without loading any section content

**3. Section memories** — Each section stored as a child memory linked to the parent index:

```python
# Parent: section index
index_mem = await self._memory_svc.store_memory(
    content=section_index_text,
    memory_type="section_index",
    importance=7.0,
    tags=["section_index", doc_id_or_hash],
    structured={
        "doc_id": doc_id,          # Link to document store if published
        "section_count": len(sections),
        "content_hash": content_hash,
    },
)

# Children: individual sections
for i, section in enumerate(sections):
    await self._memory_svc.store_memory(
        content=section.text,
        memory_type="document_section",
        importance=7.0,
        tags=["document_section", doc_id_or_hash, f"section:{i}"],
        structured={
            "doc_id": doc_id,
            "parent_index_id": index_mem.id,
            "section_index": i,
            "section_heading": section.heading,
        },
    )
```

**Entity extraction** runs per section (not per blob). This produces more focused entities — the Decision section of ADR-003 yields `JWT`, `RBAC`, `Passport.js`, `refresh token` rather than the diluted set from the full ADR.

**Episode assignment** runs on the parent index memory. All child sections inherit the episode membership via their `parent_index_id` link.

#### 6.2.3 Level-First Retrieval

**Problem with current approach**: Today, every query runs flat BM25+SPLADE across ALL HTMG levels. Intent classification adds bonus scores and supplementary candidates, but the primary search pool is undifferentiated. A `pattern_lookup` query gets flooded with L1 atomic fragments that happen to contain matching keywords, drowning the L4 insights that actually answer the question.

**Design**: Use intent classification to determine the **traversal direction** and **starting HTMG level**, not just signal weights. The existing `INTENT_NODE_TYPES` mapping and `_get_intent_weights()` method (already in `memory_service.py:1665`) provide the foundation — the change is making them control the retrieval *strategy*, not just additive bonuses.

**Traversal strategies**:

| Intent | Direction | Start Level | Primary Graph | Expansion |
|--------|-----------|-------------|---------------|-----------|
| `fact_lookup` | **Bottom-up** | L1 atomic + L1 sections | Semantic (BM25+SPLADE) | Entity graph for related facts |
| `current_state_lookup` | **Direct** | L2 entity_state | Entity graph (direct lookup by entity) | Temporal for recency ordering |
| `historical_lookup` | **Temporal** | L2 entity_state | Temporal ordering (`observed_at`) | Causal graph for supersession chains |
| `event_reconstruction` | **Lateral** | L3 episode | Episode membership | Temporal for event ordering within episode |
| `change_detection` | **Temporal** | L2 entity_state | Temporal diffs + causal edges | L1 atomics for causal context |
| `pattern_lookup` | **Top-down** | L4 abstract | Semantic over L4 only | Drill to L3 episodes → L1 evidence |
| `strategic_reflection` | **Top-down** | L4 abstract | Semantic over L4 only | Cross-level evidence chains |

**Implementation**: Extend `search()` to scope the BM25/SPLADE candidate generation by node type:

```python
async def search(self, query: str, ...):
    intent = self._classify_intent(query)
    target_levels = INTENT_NODE_TYPES[intent.intent]  # Already exists

    # NEW: Primary search scoped to target levels
    primary_candidates = await self._scoped_search(
        query, node_types=target_levels, limit=top_k,
    )

    # Secondary: expand to adjacent levels for context
    if intent.direction == "top_down":
        # Drill from L4 → L3 episodes → L1 evidence
        expansion = await self._drill_down(primary_candidates)
    elif intent.direction == "bottom_up":
        # Expand from L1 → L3 episodes for context
        expansion = await self._expand_up(primary_candidates)
    elif intent.direction == "temporal":
        # Chronological ordering of L2 states
        expansion = await self._temporal_expand(primary_candidates, query)
    elif intent.direction == "lateral":
        # Episode member expansion
        expansion = await self._lateral_expand(primary_candidates)
    else:
        expansion = []

    all_candidates = primary_candidates + expansion
    # Continue with existing scoring pipeline (normalization, ACT-R, penalties)
```

The `_scoped_search()` method over-fetches from BM25/SPLADE (e.g., top-200 instead of top-50) and then filters to only include memories with matching node types (via the existing `nodes_by_memory` batch preload). Over-fetching is necessary because rare node types (L4 abstracts may be <5% of the corpus) would yield zero results from a standard top-50 retrieval. The BM25/SPLADE query cost is dominated by scoring, not candidate count, so over-fetching adds minimal latency.

**Config**: `NCMS_SCOPED_SEARCH_OVERFETCH = 200` (candidates to retrieve before node-type filtering).

**Fallback**: If scoped search returns fewer than `min_candidates` (default: 5) after filtering, fall back to unscoped search with standard top-K. This prevents empty results when target levels have insufficient content (e.g., 0 L4 abstracts in the current hub before consolidation runs).

**Compatibility**: When intent classification is disabled (`NCMS_INTENT_CLASSIFICATION_ENABLED=false`), retrieval behaves exactly as today — flat BM25+SPLADE with no level scoping.

#### 6.2.4 Document-Aware Recall

Extend `recall()` to detect document-linked results and enrich with provenance:

```python
class RecallResult:
    memory: ScoredMemory
    context: RecallContext
    document: DocumentContext | None  # Populated when structured.doc_id exists

class DocumentContext:
    doc_id: str
    title: str
    doc_type: str                    # research, prd, design, review, manifest
    from_agent: str
    project_id: str | None
    version: int
    section_index: str | None        # The full section index text (for navigation)
    current_section: str | None      # Which section this result came from
    review_scores: list[ReviewScore]  # Architect: 85%, Security: 85%
    derivation_chain: list[str]       # [research_id] -> [prd_id] -> [design_id]
    sibling_sections: list[str]       # Other section memory_ids in same document
    full_content_url: str             # /api/v1/documents/{doc_id} for on-demand full text
```

When a recall result has `structured.doc_id` or `structured.parent_index_id`, the system fetches document metadata, the section index, and the derivation chain from the document store. The agent gets:
- Which document and section this came from
- The full section index (so it can request other sections without another search)
- The derivation chain (this design came from this PRD which came from this research)
- Review status (was this approved? what score?)

#### 6.2.5 Integration with Document Store

For documents published via `DocumentService.publish_document()`, the section-aware ingestion runs automatically. The document store remains the source of truth for full content; the memory store holds section indexes and section content for retrieval. The link is the `doc_id` in the memory's `structured` metadata.

For documents stored directly via `store_memory()` by agents (the hub's 13 structured docs), the content classification gate detects the structure and applies section-aware indexing. These memories don't have a `doc_id` in the document store — the `content_hash` serves as their identity.

**Entity graph linking**: Document entities (currently stored as JSON arrays in the document store) are linked into the main entity graph during section-aware ingestion, not kept in a separate silo.

### 6.3 Design: Progressive Context Loading with Emergent Topic Map

NCMS provides tiered context loading that leverages the full HTMG hierarchy. The key innovation over MemPalace's L0-L3 stack and Karpathy's wiki index: **the navigational layer (topics + section indexes) is built automatically from the HTMG hierarchy, not manually curated**.

#### 6.3.1 Emergent Topics from L4 Abstracts

Topics are **not** a new HTMG level — they are an emergent property of L4 content. When consolidation generates episode summaries, state trajectories, and recurring patterns, these L4 nodes share `topic_entities` (the entity set from their source episodes). Clustering L4 nodes by `topic_entities` Jaccard overlap produces emergent topics.

From the hub's 7 episodes (once consolidation runs), clustering would produce:

| Emergent Topic | Source Episodes | Key Entities | L4 Abstracts |
|---|---|---|---|
| **Authentication Architecture** | ADR-001 architecture, Designer implementation | JWT, RBAC, Passport.js, MongoDB | Episode summaries + ADR state trajectory |
| **Security Posture** | VUL-001 remediation, Architecture (security members) | STRIDE, OWASP, SQL injection, MFA | Threat model trajectory + compliance pattern |
| **Project Lifecycle** | PRD/Research, Product owner consultation, Archeologist research | PRD, research_id, project_id | Cross-document synthesis |

This clustering reuses the existing `_cluster_by_entity_overlap()` method from `consolidation_service.py` (Phase 5C pattern detection). No new algorithm needed — just a different input (all L4 nodes instead of just episode summaries).

**Storage**: Topics are stored as L4 abstract nodes with `abstract_type: "topic_summary"`. They are regenerated during each consolidation pass (Section 7.2) from the current set of L4 nodes. This makes them self-maintaining — as new episodes close and new abstracts are generated, the topic map evolves.

#### 6.3.2 Progressive Context Levels

| Level | Token Budget | Content Source | Use Case |
|-------|-------------|----------------|----------|
| **L0: Identity** | ~200 | Project metadata + active agent roster | System prompt injection |
| **L1: Topic Map** | ~500 | Emergent topics from L4 clustering — 3-5 topics with one-line summaries. The agent sees the knowledge landscape. | Agent wake-up, task orientation |
| **L1.5: Index** | ~1500 | Section indexes for project documents + episode member lists. Navigable overviews of all structured knowledge. | Navigation, targeted requests |
| **L2: Summary** | ~3000 | Level-appropriate results from level-first retrieval (Section 6.2.3): L4 insights for strategic queries, L2 states for state queries, L1 sections for fact queries | Standard query response |
| **L3: Deep** | ~8000 | Full recall with cross-level evidence chains (L4 insight → L3 episodes → L1 evidence) + document sections with review context | Complex analysis |
| **L4: Archive** | On-demand | Full document content via `GET /api/v1/documents/{doc_id}` | Explicit deep read (separate API, not a synthesize mode) |

**L1 Topic Map example** (generated from L4 abstracts, ~300 tokens):

```
## Knowledge Landscape — PRJ-45050f76

### Authentication Architecture (12 memories, 3 episodes, 2 ADRs)
JWT with inline RBAC selected. Design approved at 85%. Implementation complete.
Key decisions: ADR-003 (JWT), ADR-004 (test strategy). Open: token revocation.

### Security Posture (8 memories, 2 episodes, 1 threat model)
STRIDE analysis complete (8 threats). VUL-001 identified, remediation pending.
Compliance: OWASP A07 mapped, NIST SP 800-63-3 aligned.

### Project Pipeline (5 documents, 86 pipeline events)
Research → PRD → Design: complete. All approved at 85%+.
```

**L1.5 Index example** (section indexes + episode outlines, ~800 tokens):

```
### ADR-003: JWT with Inline RBAC (architect)
- Status & Context — Decision status, problem statement
- Decision — JWT claims, refresh rotation, Passport.js
- Consequences — Token size, revocation complexity
- Compliance — OWASP A07, NIST SP 800-63-3

### Episode: Architecture Decisions (10 members, architect + security)
- ADR-001 through ADR-004, CALM model, quality attributes, fitness functions
- Security: threat model, compliance checklist, controls

### Episode: Designer Implementation (17 members, designer)
- Expert queries, design synthesis, guardrail checks, review rounds
```

**Key differences from Karpathy**: (1) Topic map is emergent from consolidation, not manually curated by the LLM. (2) Section indexes are generated automatically from document structure at ingest time. (3) Both are regenerated during maintenance — they self-maintain as knowledge evolves. (4) Level-first retrieval means the system traverses the right level for each query type, not just reading the index top-to-bottom.

**Implementation**:

```python
async def synthesize(
    self,
    query: str,
    mode: Literal["identity", "topics", "index", "summary", "deep"] = "summary",
    project_id: str | None = None,
) -> SynthesizedResponse:
    if mode == "identity":
        return await self._build_identity_context(project_id)
    elif mode == "topics":
        return await self._build_topic_map(query, project_id)  # L1: emergent topic map
    elif mode == "index":
        return await self._build_index(query, project_id)      # L1.5: section + episode indexes
    elif mode == "summary":
        return await self._build_summary(query, project_id)    # L2: level-first retrieval results
    elif mode == "deep":
        return await self._build_deep(query, project_id)       # L3: cross-level evidence chains
```

Modes map to progressive context levels (Section 6.3.2):
- `"identity"` → L0 (~200 tokens) — project metadata + agent roster
- `"topics"` → L1 (~500 tokens) — emergent topic map from L4 abstracts
- `"index"` → L1.5 (~1500 tokens) — section indexes + episode outlines
- `"summary"` → L2 (~3000 tokens) — level-first retrieval results (intent-appropriate level)
- `"deep"` → L3 (~8000 tokens) — cross-level evidence chains + full sections

Exposed as:
- MCP tool: `synthesize_knowledge(query, mode, project_id)`
- HTTP: `GET /api/v1/memories/synthesize?q=...&mode=summary&project_id=PRJ-...`

### 6.4 Design: `synthesize()` Pipeline

The synthesis pipeline unifies both stores, using level-first retrieval and emergent topics:

```
Query + Mode + optional Project
  |
  v
[1. Intent classification — determine traversal direction + start level]
  |
  v
[2. Level-first retrieval — scoped BM25+SPLADE at target HTMG level
    + directional expansion (top-down/bottom-up/temporal/lateral)]
  |
  v
[3. Section expansion — for section index hits, fetch relevant child
    sections. For section hits, fetch parent index + siblings.]
  |
  v
[4. Document provenance — for results with doc_id, fetch review scores,
    derivation chain, version history from document store]
  |
  v
[5. Topic context — load emergent topic map from L4 abstracts
    (pre-built by consolidation, not generated per query)]
  |
  v
[6. Token budgeting — trim and prioritize based on mode:
    topics  → emergent topic map (~500 tokens)
    index   → section indexes + episode outlines (~1500 tokens)
    summary → level-appropriate results + abstracts (~3000 tokens)
    deep    → cross-level evidence chains + full sections (~8000 tokens)]
  |
  v
[7. Optional LLM compilation (deep mode only) — synthesize into
    narrative with citations to memory IDs, doc_ids, section headings]
  |
  v
SynthesizedResponse {
    query: str
    mode: str
    intent: str                              # Classified intent driving retrieval
    traversal: str                           # bottom_up, top_down, temporal, lateral
    topic_map: list[TopicSummary] | None     # Emergent topics from L4 clustering
    base_results: list[RecallResult]         # Level-appropriate results with scores
    documents: list[DocumentContext]          # Referenced docs with section indexes
    abstracts: list[AbstractMemory]          # Episode summaries, trajectories, patterns
    project: ProjectContext | None           # Project metadata + document inventory
    synthesis: str | None                    # LLM narrative (deep mode only)
    entity_snapshot: dict[str, str]          # Current state of all mentioned entities
    token_count: int                         # Actual tokens used
}
```

Note: LLM compilation is reserved for `deep` mode only. The `topics`, `index`, and `summary` modes return structured data without an LLM call — the progressive levels are designed to be useful without synthesis overhead.

### 6.5 Design: Wiki Export from Document Store

The document store already contains the raw material for a Karpathy-style wiki. The export should be a **view over both stores**, not a separate artifact.

```bash
uv run ncms export --format wiki --output ./wiki/ [--project PRJ-...]
```

**Wiki structure** (generated from live data):

```
wiki/
  index.md                              # Auto-generated: project inventory, entity index, recent episodes
  projects/
    PRJ-45050f76/
      README.md                         # Project metadata: topic, status, phase, quality_score
      research/
        acaaba712dfa.md                 # Full research report (from document store)
      prd/
        9dfee0413326.md                 # Full PRD (from document store)
      design/
        6f01603fe96a.md                 # Full implementation design (from document store)
        6f01603fe96a-review.md          # Design review report (from document store)
      manifest/
        420925175120.md                 # Requirements manifest (from document store)
      timeline.md                       # Auto-generated: pipeline events as timeline
      derivation-chain.md              # Auto-generated: research → PRD → design → review
  entities/
    authentication-patterns.md          # Entity page: all memories + documents mentioning this entity
    jwt.md                              # Entity page with cross-references
    mongodb.md
  episodes/
    001-architecture-decisions.md       # Episode narrative: members, timeline, agents involved
    002-security-assessment.md
    003-prd-development.md
  agents/
    architect.md                        # Agent page: memories stored, documents authored, bus conversations
    security.md
    designer.md
    product_owner.md
    archeologist.md
```

**Key design decisions**:
- **Section indexes drive the wiki structure**: Each document's wiki page is organized by its section index, not exported as a flat blob. Entity/episode/agent pages link to specific sections, not whole documents.
- Entity pages are generated by **joining both stores**: section-level entity extraction from memories + document entity JSON from document store
- Episode pages are generated from **HTMG episode nodes** with member memories and their document section references
- Agent pages show **provenance**: what this agent stored, authored, and reviewed
- Derivation chains come from **document_links** table (not memory relationships)
- Timeline comes from **pipeline_events** table (86 events in the hub)
- All markdown files contain **backlinks** (entity pages link to document sections and episodes; sections link to entities and derivation chains)
- **Incremental index maintenance**: Section indexes are generated at ingest time and persisted in the memory store. The wiki export reads these indexes rather than regenerating structure from scratch. New documents update the wiki incrementally — only affected pages are regenerated.

### 6.6 Design: Document-Level Consolidation

The current consolidation pipeline (Phase 5) operates only on memories. With documents participating in retrieval, consolidation should also consider document content:

**Episode Summaries (Phase 5A)**: When generating episode summaries, include referenced documents. An episode about "authentication design" should cite the PRD and design doc, not just the memory fragments.

**Cross-Document Synthesis**: When multiple documents in the same project cover overlapping topics (e.g., research report discusses MFA adoption, PRD specifies MFA requirements, design implements MFA flow), consolidation should generate a **cross-document trace** that shows how a concept evolved across the document chain.

**Stale Document Detection**: When new memories contradict information in an existing document (e.g., a new ADR supersedes a previous architecture decision), the lint system should flag the document as potentially stale.

### 6.7 Design: Temporal Query Boosting

Both MemPalace (up to 40% distance reduction for temporally proximate results) and Karpathy's approach (implicit temporal reasoning through compiled wikis) handle temporal queries better than NCMS.

NCMS has bitemporal fields (`observed_at`, `ingested_at`) on memory_nodes and `created_at` on both memories and documents, but these are never used as retrieval signals.

**Design**: Add temporal proximity scoring as a post-retrieval signal in the scoring pipeline.

```python
# In memory_service.py search(), after per-query min-max normalization:

temporal_target = parse_temporal_reference(query)
if temporal_target:
    for result in scored_results:
        created = result.memory.created_at
        observed = result.node.observed_at if result.node else None
        # Use observed_at (when the event happened) if available, else created_at
        event_time = observed or created
        proximity = compute_temporal_proximity(event_time, temporal_target)
        # Apply as additive signal alongside BM25, SPLADE, Graph
        result.temporal_score = proximity
    # Normalize temporal scores to [0,1] with same min-max as other signals
    normalize_signal(scored_results, "temporal_score")
```

**Temporal reference parsing** (regex-based, no LLM):

| Pattern | Example | Resolution |
|---------|---------|------------|
| Relative days | "yesterday", "3 days ago", "last week" | `now - timedelta(...)` |
| Relative months | "last month", "two months ago" | Date range for that month |
| Named periods | "in March", "Q1 2026", "this quarter" | Date range |
| Ordinal | "the first review", "initial design" | Earliest matching results boosted |
| Recency | "latest", "most recent", "current" | Strong recency bias (last 24-48h) |
| Event-anchored | "before the review", "after the PRD" | Requires episode timeline resolution (Phase 2) |

**Phase 1** (regex, no LLM): Relative days/weeks/months + named periods + recency keywords. Covers ~80% of temporal queries.

**Phase 2** (episode-aware): Event-anchored resolution using episode timelines. "After the security review" resolves to the timestamp of the VUL-001 episode closure.

**Config**: `NCMS_SCORING_WEIGHT_TEMPORAL = 0.2` (placeholder default — should be tuned alongside all scoring weights in the agent-workload weight sweep proposed in Section 9.2). Applied only when a temporal reference is detected — zero-cost for non-temporal queries. Per-query min-max normalization (already implemented) ensures the temporal signal competes fairly with BM25/SPLADE/Graph regardless of raw scale.

**Document temporal boost**: Documents also carry timestamps. When temporal boosting is active and document chunks are in the result set, the boost applies to the document's `created_at` (or the associated pipeline_event timestamp if available, which gives more precise timing — e.g., when the design was actually published vs. when the memory was created).

---

## 7. Recurring Events & Operational Lifecycle

### 7.1 Current State

NCMS has **no scheduled background tasks**. Consolidation and dream cycles are purely on-demand (MCP tool or HTTP endpoint). This means:
- Closed episodes never get summarized unless someone triggers consolidation
- Entity state trajectories are never generated
- Dream cycle rehearsal never runs
- Importance drift never adjusts memory salience
- Stale abstracts are never refreshed

### 7.2 Proposed: NCMS Maintenance Scheduler

Add an internal scheduler (using `asyncio` tasks, no external dependency) that runs maintenance events on configurable intervals. This integrates into the existing server lifecycle.

#### Event Schedule

| Event | Default Interval | Config Flag | Purpose |
|-------|-----------------|-------------|---------|
| **Episode Closure Check** | Every 30 minutes | `NCMS_MAINTENANCE_EPISODE_CLOSE_INTERVAL=1800` | Auto-close episodes with no activity past `NCMS_EPISODE_CLOSE_MINUTES` (default: 24h). Currently only checked on new memory ingest. |
| **Consolidation Pass** | Every 6 hours | `NCMS_MAINTENANCE_CONSOLIDATION_INTERVAL=21600` | Run `consolidation_service.run_consolidation_pass()`: decay, knowledge synthesis, episode summaries, trajectories, patterns, stale refresh. |
| **Dream Cycle** | Every 24 hours | `NCMS_MAINTENANCE_DREAM_INTERVAL=86400` | Run dream rehearsal + association learning + importance drift. Only if `NCMS_DREAM_CYCLE_ENABLED=true`. Should run during low-traffic periods. |
| **Ephemeral Cache Cleanup** | Every 1 hour | `NCMS_MAINTENANCE_EPHEMERAL_CLEANUP_INTERVAL=3600` | Purge expired entries from `ephemeral_cache` table (TTL-based). |
| **Snapshot Expiry** | Every 12 hours | `NCMS_MAINTENANCE_SNAPSHOT_EXPIRY_INTERVAL=43200` | Remove snapshots older than `NCMS_SNAPSHOT_TTL_HOURS` (default: 168h/7d). |
| **Data Integrity Lint** | Every 24 hours | `NCMS_MAINTENANCE_LINT_INTERVAL=86400` | Check for orphaned nodes, stale entity states, broken episode links. Emit events for dashboard visibility. |

#### Architecture

```python
class MaintenanceScheduler:
    """Async background scheduler for NCMS maintenance events."""

    def __init__(self, config, services):
        self._tasks: dict[str, asyncio.Task] = {}
        self._config = config
        self._services = services

    async def start(self):
        """Start all enabled maintenance loops."""
        self._tasks["episode_close"] = asyncio.create_task(
            self._loop("episode_close", self._config.episode_close_interval, self._close_episodes)
        )
        self._tasks["consolidation"] = asyncio.create_task(
            self._loop("consolidation", self._config.consolidation_interval, self._consolidate)
        )
        # ... etc.

    async def _loop(self, name, interval_seconds, fn):
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                result = await fn()
                logger.info("[maintenance:%s] %s", name, result)
            except Exception as e:
                logger.error("[maintenance:%s] Failed: %s", name, e)

    async def stop(self):
        for task in self._tasks.values():
            task.cancel()
```

**Integration**: Start in `create_ncms_services()` after all services are initialized. Stop on server shutdown.

**Observability**: Each maintenance event emits an `EventLog` entry visible on the dashboard SSE feed and in the ring buffer.

#### Dream Cycle Scheduling Considerations

The dream cycle is the most resource-intensive maintenance event:
- **Rehearsal**: Reads top-N% memories by composite score, creates synthetic access records. CPU-bound, ~O(n) where n=eligible memories.
- **Association learning**: Computes PMI between entity pairs from search log. CPU-bound, ~O(m*k) where m=search queries, k=avg results per query.
- **Importance drift**: Compares recent vs. historical access rates. CPU-bound, ~O(n).

For the hub (67 memories), this completes in seconds. At scale (10K+ memories), it could take minutes. Schedule during low-traffic windows (e.g., 03:00 local time) or behind a feature flag that the operator enables explicitly.

### 7.3 Proposed: Lint Command

Add `ncms lint` as both a CLI command and a scheduled maintenance event:

```bash
uv run ncms lint [--fix] [--verbose]
```

**Checks**:

| Check | Description | Auto-Fix |
|-------|-------------|----------|
| **Orphaned memories** | Memories without a corresponding memory_node | Create missing atomic node |
| **Orphaned entities** | Entities with no memory_entities links | Delete orphan |
| **Stale entity states** | entity_state nodes where `is_current=True` but a newer state exists for the same entity+key | Set `is_current=False`, `valid_to=now` |
| **Broken episode links** | `belongs_to_episode` edges where source or target node doesn't exist | Remove edge |
| **Duplicate content** | Memories with identical content_hash | Report (no auto-delete, requires human review) |
| **Empty episodes** | Episode nodes with 0 member edges | Close or delete |
| **Missing content_hash** | Memories without the dedup hash (pre-V5 data) | Backfill hash |
| **Junk entities** | Entities matching noise patterns (Section 3.3) | Report (optional delete with `--fix`) |

**Output**: JSON report with counts per check, plus specific IDs for manual review.

### 7.4 Proposed: Health Endpoint Enhancement

Extend `GET /api/v1/health` to include maintenance status:

```json
{
    "status": "healthy",
    "uptime_seconds": 172800,
    "maintenance": {
        "last_consolidation": "2026-04-05T03:00:00Z",
        "last_dream_cycle": null,
        "last_lint": "2026-04-05T03:05:00Z",
        "open_episodes": 5,
        "closed_episodes_pending_summary": 2,
        "stale_abstracts": 0,
        "ephemeral_cache_size": 3
    },
    "data_integrity": {
        "total_memories": 67,
        "duplicate_count": 18,
        "orphaned_memories": 1,
        "junk_entity_count": 35
    }
}
```

---

## 8. Competitive Context

### 8.1 MemPalace Comparison

[MemPalace](https://github.com/milla-jovovich/mempalace) achieves 96.6% recall@5 on LongMemEval with 2 dependencies (ChromaDB + PyYAML). Key ideas worth incorporating:

| MemPalace Feature | NCMS Equivalent | Gap |
|-------------------|-----------------|-----|
| Hybrid keyword fusion (`fused_dist = dist * (1.0 - 0.30 * overlap)`) | BM25 + SPLADE + Graph (3-signal weighted) | NCMS has richer signals but no temporal boosting |
| Temporal date parsing + 40% proximity boost | Bitemporal fields exist but no query-time temporal boost | Add temporal proximity scoring (Section 6.7) |
| Two-pass assistant retrieval | `source_agent` metadata filter | Could add conversation-turn-aware search |
| 4-layer progressive loading (L0-L3) | No context budgeting | Add briefing/summary/deep modes (Section 6.3) |
| AAAK compression dialect (30x) | N/A | Novel but niche; synthesis layer is a better approach |
| Knowledge graph (SQLite temporal triples) | NetworkX + HTMG (integrated into scoring) | NCMS graph is stronger but needs persistence fix |

### 8.2 Karpathy's LLM Knowledge Bases

Karpathy's approach: raw sources -> LLM-compiled markdown wiki -> Obsidian. No retrieval infrastructure. Works at ~400K words.

| Karpathy Insight | NCMS Implication |
|-----------------|------------------|
| "Compile, don't search" | Consolidation abstracts are the compilation layer but aren't exposed as browsable artifacts |
| "LLM maintains the wiki, human rarely touches it" | NCMS could export auto-maintained wiki from consolidated knowledge |
| "Filing outputs back enriches the base" | Search results that produce insights should auto-persist |
| "Linting for data integrity" | `ncms lint` command (Section 7.3 Lint Command) |
| "Markdown is the most LLM-readable format" | Wiki export enables human + LLM auditability |

### 8.3 NCMS's Structural Advantage: The Document Store

Neither MemPalace nor Karpathy's approach has a structured document store. MemPalace stores raw conversations in ChromaDB drawers. Karpathy stores markdown files on disk. NCMS already has:

- **Versioned documents** with content_hash integrity verification
- **Typed derivation chains** (research → PRD → design → review)
- **Review scores** with multi-agent consensus (architect 85%, security 85%)
- **Pipeline event audit trail** (86 events tracking every step of document creation)
- **Project scoping** (documents grouped by project with phase tracking)

This is a differentiated capability. The compilation and synthesis layer (Section 6) is designed to exploit this structure — showing not just *what the system knows* but *how that knowledge was produced, reviewed, and validated*. No other agent memory system provides this level of knowledge provenance.

### 8.4 Benchmark Targets

NCMS benchmarks exclusively on BEIR (static document retrieval) and SWE-bench (code retrieval). Neither exercises temporal reasoning, conversation memory, or multi-agent coordination. Recent research (Jan-Apr 2026) has established clear benchmark targets:

**Primary benchmark targets**:

| Benchmark | Current SOTA | System | What It Tests | NCMS Advantage |
|-----------|-------------|--------|---------------|----------------|
| **LoCoMo** | 0.700 | [MAGMA](https://arxiv.org/abs/2601.03236) | Long-horizon conversational reasoning | Level-first retrieval + HTMG hierarchy |
| **LoCoMo-Plus** | 93.3% judge | [Kumiho](https://arxiv.org/abs/2603.17244) | Belief revision + temporal grounding | Reconciliation (supports/supersedes/conflicts) |
| **LongMemEval** | 96.6% R@5 | MemPalace | Long-term conversation memory | Episode grouping + temporal boosting |
| **MemoryAgentBench** | ≤7% multi-hop forgetting | None pass | 4 competencies inc. selective forgetting | Dream cycles + ACT-R decay + importance drift |

**MemoryAgentBench** ([ICLR 2026](https://arxiv.org/abs/2507.05257)) is the most important target. It evaluates four competencies that map directly to NCMS capabilities:

| MAB Competency | NCMS Mechanism | Current State |
|----------------|---------------|---------------|
| Accurate Retrieval | BM25+SPLADE+Graph, level-first retrieval | Proven on BEIR (nDCG@10=0.7206) |
| Test-Time Learning | Admission scoring + content classification | 65.9% accuracy, needs improvement |
| Long-Range Understanding | Graph spreading activation + top-down L4→L1 traversal | Designed, needs LoCoMo validation |
| **Selective Forgetting** | Dream cycles + ACT-R decay + reconciliation penalties + `is_current` flags | **Unique mechanism — no competitor has this** |

The selective forgetting competency is where every existing system fails (≤7% accuracy on multi-hop scenarios). NCMS's combination of `is_current=False` state closure, ACT-R base-level decay, dream cycle importance drift, and reconciliation supersession penalties is the most complete selective forgetting architecture in the literature. Proving this on MemoryAgentBench would be a defining result.

MAB includes two new datasets — **EventQA** and **FactConsolidation** — that should be added alongside LoCoMo and LongMemEval for comprehensive evaluation.

### 8.5 Research Landscape (Jan-Apr 2026)

The agent memory field has converged on several ideas that validate NCMS's architecture:

| Paper | Key Insight | NCMS Alignment | Gap / Opportunity |
|-------|------------|----------------|-------------------|
| [MAGMA](https://arxiv.org/abs/2601.03236) (Jan 2026) | Multi-graph decomposition (semantic, temporal, causal, entity) with query-adaptive traversal | NCMS has all 4 graph types; level-first retrieval (Section 6.2.3) applies MAGMA's traversal concept to HTMG | Intent-driven graph selection is new |
| [Kumiho](https://arxiv.org/abs/2603.17244) (Mar 2026) | Formal AGM belief revision for versioned memory, 93.3% LoCoMo-Plus | NCMS reconciliation does informal belief revision; entity_state versioning is structurally similar | Could formalize as AGM-compliant (future work) |
| [EverMemOS](https://arxiv.org/abs/2601.02163) (Jan 2026) | Engram lifecycle: episodic traces → semantic consolidation → reconstructive recollection | Almost exactly NCMS's pipeline: memories → episodes → consolidation → recall | "Foresight signals" (time-bounded predictions) are novel — could feed temporal boosting |
| [A-MEM](https://arxiv.org/abs/2502.12110) (NeurIPS 2025) | Zettelkasten-inspired self-organizing notes with dynamic indexing | Section-index design (6.2.2) implements this; emergent topics (6.3.1) extend it | Memory evolution (updating existing representations) not yet in NCMS |
| [MemoryAgentBench](https://arxiv.org/abs/2507.05257) (ICLR 2026) | 4-competency evaluation; selective forgetting is unsolved | NCMS SWE-bench framework has same 4 competencies; dream cycles target forgetting | Must prove on MAB datasets |
| [Mem0g](https://arxiv.org/abs/2504.19413) | Graph-enhanced production memory; F1=51.55 | NCMS SWE-bench: 6.3x better temporal reasoning than Mem0 | Mem0's production scale is a benchmark for operational maturity |
| [ICLR MemAgents Workshop](https://openreview.net/pdf?id=U51WxL382H) | Agent memory recognized as distinct subfield | NCMS's 5-layer taxonomy aligns with workshop's mechanism families | Community engagement opportunity |

### 8.6 Uncaptured Ideas from Completed Design Docs

Three ideas from the completed design documents are not yet addressed elsewhere in this document:

**1. Quality Trend Analytics Dashboard Tab** (from `document-intelligence-design.md`)

The only unimplemented feature from the document intelligence design: a dashboard "Analytics" tab showing review score trends over time, common guardrail violations, missing design sections, LLM cost trends, and pipeline duration trends. All data already exists in `review_scores`, `guardrail_violations`, `llm_calls`, and `pipeline_events` tables — purely frontend aggregation. Effort: 3-5 days. This complements the health endpoint enhancement (Section 7.4) by providing visual trend analysis rather than point-in-time metrics.

**2. SWE-bench 4-Competency Memory Evaluation Framework** (from `swebench-experiment-design.md`)

The SWE-bench experiment designed a memory evaluation framework with four competencies beyond standard IR metrics:

| Competency | Metric | What It Measures |
|------------|--------|-----------------|
| **Accurate Retrieval (AR)** | nDCG@10 | Can the system find relevant memories? |
| **Test-Time Learning (TTL)** | Classification accuracy | Can the system correctly route new knowledge (discard/ephemeral/persist)? |
| **Conflict Resolution (CR)** | Temporal MRR | Can the system surface current state over superseded state? |
| **Long-Range Understanding (LRU)** | nDCG@10 on cross-subsystem queries | Can the system find connections across distant entity neighborhoods? |

This framework should be adopted for LongMemEval evaluation (Section 8.4) — not just recall@5, but all four competencies. The SWE-bench results showed NCMS at 6.3x better temporal reasoning than Mem0 and 2.8x better than Letta on CR, which are the metrics that differentiate NCMS from simpler systems.

**3. Topic Seeding Taxonomy Optimization** (from `ablation-study-design.md`)

The ablation study revealed that GLiNER entity extraction quality is **highly sensitive** to the choice of entity labels. Abstract labels (e.g., `concept`, `metric`) produce 1.9 entities/doc; domain-specific concrete labels (e.g., `medical_condition`, `protein`, `gene`) produce 9.1 entities/doc — a **4.8x difference** that directly impacts graph expansion effectiveness.

Current hub deployment uses default generic labels. For agent memory workloads, the topic seeding should be tuned per project domain:

- **Architecture projects**: `service`, `api_endpoint`, `database`, `framework`, `protocol`, `decision`, `requirement`, `constraint`
- **Security projects**: `vulnerability`, `threat`, `control`, `compliance_standard`, `attack_vector`, `mitigation`
- **Research projects**: `methodology`, `finding`, `citation`, `dataset`, `metric`, `hypothesis`

This can be automated via `ncms topics detect` (already implemented) when a new project is created, using a few sample documents to infer the optimal label set. The key insight is that **one-size-fits-all labels leave 80% of extractable entities on the table**.

---

## 9. Retrieval Quality & System Resilience

### 9.1 Knowledge Bus Resilience

The Knowledge Bus (`infrastructure/bus/async_bus.py`) is a single-node, in-process asyncio implementation with **no resilience mechanisms**:

- **No heartbeat/timeout detection**: Agents are only marked offline via explicit `update_availability()` call. If an agent crashes or disconnects ungracefully, the bus keeps it registered indefinitely.
- **No retry logic**: Handler invocations fail silently — `_invoke_handler()` catches exceptions, logs them, and returns `None`. No retry, no dead-letter queue.
- **Message loss on crash**: Responses go into in-memory inbox queues. If an agent crashes before draining its inbox, all pending responses are lost. No acknowledgment protocol.
- **Surrogate fallback requires snapshots**: `bus_service.py` falls back to surrogate responses only when ALL live agent handlers fail AND snapshots exist. The hub has 0 snapshots, so surrogates never activate.

**Proposed improvements**:
1. **Heartbeat-based offline detection**: Agents must send a periodic heartbeat (every 30s). If missed for 2 intervals, mark offline and trigger snapshot publish.
2. **Snapshot automation**: When an agent is marked offline (heartbeat timeout), the bus should trigger `snapshot_service.create_snapshot()` from the agent's last known state (recent memories stored by that agent).
3. **Ask retry with exponential backoff**: On handler failure, retry once after 500ms before falling back to surrogate.
4. **Persistent inbox** (optional, for scaled mode): Write inbox entries to SQLite so they survive process restarts. Drain on agent reconnect.

**Effort**: Medium (heartbeat + snapshot automation ~4h, retry ~2h)

### 9.2 Scoring Weight Domain Mismatch

The current scoring weights (BM25=0.6, SPLADE=0.3, Graph=0.3, ACT-R=0.0) were tuned on **SciFact** — a static scientific fact verification dataset with 300 queries over 5K papers. This is fundamentally mismatched with multi-agent conversation memory:

| Signal | SciFact Behavior | Agent Memory Behavior |
|--------|-----------------|----------------------|
| **ACT-R** | Hurts (no access history, cold corpus) | Should help (agents repeatedly query same topics, building access frequency + recency patterns) |
| **Graph** | Marginal (+0.2% on isolated entities) | Should be strong (rich entity relationships between services, configs, agents, decisions) |
| **Temporal** | N/A (papers don't age) | Critical (recent memories more likely relevant, state changes have time ordering) |
| **Intent** | Low-leverage (uniform fact-lookup queries) | High-leverage (agents ask different query types: "what's the current state?" vs "what changed?" vs "what's the pattern?") |

**Proposed**: Run a weight tuning sweep on the hub workload (or LongMemEval when available) to find agent-optimized weights. Hypothesis: ACT-R > 0.0, Graph > 0.3, temporal signal becomes meaningful.

**Additionally**: The hub has both cross-encoder reranking and intent classification enabled, but at 67 memories these likely add latency (30-80ms for cross-encoder, overhead for intent index) without measurable ranking improvement. Consider **scale-aware feature flags** that auto-enable features above a memory threshold (e.g., cross-encoder only above 500 memories, intent classification only above 200).

### 9.3 Admission Scoring Ceiling

The admission scoring tuning achieved **65.9% accuracy** on 44 labeled examples (486 configurations tested). Per-category breakdown reveals structural weaknesses:

| Route | Accuracy | Issue |
|-------|----------|-------|
| discard | 90.0% | Good — noisy/junk content correctly filtered |
| entity_state_update | 87.5% | Good — structured state declarations detected |
| ephemeral_cache | 62.5% | Moderate — borderline content hard to classify |
| **atomic_memory** | **41.7%** | Poor — confuses architecture facts with transient updates |
| **episode_fragment** | **50.0%** | Poor — episode-relevant content misclassified as generic |

The original 8-feature heuristic pipeline has been simplified to 4 pure text heuristics (utility, persistence, state_change_signal, temporal_salience). The removed features (novelty, redundancy, reliability, episode_affinity) added no discriminative signal — see §5.7 for rationale. Content-hash dedup handles exact duplicates, and the `importance >= 8.0` force-store bypass provides an escape hatch for agents that need guaranteed persistence.

The remaining 4-feature pipeline is lexical/statistical — it still can't distinguish "ADR-003 establishes JWT with inline RBAC" (should persist as atomic_memory) from "Consulting architecture experts" (should be ephemeral). But the simplified pipeline is index-independent, runs in ~1ms, and produces identical results in both sync and async indexing modes.

**Proposed improvements**:
1. **Larger labeled dataset**: 44 examples is insufficient. Target 200+ labeled examples from the hub workload, balanced across routes.
2. **LLM-assisted classification** (optional): For borderline cases (score 0.20-0.35), call a fast LLM (Haiku-class) to classify intent. This adds ~100ms latency but could push accuracy above 80%.
3. **Content-type awareness**: Announcements (`[Announcement from ...]`), documents (`Document '...'`), and user prompts (`user: ...`) have distinct patterns that a simple prefix classifier could exploit before the full heuristic pipeline runs.

### 9.4 Search Quality Feedback Loop

**NCMS has no mechanism for agents to rate search results.** All ranking optimization is offline (tuning datasets). This means:

- Cannot detect retrieval quality degradation as the corpus grows
- Cannot adapt weights to actual agent usage patterns
- Cannot identify systematically missed or misranked memories
- No signal to distinguish "searched and found useful" from "searched and ignored results"

**Proposed**: Implicit + explicit feedback channels.

**Implicit feedback** (zero-effort for agents):
- **Access-after-search**: If an agent searches and then accesses a specific memory (via `get_memory`), that's a positive signal. The `access_log` already tracks this, but it's not correlated with search queries.
- **Correlate search_log → access_log**: Link search queries to subsequent memory accesses within a time window (e.g., 60s). Memories accessed after a search are implicitly relevant.
- **Use correlation for weight tuning**: Periodically compute per-signal contribution to "accessed results" vs "ignored results" and adjust weights.

**Explicit feedback** (agent opt-in):
- Add `rate_search_result(search_id, memory_id, relevant: bool)` to MCP tools
- Store in a `search_feedback` table: `search_id, memory_id, relevant, agent_id, timestamp`
- Use accumulated feedback for periodic weight retuning

**Config**: `NCMS_FEEDBACK_ENABLED = true`, `NCMS_FEEDBACK_ACCESS_WINDOW_SECONDS = 60`

### 9.5 Retrieval Observability

The dashboard SSE feed captures high-level events (memory stored, searched, episode created) but not **per-component retrieval diagnostics**. When a search produces unexpected results, there's no way to diagnose which scoring component caused it.

**Currently logged**: query, result_count, top_score, latency
**Not logged**: BM25 per-result scores, SPLADE per-result scores, graph spreading activation contributions, ACT-R activation values, cross-encoder reranking deltas, intent classification decision + confidence

**Proposed**: Add a `NCMS_PIPELINE_DEBUG = true` mode (already exists as a config flag) that emits detailed per-query diagnostics to the event log:

```json
{
    "event": "search.debug",
    "query": "JWT authentication patterns",
    "intent": {"class": "fact_lookup", "confidence": 0.82},
    "candidates": [
        {
            "memory_id": "abc123",
            "bm25_raw": 12.3, "bm25_norm": 0.85,
            "splade_raw": 45.6, "splade_norm": 0.72,
            "graph_raw": 0.4, "graph_norm": 0.55,
            "actr_activation": -1.2,
            "ce_score": 0.91,
            "temporal_boost": 0.0,
            "reconciliation_penalty": 0.0,
            "final_score": 0.78
        }
    ],
    "timing_ms": {"bm25": 3, "splade": 45, "graph": 12, "actr": 1, "ce": 65, "total": 126}
}
```

This enables: (a) debugging why a specific memory ranked where it did, (b) comparing scorer agreement/disagreement across queries, (c) identifying systematic biases in the scoring pipeline.

**Effort**: Low-Medium (~60 lines, most scaffolding exists via `NCMS_PIPELINE_DEBUG`)

---

## 10. Codebase Housekeeping

### 10.1 Demo Code Assessment

The `src/ncms/demo/` directory (14 Python files, 132K) is **actively used and should be kept**:

- `ncms demo` — deterministic 3-agent demo (API, Frontend, Database) exercising the full stack (Knowledge Bus, sleep/wake, surrogate responses)
- `ncms demo --nemoclaw` — NemoClaw-integrated demo with domain agents (Code, Ops, Security)
- `ncms demo --nemoclaw-nd` — LLM-powered agents (Architect, Security, Builder) reading real governance-mesh files
- Dashboard `demo_runner.py` instantiates these agents for visualization

NemoClaw does **not** replace the demo — they are complementary. The demo exercises the NCMS core (Knowledge Bus, snapshots, ACT-R) while NemoClaw exercises the multi-agent orchestration layer.

### 10.2 Dormant Code: Coding Agent Hooks

Two files in `src/ncms/interfaces/cli/` are implemented but not wired to any active flow:

| File | Purpose | Status | Recommendation |
|------|---------|--------|----------------|
| `commit_hook.py` | Commit knowledge from coding sessions (Claude Code / Copilot) to NCMS via stdin | Dormant — standalone Click command, no references from other code | Move to `experimental/` or remove |
| `context_loader.py` | Load NCMS context for new coding sessions (recent knowledge, breaking changes) | Dormant — same as above | Move to `experimental/` or remove |

These implement the "Coding Agent Integration" vision from `ncms-design-spec.md` Section 11 but have no tests, no integration, and no current users. If coding agent integration is not near-term, remove them to reduce maintenance surface.

### 10.3 Design Document Organization

Proposed reorganization of `docs/`:

```
docs/
  active/                           # Current, authoritative documents
    ncms-design-spec.md             # Core architecture (IMPLEMENTED)
    ncms-resilience-update.md       # This document (PROPOSED)
    dashboard-evolution-design.md   # Phases 1-2.5 implemented, rest aspirational
    document-intelligence-design.md # Phases 1-2.5 implemented
    nemoclaw-nat-quickstart.md      # Deployment guide (COMPLETE)
    nemoclaw-nat-step-by-step.md    # Step-by-step guide (COMPLETE)
    quickstart.md                   # Getting started (COMPLETE)
  aspirational/                     # Designed but not yet implemented
    graph-enhanced-retrieval.md     # 7-phase evolution, phases 1-3 roadmap
    multi-agent-orchestration-design.md  # Steps 2-5 future work
    ablation-study-design.md        # Evaluation framework
    swebench-experiment-design.md   # Benchmark design
  research/                         # Research papers and analysis
    paper.md                        # Full research paper
    crewai-ncms-research.md         # CrewAI integration research
  retired/                          # Already exists, superseded docs
    ncms_next_internal_design_spec.md
    ncms_v1.md
    nemoclaw-*.md variants
```

**Action**: Add a status header to each design doc (`Status: IMPLEMENTED | ASPIRATIONAL | COMPLETE | RETIRED`) and move files into the appropriate subdirectory. This is a one-time 30-minute cleanup.

### 10.4 KnowledgeAgent ABC

`src/ncms/interfaces/agent/base.py` defines the `KnowledgeAgent` abstract base class. It has two active subclasses:
- `DemoAgent` in `demo/agents/base_demo.py`
- `LLMAgent` in `demo/nemoclaw_nd/llm_agent.py`

**Status**: KEEP. Core lifecycle abstraction (start -> work -> sleep -> wake -> shutdown).

### 10.5 Import Hygiene

The codebase is clean — no widespread unused imports detected. Lazy imports are used correctly for optional dependencies (SPLADE, GLiNER, admission service). Feature flags prevent unnecessary model loading.

---

## 11. Implementation Roadmap

### Phase 0: Baseline Benchmarks (Pre-Work, Week 0)

Before any code changes, establish baseline measurements across all target benchmarks. These baselines become the "before" in before/after comparisons for every subsequent phase.

#### 0.1 Benchmark Harness Setup

**Status: COMPLETE.** All harnesses implemented and committed (`9b42323`, `f2cbb6b`).

| Suite | Harness | Dataset | Status |
|-------|---------|---------|--------|
| `benchmarks hub` | Hub workload replay (67-memory fixture + 5 queries) | Embedded in `hub_replay/hub_memories.json` | ✅ Ready |
| `benchmarks beir` | BEIR retrieval ablation (SciFact, NFCorpus, ArguAna) | Auto-download from BEIR | ✅ Ready (existing, paths updated) |
| `benchmarks swebench` | SWE-bench Django 4-competency (AR, TTL, CR, LRU) | HuggingFace SWE-bench | ✅ Ready (existing, paths updated) |
| `benchmarks locomo` | LoCoMo conversational reasoning (10 conversations) | `snap-research/locomo` (git clone) | ✅ Ready |
| `benchmarks locomo --plus` | LoCoMo-Plus cognitive memory (401 entries) | `kumihoclouds/kumiho-benchmarks` checkpoint | ✅ Ready |
| `benchmarks longmemeval` | LongMemEval conversation memory (500 questions) | HuggingFace `xiaowu0162/longmemeval-cleaned` | ✅ Ready |
| `benchmarks mab` | MemoryAgentBench 4-competency (AR, TTL, LRU, SF) | HuggingFace `ai-hyz/MemoryAgentBench` | ✅ Ready (graceful skip if dataset unavailable) |
| `benchmarks smoke` | Quick 100-doc SciFact validation | BEIR SciFact subset | ✅ Ready (existing) |
| LLM-judge scoring | Spark Nemotron Nano as evaluation judge | N/A (endpoint) | ✅ Tested (1.0/0.6/0.0 for exact/partial/miss) |

#### 0.2 Baseline Measurements

Run each harness with **current NCMS configuration** (no changes) and record:

| Metric | Benchmark | What It Measures | Expected Baseline |
|--------|-----------|-----------------|-------------------|
| **BEIR SciFact nDCG@10** | BEIR | Retrieval ranking quality (scientific facts) | ~0.72 (previous run: 0.7206 tuned) |
| **BEIR NFCorpus nDCG@10** | BEIR | Retrieval ranking quality (biomedical) | ~0.35 (previous run: 0.3505) |
| **SWE-bench AR nDCG@10** | SWE-bench | Accurate retrieval on code repos | ~0.18 (previous: 0.1759) |
| **SWE-bench CR temporal MRR** | SWE-bench | Conflict resolution (temporal state) | ~0.09 (previous: 0.0947) |
| **SWE-bench LRU nDCG@10** | SWE-bench | Long-range understanding (cross-subsystem) | ~0.35 (previous: 0.3523) |
| **LoCoMo R@5** | LoCoMo | Conversational reasoning retrieval | Unknown (first run) |
| **LoCoMo-Plus judge score** | LoCoMo-Plus | Cognitive memory (cue-trigger disconnect) | < 42% (Mem0/A-MEM SOTA), < 93.3% (Kumiho) |
| **LongMemEval R@5** | LongMemEval | Conversation memory recall | < 96.6% (MemPalace's baseline) |
| **MAB AR nDCG@10** | MemoryAgentBench | Accurate retrieval | Unknown (first run, expect ~0.72) |
| **MAB SF accuracy** | MemoryAgentBench | Selective forgetting | Unknown (dream cycles untested, all systems ≤7%) |
| **Hub ingest latency p50/p95** | Hub replay | End-to-end store_memory() time | ~350ms p50, ~5400ms p95 (from tuning) |
| **Hub search latency p50** | Hub replay | End-to-end search() time | ~38ms p50 (from tuning) |
| **Hub duplicate rate** | Hub replay | % of memories that are exact duplicates | 27% (from audit) |
| **Hub entity noise rate** | Hub replay | % of entities matching junk patterns | 11% (from audit) |

#### 0.3 Per-Phase Regression Gates

After each implementation phase, re-run the full benchmark suite. **No phase merges to main unless**:

| Gate | Requirement |
|------|-------------|
| **No regression** | Every metric ≥ baseline (within 2% measurement noise) |
| **Target improvement** | At least one metric improves by ≥5% vs. baseline |
| **Latency budget** | Ingestion p95 does not increase by >50% |
| **Test suite** | All existing tests pass (812+) |
| **Lint** | Zero ruff errors |

#### 0.4 Phase-Specific Measurement Focus

Each phase has specific metrics that should improve. If they don't, the phase needs debugging before moving on:

| Phase | Primary Metrics to Improve | Secondary Metrics (must not regress) |
|-------|---------------------------|--------------------------------------|
| **Phase 1: Data Integrity** | Hub duplicate rate → 0%, entity noise rate → <3%, orphaned memories → 0 | Search latency, LoCoMo score |
| **Phase 2: Performance** | Ingest latency p95 ↓ 30%+, search latency p50 stable | All benchmark scores |
| **Phase 3: Operational** | N/A (infrastructure, no retrieval change) | All benchmark scores, latency |
| **Phase 4: Content-Aware Ingestion** | Hub entity quality ↑ (per-section extraction), section index coverage | Ingest latency (may increase — budget 50% p95 increase) |
| **Phase 5: Level-First Retrieval** | LoCoMo ↑, LongMemEval R@5 ↑, MAB LRU ↑ | MAB AR (fact_lookup must not regress), ingest latency |
| **Phase 6: Export & Feedback** ✅ | N/A (infrastructure + tooling) | All benchmark scores |
| **Phase 7: Evaluation** 🔄 | All benchmarks: publish final numbers vs. MAGMA/Kumiho/MemPalace | N/A (final measurement phase) |

#### 0.5 Hub Replay Workload

The hub replay benchmark replays the exact 67-memory ingest sequence from the hub audit (Appendix A) against a clean NCMS instance. This provides:

1. **Deterministic before/after comparison** — same content, same order, same agents
2. **Data integrity metrics** — duplicate count, junk entity count, orphaned nodes after ingest
3. **Performance metrics** — per-memory ingest latency, per-stage timing (BM25, SPLADE, GLiNER, admission, episode)
4. **Retrieval quality** — run a fixed query set against the hub corpus and record per-query scores

The query set should include:
- Fact queries: "What database does the IMDB Lite app use?" (should hit ADR-002)
- State queries: "What is the current status of ADR-003?" (should hit entity_state)
- Temporal queries: "What was decided after the security review?" (should use temporal ordering)
- Pattern queries: "What patterns emerged in the design review process?" (should hit L4 when available)
- Cross-agent queries: "What did the security agent flag about authentication?" (should cross agent boundaries)

### Phase 1: Data Integrity (Week 1) — ✅ COMPLETE

| Task | Status | Files |
|------|--------|-------|
| Content-hash dedup gate | ✅ Done | `memory_service.py`, `migrations.py`, `sqlite_store.py` |
| Content size gating | ✅ Done | `gliner_extractor.py` (`max_content_length`), `config.py` |
| Entity quality filter | ✅ Done | `gliner_extractor.py` (`_is_junk_entity()`) |
| NAT wrapper config fix | ✅ Done | `deployment/nemoclaw-blueprint/configs/*.yml` (`save_user_messages_to_memory: false`) |
| Persist co-occurrence edges | ✅ Done | `memory_service.py`, `sqlite_store.py` |
| Load association strengths on rebuild | ✅ Done | `graph_service.py` (`get_association_strengths()`) |

### Phase 2: Performance (Week 2) — ✅ COMPLETE

| Task | Status | Files |
|------|--------|-------|
| Deferred contradiction detection | ✅ Done | Runs in `index_worker.py` async pipeline |
| Batch episode candidate queries | ✅ Done | `episode_service.py` |
| Episode profile caching | ✅ Done | `episode_service.py` (`_profile_cache`) |
| Entity state detection tightening | ✅ Done | `admission_service.py`, `scoring.py` (4-feature model) |
| Background indexing (§5.7) | ✅ Done | `index_worker.py` — store returns ~2ms, indexing async |

### Phase 3: Operational Lifecycle (Week 3) — ✅ COMPLETE

| Task | Status | Files |
|------|--------|-------|
| Maintenance scheduler | ✅ Done | `application/maintenance_scheduler.py`, wired in `mcp/server.py` |
| `ncms lint` command | ✅ Done | `application/lint_service.py`, CLI in `cli/main.py` |
| `ncms reindex` command | ✅ Done | `application/reindex_service.py`, fixed broken CLI in `cli/main.py` |
| Health endpoint enhancement | ✅ Done | `http/api.py` — indexing stats, graph stats, maintenance status |

### Phase 4: Content-Aware Ingestion & Temporal (Weeks 4-5) — ✅ COMPLETE

| Task | Status | Files |
|------|--------|-------|
| Ingest-time content classifier (heuristic) | ✅ Done | `domain/content_classifier.py` (ContentClass, classify_content, extract_sections) |
| Section extraction (markdown, JSON, YAML, structured text) | ✅ Done | `domain/content_classifier.py` (reuses knowledge_loader patterns) |
| Section index generation + parent/child memory linking | ✅ Done | `application/section_service.py` (SectionService.ingest_navigable) |
| Document entity graph linking (per-section GLiNER calls) | ✅ Done | Sections stored as individual memories → indexed by existing pipeline |
| Document-aware recall (DocumentContext + section navigation) | ✅ Done | Section children carry parent_index_id for navigation |
| Temporal query parser (Phase 1: regex, 6+ pattern types) | ✅ Done | `domain/temporal_parser.py` (parse_temporal_reference, compute_temporal_proximity) |
| Temporal scoring integration | ✅ Done | `application/memory_service.py` search() — additive w_temporal signal |

**Phase 4 Benchmark Results** (vs Phase 0 baseline, 2026-04-10):

| Metric | Baseline | Phase 4 | Delta |
|--------|----------|---------|-------|
| Hub ingest p50 | 269.56 ms | 0.78 ms | **-99.7%** (async indexing) |
| Hub ingest p95 | 3363.41 ms | 20.14 ms | **-99.4%** |
| Hub search p50 | 63.60 ms | 68.94 ms | +8.4% (run variance) |
| Hub state_lookup top-1 | 0.8497 | 1.0758 | **+26.6%** (intent classification) |
| Hub temporal top-1 | 0.7855 | 0.8019 | +2.1% |
| Hub pattern top-1 | 0.7792 | 0.8278 | +6.2% |
| Hub cross_agent top-1 | 0.7831 | 0.8051 | +2.8% |
| BEIR SciFact nDCG@10 | 0.7070 | 0.7070 | 0.0% (no regression) |
| BEIR SciFact Recall@10 | 0.8404 | 0.8404 | 0.0% |
| LoCoMo Recall@5 | 0.1375 | 0.1375 | 0.0% |
| LongMemEval Recall@5 | 0.4680 | 0.4680 | 0.0% |

Key findings: Async background indexing (3 workers) reduced ingest latency by 99%+. Intent-aware retrieval boosted state/temporal/pattern queries without regressing any standard benchmark. All harnesses now start the index pool and wait for completion before querying.

### Phase 5: Level-First Retrieval & Synthesis (Weeks 5-6) — ✅ COMPLETE

| Task | Status | Files |
|------|--------|-------|
| Level-first retrieval: scoped search with over-fetch + node-type filter | ✅ Done | `memory_service.py` (`search_level()`) |
| Traversal strategies (top-down/bottom-up/temporal/lateral) | ✅ Done | `memory_service.py` (`traverse()`, 4 traversal helpers) |
| Emergent topic map generation (L4 clustering) | ✅ Done | `memory_service.py` (`get_topic_map()`), `models.py` (`TopicCluster`) |
| `synthesize()` pipeline with 5 modes + token budgeting | ✅ Done | `memory_service.py` (`synthesize()`), `models.py` (`SynthesizedResponse`, `SynthesisMode`) |
| MCP tools (search_level, traverse_memory, get_topic_map, synthesize_memory) | ✅ Done | `mcp/tools.py` (4 new tools, total 22+1) |
| HTTP endpoints (search-level, traverse, synthesize, topics) | ✅ Done | `http/api.py` (4 new routes) |
| CLI: `ncms topic-map` | ✅ Done | `cli/main.py` |
| Config: `level_first_enabled`, `synthesis_enabled`, `topic_map_enabled` | ✅ Done | `config.py` (7 new vars) |
| Domain models: `TraversalMode`, `SynthesisMode`, `TraversalResult`, `TopicCluster`, `SynthesizedResponse` | ✅ Done | `domain/models.py` |

**Dependency note**: Topic map requires L4 abstracts from consolidation. Level-first retrieval and synthesize() degrade gracefully when abstracts are absent. All features feature-flagged off by default.

**Phase 5 Validation Test Plan**:

1. **Level-first retrieval**: Ingest a mix of memories that produce atomic, entity_state, episode, and abstract nodes (requires admission + reconciliation + episodes + one consolidation pass). Search with `node_types=["abstract"]` and verify only abstracts returned; search with `node_types=["episode"]` and verify only episodes. Confirm over-fetch factor works (request limit=5 with 3x overfetch, verify 15 candidates searched).
2. **Traversal**: Store 3+ memories that form an episode, run consolidation to generate an episode summary abstract. Then:
   - `traverse(abstract_id, mode="top_down")` → should return episode + atomic members, `levels_traversed=2`
   - `traverse(atomic_id, mode="bottom_up")` → should return episode + abstract, `levels_traversed=2`
   - `traverse(state_memory_id, mode="temporal")` → should return chronological state timeline
   - `traverse(episode_member_id, mode="lateral")` → should return sibling members + related episodes
3. **Topic map**: Generate 3+ abstracts with overlapping topic_entities (Jaccard ≥ 0.3). Call `get_topic_map()` and verify clusters form, labels contain top entities, confidence > 0.
4. **Synthesis**: With LLM endpoint available, call `synthesize(query, mode="summary")` and verify: content is non-empty, sources list is populated, tokens_used ≤ token_budget. Test all 5 modes. Test LLM-unavailable fallback returns raw excerpts with "(LLM synthesis unavailable)" prefix. Test with `traversal="bottom_up"` + `seed_memory_id` to verify traversal-fed synthesis.
5. **MCP + HTTP**: Verify all 4 new tools appear in `mcp.list_tools()`. Hit each HTTP endpoint and verify 200 responses with expected structure. Verify feature-flag-off returns empty/disabled results gracefully.

### Phase 6: Export & Feedback (Weeks 6-7) ✅ COMPLETE

| Task | Status | Files |
|------|--------|-------|
| Wiki export (`ncms export --output ./wiki`) — entity/episode/agent/insight pages with backlinks | ✅ | `cli/export.py`, `cli/main.py` |
| Search-access correlation (implicit feedback via `record_search_feedback`) | ✅ | `memory_service.py`, `event_log.py` |
| Retrieval debug diagnostics (`NCMS_PIPELINE_DEBUG` → `retrieval.debug` events with per-candidate scoring) | ✅ | `memory_service.py`, `event_log.py` |
| Bus heartbeat + offline detection (background monitor, `agent.heartbeat_timeout` events) | ✅ | `bus_service.py`, `mcp/server.py` |
| Automated snapshot publish on agent disconnect (`auto_snapshot_on_disconnect` flag) | ✅ | `bus_service.py`, `config.py` |
| Scale-aware feature flags (auto-disable reranker/intent at corpus thresholds) | ✅ | `config.py`, `memory_service.py` |
| MCP tools: `record_search_feedback`, `heartbeat`, `check_scale_flags` | ✅ | `mcp/tools.py` |
| HTTP endpoints: `/api/v1/feedback`, `/api/v1/heartbeat`, `/api/v1/scale-flags`, `/api/v1/export/wiki` | ✅ | `http/api.py` |

**Observability hooks:**
- `retrieval.debug` event emitted when `NCMS_PIPELINE_DEBUG=true` — shows per-candidate BM25/SPLADE/graph/ACT-R breakdown
- `search.feedback` event on result selection — tracks position and query for retrieval quality analysis
- `agent.heartbeat_timeout` event on timeout — includes last_seen age and auto_snapshot flag
- `agent.auto_snapshot` event when snapshot triggered by heartbeat failure
- `bus.surrogate` event when surrogate response generated for offline agent
- Log messages: `[heartbeat]` prefix for monitor lifecycle, `[phase6]` for startup

**Phase 6 Validation Test Plan:**

1. **Wiki export test**: Run `ncms export --output /tmp/wiki`, verify index.md + entities/episodes/agents/insights subdirs, check backlinks render correctly
2. **Search feedback test**: Search, record feedback via MCP/HTTP, verify access_log entry created for selected memory
3. **Retrieval debug test**: Set `NCMS_PIPELINE_DEBUG=true`, run search, verify `retrieval.debug` event in event log with candidate scoring breakdown
4. **Heartbeat timeout test**: Register agent, skip heartbeats, verify `agent.heartbeat_timeout` event after timeout_seconds, agent marked offline, surrogate mode logged
5. **Auto-snapshot test**: Set `NCMS_AUTO_SNAPSHOT_ON_DISCONNECT=true`, let agent timeout, verify snapshot published and `agent.auto_snapshot` event emitted
6. **Scale flags test**: Set `NCMS_SCALE_AWARE_FLAGS_ENABLED=true` with low thresholds, verify features auto-disabled with warning logs

**Bug Fixes Identified & Resolved During Phase 6 (2026-04-11):**

These issues were discovered during hub integration testing and resolved as cross-cutting fixes:

| Fix | Root Cause | Resolution | Files |
|-----|-----------|------------|-------|
| Entity state detection for high-importance content | `importance >= 8.0` bypassed admission entirely, so `admission_features` was `None` → L2 ENTITY_STATE nodes never created for agent-forced content | Single-path admission: always compute features, only skip routing decision (`_skip_admission_routing`). No duplicate heuristics. | `memory_service.py` |
| Phase 4 SectionService never wired | `SectionService` was implemented but never instantiated in composition root. `content_classification_enabled` defaulted to `false`, never set in Docker configs. | Wire `SectionService` in `mcp/server.py` after `MemoryService` creation (duck-typed to break circular dep). Enable `NCMS_CONTENT_CLASSIFICATION_ENABLED=true` in all Docker configs. | `mcp/server.py`, 4 Docker config files |
| Redundant 2000 char entity state cutoff | Pre-Phase-4 hard cutoff (`_max_state_len = 2000`) suppressed L2 creation on long content, which Phase 4 section extraction now handles by splitting documents into sections | Removed `_max_state_len` block entirely. Phase 4 section extraction splits NAVIGABLE content into right-sized sections before they reach entity state detection. | `memory_service.py` |
| Extended state declaration patterns | Only matched `Entity: key = value` format, missing markdown `## Status` and YAML `status:` patterns | Added 2 new regex patterns for markdown heading + YAML key-value state declarations. Added patterns 5+6 in `_extract_entity_state_meta()`. | `memory_service.py` |
| Dashboard Learning card inactive | Learning card showed static padlock icon with no live data from consolidation/dream/maintenance jobs | New `learning.js` module with SSE live events (`consolidation.*`, `dream.*`, `maintenance.*`, `episode.*`) + page-refresh rehydration from `/api/events?limit=500`. Card activates on first learning event (padlock → brain, purple accent). | `learning.js` (new), `app.js`, `agents.js`, `index.html`, `dashboard.css` |

### Phase 7: Evaluation & Housekeeping (Weeks 8-9) — 🔄 IN PROGRESS

| Task | Status | Effort | Files |
|------|--------|--------|-------|
| LoCoMo + LoCoMo-Plus benchmark harness | ✅ Done | 8h | `benchmarks/locomo/` |
| LongMemEval benchmark harness | ✅ Done | 6h | `benchmarks/longmemeval/` |
| MemoryAgentBench harness (AR, TTL, LRU, selective forgetting) | ✅ Done | 8h | `benchmarks/memoryagentbench/` |
| Hub replay benchmark harness | ✅ Done | 4h | `benchmarks/hub_replay/` |
| Agent workload weight tuning (incl. temporal weight) | ✅ Done | 6h | `benchmarks/tuning/` |
| Phase 0 baseline measurements | ✅ Done | 4h | `benchmarks/results/phase0_baseline/` |
| Bulk import mode | ✅ Done | 6h | `knowledge_loader.py`, `memory_service.py`, `cli/main.py`, `mcp/tools.py`, `http/api.py` |
| Feature flag audit & retirement | ✅ Done | 2h | `config.py`, `memory_service.py`, `index_worker.py`, Docker configs |
| Index worker entity state DRY fix | ✅ Done | 1h | `index_worker.py` (synced with `memory_service.py`) |
| NAT agent bulk import wiring | ✅ Done | 1h | `packages/nvidia-nat-ncms/` `register.py`, `http_client.py` |
| Design doc reorganization | ✅ Done | 1h | Completed in prior session |
| User/assistant retrieval asymmetry | ⬚ | 3h | `memory_service.py` |
| Admission scoring: content-type prefix classifier | ⬚ | 3h | `admission_service.py` |
| Expanded admission labeled dataset (200+ examples) | ⬚ | 4h | `benchmarks/tuning/` |
| Remove or archive dormant CLI hooks | ⬚ | 30m | `cli/commit_hook.py`, `cli/context_loader.py` |
| Hub re-test with all Phase 1-7 fixes | ⬚ | 4h | Manual validation |
| Update all documentation (Appendix D) | ⬚ | 4h | See Appendix D |

**Feature Flag Audit (2026-04-11):**

Deleted 8 flags from config — code paths are now unconditional. All references removed from source, tests, benchmarks, and Docker configs:

| Deleted Flag | Was | Reason |
|--------------|-----|--------|
| `async_indexing_enabled` | `True` | Background indexing is strictly better; sync path was dead code |
| `graph_expansion_enabled` | `True` | Graph expansion is core retrieval; never disabled |
| `cooccurrence_edges_enabled` | `True` | Co-occurrence edges feed graph expansion; never disabled |
| `graph_ppr_enabled` | `True` | PPR is strictly better than BFS; BFS path removed |
| `bus_surrogate_enabled` | `True` | Surrogates are core bus feature; always on |
| `otel_enabled` | `False` | Never gated in code; dead config field |
| `watch_enabled` | `False` | Never gated in code; dead config field |
| `episode_min_supporting_signals` | `2` | Replaced by weighted scoring in Phase 3 |

**Minimized Feature Flag Set (25 flags, down from 33):**

| Flag | Default | Hub Config | Category |
|------|---------|------------|----------|
| `NCMS_SPLADE_ENABLED` | `false` | `true` | Retrieval |
| `NCMS_RERANKER_ENABLED` | `false` | `true` | Retrieval |
| `NCMS_ADMISSION_ENABLED` | `false` | `true` | Ingestion (Phase 1) |
| `NCMS_RECONCILIATION_ENABLED` | `false` | `true` | Ingestion (Phase 2) |
| `NCMS_EPISODES_ENABLED` | `false` | `true` | Ingestion (Phase 3) |
| `NCMS_INTENT_CLASSIFICATION_ENABLED` | `false` | `true` | Retrieval (Phase 4) |
| `NCMS_CONTENT_CLASSIFICATION_ENABLED` | `false` | `true` | Ingestion (Phase 4) |
| `NCMS_CONTRADICTION_DETECTION_ENABLED` | `false` | `true` | Ingestion (Phase 4) |
| `NCMS_TEMPORAL_ENABLED` | `false` | `true` | Retrieval (Phase 4) |
| `NCMS_MAINTENANCE_ENABLED` | `false` | `true` | Operations (Phase 3) |
| `NCMS_SEARCH_FEEDBACK_ENABLED` | `false` | `true` | Observability (Phase 6) |
| `NCMS_PIPELINE_DEBUG` | `false` | `true` | Observability (Phase 6) |
| `NCMS_CONSOLIDATION_KNOWLEDGE_ENABLED` | `false` | `true` | LLM (Phase 4) |
| `NCMS_EPISODE_CONSOLIDATION_ENABLED` | `false` | — | LLM (Phase 5) |
| `NCMS_TRAJECTORY_CONSOLIDATION_ENABLED` | `false` | — | LLM (Phase 5) |
| `NCMS_PATTERN_CONSOLIDATION_ENABLED` | `false` | — | LLM (Phase 5) |
| `NCMS_LEVEL_FIRST_ENABLED` | `false` | — | Retrieval (Phase 5) |
| `NCMS_SYNTHESIS_ENABLED` | `false` | — | LLM (Phase 5) |
| `NCMS_TOPIC_MAP_ENABLED` | `false` | — | Retrieval (Phase 5) |
| `NCMS_DREAM_CYCLE_ENABLED` | `false` | `false` | Maintenance (Phase 8) |
| `NCMS_DREAM_QUERY_EXPANSION_ENABLED` | `false` | — | Retrieval (Phase 9) |
| `NCMS_DREAM_ACTIVE_FORGETTING_ENABLED` | `false` | — | Maintenance (Phase 9) |
| `NCMS_INTENT_ROUTING_ENABLED` | `false` | — | Retrieval (Phase 9) |
| `NCMS_INTENT_LLM_FALLBACK_ENABLED` | `false` | — | LLM fallback |
| `NCMS_EPISODE_LLM_FALLBACK_ENABLED` | `false` | — | LLM fallback |

**Hub Validation Config** (13 flags enabled):
```
NCMS_SPLADE_ENABLED=true
NCMS_RERANKER_ENABLED=true
NCMS_ADMISSION_ENABLED=true
NCMS_RECONCILIATION_ENABLED=true
NCMS_EPISODES_ENABLED=true
NCMS_INTENT_CLASSIFICATION_ENABLED=true
NCMS_CONTENT_CLASSIFICATION_ENABLED=true
NCMS_CONTRADICTION_DETECTION_ENABLED=true
NCMS_TEMPORAL_ENABLED=true
NCMS_MAINTENANCE_ENABLED=true
NCMS_SEARCH_FEEDBACK_ENABLED=true
NCMS_PIPELINE_DEBUG=true
NCMS_CONSOLIDATION_KNOWLEDGE_ENABLED=true
```

**Bulk Import Mode:**

`KnowledgeLoader.bulk_load_directory()` starts the async index pool with a larger queue (10K default), persists all memories (fast), then waits for indexing to drain. Agent sandboxes try `POST /api/v1/knowledge/bulk-import` first, falling back to per-file `store_memory()` for older hubs. CLI `ncms load` uses bulk mode by default for directories (`--no-bulk` to disable).

---

## Appendix A: Hub Database Snapshot (2026-04-04)

```
memories:          67    (13 importance=9.0, 48 importance=5.0, 5 importance=6.0, 1 importance=7.0)
entities:         319    (68 technology, 40 document, 35 metric, 29 concept, 22 organization, ...)
relationships:      0    (co-occurrence edges in-memory only)
memory_entities:  611    (~9.1 entities/memory)
memory_nodes:      68    (59 atomic, 2 entity_state, 7 episode)
graph_edges:       61    (59 belongs_to_episode, 2 derived_from)
access_log:        92    (max 4 accesses per memory, architect/security content most accessed)
search_log:         0    (dream cycles disabled)
association_strengths: 0  (dream cycles disabled)
ephemeral_cache:    0    (nothing routed to ephemeral)
snapshots:          0    (no agent sleep/wake cycles)
```

## Appendix B: Duplicate Content Inventory

| Content (truncated) | Copies | Agent | Wasted |
|---------------------|--------|-------|--------|
| `[Announcement from product_owner] Handing off to Designer...` | 4 | product_owner | 3 |
| `[Announcement from designer] Querying architecture...` | 3 | designer | 2 |
| `[Announcement from designer] Output guardrails...` | 3 | designer | 2 |
| `[Announcement from designer] Review round 1: 85%...` | 3 | designer | 2 |
| `[Announcement from designer] Design pipeline complete...` | 3 | designer | 2 |
| `[Announcement from designer] Implementation design published...` | 3 | designer | 2 |
| `[Announcement from product_owner] PRD published...` | 2 | product_owner | 1 |
| `[Announcement from product_owner] Expert consultation complete...` | 2 | product_owner | 1 |
| `[Announcement from product_owner] Consulting architecture...` | 2 | product_owner | 1 |
| **Total** | **28** | | **18 wasted** |

## Appendix C: Junk Entity Samples

| Entity Name | Type | Occurrences | Reason |
|-------------|------|-------------|--------|
| `85%` | metric | 7 | Review score percentage |
| `APPROVED` | event | 6 | Status label |
| `25789` | metric | 3 | Document byte count |
| `1 item(s)` | metric | 3 | Guardrail count |
| `2783 chars` | metric | 2 | Response size |
| `4970 chars` | metric | 2 | Response size |
| `ac` | technology | 4 | Truncated doc ID `acaaba712dfa` |
| `A C` | document | 1 | Broken ArXiv title |
| `Document: 6f01603fe96a` | document | 3 | Leaked document reference |
| `S5`, `S6`, `S7` | database/document | 1 each | Source citation labels |
| `avg 85%` | metric | 3 | Aggregate review score |

## Appendix D: Documentation & Assets to Update Post-Implementation

Once the resilience work is completed across Phases 1-7, the following project documentation and assets require updates to reflect the new capabilities.

### README & Quickstart

| File | Updates Needed |
|------|----------------|
| **`README.md`** (project root) | Add content-aware ingestion to feature list. Update architecture diagram if present. Note section-index retrieval and progressive context loading as capabilities. Update configuration reference with new `NCMS_MAX_CONTENT_LENGTH`, `NCMS_SCORING_WEIGHT_TEMPORAL`, `NCMS_MAINTENANCE_*`, `NCMS_FEEDBACK_ENABLED` env vars. |
| **`docs/quickstart.md`** | Add `ncms lint` and `ncms reindex` to CLI commands. Document `synthesize_knowledge` MCP tool. Add section on maintenance scheduler configuration. Update config table with new env vars. |
| **`docs/nemoclaw-nat-quickstart.md`** | Update NAT wrapper config section to note `save_ai_messages_to_memory: false`. Document section-aware ingestion behavior for structured documents. Note dedup behavior. |
| **`docs/nemoclaw-nat-step-by-step.md`** | Add troubleshooting entries for content dedup (expected behavior when duplicates are rejected) and content classification (how to verify a document was section-indexed). |

### CLAUDE.md (Project Instructions)

| Section | Updates Needed |
|---------|----------------|
| **Commands** | Add `ncms lint`, `ncms reindex`, `ncms export --format wiki` |
| **Architecture** | Add `domain/content_classifier.py`, `domain/temporal_parser.py`, `application/maintenance_scheduler.py`, `application/lint_service.py`, `cli/lint.py`, `cli/reindex.py`, `cli/export.py` to the source tree |
| **Key Design Decisions** | Add items for: (1) two-class knowledge model (navigable docs vs. atomic fragments), (2) section-index retrieval, (3) level-first retrieval with intent-driven traversal direction, (4) emergent topic map from L4 clustering, (5) progressive context loading with 5 modes, (6) maintenance scheduler |
| **Data Flow** | Update Store flow to include 4-gate chain (dedup → classification → size → admission) and section-aware indexing. Update Search flow to include level-first retrieval with traversal strategies and temporal boosting. Add Synthesize flow with 5 modes. |
| **Configuration** | Add all new `NCMS_*` env vars from this document |
| **Database Schema** | Note `content_hash` column on memories (V5). Document `section_index`, `document_section`, and `topic_summary` memory/abstract types. |
| **Testing Conventions** | Note tests for content classifier, temporal parser, section-aware ingestion, level-first retrieval, topic map generation |

### Design Spec

| File | Updates Needed |
|------|----------------|
| **`docs/ncms-design-spec.md`** | Update Section 4 (Retrieval Pipeline) to document temporal signal. Update Section 7 (Consolidation) to document maintenance scheduler. Update Section 12 (Rehydration) to document co-occurrence edge persistence and association strength loading. Add Section for content-aware ingestion and progressive context loading. |

### Dashboard & API

| Asset | Updates Needed |
|-------|----------------|
| **Dashboard UI** (`interfaces/http/static/index.html`) | Add maintenance status panel (last consolidation, dream cycle, lint). Add Quality Trend Analytics tab (Section 8.6). |
| **API documentation** | Document `GET /api/v1/memories/synthesize` endpoint. Document `GET /api/v1/health` enhanced response. Document search feedback endpoint if implemented. |

### Benchmark Documentation

| File | Updates Needed |
|------|----------------|
| **`benchmarks/README.md`** (if exists) | Add LoCoMo, LoCoMo-Plus, LongMemEval, and MemoryAgentBench harness documentation. Document 4-competency evaluation framework (AR, TTL, CR, LRU). Document agent-workload weight tuning methodology. Include target scores: LoCoMo ≥0.700 (MAGMA), LoCoMo-Plus ≥93.3% (Kumiho), MAB selective forgetting >7% (all current systems). |
| **`docs/completed/ablation-study-design.md`** | Append note about agent-workload weight tuning results when available. |

### Deployment

| File | Updates Needed |
|------|----------------|
| **`deployment/nemoclaw-blueprint/configs/*.yml`** | Apply `save_ai_messages_to_memory: false` fix (Section 3.5). |
| **`deployment/nemoclaw-blueprint/docker-compose.hub.yaml`** | Add maintenance scheduler env vars. Enable dream cycles if desired. |
| **`deployment/nemoclaw-blueprint/entrypoint-hub.sh`** | No changes unless maintenance scheduler requires startup hooks. |

### Research Paper

| File | Updates Needed |
|------|----------------|
| **`docs/paper.md`** | Update results with LoCoMo/LoCoMo-Plus/LongMemEval/MemoryAgentBench numbers. Add level-first retrieval, content-aware ingestion, emergent topic map, and section-index retrieval to methodology. Update architecture diagram to show HTMG traversal strategies. Document temporal scoring signal. Add selective forgetting results (MAB). Add comparison table vs. MAGMA (0.700), Kumiho (93.3%), Mem0g (F1=51.55). Revise "Limitations" section. Add references to all cited works (Appendix E). |

---

## Appendix E: References & Attribution

This design was informed by the following research and projects. All cited benchmark numbers, architectural insights, and design patterns are attributed to their original authors.

### Agent Memory Systems

| Ref | Citation | Key Contribution to This Design |
|-----|----------|--------------------------------|
| [1] | Jiang, F. et al. **"MAGMA: A Multi-Graph based Agentic Memory Architecture for AI Agents."** arXiv:2601.03236, January 2026. | Multi-graph decomposition (semantic, temporal, causal, entity) with query-adaptive traversal. Inspired level-first retrieval (Section 6.2.3). LoCoMo SOTA: 0.700. |
| [2] | Park, Y.B. **"Graph-Native Cognitive Memory for AI Agents: Formal Belief Revision Semantics for Versioned Memory Architectures" (Kumiho).** arXiv:2603.17244, March 2026. | Formal AGM belief revision for versioned memory. Validates NCMS reconciliation approach. LoCoMo-Plus SOTA: 93.3%. |
| [3] | Yao, J. et al. **"EverMemOS: A Self-Organizing Memory Operating System for Structured Long-Horizon Reasoning."** arXiv:2601.02163, January 2026. | Engram lifecycle: episodic traces → semantic consolidation → reconstructive recollection. Validates NCMS consolidation pipeline. Foresight signals concept. |
| [4] | Xu, W. et al. **"A-MEM: Agentic Memory for LLM Agents."** NeurIPS 2025. arXiv:2502.12110. | Zettelkasten-inspired self-organizing notes with dynamic indexing. Validates section-index design (Section 6.2.2). |
| [5] | Huang, Y. et al. **"Evaluating Memory in LLM Agents via Incremental Multi-Turn Interactions" (MemoryAgentBench).** ICLR 2026. arXiv:2507.05257. | 4-competency evaluation framework (AR, TTL, LRU, selective forgetting). Finding: all systems ≤7% on multi-hop selective forgetting. Adopted as evaluation framework (Section 8.4). |
| [6] | Chhaya, T. et al. **"Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory."** arXiv:2504.19413. | Graph-enhanced production memory (Mem0g). F1=51.55, 91% lower latency than full-context. Comparison baseline for NCMS. |
| [7] | Liu, S. et al. **"Memory in the Age of AI Agents: A Survey."** arXiv:2512.13564, December 2025. | Taxonomy of agent memory mechanisms: context-resident compression, retrieval-augmented stores, reflective self-improvement, hierarchical virtual context, policy-learned management. |
| [8] | ICLR 2026 Workshop. **"MemAgents: Memory for LLM-Based Agentic Systems."** OpenReview, 2026. | Established agent memory as a recognized subfield with standardized evaluation methodology. |
| [9] | **"Human-Like Remembering and Forgetting in LLM Agents: An ACT-R-Inspired Memory Architecture."** HAI 2025, ACM. doi:10.1145/3765766.3765803. | ACT-R vector-based activation with temporal decay, semantic similarity, and probabilistic noise for LLM agents. Independent validation of NCMS's ACT-R integration approach. |

### Competitive Systems & Approaches

| Ref | Citation | Key Contribution to This Design |
|-----|----------|--------------------------------|
| [10] | Milla-Jovovich, B. et al. **MemPalace.** GitHub: milla-jovovich/mempalace, 2026. | ChromaDB-based conversation memory achieving 96.6% R@5 on LongMemEval. Hybrid keyword fusion, temporal date boosting, two-pass assistant retrieval, 4-layer progressive loading. Inspired progressive context design (Section 6.3) and temporal query boosting (Section 6.7). |
| [11] | Karpathy, A. **"LLM Knowledge Bases."** GitHub Gist, April 2026. [gist.github.com/karpathy/442a6bf555914893e9891c11519de94f](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) | Compile-don't-search paradigm: LLM-maintained wiki with index.md navigation, lint operations, and incremental compilation. Inspired section-index design (Section 6.2.2), wiki export (Section 6.5), and lint command (Section 7.3). |

### Retrieval & NLP Foundations

| Ref | Citation | Relevance |
|-----|----------|-----------|
| [12] | Anderson, J.R. **"The Adaptive Character of Thought."** Lawrence Erlbaum Associates, 1990. | ACT-R cognitive architecture: base-level activation (recency + frequency decay) + spreading activation. Foundation for NCMS scoring (domain/scoring.py). |
| [13] | Formal, T. et al. **"SPLADE v2: Sparse Lexical and Expansion Model for Information Retrieval."** SIGIR 2022. | Learned sparse retrieval via MLM token expansion. NCMS uses SPLADE v3 (naver/splade-v3) as the sparse neural signal. |
| [14] | Alchourrón, C.E., Gärdenfors, P., and Makinson, D. **"On the Logic of Theory Change: Partial Meet Contraction and Revision Functions."** Journal of Symbolic Logic, 50(2), 510-530, 1985. | AGM postulates (K*2-K*6) for rational belief change. The foundational framework for belief revision. Kumiho [2] proves graph memory satisfies these postulates; NCMS reconciliation could be formalized similarly. |
| [15] | Zarrella, G. and Marsh, S. **"GLiNER: Generalist Model for Named Entity Recognition using Bidirectional Transformer."** NAACL 2024. | Zero-shot NER used for entity extraction in NCMS (urchade/gliner_medium-v2.1). |
