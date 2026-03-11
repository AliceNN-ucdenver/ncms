# Graph-Enhanced Retrieval Pipeline

## Design Specification

**From Regex Heuristics to Cognitive Retrieval**

Version 0.1 Draft | March 2026

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Graph-Expanded Retrieval (Tier 1.5)](#3-graph-expanded-retrieval-tier-15)
4. [GLiNER Entity Extraction](#4-gliner-entity-extraction)
5. [Semantic Keyword Bridge Nodes](#5-semantic-keyword-bridge-nodes)
6. [Consolidation Layer](#6-consolidation-layer)
7. [SPLADE Sparse Neural Retrieval](#7-splade-sparse-neural-retrieval)
8. [Contradiction Detection](#8-contradiction-detection)
9. [Synthesized Retrieval (Tier 4)](#9-synthesized-retrieval-tier-4)
10. [Configuration](#10-configuration)
11. [Migration](#11-migration)

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

| Capability | What it adds | LLM required | Improves |
|------------|-------------|--------------|----------|
| Graph Expansion (Tier 1.5) | Entity-based cross-memory discovery | No | Query-time candidate discovery |
| GLiNER NER | Semantic entity extraction at store-time | No (209M NER model) | Graph data richness |
| Keyword Bridges | Semantic concept bridge nodes | Yes (small/local) | Cross-subgraph connectivity |
| Consolidation | Background clustering + insight synthesis | Yes (periodic) | Emergent relationship discovery |
| SPLADE | Learned sparse term expansion fused with BM25 | No (530M ONNX model) | Semantic recall in Tier 1 |
| Contradiction Detection | LLM comparison at ingest time | Yes (at ingest) | Knowledge freshness/accuracy |
| Synthesized Retrieval (Tier 4) | Multi-memory summarization | Yes (query-time) | Answer quality |

---

## 2. Architecture

### Target Pipeline (All Phases)

```
                           ┌─────────────────────────────────────┐
                           │           STORE TIME                 │
                           │                                      │
                           │  Content ──▶ [Entity Extract] ───┐  │
                           │    │          Regex / GLiNER      │  │
                           │    │              │               │  │
                           │    │         ┌────┴────┐    ┌─────┴─┐│
                           │    │         │ Entities│    │Keyword ││
                           │    │         │ + Rels  │    │Bridges ││
                           │    │         └────┬────┘    └──┬────┘│
                           │    │              │   Graph    │     │
                           │    │         ┌────┴───────────┴───┐ │
                           │    │         │  NetworkX / Neo4j  │ │
                           │    │         └────────────────────┘ │
                           │    │                                 │
                           │    ├──▶ [SPLADE Index]               │
                           │    │     Sparse vector stored        │
                           │    │                                 │
                           │    └──▶ [Contradiction Check]        │
                           │          LLM compares vs candidates  │
                           │          Annotates both sides        │
                           │                                      │
                           │  Background: [Consolidation]         │
                           │  Discovers cross-memory patterns     │
                           │  Creates insight nodes + rels        │
                           └──────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│                            QUERY TIME                                    │
│                                                                          │
│  Query ──▶ [Tier 1: BM25 + SPLADE Hybrid]                               │
│              BM25 (Tantivy) + SPLADE (fastembed)                         │
│              fused via Reciprocal Rank Fusion                            │
│                    │                                                     │
│                    ▼                                                     │
│            [Tier 1.5: Graph Expansion]                                   │
│              Discover entity-related                                     │
│              memories search missed                                      │
│                    │                                                     │
│                    ▼                                                     │
│            [Tier 2: ACT-R Scoring]                                       │
│              base-level + spreading                                      │
│              + noise → combined score                                    │
│                    │                                                     │
│                    ▼                                                     │
│            [Tier 3: LLM-as-Judge]                                        │
│              optional reranking                                          │
│                    │                                                     │
│                    ▼                                                     │
│            [Tier 4: LLM Synthesis]  (planned)                            │
│              multi-memory summarization                                  │
│              with citations                                              │
│                    │                                                     │
│                    ▼                                                     │
│              Results                                                     │
└──────────────────────────────────────────────────────────────────────────┘
```

### Capability Dependencies

```
Graph Expansion          ← standalone, uses existing graph data
GLiNER Extraction        ← standalone, enriches graph data
Keyword Bridges          ← benefits from GLiNER (richer extraction)
Consolidation            ← benefits from GLiNER + Bridges (richer graph)
SPLADE                   ← standalone, parallel Tier 1 channel
Contradiction Detection  ← standalone, uses search + graph for candidates
Synthesis                ← benefits from all above (better retrieval)
```

Graph Expansion and GLiNER can be enabled independently. Graph Expansion improves immediately with whatever graph data exists. GLiNER makes Graph Expansion dramatically more effective. SPLADE and Contradiction Detection are fully independent of each other and the graph capabilities.

---

## 3. Graph-Expanded Retrieval (Tier 1.5)

**Status: Implemented**

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

## 4. GLiNER Entity Extraction

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

## 5. Semantic Keyword Bridge Nodes

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

## 6. Consolidation Layer

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

## 7. SPLADE Sparse Neural Retrieval

**Status: Implemented**

### 7.1 Motivation

BM25 relies on exact lexical matching — the query term must appear in the document. SPLADE (Sparse Lexical and Expansion Model) uses BERT's masked language model head to learn term expansions. A query for "API specification" also activates "endpoint", "schema", "contract" — terms a human would associate but BM25 cannot discover.

Unlike dense vector retrieval, SPLADE produces sparse vectors that remain interpretable and compatible with inverted index architectures. This preserves the precision advantages of lexical search while adding semantic recall.

### 7.2 Design

**File:** `src/ncms/infrastructure/indexing/splade_engine.py`

```python
class SparseVector:
    """Parallel arrays of vocabulary indices and weights."""
    indices: list[int]
    values: list[float]

class SpladeEngine:
    def __init__(self, model_name="prithivida/Splade_PP_en_v1"):
        self._model = None           # Lazy-loaded SparseTextEmbedding
        self._vectors: dict[str, SparseVector] = {}  # memory_id → sparse vec

    def index_memory(self, memory: Memory) -> None:
        """Encode content → sparse vector, store in dict."""

    def search(self, query: str, limit: int = 50) -> list[tuple[str, float]]:
        """Brute-force dot-product search against all stored vectors."""

    def remove(self, memory_id: str) -> None
```

Uses [fastembed](https://github.com/qdrant/fastembed) for ONNX-based SPLADE inference (~530MB model, CPU). Lazy-loaded on first use. Install: `pip install ncms[splade]`.

### 7.3 Reciprocal Rank Fusion (RRF)

BM25 and SPLADE produce scores on incompatible scales. Rather than normalizing (fragile, distribution-dependent), results are fused via Reciprocal Rank Fusion (Cormack et al. 2009):

```
RRF(d) = Σ_r  1 / (k + rank_r(d))
```

Where `k = 60` (standard constant). Documents appearing in both BM25 and SPLADE result sets receive a rank boost. This is parameter-free and robust across score distributions.

**File:** `src/ncms/application/memory_service.py` → `_rrf_fuse()` static method.

### 7.4 Integration

- **Store time:** After Tantivy indexing, SPLADE encodes and stores the sparse vector
- **Search time:** BM25 and SPLADE run in parallel, fused via RRF, then passed to Tier 1.5 (Graph Expansion) and Tier 2 (ACT-R Scoring)
- **Delete:** Removes from both Tantivy and SPLADE index
- **Combined scoring:** `combined = bm25 * w_bm25 + splade * w_splade + activation * w_actr`

### 7.5 Configuration

```
NCMS_SPLADE_ENABLED=false                        # Default: false (opt-in)
NCMS_SPLADE_MODEL=prithivida/Splade_PP_en_v1     # ONNX model via fastembed
NCMS_SPLADE_TOP_K=50                              # SPLADE candidates per search
NCMS_SCORING_WEIGHT_SPLADE=0.0                    # Weight in combined score
```

Install: `pip install ncms[splade]`

---

## 8. Contradiction Detection

**Status: Implemented**

### 8.1 Motivation

Knowledge evolves over time. A memory saying "The API uses session cookies" may be superseded by "The API uses JWT tokens". Without contradiction detection, both memories exist and compete during retrieval — the stale one potentially outranking the fresh one due to higher access frequency.

Contradiction detection identifies these conflicts at ingest time and annotates both sides bidirectionally, so retrieval consumers can surface or filter stale knowledge.

### 8.2 Design

**File:** `src/ncms/infrastructure/llm/contradiction_detector.py`

```python
async def detect_contradictions(
    new_memory: Memory,
    existing_memories: list[Memory],
    model: str = "gpt-4o-mini",
    api_base: str | None = None,
) -> list[dict]:
    """Compare new memory against existing candidates via LLM.

    Returns list of contradictions, each with:
    - existing_memory_id: ID of the contradicted memory
    - contradiction_type: factual | temporal | configuration
    - explanation: brief description
    - severity: low | medium | high
    """
```

Follows the standard litellm pattern (code fence stripping, `think=False` for Ollama, non-fatal on error). Validates returned memory IDs against the actual candidate set to filter LLM hallucinated IDs.

### 8.3 Candidate Discovery

At ingest time, candidates are found via two channels:

1. **Search similarity:** `self._index.search(content)` finds lexically similar memories
2. **Graph traversal:** Entity overlap discovers related memories search missed

Candidates are domain-scoped (only memories with overlapping domains) and capped at `contradiction_candidate_limit`.

### 8.4 Bidirectional Annotation

Contradictions are stored in `Memory.structured`:

**New memory:**
```json
{"contradictions": [{"existing_memory_id": "abc", "contradiction_type": "factual",
  "explanation": "Auth method differs", "severity": "high"}]}
```

**Existing memory:**
```json
{"contradicted_by": [{"newer_memory_id": "def", "contradiction_type": "factual",
  "explanation": "Auth method differs", "severity": "high"}]}
```

Both sides are updated via `store.update_memory()`.

### 8.5 Configuration

Contradiction detection reuses the existing `llm_model` and `llm_api_base` config — no separate model configuration needed.

```
NCMS_CONTRADICTION_DETECTION_ENABLED=false    # Default: false (opt-in)
NCMS_CONTRADICTION_CANDIDATE_LIMIT=5          # Max candidates per check
NCMS_LLM_MODEL=gpt-4o-mini                   # Shared with LLM judge
NCMS_LLM_API_BASE=                            # Shared with LLM judge
```

---

## 9. Synthesized Retrieval (Tier 4)

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

## 10. Configuration Summary

### All Config Values

| Variable | Capability | Default | Purpose |
|----------|-----------|---------|---------|
| `NCMS_GRAPH_EXPANSION_ENABLED` | Graph Expansion | `true` | Enable Tier 1.5 graph expansion |
| `NCMS_GRAPH_EXPANSION_DEPTH` | Graph Expansion | `1` | Graph traversal depth |
| `NCMS_GRAPH_EXPANSION_MAX` | Graph Expansion | `10` | Max graph-discovered candidates |
| `NCMS_GLINER_ENABLED` | GLiNER NER | `false` | Enable GLiNER entity extraction |
| `NCMS_GLINER_MODEL` | GLiNER NER | `urchade/gliner_medium-v2.1` | GLiNER model for extraction |
| `NCMS_GLINER_THRESHOLD` | GLiNER NER | `0.3` | Min confidence for entity inclusion |
| `NCMS_KEYWORD_BRIDGE_ENABLED` | Keyword Bridges | `false` | Enable keyword bridge nodes |
| `NCMS_KEYWORD_MAX_PER_MEMORY` | Keyword Bridges | `8` | Max keywords per memory |
| `NCMS_KEYWORD_LLM_MODEL` | Keyword Bridges | `gpt-4o-mini` | LLM model for keyword extraction |
| `NCMS_KEYWORD_LLM_API_BASE` | Keyword Bridges | *(none)* | vLLM endpoint for keyword extraction |
| `NCMS_SPLADE_ENABLED` | SPLADE | `false` | Enable SPLADE sparse retrieval |
| `NCMS_SPLADE_MODEL` | SPLADE | `prithivida/Splade_PP_en_v1` | SPLADE ONNX model |
| `NCMS_SPLADE_TOP_K` | SPLADE | `50` | SPLADE candidates per search |
| `NCMS_SCORING_WEIGHT_SPLADE` | SPLADE | `0.0` | SPLADE weight in combined score |
| `NCMS_CONTRADICTION_DETECTION_ENABLED` | Contradiction | `false` | Enable contradiction detection |
| `NCMS_CONTRADICTION_CANDIDATE_LIMIT` | Contradiction | `5` | Max candidates per check |
| `NCMS_LLM_API_BASE` | LLM Judge + Contradiction | *(none)* | vLLM endpoint for LLM-as-judge |
| `NCMS_CONSOLIDATION_KNOWLEDGE_ENABLED` | Consolidation | `false` | Enable knowledge consolidation |
| `NCMS_CONSOLIDATION_KNOWLEDGE_MIN_CLUSTER_SIZE` | Consolidation | `3` | Min cluster size |
| `NCMS_CONSOLIDATION_KNOWLEDGE_MODEL` | Consolidation | `gpt-4o-mini` | Consolidation model |
| `NCMS_CONSOLIDATION_KNOWLEDGE_API_BASE` | Consolidation | *(none)* | vLLM endpoint for consolidation |
| `NCMS_CONSOLIDATION_KNOWLEDGE_MAX_INSIGHTS_PER_RUN` | Consolidation | `5` | Max insights per run |
| `NCMS_SYNTHESIS_ENABLED` | Synthesis | `false` | Enable Tier 4 synthesis |
| `NCMS_SYNTHESIS_MODEL` | Synthesis | `gpt-4o-mini` | Synthesis model |
| `NCMS_SYNTHESIS_TOP_K` | Synthesis | `5` | Memories to synthesize |

All default to `false`/off — each phase is opt-in and backward compatible.

---

## 11. Migration

### Graph Expansion: No Migration Required

Graph expansion uses existing entity data. Whatever entities the regex extractor has already produced are traversed at query time. No data changes needed.

### GLiNER: Optional Re-extraction

After enabling GLiNER extraction, existing memories still have their regex-extracted entities. Options:
- **Lazy**: New memories get GLiNER extraction, old memories keep regex entities
- **Batch**: Background task re-extracts entities for all existing memories using GLiNER

### Keyword Bridges: One-Time Bridge Generation

After enabling keyword bridges, run a one-time pass to extract keywords from all existing memories and create bridge nodes.

### Consolidation: Automatic

Consolidation processes all memories from the start. No special migration.

### SPLADE: Requires Reindexing

SPLADE vectors are stored in-memory. After enabling SPLADE, existing memories need to be re-indexed to build sparse vectors. New memories are indexed automatically. A reindex utility is planned.

### Contradiction Detection: No Migration Required

Contradiction detection runs at ingest time only. Existing memories are not retroactively checked. New memories stored after enabling will be checked against all existing candidates.

### Synthesis: No Migration Required

Synthesis is a query-time operation. No data changes needed.

---

## References

- Anderson, J.R. (2007). *How Can the Human Mind Occur in the Physical Universe?* — ACT-R activation theory
- Zaratiana, U. et al. (2024). *GLiNER: Generalist Model for Named Entity Recognition using Bidirectional Transformer* (NAACL 2024) — zero-shot NER model for entity extraction
- Formal, T. et al. (2021). *SPLADE: Sparse Lexical and Expansion Model for First Stage Ranking* — learned sparse retrieval
- Cormack, G.V. et al. (2009). *Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods* — RRF fusion used for BM25 + SPLADE
- [fastembed](https://github.com/qdrant/fastembed) — ONNX-based embedding library for SPLADE inference
- Google Always-On Memory Agent — consolidation and multi-stage retrieval patterns
- NCMS Design Specification (`docs/ncms-design-spec.md`) — core architecture
