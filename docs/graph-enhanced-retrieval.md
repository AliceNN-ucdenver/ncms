# Graph-Enhanced Retrieval Pipeline

## Design Specification

**From Regex Heuristics to LLM-Powered Entity Extraction**

Version 0.1 Draft | March 2026

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Phase 1: Graph-Expanded Retrieval (Tier 1.5)](#3-phase-1-graph-expanded-retrieval-tier-15)
4. [Phase 2: GLiNER Entity Extraction](#4-phase-2-gliner-entity-extraction)
5. [Phase 3: Semantic Keyword Bridge Nodes](#5-phase-3-semantic-keyword-bridge-nodes)
6. [Phase 4: Consolidation Layer](#6-phase-4-consolidation-layer)
7. [Phase 5: Synthesized Retrieval (Tier 4)](#7-phase-5-synthesized-retrieval-tier-4)
8. [Configuration](#8-configuration)
9. [Migration](#9-migration)

---

## 1. Overview

### Current State

The NCMS retrieval pipeline uses a three-tier architecture:

```
Query ──▶ [Tier 1: BM25]  ──▶  [Tier 2: ACT-R Scoring]  ──▶  [Tier 3: LLM Judge]  ──▶ Results
           Tantivy lexical       base-level + spreading        optional reranking
           match (50 cands)      + noise → combined score
```

Entity extraction is regex-based (`domain/entity_extraction.py`), matching five syntactic patterns:

| Pattern | Type | Example |
|---------|------|---------|
| Curated tech catalog | `technology` | PostgreSQL, React, FastAPI |
| API paths | `endpoint` | /api/v2/users |
| PascalCase identifiers | `component` | UserService, AuthTokenManager |
| Table references | `table` | "users table" |
| Dotted names | `module` | react.query, shadcn.ui |

### The Problem

The regex extractor is syntactically useful but semantically blind. Given:

> "The authentication system validates JWT tokens before granting access to user data"

It extracts only `JWT` (technology). It misses: `authentication`, `access control`, `user data`, and the relationship `validates → grants_access`. This means spreading activation in Tier 2 rarely fires — the graph is structurally sound but data-starved.

### The Goal

A five-phase evolution where each phase independently adds value and makes subsequent phases more powerful:

| Phase | What it adds | LLM required | Improves |
|-------|-------------|--------------|----------|
| 1 | Graph-expanded retrieval (Tier 1.5) | No | Query-time candidate discovery |
| 2 | GLiNER entity extraction at store-time | No (209M NER model) | Graph data richness |
| 3 | Semantic keyword bridge nodes | Yes (small/local) | Cross-subgraph connectivity |
| 4 | Background consolidation | Yes (periodic) | Emergent relationship discovery |
| 5 | Synthesized retrieval (Tier 4) | Yes (query-time) | Multi-memory answer quality |

---

## 2. Architecture

### Target Pipeline (All Phases)

```
                           ┌─────────────────────────────────────┐
                           │           STORE TIME                 │
                           │                                      │
                           │  Content ──▶ [LLM Extract] ──────┐  │
                           │               (Phase 2)           │  │
                           │                 │                 │  │
                           │            ┌────┴────┐      ┌────┴──┐
                           │            │ Entities│      │Keywords│
                           │            │ + Rels  │      │+Domain│
                           │            └────┬────┘      └───┬───┘
                           │                 │    Graph      │    │
                           │            ┌────┴───────────────┴──┐ │
                           │            │   NetworkX / Neo4j    │ │
                           │            │   (Phase 3 bridges)   │ │
                           │            └───────────────────────┘ │
                           │                                      │
                           │  Background: [Consolidation]         │
                           │              (Phase 4)               │
                           │  Discovers cross-memory patterns     │
                           │  Creates insight nodes + rels        │
                           └──────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│                            QUERY TIME                                    │
│                                                                          │
│  Query ──▶ [Tier 1: BM25]                                               │
│              Tantivy lexical match                                       │
│                    │                                                     │
│                    ▼                                                     │
│            [Tier 1.5: Graph Expansion]  ◀── Phase 1                     │
│              Discover entity-related                                     │
│              memories BM25 missed                                        │
│                    │                                                     │
│                    ▼                                                     │
│            [Tier 2: ACT-R Scoring]                                       │
│              base-level + spreading                                      │
│              + noise → combined score                                    │
│                    │                                                     │
│                    ▼                                                     │
│            [Tier 2.5: SPLADE]  (future, separate initiative)            │
│                    │                                                     │
│                    ▼                                                     │
│            [Tier 3: LLM-as-Judge]                                        │
│              optional reranking                                          │
│                    │                                                     │
│                    ▼                                                     │
│            [Tier 4: LLM Synthesis]  ◀── Phase 5                         │
│              multi-memory summarization                                  │
│              with citations                                              │
│                    │                                                     │
│                    ▼                                                     │
│              Results                                                     │
└──────────────────────────────────────────────────────────────────────────┘
```

### Phase Dependencies

```
Phase 1 (Graph Expansion)     ← standalone, uses existing graph data
Phase 2 (GLiNER Extraction)    ← standalone, enriches graph data
Phase 3 (Bridge Nodes)         ← builds on Phase 2 (richer extraction)
Phase 4 (Consolidation)        ← builds on Phase 2+3 (richer graph)
Phase 5 (Synthesis)            ← builds on Phase 1-4 (better retrieval)
```

Phases 1 and 2 can be implemented independently and in any order. Phase 1 improves immediately with whatever graph data exists. Phase 2 makes Phase 1 dramatically more effective.

---

## 3. Phase 1: Graph-Expanded Retrieval (Tier 1.5)

### 3.1 Motivation

BM25 operates on lexical matching. Two memories about PostgreSQL connection pooling could use entirely different vocabulary:

- Memory A: *"PgBouncer pool size configuration for production databases"*
- Memory B: *"PostgreSQL connection limits and timeout settings"*

A query for "connection pooling configuration" matches Memory A via BM25 (shared terms). Memory B is lexically different but shares the `PostgreSQL` entity in the knowledge graph. Graph expansion bridges this gap by following entity relationships to discover Memory B.

### 3.2 Algorithm

```python
def search(query, ...):
    # Tier 1: BM25
    bm25_results = index.search(query, limit=tier1_candidates)  # [(id, score), ...]

    # Tier 1.5: Graph Expansion (new)
    if graph_expansion_enabled and bm25_results:
        # Step 1: Collect entities from BM25 hits
        bm25_entity_pool = set()
        for memory_id, _ in bm25_results:
            bm25_entity_pool |= set(graph.get_entity_ids_for_memory(memory_id))

        # Step 2: Traverse graph to find related memories
        if bm25_entity_pool:
            related = graph.get_related_memory_ids(
                list(bm25_entity_pool), depth=graph_expansion_depth
            )

            # Step 3: Deduplicate and cap
            novel = related - {mid for mid, _ in bm25_results}
            novel = set(list(novel)[:graph_expansion_max])

            # Step 4: Add to candidate pool with bm25_score=0.0
            for gid in novel:
                all_candidates.append((gid, 0.0))

    # Tier 2: ACT-R scoring on all candidates (existing)
    for memory_id, bm25_score in all_candidates:
        # ... base_level + spreading + noise → combined score
```

### 3.3 Scoring Implications

Graph-discovered memories enter ACT-R scoring with `bm25_score = 0.0`:

```
combined = bm25_score × w_bm25 + activation × w_actr
         = 0.0 × 0.6 + activation × 0.4
         = activation × 0.4
```

They can still rank above weak BM25 hits because:
- **Spreading activation** is high — they were found via shared entities, so entity overlap with the query context is strong
- **Base-level activation** contributes if the memory has been accessed recently/frequently
- A graph memory with `activation = 3.0` scores `1.2`, matching a BM25 hit with `bm25_score = 2.0` and zero activation (`2.0 × 0.6 = 1.2`)

### 3.4 Configuration

```
NCMS_GRAPH_EXPANSION_ENABLED=true    # Default: true (enabled by default)
NCMS_GRAPH_EXPANSION_DEPTH=1         # Default: 1 (one-hop)
NCMS_GRAPH_EXPANSION_MAX=10          # Default: 10 (cap on novel candidates)
```

### 3.5 Edge Cases

| Scenario | Behavior |
|----------|----------|
| No BM25 results | No expansion (nothing to expand from) |
| No entities in graph | Expansion adds nothing |
| BM25 hit also found by graph | Appears once (dedup) |
| More graph candidates than max | Capped, ACT-R scoring selects best |
| Domain filter active | Applied to graph-expanded candidates too |
| Graph expansion disabled | Pipeline unchanged from current behavior |

### 3.6 Existing Infrastructure (No Changes Needed)

- `NetworkXGraph.get_entity_ids_for_memory(memory_id) → list[str]`
- `NetworkXGraph.get_related_memory_ids(entity_ids, depth) → set[str]`
- `GraphEngine` protocol declares both methods
- `ScoredMemory.bm25_score` defaults to `0.0`

---

## 4. Phase 2: GLiNER Entity Extraction

**Status: Implemented**

### 4.1 Motivation

Replace regex heuristics with semantically-aware entity extraction at store-time. The regex extractor is syntactically useful but misses conceptual entities. Given "The authentication system validates JWT tokens", regex extracts only `JWT` (technology). GLiNER additionally extracts: `authentication system` (concept), `JWT tokens` (technology) — enriching the knowledge graph for spreading activation and graph expansion.

### 4.2 Implementation: GLiNER

[GLiNER](https://github.com/urchade/GLiNER) (Zaratiana et al., NAACL 2024) is a zero-shot Named Entity Recognition model that runs in-process on CPU. It uses a bidirectional transformer encoder (DeBERTa-v3) and supports custom entity type labels at runtime — no fine-tuning needed.

**File:** `src/ncms/infrastructure/extraction/gliner_extractor.py`

```python
DEFAULT_LABELS = [
    "technology", "service", "endpoint", "database",
    "concept", "data model", "protocol", "library",
]

def extract_entities_gliner(text, model_name, threshold, labels=None):
    model = _get_model(model_name)  # lazy-loaded, cached
    raw = model.predict_entities(text, labels or DEFAULT_LABELS, threshold=threshold)
    # Dedup by lowercase name, cap at 20, return [{"name": ..., "type": ...}]
```

**Routing function:** `src/ncms/domain/entity_extraction.py` → `extract_entities()`
- GLiNER enabled + installed → use GLiNER
- GLiNER enabled + not installed → warn, fall back to regex
- GLiNER disabled → use regex
- GLiNER error at runtime → warn, fall back to regex

### 4.3 Model Options

| Model | Params | Backbone | Latency | Notes |
|-------|--------|----------|---------|-------|
| `urchade/gliner_small-v2.1` | 166M | DeBERTa-v3-small | ~15ms | Fast, good for simple entities |
| `urchade/gliner_medium-v2.1` | 209M | DeBERTa-v3-base | ~30ms | **Default — best quality/speed tradeoff** |
| `urchade/gliner_large-v2.1` | 459M | DeBERTa-v3-large | ~80ms | Highest quality, heavier |

All models run on CPU. No GPU required. First call downloads and caches the model (~400MB for medium).

### 4.4 Integration

Both store-time and query-time extraction route through `extract_entities()`:

```python
# memory_service.py — store-time
auto_entities = extract_entities(content, config=self._config)

# memory_service.py — search-time
query_entity_names = extract_entities(query, config=self._config)
```

### 4.5 Configuration

```
NCMS_GLINER_ENABLED=false          # Default: false (opt-in)
NCMS_GLINER_MODEL=urchade/gliner_medium-v2.1
NCMS_GLINER_THRESHOLD=0.3         # Minimum confidence for entity inclusion
```

Install: `pip install ncms[gliner]`

---

## 5. Phase 3: Semantic Keyword Bridge Nodes

**Status: Implemented**

### 5.1 Motivation

Even with GLiNER-extracted entities, the graph can have disconnected subgraphs. An `authentication` subgraph and a `user management` subgraph may not share any entities, but they are conceptually related. Keyword bridge nodes create connections:

```
Memory: "JWT validation middleware"     Memory: "User role management"
    │                                       │
    ├── entity: JWT                         ├── entity: UserRole
    ├── entity: middleware                  ├── entity: RoleManager
    │                                       │
    └── keyword: security ◀───────────────┘── keyword: security
```

When two memories share a keyword entity (e.g., both link to "security"), they are connected in the graph via that shared entity node. The existing `get_related_memory_ids()` traversal in graph expansion discovers them automatically — no new relationship types or traversal logic needed.

### 5.2 Design

Keywords are entities with `type="keyword"` in the graph. They are:
- Extracted at store-time via LLM (`litellm.acompletion`)
- Stored as `Entity(type="keyword")` nodes in the graph
- Linked to memories via the existing `memory_entities` junction
- Deduplicated against existing entities (case-insensitive)
- Non-fatal on error: if LLM extraction fails, `store_memory()` completes normally

**File:** `src/ncms/infrastructure/extraction/keyword_extractor.py`

```python
async def extract_keywords(
    content: str,
    existing_entities: list[dict[str, str]],
    model: str = "gpt-4o-mini",
    max_keywords: int = 8,
) -> list[dict[str, str]]:
    """Extract semantic keywords via LLM. Returns [{"name": ..., "type": "keyword"}]."""
```

**Integration:** `src/ncms/application/memory_service.py` — called after entity extraction in `store_memory()`:

```python
if self._config.keyword_bridge_enabled:
    keywords = await extract_keywords(content, existing_entities=all_entities, ...)
    for kw in keywords:
        kw_entity = await self.add_entity(name=kw["name"], entity_type="keyword")
        await self._store.link_memory_entity(memory.id, kw_entity.id)
        self._graph.link_memory_entity(memory.id, kw_entity.id)
```

### 5.3 Why This Works Without New Edge Types

The existing graph expansion traversal (`get_related_memory_ids`) discovers all memories linked to any shared entity — regardless of entity type. When two memories share a keyword entity:

```
Memory A ──linked──▶ Entity("security", type="keyword") ◀──linked── Memory B
```

Graph expansion from Memory A discovers Memory B via the shared keyword node, using the same traversal that already works for technology entities. No explicit `related_concept` edges needed for the basic bridge behavior.

### 5.4 Configuration

```
NCMS_KEYWORD_BRIDGE_ENABLED=false        # Default: false (opt-in)
NCMS_KEYWORD_MAX_PER_MEMORY=8            # Default: 8
NCMS_KEYWORD_LLM_MODEL=gpt-4o-mini      # Default: gpt-4o-mini
```

---

## 6. Phase 4: Consolidation Layer

**Status: Implemented**

### 6.1 Motivation

Individual memories are atomic facts. Consolidation discovers emergent relationships that no single memory contains:

> "The API layer depends on the auth middleware for all protected endpoints. Changes to JWT validation affect 5 endpoints."

This insight links memories from the `api` domain to memories in the `auth` domain — a relationship that was never explicitly stated.

### 6.2 Design

**File:** `src/ncms/infrastructure/consolidation/clusterer.py`

Entity co-occurrence clustering uses union-find to group memories sharing entities in the knowledge graph:

```python
@dataclass
class MemoryCluster:
    memories: list[Memory]
    shared_entity_ids: set[str]  # entities linked to 2+ memories
    domains: set[str]             # union of all memory domains

def find_entity_clusters(memories, graph, min_cluster_size=3) -> list[MemoryCluster]:
    """Group memories by entity co-occurrence via union-find."""
```

Algorithm:
1. For each memory, get linked entity IDs from the graph (O(1) lookup)
2. Build entity → memory index
3. Union memories sharing any entity
4. Filter by min_cluster_size, exclude `Memory(type="insight")` to prevent re-consolidation
5. Return clusters sorted by size (largest first)

**File:** `src/ncms/infrastructure/consolidation/synthesizer.py`

LLM-based pattern synthesis via `litellm.acompletion()`:

```python
async def synthesize_insight(cluster, model="gpt-4o-mini", api_base=None) -> dict | None:
    """Synthesize a cross-memory pattern. Returns insight dict or None."""
```

Returns `{"insight": str, "pattern_type": str, "confidence": float, "key_entities": [str]}` or `None` on failure (non-fatal).

Supports vLLM/OpenAI-compatible endpoints via `api_base` parameter.

**File:** `src/ncms/application/consolidation_service.py`

```python
async def consolidate_knowledge(self) -> int:
    """Discover cross-memory patterns and create insight memories.

    Returns the number of insights created.
    """
    # 1. Get last run timestamp from consolidation_state
    # 2. Fetch memories since last run (incremental)
    # 3. Cluster by entity co-occurrence
    # 4. Synthesize insights from top clusters via LLM
    # 5. Store insights as Memory(type="insight") — indexed, linked to entities
    # 6. Update last run timestamp
```

### 6.3 Insight Storage

Insights are stored as `Memory(type="insight")`, reusing all existing infrastructure:
- **Indexed** in Tantivy → discoverable via BM25 search
- **Scored** by ACT-R → rank alongside regular memories
- **Linked** to key entities in the graph → discoverable via graph expansion
- **Metadata** in `structured` field: source memory IDs, pattern type, confidence, synthesis model

### 6.4 vLLM / Local LLM Support

All LLM features (judge, keywords, consolidation) support `api_base` configuration for routing to vLLM or any OpenAI-compatible endpoint:

```bash
# Start vLLM locally
vllm serve meta-llama/Llama-3.2-3B-Instruct --port 8000

# Configure NCMS to use local vLLM for consolidation
export NCMS_CONSOLIDATION_KNOWLEDGE_API_BASE=http://localhost:8000/v1
export NCMS_CONSOLIDATION_KNOWLEDGE_MODEL=openai/meta-llama/Llama-3.2-3B-Instruct
```

### 6.5 Configuration

```
NCMS_CONSOLIDATION_KNOWLEDGE_ENABLED=false        # Default: false (opt-in)
NCMS_CONSOLIDATION_KNOWLEDGE_MIN_CLUSTER_SIZE=3    # Min memories per cluster
NCMS_CONSOLIDATION_KNOWLEDGE_MODEL=gpt-4o-mini     # LLM for synthesis
NCMS_CONSOLIDATION_KNOWLEDGE_API_BASE=             # vLLM / OpenAI-compatible endpoint
NCMS_CONSOLIDATION_KNOWLEDGE_MAX_INSIGHTS_PER_RUN=5
```

---

## 7. Phase 5: Synthesized Retrieval (Tier 4)

### 7.1 Motivation

When a query spans multiple memories (e.g., "What would break if we changed the auth system?"), returning individual memory records is insufficient. The user needs a synthesized answer with citations.

### 7.2 Design

After Tier 3 ranking, optionally pass the top-K results to an LLM for synthesis:

```python
if config.synthesis_enabled and len(results) > 1:
    synthesis = await llm.synthesize(
        query=query,
        memories=results[:config.synthesis_top_k],
    )
    # Returns: synthesized answer + list of cited memory IDs
```

### 7.3 When to Trigger

Not every query needs synthesis. Heuristics:
- Query contains relational words ("how does X relate to Y", "what depends on", "impact of")
- Top-K results span multiple domains
- Explicit flag from the caller

### 7.4 Configuration

```
NCMS_SYNTHESIS_ENABLED=false
NCMS_SYNTHESIS_MODEL=gpt-4o-mini
NCMS_SYNTHESIS_TOP_K=5
```

---

## 8. Configuration Summary

### All New Config Values

| Variable | Phase | Default | Purpose |
|----------|-------|---------|---------|
| `NCMS_GRAPH_EXPANSION_ENABLED` | 1 | `true` | Enable Tier 1.5 graph expansion |
| `NCMS_GRAPH_EXPANSION_DEPTH` | 1 | `1` | Graph traversal depth |
| `NCMS_GRAPH_EXPANSION_MAX` | 1 | `10` | Max graph-discovered candidates |
| `NCMS_GLINER_ENABLED` | 2 | `false` | Enable GLiNER entity extraction |
| `NCMS_GLINER_MODEL` | 2 | `urchade/gliner_medium-v2.1` | GLiNER model for extraction |
| `NCMS_GLINER_THRESHOLD` | 2 | `0.3` | Min confidence for entity inclusion |
| `NCMS_KEYWORD_BRIDGE_ENABLED` | 3 | `false` | Enable keyword bridge nodes |
| `NCMS_KEYWORD_MAX_PER_MEMORY` | 3 | `8` | Max keywords per memory |
| `NCMS_KEYWORD_LLM_MODEL` | 3 | `gpt-4o-mini` | LLM model for keyword extraction |
| `NCMS_KEYWORD_LLM_API_BASE` | 3 | *(none)* | vLLM endpoint for keyword extraction |
| `NCMS_LLM_API_BASE` | 3 | *(none)* | vLLM endpoint for LLM-as-judge |
| `NCMS_CONSOLIDATION_KNOWLEDGE_ENABLED` | 4 | `false` | Enable knowledge consolidation |
| `NCMS_CONSOLIDATION_KNOWLEDGE_MIN_CLUSTER_SIZE` | 4 | `3` | Min cluster size |
| `NCMS_CONSOLIDATION_KNOWLEDGE_MODEL` | 4 | `gpt-4o-mini` | Consolidation model |
| `NCMS_CONSOLIDATION_KNOWLEDGE_API_BASE` | 4 | *(none)* | vLLM endpoint for consolidation |
| `NCMS_CONSOLIDATION_KNOWLEDGE_MAX_INSIGHTS_PER_RUN` | 4 | `5` | Max insights per run |
| `NCMS_SYNTHESIS_ENABLED` | 5 | `false` | Enable Tier 4 synthesis |
| `NCMS_SYNTHESIS_MODEL` | 5 | `gpt-4o-mini` | Synthesis model |
| `NCMS_SYNTHESIS_TOP_K` | 5 | `5` | Memories to synthesize |

All default to `false`/off — each phase is opt-in and backward compatible.

---

## 9. Migration

### Phase 1: No Migration Required

Graph expansion uses existing entity data. Whatever entities the regex extractor has already produced are traversed at query time. No data changes needed.

### Phase 2: Optional Re-extraction

After enabling GLiNER extraction, existing memories still have their regex-extracted entities. Options:
- **Lazy**: New memories get GLiNER extraction, old memories keep regex entities
- **Batch**: Background task re-extracts entities for all existing memories using GLiNER

### Phase 3: One-Time Bridge Generation

After enabling keyword bridges, run a one-time pass to extract keywords from all existing memories and create bridge nodes.

### Phase 4: Automatic

Consolidation processes all memories from the start. No special migration.

### Phase 5: No Migration Required

Synthesis is a query-time operation. No data changes needed.

---

## References

- Anderson, J.R. (2007). *How Can the Human Mind Occur in the Physical Universe?* — ACT-R activation theory
- Zaratiana, U. et al. (2024). *GLiNER: Generalist Model for Named Entity Recognition using Bidirectional Transformer* (NAACL 2024) — zero-shot NER model used in Phase 2
- Google Always-On Memory Agent — consolidation and multi-stage retrieval patterns
- NCMS Design Specification (`docs/ncms-design-spec.md`) — core architecture
