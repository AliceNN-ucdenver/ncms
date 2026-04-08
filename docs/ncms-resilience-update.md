# NCMS Resilience, Data Integrity & Performance Update

**Status:** Proposed
**Date:** 2026-04-06
**Context:** Audit of the live NCMS Hub (ncms-hub container, 67 memories, 6 agents, IMDB Lite architecture exercise) revealed data integrity issues in the ingestion pipeline, gaps in graph persistence, and opportunities for performance improvement. This document proposes concrete fixes, architectural improvements, and an operational lifecycle for recurring maintenance events.

---

## Table of Contents

1. [Hub Audit Findings](#1-hub-audit-findings)
2. [SOTA Priority Analysis](#2-sota-priority-analysis)
3. [Data Integrity Improvements](#3-data-integrity-improvements)
4. [Graph Persistence & Rebuild-from-Store](#4-graph-persistence--rebuild-from-store)
5. [Ingestion Performance Improvements](#5-ingestion-performance-improvements)
6. [Compilation & Synthesis Layer](#6-compilation--synthesis-layer)
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

**Design**: Add a content-hash dedup check as the first gate in `store_memory()`, before admission scoring.

```
store_memory(content, ...)
  |
  v
content_hash = hashlib.sha256(content.encode()).hexdigest()
  |
  v
[Check dedup_hashes set or SQLite index] -- duplicate? --> return existing memory (no-op)
  |
  v
[Existing pipeline: admission -> persist -> index -> entity -> episode]
```

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

**Design**: Add configurable `NCMS_MAX_CONTENT_LENGTH` (default: 5,000 chars).

- Content exceeding the limit is **rejected at the API layer** with a 413 response
- The `store_memory()` method validates before any processing
- Document ingestion (via `knowledge_loader` or `document_service`) uses its own chunking and is exempt from this limit
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

---

## 6. Compilation, Synthesis & Document Integration

### 6.1 Current Architecture: Two Separate Knowledge Stores

NCMS has two independent persistent stores that today operate in isolation:

| Aspect | Memory Store | Document Store |
|--------|-------------|----------------|
| **Content** | Semantic chunks (typically 80-500 chars), agent announcements, episode seeds | Full-text versioned artifacts (1K-29K chars): research reports, PRDs, designs, reviews |
| **Indexing** | BM25 + SPLADE + entity graph expansion | SQL queries (LIKE on entity JSON, JOIN on review scores) |
| **Entities** | GLiNER per memory chunk → entity graph (319 entities, 611 links in hub) | GLiNER on full document → JSON array (stored inline, not graph-linked) |
| **Relationships** | HTMG: episodes, derived_from, co-occurrence edges | document_links: derivation chains, review relationships |
| **Retrieval** | Semantic search with multi-signal scoring | Direct lookup by doc_id, entity name, project_id |
| **Provenance** | source_agent, domains, tags | from_agent, project_id, content_hash, version chain |
| **Tables** | memories, entities, relationships, memory_entities, memory_nodes, graph_edges | documents, projects, document_links, review_scores, pipeline_events |

**The gap**: The memory store has rich retrieval (BM25 + SPLADE + graph + ACT-R) but only sees 460-char summary stubs of documents. The document store has full content with versioning and traceability but has no semantic search — only SQL LIKE queries on entity JSON. Neither store references the other except through weak tag-based links (`tags=["document", doc_id]` on memory stubs).

In the hub, the 5 published documents (research report, PRD, requirements manifest, implementation design, design review) total 83,830 chars of substantive content. The memory store holds 460-488 char stubs of each — **roughly 3% of the actual knowledge**. When an agent searches for "JWT authentication patterns," it hits the memory stub but has no path to the full 25K-char design document that contains the detailed implementation.

### 6.2 Design: Unified Knowledge Retrieval

**Principle**: Documents are first-class knowledge artifacts, not metadata appendages. They should participate in the same retrieval pipeline as memories.

#### 6.2.1 Document Indexing in BM25 + SPLADE

When a document is published via `DocumentService.publish_document()`, in addition to storing the full content in the document store:

1. **Chunk the document** using the same sentence-boundary splitter as `knowledge_loader.py`
2. **Index each chunk** in Tantivy (BM25) and SPLADE with metadata: `{doc_id, chunk_index, doc_type, project_id, from_agent}`
3. **Store chunk→document mapping** so search results can expand to full document context on demand
4. **Entity-link document entities** into the main entity graph (not just JSON arrays)

This means `search()` and `recall()` will surface document chunks alongside memory fragments, scored by the same BM25 + SPLADE + graph signals. The document store remains the source of truth for full content; the memory indexes provide retrieval access.

```python
# In document_service.py publish_document():
async def publish_document(self, title, content, ...):
    doc = await self._doc_store.save_document(...)  # Full content to document store

    # NEW: Index document chunks in memory retrieval pipeline
    chunks = chunk_text(content, max_chars=2000, overlap=200)
    for i, chunk in enumerate(chunks):
        await self._memory_svc.store_memory(
            content=chunk,
            memory_type="document_chunk",
            domains=domains,
            source_agent=from_agent,
            importance=7.0,  # Higher than announcements (5.0), lower than knowledge files (9.0)
            tags=["document_chunk", doc.id, f"chunk:{i}"],
            structured={"doc_id": doc.id, "chunk_index": i, "doc_type": doc_type},
        )

    # NEW: Link document entities into entity graph
    for entity in doc.entities:
        entity_id = await self._graph_svc.resolve_or_create_entity(entity["name"], entity["type"])
        await self._store.link_entity_to_memory(doc.id, entity_id)
```

**Trade-off**: This increases memory store size. A 25K-char document produces ~13 chunks at 2K each. For 5 documents, that's ~65 new memories. Acceptable — the retrieval quality improvement justifies the storage cost.

#### 6.2.2 Document-Aware Recall

Extend `recall()` to detect when results reference document chunks and automatically provide document context:

```python
class RecallResult:
    memory: ScoredMemory
    context: RecallContext
    document: DocumentContext | None  # NEW

class DocumentContext:
    doc_id: str
    title: str
    doc_type: str                    # research, prd, design, review, manifest
    from_agent: str
    project_id: str | None
    version: int
    review_scores: list[ReviewScore]  # Architect: 85%, Security: 85%
    derivation_chain: list[str]       # [research_id] -> [prd_id] -> [design_id]
    sibling_chunks: list[str]         # Adjacent chunk memory_ids for context expansion
    full_content_url: str             # /api/v1/documents/{doc_id} for on-demand full text
```

When a recall result's `structured.doc_id` exists, the system fetches the document metadata and derivation chain from the document store. This gives the consuming agent:
- The document's review history (was this approved? what score?)
- The derivation chain (this design came from this PRD which came from this research)
- Adjacent chunks for context expansion without loading the entire document

### 6.3 Design: Progressive Context Loading

Inspired by MemPalace's L0-L3 stack, but incorporating documents as a distinct knowledge tier:

| Level | Token Budget | Content Sources | Use Case |
|-------|-------------|-----------------|----------|
| **L0: Identity** | ~200 tokens | Project metadata + active agent roster | System prompt injection |
| **L1: Briefing** | ~800 tokens | Entity state snapshots + latest episode summaries + document titles with review status | Agent wake-up, task handoff |
| **L2: Summary** | ~3000 tokens | Top-5 recall results + related abstracts + document chunk excerpts with derivation context | Standard query response |
| **L3: Deep** | ~8000 tokens | Full recall + synthesis + causal chains + full document sections | Complex analysis, design review |
| **L4: Archive** | On-demand | Full document content via `/api/v1/documents/{doc_id}` | Agent requests explicit deep read |

**Key difference from MemPalace**: NCMS's progressive loading is **document-aware**. At L1, an agent knows "there's an approved design doc at 85% for authentication patterns." At L2, it gets the relevant chunks. At L3, it gets the full section with review context. At L4, it can pull the entire 25K-char document if needed.

**Implementation**:

```python
async def synthesize(
    self,
    query: str,
    mode: Literal["identity", "briefing", "summary", "deep"] = "summary",
    project_id: str | None = None,
) -> SynthesizedResponse:
    if mode == "identity":
        return await self._build_identity_context(project_id)
    elif mode == "briefing":
        return await self._build_briefing(query, project_id)
    elif mode == "summary":
        return await self._build_summary(query, project_id)
    elif mode == "deep":
        return await self._build_deep(query, project_id)
```

Exposed as:
- MCP tool: `synthesize_knowledge(query, mode, project_id)`
- HTTP: `GET /api/v1/memories/synthesize?q=...&mode=summary&project_id=PRJ-...`

### 6.4 Design: `synthesize()` Pipeline

The synthesis pipeline unifies both stores:

```
Query + Mode + optional Project
  |
  v
[1. recall() — memory search pipeline: BM25 + SPLADE + graph + scoring]
  |
  v
[2. Document expansion — for results with doc_id, fetch document metadata,
    review scores, derivation chain from document store]
  |
  v
[3. Abstract gathering — episode summaries, state trajectories, patterns
    from consolidation layer]
  |
  v
[4. Project context — if project_id given, load project metadata,
    all documents in project with their review status]
  |
  v
[5. Token budgeting — trim and prioritize based on mode:
    briefing → entity states + doc titles + latest episode
    summary  → top-5 results + abstracts + doc excerpts
    deep     → everything + full document sections + causal chains]
  |
  v
[6. LLM compilation — synthesize into structured narrative
    with citations back to memory IDs and document IDs]
  |
  v
SynthesizedResponse {
    query: str
    mode: str
    base_results: list[RecallResult]         # Ranked fragments with scores
    documents: list[DocumentContext]          # Referenced documents with review status
    abstracts: list[AbstractMemory]          # Episode summaries, trajectories, patterns
    project: ProjectContext | None           # Project metadata and document inventory
    synthesis: str                           # LLM-generated narrative with citations
    entity_snapshot: dict[str, str]          # Current state of all mentioned entities
    token_count: int                         # Actual tokens used
}
```

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
- Documents are exported **from the document store** (full content, not memory stubs)
- Entity pages are generated by **joining both stores**: memory search results + document entity JSON
- Episode pages are generated from **HTMG episode nodes** with member memories and their document references
- Agent pages show **provenance**: what this agent stored, authored, and reviewed
- Derivation chains come from **document_links** table (not memory relationships)
- Timeline comes from **pipeline_events** table (86 events in the hub)
- All markdown files contain **backlinks** (entity pages link to documents and episodes; documents link to entities and derivation chains)
- The wiki is a **read-only snapshot** — regenerated on demand, not maintained incrementally (unlike Karpathy's approach where the LLM edits the wiki)

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

**Config**: `NCMS_SCORING_WEIGHT_TEMPORAL = 0.2` (default, tunable via grid search). Applied only when a temporal reference is detected — zero-cost for non-temporal queries.

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
| Temporal date parsing + 40% proximity boost | Bitemporal fields exist but no query-time temporal boost | Add temporal proximity scoring (Section 8.3) |
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

### 8.4 Benchmark Gap: LongMemEval

NCMS benchmarks exclusively on BEIR (static document retrieval) and SWE-bench (code retrieval). Neither exercises temporal reasoning, conversation memory, or multi-agent coordination. Adding LongMemEval evaluation would:

- Validate temporal/episode/reconciliation features that show no gain on BEIR
- Enable direct comparison with MemPalace's 96.6% baseline
- Expose whether ACT-R and dream cycles help on realistic access patterns

### 8.5 Uncaptured Ideas from Completed Design Docs

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

The 8-feature heuristic pipeline (novelty, utility, reliability, temporal_salience, persistence, redundancy, episode_affinity, state_change_signal) has reached its ceiling. The features are lexical/statistical — they can't distinguish "ADR-003 establishes JWT with inline RBAC" (should persist as atomic_memory) from "Consulting architecture experts" (should be ephemeral).

**Proposed improvements**:
1. **Larger labeled dataset**: 44 examples is insufficient. Target 200+ labeled examples from the hub workload, balanced across routes.
2. **LLM-assisted classification** (optional): For borderline cases (score 0.30-0.45), call a fast LLM (Haiku-class) to classify intent. This adds ~100ms latency but could push accuracy above 80%.
3. **Content-type awareness**: Announcements (`[Announcement from ...]`), documents (`Document '...'`), and user prompts (`user: ...`) have distinct patterns that a simple prefix classifier could exploit before the full heuristic pipeline runs.
4. **Feedback from episode assignment**: If a memory gets assigned to an episode with high match_score (>0.7), retroactively boost its admission classification for future similar content.

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

### Phase 1: Data Integrity (Week 1)

| Task | Effort | Files |
|------|--------|-------|
| Content-hash dedup gate | 2h | `memory_service.py`, `migrations.py`, `sqlite_store.py` |
| Content size gating | 1h | `memory_service.py`, `api.py`, `config.py` |
| Entity quality filter | 2h | `gliner_extractor.py` |
| NAT wrapper config fix | 30m | `deployment/nemoclaw-blueprint/configs/*.yml` |
| Persist co-occurrence edges | 2h | `memory_service.py`, `sqlite_store.py` |
| Load association strengths on rebuild | 1h | `graph_service.py` |

### Phase 2: Performance (Week 2)

| Task | Effort | Files |
|------|--------|-------|
| Deferred contradiction detection | 2h | `memory_service.py` |
| Batch episode candidate queries | 4h | `episode_service.py`, `sqlite_store.py` |
| Episode profile caching | 3h | `episode_service.py` |
| Entity state detection tightening | 2h | `admission_service.py`, `scoring.py` |

### Phase 3: Operational Lifecycle (Week 3)

| Task | Effort | Files |
|------|--------|-------|
| Maintenance scheduler | 4h | New: `application/maintenance_scheduler.py`, wire in `mcp/server.py` |
| `ncms lint` command | 4h | New: `cli/lint.py`, `application/lint_service.py` |
| `ncms reindex` command | 6h | New: `cli/reindex.py`, `application/reindex_service.py` |
| Health endpoint enhancement | 2h | `http/api.py` |

### Phase 4: Document Integration & Temporal (Week 4)

| Task | Effort | Files |
|------|--------|-------|
| Document chunk indexing in BM25 + SPLADE | 6h | `document_service.py`, `memory_service.py` |
| Document entity graph linking | 3h | `document_service.py`, `graph_service.py` |
| Document-aware recall (DocumentContext) | 4h | `memory_service.py`, `models.py` |
| Temporal query parser (Phase 1: regex) | 4h | New: `domain/temporal_parser.py`, `scoring.py` |
| Temporal scoring integration | 2h | `memory_service.py` |

### Phase 5: Synthesis & Export (Week 5)

| Task | Effort | Files |
|------|--------|-------|
| `synthesize()` pipeline with token budgeting | 6h | `memory_service.py`, new `SynthesizedResponse` model |
| Progressive context modes (identity/briefing/summary/deep) | 4h | `memory_service.py` |
| MCP + HTTP synthesis endpoints | 2h | `mcp/tools.py`, `http/api.py` |
| Wiki export from document store + memory store | 8h | New: `cli/export.py` |

### Phase 6: Retrieval Quality & Feedback (Week 6)

| Task | Effort | Files |
|------|--------|-------|
| Search-access correlation (implicit feedback) | 4h | `memory_service.py`, `sqlite_store.py` |
| Retrieval debug diagnostics (`NCMS_PIPELINE_DEBUG`) | 3h | `memory_service.py`, `event_log.py` |
| Bus heartbeat + offline detection | 4h | `async_bus.py`, `bus_service.py` |
| Automated snapshot publish on agent disconnect | 3h | `bus_service.py`, `snapshot_service.py` |
| Scale-aware feature flags (reranker/intent thresholds) | 2h | `config.py`, `memory_service.py` |

### Phase 7: Evaluation & Housekeeping (Week 7)

| Task | Effort | Files |
|------|--------|-------|
| LongMemEval benchmark harness | 8h | `benchmarks/longmemeval/` |
| Agent workload weight tuning (hub or LongMemEval) | 6h | `benchmarks/tuning/` |
| Bulk import mode | 6h | `cli/main.py`, `memory_service.py` |
| User/assistant retrieval asymmetry | 3h | `memory_service.py` (search filter by `source_agent` role, two-pass session matching) |
| Admission scoring: content-type prefix classifier | 3h | `admission_service.py` |
| Expanded admission labeled dataset (200+ examples) | 4h | `benchmarks/tuning/` |
| Design doc reorganization | 1h | `docs/` directory restructure (Section 10.3) |
| Remove or archive dormant CLI hooks | 30m | `cli/commit_hook.py`, `cli/context_loader.py` |
| Hub re-test with all fixes | 4h | Manual validation |

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
