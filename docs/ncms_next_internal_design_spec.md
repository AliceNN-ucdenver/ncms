# NCMS-Next Internal Design Specification
## Hierarchical Temporal Memory Graph (HTMG)

**Status:** Draft v2 — Revised with implementation phases
**Audience:** Research engineers, platform engineers, coding agents, evaluation engineers
**Last Updated:** 2026-03-12
**Goal:** Define a concrete, implementable architecture for evolving NCMS into a selective, episodic, bitemporal, sparse memory system with graph-native hierarchy.

---

# 1. Executive Summary

NCMS-Next extends the current NCMS sparse memory architecture into a full **memory lifecycle system**.

Instead of treating memory primarily as a retrieval problem, NCMS-Next treats memory as a pipeline:

**observe → admit → normalize → reconcile → structure → consolidate → retrieve**

The key design change is the introduction of a **Hierarchical Temporal Memory Graph (HTMG)** with four memory levels:

1. **Atomic Memory** — individual facts, observations, claims, decisions, event fragments
2. **Entity State** — temporally scoped assertions about the current or historical state of an entity/artifact/relationship
3. **Episode** — bounded event/workflow/incident/change arc composed of multiple traces and state transitions
4. **Abstract Memory** — higher-order summaries, recurring patterns, state trajectories, and strategic insights

This document is specific enough for a coding agent to implement a working version, phase by phase, with each phase independently testable and verifiable.

---

# 2. Research Context and Novelty

## 2.1 Related work

The following systems address subsets of NCMS-Next's scope. No single system combines all components.

### Cognitive scoring in retrieval
- **"Human-Like Remembering and Forgetting in LLM Agents"** (HAI 2024, ACM) — integrates ACT-R base-level activation into LLM agent memory, but treats it as a downstream scoring component, not as part of a multi-tier retrieval pipeline with BM25+SPLADE+graph fusion.
- **Generative Agents** (Park et al., 2023) — uses recency × importance × relevance scoring inspired by cognitive science, but without formal ACT-R math, spreading activation, or sparse neural retrieval.

### Agent memory systems
- **A-MEM** (NeurIPS 2025) — Zettelkasten-inspired agentic memory with dynamic indexing and linking. Focuses on note organization, not retrieval quality or multi-agent coordination.
- **MemGPT** (Packer et al., 2024) — OS-inspired virtual memory with paging between context and archival storage. No knowledge graph, no ACT-R scoring, no episode formation.
- **Adaptive Memory Admission Control for LLM Agents** (arXiv 2025) — addresses memory admission scoring but without graph-native hierarchy, bitemporal tracking, or episode formation.

### Sleep-inspired consolidation
- **Active Dreaming Memory (ADM)** (Engineering Archive, 2025) — wake/sleep phases for agents where episodic traces are consolidated into semantic rules via counterfactual simulation. Focused on task-learning, not knowledge graph consolidation.
- **NeuroDream** (SSRN, 2025) — sleep-inspired consolidation for neural networks via internally generated simulations. Operates at the neural training level, not at the knowledge representation level.

### Knowledge graphs in retrieval
- **GraphRAG** (Microsoft, 2024) — builds entity knowledge graphs from documents, generates community summaries, and uses hierarchical community detection for global queries. Heavy LLM dependency for graph construction; no cognitive scoring, no bitemporal model, no agent coordination.

### Sparse retrieval fusion
- **BM25 + SPLADE + RRF** — standard in production search (Elastic, Weaviate). RRF improves nDCG@10 by ~1.4% over SPLADE alone on BEIR. NCMS uses weighted scoring with ACT-R cognitive rescoring rather than RRF, which is a distinct approach.

## 2.2 NCMS-Next novelty claim

NCMS-Next is novel in combining **all** of the following in a single, embedded, vector-free system:

1. **Cognitive retrieval scoring** — ACT-R activation math (base-level learning, spreading activation, logistic noise) integrated into a multi-tier retrieval pipeline
2. **Dual sparse fusion** — BM25 (Tantivy/Rust) + SPLADE (neural sparse expansion) without dense vectors
3. **Selective memory admission** — heuristic feature scoring for novelty, utility, persistence, and redundancy with routing to typed memory levels
4. **Truth-maintaining state reconciliation** — explicit supersession, refinement, and conflict edges with bitemporal validity
5. **Episode formation** — automatic grouping of related traces into bounded event/workflow arcs
6. **Hierarchical consolidation** — four levels of abstraction (atomic → episode summary → pattern → strategic insight)
7. **Bitemporal model** — separate valid-time and system-time tracking for historical queries
8. **Agent coordination** — embedded knowledge bus with surrogate/snapshot system for offline agents
9. **Zero infrastructure** — everything runs in-process with `pip install ncms`

No existing system combines cognitive science-grounded scoring, typed hierarchical memory, bitemporal truth maintenance, and embedded multi-agent coordination. Individual components exist in isolation across different research threads; NCMS-Next unifies them.

### Gap analysis

| Capability | Closest System | What NCMS-Next Adds |
|---|---|---|
| ACT-R scoring in retrieval | HAI 2024 (LLM generation only) | Uses ACT-R as retrieval rescorer, not generation modifier |
| BM25 + SPLADE fusion | Elastic, Qdrant (production systems) | Adds ACT-R cognitive third tier on top of fusion |
| Persistent agent memory | A-MEM, Letta/MemGPT | Adds inter-agent bus + surrogate responses |
| KG + entity extraction + RAG | Microsoft GraphRAG | Uses local NER (GLiNER) instead of LLM extraction; cognitive scoring of graph features |
| Surrogate/offline agents | Nothing comparable | **Entirely novel**: snapshot-based keyword surrogate |
| Sleep-wake consolidation | ADM (single-agent) | Multi-agent knowledge consolidation with LLM synthesis |
| Vector-free retrieval | BM25-only systems | Sparse neural (SPLADE) without dense vectors |

### Additional references

- **Adaptive Memory Admission Control for LLM Agents** (arXiv 2025) — addresses admission scoring but without graph hierarchy, bitemporal tracking, or episodes
- **ICLR 2026 Workshop: MemAgents** — dedicated workshop on memory for LLM-based agentic systems, indicating community interest
- **KG-RAG** (Nature Scientific Reports, 2025) — combines DPR with graph neural networks, showing 29.1% F1 improvement over LLM-only
- **BM42** (Qdrant, 2024) — converges BM25 IDF with transformer attention weights; validates the sparse-over-dense direction
- **Memory in the Age of AI Agents** survey (arXiv, Dec 2025) — comprehensive taxonomy noting multi-agent shared memory remains an open challenge

## 2.3 Potential publication angles

Based on the research landscape, the strongest novel contributions for publication:

1. **Three-tier retrieval pipeline** (BM25 → SPLADE fusion → ACT-R rescoring) with ablation results showing component contributions
2. **Surrogate agent pattern** — snapshot + keyword matching for offline response, addressing a gap in multi-agent resilience
3. **Embedded cognitive memory architecture** — the unified system combining all components, positioned as a cognitive science-grounded alternative to vector-dependent RAG
4. **Position paper: "vector-free but neural-enriched retrieval"** — distinguishing sparse neural (SPLADE) from dense embedding approaches, with empirical evidence

## 2.4 Key research questions

1. Does selective admission improve retrieval quality, or does aggressive filtering lose important context?
2. Does episodic grouping improve event reconstruction queries compared to flat memory?
3. Does bitemporal state tracking reduce stale-fact leakage in change-detection queries?
4. Does hierarchical consolidation produce useful abstractions, or does it drift from source truth?
5. Does intent-aware retrieval routing outperform uniform scoring across query types?

Each phase below is designed to answer one or more of these questions through ablation.

---

# 3. Design Objectives

## 3.1 Primary objectives

NCMS-Next must:

- Preserve current sparse retrieval strengths (BM25 + SPLADE + ACT-R + graph expansion)
- Improve memory quality before retrieval through selective storage
- Support event-level and change-level recall
- Model stale, superseded, and conflicting knowledge explicitly
- Support both current-state and historical-state queries
- Support hierarchical abstraction over time
- Remain understandable, debuggable, and incrementally deployable
- Maintain backward compatibility with existing NCMS API at each phase

## 3.2 Non-objectives for v1

The first implementation does **not** need:

- End-to-end learned memory admission (use heuristics first)
- Automatic ontology induction
- Full probabilistic belief revision
- Dense-vector-first storage
- Multi-agent negotiation loops
- Streaming online training
- Real-time distributed systems (stay in-process)

---

# 4. Current NCMS Baseline

Understanding the current implementation is essential for incremental evolution.

## 4.1 Current data model (Pydantic)

| Model | Purpose | Key Fields |
|-------|---------|------------|
| `Memory` | Single knowledge unit | id, content, type (11 literals), importance, domains, tags, source_agent |
| `Entity` | Graph node | id, name, type, attributes |
| `Relationship` | Graph edge | source_entity_id, target_entity_id, type, valid_at, invalid_at |
| `ScoredMemory` | Search result | memory, bm25_score, splade_score, base_level, spreading, total_activation |
| `KnowledgeAsk` | Bus question | from_agent, question, domains, urgency, context |
| `KnowledgeResponse` | Bus answer | confidence, knowledge, provenance, source_mode (live/warm/cold) |
| `KnowledgeSnapshot` | Agent state | agent_id, entries, ttl_hours |
| `AgentInfo` | Agent registry | agent_id, domains, status (online/offline/sleeping) |

## 4.2 Current database schema (7 tables)

`memories`, `entities`, `relationships`, `memory_entities`, `access_log`, `snapshots`, `consolidation_state` — all SQLite with WAL mode.

## 4.3 Current protocols (DI contracts)

| Protocol | Methods | Implementation |
|----------|---------|----------------|
| `MemoryStore` | 20+ methods (CRUD, access log, snapshots, consolidation state) | `SQLiteStore` |
| `IndexEngine` | initialize, index_memory, search, remove | `TantivyEngine` |
| `GraphEngine` | add_entity/relationship, find, get_neighbors, get_related_memory_ids | `NetworkXGraph` |
| `KnowledgeBusTransport` | register, ask, respond, announce, subscribe, inbox | `AsyncBus` |

## 4.4 Current retrieval pipeline

```
Query → BM25 candidates → SPLADE fusion → Graph expansion → ACT-R scoring → Optional LLM judge → Ranked results
```

## 4.5 Current ablation results (SciFact, 300 queries)

| Configuration | nDCG@10 | Recall@100 |
|---------------|:-------:|:----------:|
| BM25 Only | 0.685 | 0.893 |
| + Graph | 0.687 | 0.893 |
| + SPLADE | 0.700 | 0.944 |
| Full Pipeline | 0.702 | 0.944 |

These are the baseline numbers that must be preserved or improved through NCMS-Next evolution.

## 4.6 Current implementation strengths (from codebase audit)

- **Clean layered architecture** with genuine separation of concerns across domain/application/infrastructure/interfaces
- **3-tier retrieval pipeline** (BM25 + SPLADE RRF fusion + ACT-R rescoring + optional LLM judge) is sophisticated and well-instrumented
- **Every optional feature** degrades gracefully on error and is independently toggleable via config
- **Full observability** via `EventLog` ring buffer with per-stage pipeline timing and SSE streaming
- **Surrogate response pattern** via keyword-indexed snapshots is architecturally distinctive
- **Zero TODO/FIXME markers** — codebase is consistently finished
- **Comprehensive test suite** — 26 test files covering unit and integration levels
- **Automatic text chunking** — GLiNER (1,200 char chunks) and SPLADE (400 char chunks) with sentence-boundary splitting and dedup/max-pool merge

## 4.7 Current implementation gaps (from codebase audit)

These gaps should be addressed before or during HTMG implementation:

| Gap | Location | Impact | Fix Phase |
|-----|----------|--------|-----------|
| **Protocol compliance** — `MemoryService` constructor takes concrete `SQLiteStore`, `TantivyEngine`, `NetworkXGraph` instead of `Protocol` interfaces from `domain/protocols.py` | `application/memory_service.py` | Undermines swappability for HTMG backends | Phase 0 |
| **`memory_count()` scalability** — loads all records via `list_memories(limit=100000)` instead of `SELECT COUNT(*)` | `application/memory_service.py` | Will not scale with HTMG's additional data | Phase 0 |
| **`mismatch_penalty` unused** — parameter exists in `total_activation()` but always defaults to 0.0 | `domain/scoring.py` | Can be leveraged for `SupersessionPenalty` in Phase 2 | Phase 2 |
| **`association_strengths` unpopulated** — parameter exists in `spreading_activation()` but never provided | `domain/scoring.py` | Can be populated from graph edge weights in Phase 2-3 | Phase 2 |
| **`run_decay_pass()` is diagnostic only** — flags low-activation memories but takes no corrective action (no pruning, archiving, or importance decay) | `application/consolidation_service.py` | Phase 5 consolidation should make this actionable | Phase 5 |
| **GraphService is thin** — 48 lines, no entity resolution, merging, or subgraph extraction despite CLAUDE.md describing these capabilities | `application/graph_service.py` | Phases 2-3 will substantially expand this | Phase 2-3 |
| **Demo agents are cookie-cutter** — all 3 share 90% identical logic; differ only in domain filter and knowledge type label | `demo/agents/` | Phase 6 demo can use generic configurable agent | Phase 6 |
| **No agent implements `on_restore()`** — snapshot data published but never consumed on wake-up | `interfaces/agent/base.py` | Episode/state system gives snapshots real utility | Phase 3 |
| **Surrogate matching is basic** — pure keyword term overlap, no TF-IDF or semantic consideration | `application/snapshot_service.py` | Could upgrade with BM25 scoring in future | Post-v1 |
| **`MemoryStore` protocol is monolithic** — 20+ methods covering memory, entity, snapshot, and consolidation CRUD | `domain/protocols.py` | Could split into `MemoryStore`, `EntityStore`, `SnapshotStore`, `ConsolidationStore` | Phase 0 |
| **Bus service surrogate discovery gap** — cannot enumerate snapshots for fully deregistered agents (acknowledged with `pass` comment at line 142-144) | `application/bus_service.py` | Needs snapshot-by-domain query | Phase 0 |
| **`set_ask_handler` types handler as `object`** — sacrifices type safety; should be `Callable[[KnowledgeAsk], Awaitable[KnowledgeResponse | None]]` | `domain/protocols.py` | Type annotation fix | Phase 0 |

---

# 5. System Architecture Overview

## 5.1 End-to-end pipeline

```
Incoming Observation
    │
    ▼
Feature Extraction (entities, keywords, temporal markers, change signals)
    │
    ▼
Admission Scoring (novelty, utility, reliability, persistence, redundancy, etc.)
    │
    ▼
Routing Decision
    ├── Discard (score too low)
    ├── Ephemeral Cache (useful but transient)
    ├── Atomic Memory (store + index + graph)
    ├── Entity State Update (state reconciliation)
    └── Episode Fragment (episode builder)
    │
    ▼
Graph Insert (nodes + edges based on type)
    │
    ▼
Hierarchical Consolidation (periodic background process)
    │
    ▼
Search Index + Graph Views (materialized for retrieval)
    │
    ▼
Intent-Aware Retrieval (classify query → route to appropriate node types)
    │
    ▼
Multi-signal Ranking (BM25 + SPLADE + Graph + ACT-R + temporal + hierarchy)
    │
    ▼
Optional LLM Judge
    │
    ▼
Final Memory Context
```

## 5.2 Core architecture layers

1. **Ingestion and feature extraction** — entity extraction (GLiNER), keyword extraction, temporal parsing, change/decision/incident marker detection
2. **Selective memory admission** — multi-feature scoring, routing to typed memory levels
3. **Memory normalization** — canonical entity resolution, state-slot normalization
4. **State reconciliation and supersession** — truth-maintenance with typed edges (supports, refines, supersedes, conflicts)
5. **Episode formation** — anchor detection, fragment assignment, episode lifecycle
6. **Hierarchical graph persistence** — typed nodes and edges in SQLite + NetworkX
7. **Hierarchical consolidation** — episode summaries, state trajectories, recurring patterns, strategic insights
8. **Intent-aware retrieval and ranking** — query intent classification, type-appropriate candidate selection, multi-signal ranking

---

# 6. Data Model

All models use Pydantic `BaseModel` (matching current NCMS convention). All models extend from a common base.

## 6.1 Common base fields

All memory-like nodes include:

```python
class MemoryNodeBase(BaseModel):
    id: str = Field(default_factory=_uuid)
    content: str
    summary: str | None = None
    source_id: str | None = None
    source_type: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    observed_at: datetime | None = None
    ingested_at: datetime = Field(default_factory=_utcnow)
    confidence: float = 1.0
    importance: float = 5.0
    domains: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
```

## 6.2 AtomicMemory

Extends current `Memory` model with admission and lifecycle fields.

```python
class AtomicMemory(MemoryNodeBase):
    node_type: Literal["atomic"] = "atomic"
    memory_kind: Literal[
        "fact", "observation", "claim", "decision",
        "event_detail", "instruction", "outcome",
    ] = "fact"
    # Backward compat: map to existing Memory.type
    legacy_type: str = "fact"

    # Admission scores (computed at ingest)
    admission_score: float = 0.0
    novelty_score: float = 0.0
    utility_score: float = 0.0
    redundancy_score: float = 0.0
    persistence_score: float = 0.0
    state_change_signal: float = 0.0

    # Linking
    episode_id: str | None = None
    source_agent: str | None = None
    project: str | None = None
```

## 6.3 EntityState

Temporally scoped assertion about an entity.

```python
class EntityState(MemoryNodeBase):
    node_type: Literal["entity_state"] = "entity_state"
    entity_id: str
    state_key: str
    state_value: str
    state_scope: str | None = None

    # Bitemporal fields
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    is_current: bool = True

    # Truth maintenance
    supersedes: str | None = None
    superseded_by: str | None = None
    revision_reason: str | None = None
```

## 6.4 Episode

Bounded event/workflow arc.

```python
class Episode(MemoryNodeBase):
    node_type: Literal["episode"] = "episode"
    title: str
    episode_type: Literal[
        "incident", "release", "migration", "meeting", "task",
        "investigation", "conversation_arc", "decision_process", "workflow",
    ] = "workflow"
    status: Literal["open", "closed", "merged", "superseded"] = "open"
    start_time: datetime | None = None
    end_time: datetime | None = None
    actors: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    outcome: str | None = None
    cause_summary: str | None = None
    member_memory_ids: list[str] = Field(default_factory=list)
    member_state_ids: list[str] = Field(default_factory=list)
    related_episode_ids: list[str] = Field(default_factory=list)
```

## 6.5 AbstractMemory

Higher-level abstraction derived from lower-level nodes.

```python
class AbstractMemory(MemoryNodeBase):
    node_type: Literal["abstract"] = "abstract"
    abstract_type: Literal[
        "episode_summary", "state_trajectory", "recurring_pattern",
        "strategic_insight", "policy", "invariant", "lesson",
    ] = "episode_summary"
    scope: Literal["entity", "artifact", "team", "workflow", "global"] = "entity"
    derived_from_ids: list[str] = Field(default_factory=list)
    stability_score: float = 0.5
    refresh_due_at: datetime | None = None
```

## 6.6 Backward compatibility

The existing `Memory` model remains the public API type. Internally, NCMS-Next stores `AtomicMemory` nodes but converts to/from `Memory` at the protocol boundary. This means:

- Existing MCP tools continue to work unchanged
- Existing tests pass without modification
- The `ScoredMemory` result type gains optional fields for node_type and hierarchy info
- Migration is additive: new columns + new tables, no destructive changes

---

# 7. Graph Schema

## 7.1 Node types

- `AtomicMemory` (extends current memory nodes in graph)
- `EntityState` (new)
- `Episode` (new)
- `AbstractMemory` (new)
- `Entity` (existing)

## 7.2 Edge types

### Membership / hierarchy edges
- `BELONGS_TO_EPISODE` — atomic/state → episode
- `ABSTRACTS` — episode/state → abstract
- `DERIVED_FROM` — abstract → source nodes
- `SUMMARIZES` — abstract → episode

### Semantic / support edges
- `MENTIONS_ENTITY` — memory → entity (existing, currently implicit via memory_entities table)
- `RELATED_TO` — memory ↔ memory
- `SUPPORTS` — memory → memory
- `REFINES` — memory → memory

### Truth-maintenance edges
- `SUPERSEDES` — state → prior state
- `SUPERSEDED_BY` — inverse
- `CONFLICTS_WITH` — bidirectional
- `CURRENT_STATE_OF` — state → entity

### Temporal / causal edges
- `PRECEDES` — memory → memory
- `CAUSED_BY` — memory → memory

## 7.3 Graph hierarchy

```
AtomicMemory ──BELONGS_TO_EPISODE──► Episode ──ABSTRACTS──► AbstractMemory
EntityState  ──BELONGS_TO_EPISODE──► Episode
EntityState  ──CURRENT_STATE_OF────► Entity
EntityState₂ ──SUPERSEDES──────────► EntityState₁
AbstractMemory ──DERIVED_FROM──────► EntityState₁, EntityState₂
```

## 7.4 Materialized views (SQLite)

For performance, maintain indexed views:

- `current_state_view` — `SELECT * FROM entity_states WHERE is_current = 1`
- `open_episode_view` — `SELECT * FROM episodes WHERE status = 'open'`
- `superseded_memory_view` — entity states with `superseded_by IS NOT NULL`

---

# 8. Selective Memory Admission

## 8.1 Purpose

Admission decides whether new information should be ignored, cached, stored as atomic memory, stored as entity state, or routed into an episode.

## 8.2 Admission features

For each candidate memory `m`, compute normalized scores in `[0,1]`.

### Novelty — `1 - max_sparse_similarity_to_top_k_related`
- BM25 search for top-3 existing memories
- Cosine-like overlap measure
- Near-duplicate detection via high-similarity threshold

### Utility — heuristic keyword + structure signals
- Contains decision/change/outcome/error/fix/version/release/incident markers
- References known tracked entities
- Contains question-answerable structure

### Reliability — source and extraction confidence
- Structured/system source → high
- Agent inference without evidence → medium
- Speculative wording → lower
- Repeated corroboration → boost

### Temporal Salience — time-sensitivity signals
- Explicit dates/timestamps present
- Change verbs: `changed`, `updated`, `released`, `deprecated`, `migrated`, `fixed`
- Temporal markers: `now`, `currently`, `since`, `as of`

### Persistence — durability signals
- Policy, decision, architectural fact → high
- Recurring process or issue → medium
- Ephemeral task context → low

### Redundancy — overlap with existing knowledge
- Overlap with existing atomic memories (BM25 similarity)
- Overlap with current entity state assertions
- Overlap with existing consolidated abstractions

### Episode Affinity — episode membership signals (lightweight heuristic for admission)
- Structured anchor detection (issue IDs, PR numbers, incident markers) — bonus signal
- Causal cue words (caused, triggered, resolved, etc.)
- Change/incident markers (deploy, release, outage, etc.)
- Capitalized named entity proxy (entity density heuristic)

Note: Full episode matching uses the hybrid linker (Phase 3) with BM25/SPLADE candidate generation and 7 weighted signals, running post-admission for all stored content when `episodes_enabled=True`.

### State Change Signal — entity state mutation signals
- Same entity + same state key + different value
- Change verbs or version markers
- Contradiction with existing current state

## 8.3 Admission score

```
AdmissionScore =
    0.20 × Novelty +
    0.18 × Utility +
    0.12 × Reliability +
    0.12 × TemporalSalience +
    0.15 × Persistence −
    0.15 × Redundancy +
    0.04 × EpisodeAffinity +
    0.14 × StateChangeSignal
```

Weights are interpretable and tunable.

## 8.4 Routing policy

| Route | Condition |
|-------|-----------|
| **Discard** | `score < 0.25` AND `persistence < 0.20` AND `state_change_signal < 0.20` |
| **Ephemeral cache** | `0.25 ≤ score < 0.45` or useful but low persistence |
| **Atomic memory** | `score ≥ 0.45` AND `state_change_signal < 0.50` AND `episode_affinity < 0.55` |
| **Entity state update** | `state_change_signal ≥ 0.50` |
| **Episode fragment** | `episode_affinity ≥ 0.55` or explicit event markers |

## 8.5 Implementation sketch

```python
@dataclass
class AdmissionFeatures:
    novelty: float
    utility: float
    reliability: float
    temporal_salience: float
    persistence: float
    redundancy: float
    episode_affinity: float
    state_change_signal: float

def score_admission(f: AdmissionFeatures) -> float:
    return (
        0.20 * f.novelty +
        0.18 * f.utility +
        0.12 * f.reliability +
        0.12 * f.temporal_salience +
        0.15 * f.persistence -
        0.15 * f.redundancy +
        0.04 * f.episode_affinity +
        0.14 * f.state_change_signal
    )

def route_memory(f: AdmissionFeatures, score: float) -> str:
    if score < 0.25 and f.persistence < 0.20 and f.state_change_signal < 0.20:
        return "discard"
    if f.state_change_signal >= 0.50:
        return "entity_state_update"
    if f.episode_affinity >= 0.55:
        return "episode_fragment"
    if 0.25 <= score < 0.45:
        return "ephemeral_cache"
    return "atomic_memory"
```

---

# 9. State Reconciliation and Supersession

## 9.1 Purpose

Replace narrow contradiction detection with a truth-maintenance subsystem. When a new state or memory arrives, classify its relationship to existing knowledge.

## 9.2 Relation classes

| Relation | When to use |
|----------|-------------|
| `supports` | Same entity/topic, semantically compatible, same time scope, no value mismatch |
| `refines` | Same entity/topic, narrower scope or richer detail, earlier claim remains valid |
| `supersedes` | Same entity + state key, incompatible value, newer timestamp or stronger source |
| `conflicts` | Incompatible assertions, insufficient evidence for supersession |
| `unrelated` | No meaningful relationship |

## 9.3 Reconciliation actions

- `supports` → create `SUPPORTS` edge, boost both nodes' importance
- `refines` → create `REFINES` edge, link detail to broader claim
- `supersedes` → close prior `valid_to`, set new `valid_from`, create `SUPERSEDES`/`SUPERSEDED_BY` edges, flip `is_current`
- `conflicts` → create `CONFLICTS_WITH` edge, flag for human review
- `unrelated` → insert independently

## 9.4 Reconciliation triggers

- Trigger episode update if the change belongs to an open episode
- Trigger consolidation refresh if a high-level abstraction depends on the changed state
- Emit event to observability log

---

# 10. Episode Formation — Hybrid Episode Linker

## 10.1 Purpose

An episode is a lightweight, evolving memory container that groups new fragments into the same ongoing thread using a small set of complementary signals. Works across all content domains (software tickets, scientific papers, general prose) without requiring LLM at runtime.

## 10.2 Episode profiles

Each episode maintains a compact profile as its backing Memory content, built from members:
- Entity names from all members (via GLiNER)
- Domain tags
- Structured anchors when present (JIRA-123, etc.)

Example profile: `"auth-service, PostgreSQL, JWT, deployment | domains: api | anchors: JIRA-100"`

This backing Memory is indexed in both Tantivy (BM25) and SPLADE, making episodes searchable via existing infrastructure. When a new member joins, the profile is enriched and re-indexed.

## 10.3 Candidate generation

BM25 and SPLADE search the fragment content against episode profiles. Additionally, any open episode with entity overlap is added as a candidate. Union of all three sources = candidate set.

## 10.4 Multi-signal scoring

Each candidate episode is scored using 7 weighted signals:

| Signal | Weight | Role |
|--------|--------|------|
| BM25 score | 0.20 | Lexical overlap with episode profile (normalized via sigmoid) |
| SPLADE score | 0.20 | Semantic overlap (0 if disabled, weight redistributed) |
| Entity overlap | 0.25 | `\|shared\| / min(\|frag\|, \|ep\|)` — topic structure from GLiNER |
| Domain overlap | 0.15 | Same knowledge domain |
| Temporal proximity | 0.10 | Linear decay within window (1.0 at same time → 0.0 at edge) |
| Source agent | 0.05 | 1.0 if same producing agent, 0.0 otherwise |
| Structured anchor | 0.05 | 1.0 if same anchor ID in both, 0.0 otherwise |

When SPLADE is disabled, its weight is redistributed proportionally to other signals.

## 10.5 Decision rules

- Best score ≥ threshold (0.30) → assign to that episode, update profile
- No match + fragment has ≥ min_entities (2) → create new episode
- Otherwise → no episode (fragment stays unattached)

Structured anchors (JIRA-123, PR-456, INCIDENT-789) are detected via regex as a bonus signal, not as a gate. Entity-based matching is the primary mechanism.

## 10.6 Episode closure

Close an episode when:
- Explicit resolution marker appears in fragment text
- No new member arrives within window `T_close` (configurable, default 24h)

## 10.7 Episode lifecycle

```
[*] → Open → Open (add fragment, enrich profile)
Open → Closed (resolution marker or stale timeout)
Closed → Reopened (future: new linked fragment arrives)
```

---

# 11. Bitemporal Model

## 11.1 Purpose

Model both:
- **Valid time** — when something was true in the real world
- **System time** — when NCMS learned or revised it

## 11.2 Semantics

| Field | Meaning |
|-------|---------|
| `observed_at` | When the source says the event/state was observed |
| `ingested_at` | When NCMS stored the record |
| `valid_from` | Start of interval when state is believed to hold |
| `valid_to` | End of interval (NULL = still current) |
| `revision_reason` | Why the previous state changed |

## 11.3 Query modes enabled

- **Current state** — `WHERE is_current = 1`
- **State at time T** — `WHERE valid_from <= T AND (valid_to IS NULL OR valid_to > T)`
- **What changed since T** — `WHERE ingested_at > T AND node_type = 'entity_state'`
- **What was believed at time T** — `WHERE ingested_at <= T` (system-time query)

---

# 12. Hierarchical Consolidation

## 12.1 Consolidation levels

| Level | Input | Output |
|-------|-------|--------|
| **1 — Episode Summary** | Episode fragments + member state transitions | Coherent event narrative with actors, artifacts, decisions, outcome |
| **2 — State Trajectory** | Multiple state nodes for same entity/key over time | Temporal progression summary with major transitions |
| **3 — Recurring Pattern** | Multiple similar episodes or repeated state transitions | Generalized pattern abstraction |
| **4 — Strategic Insight** | Recurring patterns or repeated abstractions | Durable lesson, policy, invariant, or strategic model |

## 12.2 Consolidation triggers

- Episode closes → Level 1
- State transitions exceed threshold for entity/key → Level 2
- Similar episodes accumulate (≥3) → Level 3
- Retrieval repeatedly returns same cluster → Level 3
- Stable Level 3 patterns persist across time windows → Level 4

## 12.3 Consolidation outputs

Each consolidation creates:
- New `AbstractMemory` node with generated content
- `DERIVED_FROM` edges to source nodes
- Searchable summary indexed in Tantivy + SPLADE
- `refresh_due_at` if underlying nodes may change

---

# 13. Retrieval Design

## 13.1 Query intent classes

| Intent | Target Node Types | Signals |
|--------|-------------------|---------|
| `fact_lookup` | AtomicMemory, EntityState | Direct factual question |
| `current_state_lookup` | Current EntityState + supporting AtomicMemory | "What is X now?" |
| `historical_lookup` | Time-filtered EntityState, relevant Episodes | "What was X in January?" |
| `event_reconstruction` | Episode + member traces + state transitions | "What happened during..." |
| `change_detection` | EntityState chains (SUPERSEDES), Episodes | "What changed..." |
| `pattern_lookup` | AbstractMemory + supporting episodes | "Do we see a pattern..." |
| `strategic_reflection` | AbstractMemory (insights, lessons) | "What have we learned..." |

## 13.2 Intent classification (v1 — heuristic)

Rule-based classification using keyword patterns:
- "what is", "current", "status" → `current_state_lookup`
- "what was", "in [month]", "historically" → `historical_lookup`
- "what happened", "incident", "during" → `event_reconstruction`
- "what changed", "different", "compared to" → `change_detection`
- "pattern", "tend to", "usually" → `pattern_lookup`
- Default → `fact_lookup`

## 13.3 Ranking formula

```
FinalScore =
    a × BM25 +
    b × SPLADE +
    c × GraphExpansion +
    d × ACTRActivation +
    e × TemporalCompatibility +
    f × HierarchyMatch +
    g × EpisodeCompleteness −
    h × SupersessionPenalty −
    i × ConflictPenalty
```

Initial weights:
- `a=0.20, b=0.22, c=0.10, d=0.12, e=0.14, f=0.10, g=0.08, h=0.02, i=0.02`

## 13.4 Retrieval pipeline

```
Query → Intent Classifier → Candidate Retriever (type-filtered) →
    Temporal Filter → Hierarchy Reranker → ACT-R + Sparse Fusion →
    Optional LLM Judge → Final Context
```

---

# 14. Storage and Migration

## 14.1 New tables (additive, no existing table changes)

```sql
-- Typed memory nodes (extends memories table concept)
CREATE TABLE IF NOT EXISTS memory_nodes (
    id TEXT PRIMARY KEY,
    node_type TEXT NOT NULL DEFAULT 'atomic',  -- atomic, entity_state, episode, abstract
    content TEXT NOT NULL,
    summary TEXT,
    source_id TEXT,
    source_type TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    observed_at TEXT,
    ingested_at TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    importance REAL DEFAULT 5.0,
    domains TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]',
    metadata TEXT DEFAULT '{}',

    -- AtomicMemory fields
    memory_kind TEXT,
    legacy_type TEXT,
    admission_score REAL,
    novelty_score REAL,
    utility_score REAL,
    redundancy_score REAL,
    persistence_score REAL,
    state_change_signal REAL,
    episode_id TEXT,
    source_agent TEXT,
    project TEXT,

    -- EntityState fields
    entity_id TEXT,
    state_key TEXT,
    state_value TEXT,
    state_scope TEXT,
    valid_from TEXT,
    valid_to TEXT,
    is_current INTEGER,
    supersedes TEXT,
    superseded_by TEXT,
    revision_reason TEXT,

    -- Episode fields
    title TEXT,
    episode_type TEXT,
    status TEXT,
    start_time TEXT,
    end_time TEXT,
    actors TEXT DEFAULT '[]',
    artifacts TEXT DEFAULT '[]',
    outcome TEXT,
    cause_summary TEXT,
    member_memory_ids TEXT DEFAULT '[]',
    member_state_ids TEXT DEFAULT '[]',
    related_episode_ids TEXT DEFAULT '[]',

    -- AbstractMemory fields
    abstract_type TEXT,
    scope TEXT,
    derived_from_ids TEXT DEFAULT '[]',
    stability_score REAL,
    refresh_due_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_mn_node_type ON memory_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_mn_entity_id ON memory_nodes(entity_id);
CREATE INDEX IF NOT EXISTS idx_mn_state_key ON memory_nodes(entity_id, state_key);
CREATE INDEX IF NOT EXISTS idx_mn_is_current ON memory_nodes(is_current) WHERE node_type = 'entity_state';
CREATE INDEX IF NOT EXISTS idx_mn_episode_id ON memory_nodes(episode_id);
CREATE INDEX IF NOT EXISTS idx_mn_status ON memory_nodes(status) WHERE node_type = 'episode';
CREATE INDEX IF NOT EXISTS idx_mn_domains ON memory_nodes(domains);

-- Graph edges (supplements existing relationships table)
CREATE TABLE IF NOT EXISTS graph_edges (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ge_source ON graph_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_ge_target ON graph_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_ge_type ON graph_edges(edge_type);

-- Ephemeral cache (TTL-based, not indexed)
CREATE TABLE IF NOT EXISTS ephemeral_cache (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    features TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ec_expires ON ephemeral_cache(expires_at);
```

## 14.2 Migration strategy

- **Phase 1:** New tables are additive alongside existing tables
- **Phase 2–3:** New ingestion paths write to both `memories` (for backward compat) and `memory_nodes`
- **Phase 4+:** Retrieval reads from `memory_nodes` with fallback to `memories` for legacy data
- **Future:** Optional migration script to backfill existing `memories` into `memory_nodes` as `AtomicMemory`

---

# 15. Heuristic Design Details

## 15.1 Keyword lexicons

### Change markers
`changed`, `updated`, `switched`, `migrated`, `rolled back`, `fixed`, `deprecated`, `retired`, `released`, `promoted`, `renamed`

### Decision markers
`decided`, `approved`, `rejected`, `selected`, `adopted`, `chose`

### Incident markers
`incident`, `outage`, `error spike`, `failed`, `degraded`, `rollback`, `mitigation`, `root cause`

### Temporal markers
`today`, `yesterday`, `now`, `currently`, `since`, `before`, `after`, `effective`, `as of`

These feed Utility, Temporal Salience, and State Change Signal features.

## 15.2 Duplicate detection

Conservative threshold based on:
- Normalized exact match (lowercase, stripped)
- High BM25 similarity (>0.90) against top-1 result
- Same entities + same state slot + same value

If duplicate: discard, or add support count + update access frequency.

## 15.3 Reliability heuristics

- Structured/system source → `0.90`
- Agent inference with evidence → `0.70`
- Agent inference without evidence → `0.50`
- Speculative wording detected → `0.30`
- Repeated corroboration → `+0.10` per additional source

---

# 16. Implementation Phases

Each phase is independently testable, deployable, and measurable. Phases build on each other but do not break existing functionality.

---

## Phase 0: Pre-Work Cleanups

**Goal:** Fix existing implementation gaps that would impede HTMG development. These are mechanical fixes with no functional changes.

**Duration estimate:** 2-3 days

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 0.1 | **Protocol compliance** — Change `MemoryService` constructor to accept `MemoryStore`, `IndexEngine`, `GraphEngine` protocol types instead of concrete `SQLiteStore`, `TantivyEngine`, `NetworkXGraph` | `application/memory_service.py` | `uv run mypy src/` passes; all existing tests pass unchanged |
| 0.2 | **Protocol compliance** — Change `BusService` constructor to accept protocol types instead of concrete `AsyncKnowledgeBus` and `SnapshotService` | `application/bus_service.py` | `uv run mypy src/` passes |
| 0.3 | **Protocol compliance** — Change `ConsolidationService`, `GraphService` constructors to accept protocol types | `application/consolidation_service.py`, `application/graph_service.py` | `uv run mypy src/` passes |
| 0.4 | **Fix `memory_count()`** — Add `count_memories()` method to `MemoryStore` protocol and `SQLiteStore` using `SELECT COUNT(*)`, replace `len(list_memories(limit=100000))` | `domain/protocols.py`, `infrastructure/storage/sqlite_store.py`, `application/memory_service.py` | Unit test: count matches actual row count; no full table scan |
| 0.5 | **Fix `set_ask_handler` typing** — Change handler parameter from `object` to proper `Callable[[KnowledgeAsk], Awaitable[KnowledgeResponse | None]]` type | `domain/protocols.py`, `infrastructure/bus/async_bus.py` | `uv run mypy src/` passes with strict mode |
| 0.6 | **Split `MemoryStore` protocol** — Extract `EntityStore`, `SnapshotStore`, and `ConsolidationStore` as separate protocols; `MemoryStore` composes or extends them | `domain/protocols.py` | All existing tests pass; `uv run mypy src/` passes |
| 0.7 | **Add snapshot-by-domain query** — Add `get_snapshots_by_domain(domain)` to `SnapshotStore` protocol and `SQLiteStore`; fix the `pass` gap in `bus_service.py` surrogate discovery | `domain/protocols.py`, `infrastructure/storage/sqlite_store.py`, `application/bus_service.py` | Unit test: sleeping agent's snapshot found by domain; surrogate responds for deregistered agents |
| 0.8 | **Verify all existing tests pass** — Full regression run | All test files | `uv run pytest tests/ -v` — zero failures |

**Phase 0 exit criteria:**
- [x] All application services use Protocol types, not concrete implementations
- [x] `memory_count()` uses SQL COUNT
- [x] `MemoryStore` protocol split into cohesive sub-protocols
- [x] Surrogate discovery works for deregistered agents
- [x] `uv run mypy src/` and `uv run pytest tests/` both pass
- [x] Zero functional changes — all existing behavior preserved

---

## Phase 1: Typed Node Schema + Admission Scoring

**Goal:** Add typed memory nodes and admission scoring/routing without changing existing retrieval.

**Research question addressed:** Does selective admission improve retrieval quality?

### Phase 1A: Data Model Foundation (estimated: 3-4 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 1.A.1 | Add `MemoryNodeBase`, `AtomicMemory`, `EntityState`, `Episode`, `AbstractMemory` Pydantic models to `domain/models.py` | `domain/models.py` | `uv run pytest tests/unit/domain/` — all models construct, serialize, and validate |
| 1.A.2 | Add `AdmissionFeatures` dataclass and `score_admission()` / `route_memory()` pure functions to `domain/scoring.py` | `domain/scoring.py` | Unit tests: 8+ routing scenarios (discard, ephemeral, atomic, state_update, episode_fragment) with known feature vectors |
| 1.A.3 | Add `node_type` enum and edge type constants to `domain/models.py` | `domain/models.py` | Enum values match spec section 7 |
| 1.A.4 | Add `memory_nodes`, `graph_edges`, `ephemeral_cache` tables to migrations | `infrastructure/storage/migrations.py` | `SCHEMA_VERSION` bumps to 2; `run_migrations()` creates new tables without touching existing ones |
| 1.A.5 | Add CRUD methods for `memory_nodes` to `SQLiteStore` | `infrastructure/storage/sqlite_store.py` | Unit tests: save/get/list memory_nodes by type, filter by node_type |
| 1.A.6 | Add graph edge CRUD to `SQLiteStore` and `NetworkXGraph` | `infrastructure/storage/sqlite_store.py`, `infrastructure/graph/networkx_store.py` | Unit tests: create edges, query by type, traverse |
| 1.A.7 | Verify existing `memories` table and API still work unchanged | All existing tests | `uv run pytest tests/` — zero regressions |

**Phase 1A verification:** All existing tests pass. New models serialize/deserialize. New tables exist. Schema migration runs idempotently. ✅ Complete — 54 new tests (15 model + 18 scoring + 21 CRUD/migration).

### Phase 1B: Admission Feature Extraction (estimated: 4-5 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 1.B.1 | Create `application/admission_service.py` with `AdmissionService` class | New file | Skeleton with constructor taking `MemoryStore`, `IndexEngine`, `GraphEngine` |
| 1.B.2 | Implement `_compute_novelty()` — BM25 top-3 lookup, max similarity inversion | `application/admission_service.py` | Unit test: known content → known novelty score; duplicate → ~0.0; unique → ~1.0 |
| 1.B.3 | Implement `_compute_utility()` — keyword lexicon matching against change/decision/incident markers | `application/admission_service.py` | Unit test: "API was rolled back" → high utility; "hello world" → low utility |
| 1.B.4 | Implement `_compute_reliability()` — source type + wording heuristics | `application/admission_service.py` | Unit test: source_type="system" → 0.90; speculative wording → 0.30 |
| 1.B.5 | Implement `_compute_temporal_salience()` — date/timestamp detection + temporal marker lexicon | `application/admission_service.py` | Unit test: "deployed as of 2026-03-01" → high; "some general info" → low |
| 1.B.6 | Implement `_compute_persistence()` — policy/decision/architecture detection | `application/admission_service.py` | Unit test: "architectural decision: use PostgreSQL" → high persistence |
| 1.B.7 | Implement `_compute_redundancy()` — BM25 similarity overlap with existing memories | `application/admission_service.py` | Unit test: identical content already stored → high redundancy |
| 1.B.8 | Implement `_compute_state_change_signal()` — entity+key match with different value | `application/admission_service.py` | Unit test: existing state "status=deployed", new "status=rolled_back" → high signal |
| 1.B.9 | Implement `_compute_episode_affinity()` — open episode entity/temporal overlap | `application/admission_service.py` | Unit test: matching open episode entities → high affinity |
| 1.B.10 | Wire all features into `compute_features()` → `score_admission()` → `route_memory()` | `application/admission_service.py` | Integration test: 10 diverse inputs → correct routing categories |

**Phase 1B verification:** `uv run pytest tests/unit/application/test_admission_service.py` — all 10+ scenarios pass. Feature computation is deterministic and traceable. ✅ Complete — 28 tests covering all 8 features + full pipeline. BM25 sigmoid normalization (divisor=2.0) maps Tantivy scores to [0,1].

### Phase 1C: Admission Integration (estimated: 2-3 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 1.C.1 | Add `NCMS_ADMISSION_ENABLED` config toggle (default `false`) | `config.py` | Feature flag defaults off; existing behavior unchanged |
| 1.C.2 | Integrate `AdmissionService` into `MemoryService.store_memory()` as optional pre-step | `application/memory_service.py` | When enabled: admission features computed before storage; when disabled: bypass |
| 1.C.3 | Store admission features as metadata on the memory record | `application/memory_service.py` | Stored memories have `admission_score`, `novelty_score`, etc. queryable |
| 1.C.4 | Write to `memory_nodes` table in parallel with `memories` table | `application/memory_service.py` | Both tables populated; existing retrieval from `memories` unchanged |
| 1.C.5 | Add `discard` routing — when admission says discard, skip storage entirely | `application/memory_service.py` | Discarded memories not in `memories` or `memory_nodes` tables |
| 1.C.6 | Add `ephemeral_cache` routing — TTL-based storage in ephemeral table | `application/memory_service.py` | Ephemeral entries auto-expire; not indexed in Tantivy |
| 1.C.7 | Add admission metrics to observability event log | `infrastructure/observability/event_log.py` | Dashboard shows admission scores and routing decisions |
| 1.C.8 | Run BEIR ablation: B0 (current) vs B1 (admission enabled) | Benchmark scripts | nDCG@10 preserved or improved on SciFact |

**Phase 1C verification:** `uv run pytest tests/` — zero regressions. Admission can be toggled. Ablation shows retrieval quality maintained. ✅ Complete — 8 integration tests (disabled/enabled/metadata). Admission wired into store_memory() with graceful degradation on failure.

**Phase 1 exit criteria:**
- [x] All existing tests pass (402 passed, 7 skipped — zero regressions)
- [x] New models, tables, and admission service fully tested (62 new tests across 5 files)
- [x] Feature flag allows clean toggle (`NCMS_ADMISSION_ENABLED=false` default, zero impact when off)
- [ ] BEIR ablation shows no regression with admission enabled (deferred — requires benchmark run)
- [x] Dashboard shows admission routing decisions (`admission_scored` event in EventLog)

**Phase 1 completed:** 2026-03-13 (commit `5081227`). Implementation notes:
- Schema V2 adds `memory_nodes`, `graph_edges`, `ephemeral_cache` tables (incremental migration from V1)
- 8 heuristic feature extractors: novelty (BM25 sigmoid), utility (keyword lexicon), reliability (source type + hedging), temporal salience (date regex), persistence (durability markers), redundancy (BM25 overlap), state change signal (mutation indicators), episode affinity (stub → 0.0 until Phase 2)
- Routing: score < 0.25 → discard, 0.25-0.45 → ephemeral, ≥ 0.45 → atomic; state_change ≥ 0.50 → entity_state_update; episode_affinity ≥ 0.55 → episode_fragment
- Remaining: BEIR ablation comparison (B0 vs B1) deferred to benchmark pass

---

## Phase 2: State Reconciliation and Supersession

**Goal:** Replace simple contradiction detection with a truth-maintenance subsystem using typed edges.

**Research question addressed:** Does bitemporal state tracking reduce stale-fact leakage?

### Phase 2A: State Reconciliation Engine (estimated: 4-5 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 2.A.1 | Create `application/reconciliation_service.py` with `ReconciliationService` class | New file | Constructor takes `MemoryStore`, `IndexEngine` |
| 2.A.2 | Implement `_find_related_states()` — retrieve existing states for same entity/key | `application/reconciliation_service.py` | Unit test: finds matching entity states by entity_id + state_key |
| 2.A.3 | Implement `classify_relation()` — heuristic classification (supports/refines/supersedes/conflicts/unrelated) | `application/reconciliation_service.py` | Unit tests: 10+ scenarios covering all 5 relation types |
| 2.A.4 | Implement `_apply_supports()` — create SUPPORTS edge, boost importance | `application/reconciliation_service.py` | Both nodes' importance increased; edge exists in graph |
| 2.A.5 | Implement `_apply_refines()` — create REFINES edge, link detail to broader claim | `application/reconciliation_service.py` | Edge created; source remains valid |
| 2.A.6 | Implement `_apply_supersedes()` — close prior valid_to, create SUPERSEDES/SUPERSEDED_BY edges, flip is_current | `application/reconciliation_service.py` | Prior state: is_current=false, valid_to set; new state: is_current=true, valid_from set |
| 2.A.7 | Implement `_apply_conflicts()` — create CONFLICTS_WITH edge, emit conflict event | `application/reconciliation_service.py` | Bidirectional edge created; event logged |
| 2.A.8 | Wire reconciliation into admission routing — when `route == "entity_state_update"`, run reconciliation | `application/admission_service.py`, `application/memory_service.py` | State updates go through reconciliation pipeline |

**Phase 2A verification:** `uv run pytest tests/unit/application/test_reconciliation_service.py` — all relation classification and application scenarios pass.

### Phase 2B: Bitemporal Fields and Historical Queries (estimated: 3-4 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 2.B.1 | Add `valid_from`, `valid_to`, `observed_at`, `ingested_at` to entity state storage | `infrastructure/storage/sqlite_store.py` | Entity states stored with temporal fields |
| 2.B.2 | Implement `get_current_state(entity_id, state_key)` → most recent is_current=true | `infrastructure/storage/sqlite_store.py` | Returns current state for entity |
| 2.B.3 | Implement `get_state_at_time(entity_id, state_key, timestamp)` → state valid at T | `infrastructure/storage/sqlite_store.py` | Returns historically valid state |
| 2.B.4 | Implement `get_state_changes_since(timestamp)` → all state transitions after T | `infrastructure/storage/sqlite_store.py` | Returns ordered list of state changes |
| 2.B.5 | Implement `get_state_history(entity_id, state_key)` → full temporal chain | `infrastructure/storage/sqlite_store.py` | Returns all states ordered by valid_from, linked by supersession |
| 2.B.6 | Add temporal query support to retrieval pipeline — filter candidates by temporal compatibility | `application/memory_service.py` | Historical queries only return temporally valid results |
| 2.B.7 | Add `TemporalCompatibility` scoring factor to ranking formula | `domain/scoring.py` | Memories with matching temporal scope score higher |

**Phase 2B verification:** Integration test: store 3 versions of an entity state over time → query at each point returns correct version. Historical retrieval returns only valid-at-time results.

### Phase 2C: Supersession in Retrieval (estimated: 2-3 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 2.C.1 | Add `SupersessionPenalty` to ranking — superseded states get score penalty | `application/memory_service.py` | Superseded states ranked lower than current states |
| 2.C.2 | Add `ConflictPenalty` to ranking — conflicting states flagged | `application/memory_service.py` | Conflicting states annotated in results |
| 2.C.3 | Add `NCMS_RECONCILIATION_ENABLED` config toggle | `config.py` | Default false; existing behavior preserved |
| 2.C.4 | Migrate existing contradiction detection to use reconciliation service | `application/memory_service.py` | Old contradiction detector deprecated but still callable |
| 2.C.5 | Add reconciliation events to observability log | `infrastructure/observability/event_log.py` | Dashboard shows supersession/conflict events |
| 2.C.6 | Run BEIR ablation: B1 vs B2 (admission + reconciliation) | Benchmark scripts | Measure stale-fact leakage reduction |

**Phase 2 exit criteria:**
- [x] Entity state supersession chain works end-to-end
- [x] Bitemporal queries return correct results at any point in time
- [x] Superseded states penalized in retrieval ranking
- [x] Conflict detection replaces simple contradiction detection
- [ ] BEIR ablation shows no regression, ideally reduced stale recall (deferred — requires benchmark run)

**Phase 2 completed:** 2026-03-13 (commit `ceb0637`). Implementation notes:
- ReconciliationService with 5 heuristic classifiers (supports/refines/supersedes/conflicts/unrelated) using entity_id + state_key + state_value + state_scope comparison — pure heuristic, no LLM
- Entity state metadata stored in MemoryNode.metadata dict with json_extract() queries; EntityStateMeta Pydantic helper for typed extraction
- Bidirectional typed edges: SUPERSEDES/SUPERSEDED_BY for supersession chains, CONFLICTS_WITH for parallel truths (different scopes)
- Schema V3 adds bitemporal columns (observed_at, ingested_at) via ALTER TABLE; 4 temporal query methods (current state, state at time, changes since, full history)
- Supersession penalty (0.3) and conflict annotation penalty (0.15) feed into ACT-R mismatch_penalty parameter; ScoredMemory annotated with is_superseded, has_conflicts, superseded_by
- MCP search_memory tool includes supersession/conflict annotations in results
- Feature-flagged: NCMS_RECONCILIATION_ENABLED=false default, requires admission to be on (reconciliation fires only for entity_state_update routed content)
- 85 new tests (487 total), zero new mypy errors
- Remaining: BEIR ablation comparison (B1 vs B2) deferred to benchmark pass

---

## Phase 3: Episode Formation — Hybrid Episode Linker

**Goal:** Automatically group related fragments into bounded episodes using incremental multi-signal matching. Works across all content domains (software, scientific, general prose) without requiring LLM at runtime.

**Research question addressed:** Does episodic grouping improve event reconstruction queries?

**Approach:** Each episode maintains a compact profile (entities + domains + anchors) indexed in both BM25 (Tantivy) and SPLADE. New fragments are scored against candidate episodes using 7 weighted signals. Weights auto-redistribute when SPLADE is disabled.

### Episode Linking Flow

```
Fragment arrives (entities already extracted by GLiNER)
    │
    ├─ 1. CANDIDATE GENERATION (cheap, parallel)
    │     ├─ BM25 search fragment content → filter to episode memory IDs
    │     ├─ SPLADE search fragment content → filter to episode memory IDs (if enabled)
    │     └─ Entity overlap scan against open episodes
    │
    ├─ 2. MULTI-SIGNAL SCORING (per candidate)
    │     ├─ BM25 score (normalized via sigmoid)
    │     ├─ SPLADE score (normalized, 0 if disabled)
    │     ├─ Entity overlap coefficient: |shared| / min(|frag|, |ep|)
    │     ├─ Domain overlap coefficient
    │     ├─ Temporal proximity (linear decay within window)
    │     ├─ Source agent match (1.0 or 0.0)
    │     └─ Structured anchor match (bonus, 1.0 if same ID)
    │
    └─ 3. DECISION
          ├─ Best score ≥ threshold → assign to that episode, update profile
          ├─ No match + ≥ min_entities → create new episode
          └─ Otherwise → no episode (fragment stays unattached)
```

### Signal Weights

| Signal | Weight (with SPLADE) | Weight (without SPLADE) | Role |
|--------|---------------------|------------------------|------|
| BM25 score | 0.20 | 0.25 | Lexical overlap with episode profile |
| SPLADE score | 0.20 | — | Semantic overlap with episode profile |
| Entity overlap | 0.25 | 0.3125 | Topic structure from GLiNER |
| Domain overlap | 0.15 | 0.1875 | Same knowledge domain |
| Temporal proximity | 0.10 | 0.125 | Recent activity |
| Source agent | 0.05 | 0.0625 | Same producing agent |
| Structured anchor | 0.05 | 0.0625 | Bonus for explicit IDs |

### Phase 3A: Hybrid Episode Linker (implemented)

| # | Task | Files | Status |
|---|------|-------|--------|
| 3.A.1 | `EpisodeService` with BM25/SPLADE candidate generation | `application/episode_service.py` | Done |
| 3.A.2 | Weighted multi-signal scoring (7 signals, auto-redistribution) | `application/episode_service.py` | Done |
| 3.A.3 | Episode profile enrichment on member join (re-index in BM25/SPLADE) | `application/episode_service.py` | Done |
| 3.A.4 | Structured anchor detection as bonus signal (not gate) | `application/episode_service.py` | Done |
| 3.A.5 | `EpisodeMeta.topic_entities` for entity-based episode tracking | `domain/models.py` | Done |
| 3.A.6 | Episode closure (stale timeout + resolution markers) | `application/episode_service.py` | Done |
| 3.A.7 | Episode config: weights, thresholds, candidate limits | `config.py` | Done |
| 3.A.8 | `BELONGS_TO_EPISODE` graph edges on assignment | `application/episode_service.py` | Done |

**Phase 3A verification:** `uv run pytest tests/unit/application/test_episode_service.py -v` — 29 tests pass (entity overlap, BM25 candidates, scoring, profile enrichment, closure, meta round-trip).

### Phase 3B: Episode Integration (implemented)

| # | Task | Files | Status |
|---|------|-------|--------|
| 3.B.1 | Decouple episode formation from admission routing (runs for all stored content) | `application/memory_service.py` | Done |
| 3.B.2 | Domain-agnostic admission heuristics (no regex gate) | `application/admission_service.py` | Done |
| 3.B.3 | Episode profile indexed in Tantivy (BM25-searchable) | `application/episode_service.py` | Done |
| 3.B.4 | SPLADE integration for candidate generation + profile indexing | `application/episode_service.py` | Done |
| 3.B.5 | `NCMS_EPISODES_ENABLED` config toggle (default false) | `config.py` | Done |
| 3.B.6 | Episode events in observability log with match_score | `infrastructure/observability/event_log.py` | Done |
| 3.B.7 | Pass `splade` to EpisodeService in composition roots | `interfaces/mcp/server.py`, `interfaces/http/dashboard.py` | Done |
| 3.B.8 | Cross-domain integration tests (scientific, ticket, prose, isolation) | `tests/integration/test_episode_pipeline.py` | Done |

**Phase 3 exit criteria:**
- [x] Episodes auto-created from entity clusters (no regex dependency)
- [x] Fragments correctly assigned via weighted multi-signal scoring
- [x] Episode closure works with timeout and explicit resolution markers
- [x] Episode profiles searchable via BM25
- [x] Cross-domain: scientific, ticket, and prose fragments form correct episodes
- [x] Different entity clusters create separate episodes
- [ ] Event reconstruction queries return episode + members (Phase 4+)
- [ ] BEIR ablation shows no regression (Phase 7)

---

## Phase 4: Intent-Aware Retrieval ✅

**Goal:** Classify query intent and boost appropriate node types for more precise retrieval.

**Research question addressed:** Does intent-aware scoring outperform uniform scoring?

**Approach:** BM25 exemplar index in infrastructure layer (`infrastructure/indexing/exemplar_intent_index.py`). ~70 exemplar queries indexed in a small in-memory Tantivy index. At query time, user's query is matched against exemplars via BM25; scores aggregated per intent class, highest wins. Keyword fallback classifier (`domain/intent.py:classify_intent`) used when index unavailable. 7 intent classes, each mapping to preferred node types. Additive hierarchy bonus (not hard filter). Two-toggle safety: classification enabled separately from scoring weight. Batch node preload eliminates N+1 queries. Supplementary candidates injected from specialized stores based on intent. IntentClassifier protocol in domain layer allows swapping implementations.

### Phase 4A: Intent Classifier

| # | Task | Files | Status |
|---|------|-------|--------|
| 4.A.1 | Create `domain/intent.py` with `QueryIntent` enum (7 classes) + `IntentResult` dataclass | `domain/intent.py` | ✅ Done |
| 4.A.2 | Implement keyword pattern matching as fallback classifier | `domain/intent.py` | ✅ Done |
| 4.A.3 | Add `INTENT_EXEMPLARS` — 10-15 example queries per intent class (BM25 training data) | `domain/intent.py` | ✅ Done |
| 4.A.4 | Create `ExemplarIntentIndex` — in-memory Tantivy BM25 index of exemplars | `infrastructure/indexing/exemplar_intent_index.py` | ✅ Done |
| 4.A.5 | Add `IntentClassifier` protocol + wire into MemoryService | `domain/protocols.py`, `application/memory_service.py` | ✅ Done |
| 4.A.6 | Add 5 config params and wire exemplar index in composition root | `config.py`, `interfaces/mcp/server.py` | ✅ Done |

**Phase 4A verification:** `uv run pytest tests/unit/domain/test_intent.py tests/unit/infrastructure/test_exemplar_intent_index.py` — 60 tests, 100% pass. Covers BM25 exemplar classification, keyword fallback, paraphrase handling, all 7 intent classes, confidence levels, exemplar data quality.

### Phase 4B: Type-Boosted Retrieval

| # | Task | Files | Status |
|---|------|-------|--------|
| 4.B.1 | Implement `hierarchy_match_bonus()` — additive bonus when candidate node type matches intent targets | `domain/scoring.py` | ✅ Done |
| 4.B.2 | Extend `ScoredMemory` with `node_types`, `intent`, `hierarchy_bonus` fields | `domain/models.py` | ✅ Done |
| 4.B.3 | Add batch node preload — single SQL query for all candidate memory IDs | `domain/protocols.py`, `infrastructure/storage/sqlite_store.py` | ✅ Done |
| 4.B.4 | Integrate intent classification into search pipeline (before BM25 stage) | `application/memory_service.py` | ✅ Done |
| 4.B.5 | Implement `_intent_supplement()` — inject candidates from entity states, episode members, state history based on intent | `application/memory_service.py` | ✅ Done |
| 4.B.6 | Add hierarchy bonus to scoring loop with configurable weight (`scoring_weight_hierarchy`) | `application/memory_service.py` | ✅ Done |
| 4.B.7 | Expose `node_types`, `intent`, `hierarchy_bonus` in MCP search_memory tool | `interfaces/mcp/tools.py` | ✅ Done |

**Phase 4 exit criteria:**
- [x] BM25 exemplar index classifies 7 intent classes (~70 exemplar queries)
- [x] Keyword fallback classifier retained for when no index available
- [x] IntentClassifier protocol enables swapping implementations
- [x] Hierarchy bonus boosts matching node types without filtering others
- [x] Two-toggle safety: classification and scoring weight independently configurable
- [x] Batch node preload eliminates N+1 queries
- [x] Supplementary candidates injected for state/episode/change/historical intents
- [x] 98 new tests (35 keyword + 25 exemplar + 7 scoring + 5 batch query + 9 integration + 9 intent LLM + 8 episode LLM)
- [ ] BEIR ablation: B3 vs B4 (pending ablation run)

### LLM Fallback for Tuning (Phase 3 + Phase 4)

Both the episode linker (Phase 3) and intent classifier (Phase 4) support optional LLM fallback for edge cases where heuristic scoring produces low-confidence results. The LLM answers serve dual purpose: immediate classification/linking and training data for tuning BM25 exemplars and episode weights.

**Intent LLM fallback** (`NCMS_INTENT_LLM_FALLBACK_ENABLED`):
- Triggered when BM25 exemplar confidence < `intent_confidence_threshold` (0.6)
- Calls LLM with query + 7 intent definitions → returns intent + confidence
- On failure → gracefully degrades to `fact_lookup`
- Emits `intent_miss` event with query text for exemplar tuning
- Implementation: `infrastructure/llm/intent_classifier_llm.py`

**Episode LLM fallback** (`NCMS_EPISODE_LLM_FALLBACK_ENABLED`):
- Triggered when no episode scores above `episode_match_threshold` (0.30)
- Calls LLM with fragment + top-5 episode summaries → returns episode matches
- On failure → gracefully degrades (creates new episode or returns None)
- Implementation: `infrastructure/llm/episode_linker_llm.py`

**Shared properties:** Non-fatal (try/catch all), feature-flagged off by default, uses shared `llm_model` + `llm_api_base` config, follows contradiction detector pattern.

---

## Phase 5: Hierarchical Consolidation ✅ COMPLETE

**Goal:** Generate reusable higher-order knowledge from lower-level traces.

**Research question addressed:** Does hierarchical consolidation produce useful abstractions?

**Implementation notes:** All three sub-phases implemented as batch consolidation passes in `ConsolidationService`. LLM synthesis extracted to `infrastructure/consolidation/abstract_synthesizer.py` with three dedicated functions. Dual storage pattern: each abstract creates both `Memory(type="insight")` for Tantivy/SPLADE indexing and `MemoryNode(node_type=ABSTRACT)` for HTMG hierarchy. Stability-based promotion formula: `min(1.0, cluster_size/5) * confidence`. Staleness tracking via `refresh_due_at` metadata. Union-find clustering with Jaccard overlap for episode pattern detection. 9 new config params, 3 new protocol methods, 3 new SQLite queries, 45 new tests (669 total).

### Phase 5A: Episode Summary Consolidation ✅

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 5.A.1 | ✅ Extend `ConsolidationService` with episode summary generation | `application/consolidation_service.py` | Closed episodes trigger summary generation |
| 5.A.2 | ✅ Implement episode summary prompt — LLM generates narrative from member fragments | `infrastructure/consolidation/abstract_synthesizer.py` | Summary covers actors, artifacts, decisions, outcome |
| 5.A.3 | ✅ Store summary as `AbstractMemory` with `abstract_type='episode_summary'` | `application/consolidation_service.py` | Summary node linked to episode via `SUMMARIZES` + `DERIVED_FROM` |
| 5.A.4 | ✅ Index summary in Tantivy + SPLADE for retrieval | `application/consolidation_service.py` | Summaries appear in search results |
| 5.A.5 | ✅ Add `NCMS_EPISODE_CONSOLIDATION_ENABLED` config toggle | `config.py` | Default false |

### Phase 5B: State Trajectory Consolidation ✅

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 5.B.1 | ✅ Implement state trajectory detection — find entities with ≥N state transitions | `application/consolidation_service.py` | SQL aggregation via `get_entities_with_state_count()` |
| 5.B.2 | ✅ Implement trajectory summary — LLM generates temporal progression narrative | `infrastructure/consolidation/abstract_synthesizer.py` | Summary covers major transitions and trend |
| 5.B.3 | ✅ Store trajectory as `AbstractMemory` with `abstract_type='state_trajectory'` | `application/consolidation_service.py` | Trajectory linked to component states via `DERIVED_FROM` |

### Phase 5C: Pattern and Insight Consolidation ✅

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 5.C.1 | ✅ Implement similar episode clustering — Jaccard overlap on topic_entities | `application/consolidation_service.py` | Union-find clustering with configurable threshold |
| 5.C.2 | ✅ Implement recurring pattern detection — LLM generates generalized pattern | `infrastructure/consolidation/abstract_synthesizer.py` | Pattern describes what recurs and why |
| 5.C.3 | ✅ Store pattern as `AbstractMemory` with `abstract_type='recurring_pattern'` | `application/consolidation_service.py` | Pattern linked to source episodes via `DERIVED_FROM` |
| 5.C.4 | ✅ Implement strategic insight promotion — stable patterns promoted above threshold | `application/consolidation_service.py` | `stability_score > 0.7` → `strategic_insight` |
| 5.C.5 | ✅ Implement `refresh_due_at` tracking — flag abstractions for refresh | `application/consolidation_service.py` | Stale abstractions detected via metadata check |
| 5.C.6 | Run ablation: retrieval with vs without consolidated abstractions | Benchmark scripts | Deferred to benchmarking phase |

**Phase 5 exit criteria:**
- [x] Episode summaries generated from closed episodes
- [x] State trajectories generated for entities with rich histories
- [x] Recurring patterns detected from similar episode clusters
- [x] Abstractions indexed and retrievable
- [x] Stale abstractions flagged for refresh
- [x] Consolidation is LLM-optional (degrades gracefully without LLM)

---

## Phase 6: MCP Tools + Dashboard + Demo

**Goal:** Expose NCMS-Next capabilities through MCP tools, update dashboard, and create a comprehensive demo.

### Phase 6A: MCP Tool Updates ✅ (completed)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 6.A.1 | ✅ Enhanced `store_memory` with `show_admission` parameter | `interfaces/mcp/tools.py` | Admission data included when `show_admission=True` |
| 6.A.2 | ✅ Add `get_current_state` MCP tool — entity state lookup | `interfaces/mcp/tools.py` | Returns current state for entity/key |
| 6.A.3 | ✅ Add `get_state_history` MCP tool — temporal state chain | `interfaces/mcp/tools.py` | Returns ordered state history with transitions |
| 6.A.4 | ✅ Add `list_episodes` MCP tool — open/closed episodes | `interfaces/mcp/tools.py` | Returns episodes with member counts |
| 6.A.5 | ✅ Add `get_episode` MCP tool — episode with all members | `interfaces/mcp/tools.py` | Returns episode + member fragments + state transitions |
| 6.A.6 | ✅ Update `search_memory` with `intent` override parameter | `interfaces/mcp/tools.py`, `application/memory_service.py` | Optional `intent` parameter bypasses auto-classifier |
| 6.A.7 | ✅ Add `ncms://entities/{id}/state` resource | `interfaces/mcp/resources.py` | Resource returns current state and recent history |

**Phase 6A verification:** All new MCP tools callable from Claude Code. Existing tools unchanged. 14 tools without consolidation, 15 with.

### Phase 6B: Dashboard + Demo Updates (partial ✅)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 6.B.1 | ⏳ Update dashboard to show memory node types with color coding | `interfaces/http/static/index.html` | Atomic=blue, EntityState=green, Episode=orange, Abstract=purple |
| 6.B.2 | ⏳ Add episode timeline view to dashboard | `interfaces/http/static/index.html` | Episodes shown on timeline with member fragments |
| 6.B.3 | ⏳ Add state history view to dashboard | `interfaces/http/static/index.html` | Entity state transitions shown as timeline |
| 6.B.4 | ⏳ Add admission scoring panel to dashboard | `interfaces/http/static/index.html` | Real-time admission scores and routing visible |
| 6.B.5 | ✅ Enhanced demo with intent-aware search phase | `demo/run_demo.py`, `interfaces/http/demo_runner.py` | Demo shows intent override search with hierarchy bonus |
| 6.B.6 | ✅ Add CLI `ncms state`, `ncms episodes` subcommands | `interfaces/cli/main.py` | CLI access to state and episode data |

**Dashboard REST endpoints added** (supporting future SPA work): `GET /api/episodes`, `GET /api/episodes/{id}`, `GET /api/entity-states/{id}`, `GET /api/entity-states/{id}/history`.

**Phase 6 exit criteria:**
- [x] All MCP tools working and tested
- [ ] Dashboard shows new node types and relationships (deferred: 6B.1-6B.4)
- [x] Demo scenario exercises intent-aware search
- [x] CLI provides state and episode access

---

## Phase 7: Full Pipeline Tuning + Evaluation

**Goal:** Tune the complete system, run comprehensive ablation, and validate research claims.

### Phase 7A: Weight Tuning (estimated: 3-4 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 7.A.1 | Create evaluation dataset with intent-labeled queries (fact, state, historical, event, change, pattern) | `benchmarks/` | ≥100 queries with ground-truth intents and expected results |
| 7.A.2 | Grid search admission weights on evaluation dataset | `benchmarks/` | Find weights that maximize retrieval quality while minimizing memory growth |
| 7.A.3 | Grid search ranking weights on evaluation dataset | `benchmarks/` | Find optimal balance of BM25/SPLADE/Graph/ACT-R/Temporal/Hierarchy |
| 7.A.4 | Tune episode formation thresholds | `benchmarks/` | Episode precision (% correct assignments) ≥ 80% |
| 7.A.5 | Tune reconciliation heuristics | `benchmarks/` | Supersession accuracy ≥ 85% on labeled examples |

### Phase 7B: Full Ablation (estimated: 2-3 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 7.B.1 | Run staged ablation: B0 → B1 → B2 → B3 → B4 → B5 → B6 → Best | `benchmarks/` | Each stage measured independently on BEIR |
| 7.B.2 | Measure memory-quality metrics (duplicate rate, stale-fact leakage, superseded leakage, contradiction leakage, memory growth efficiency) | `benchmarks/` | Quality metrics computed and reported |
| 7.B.3 | Measure temporal metrics (current-state accuracy, historical accuracy, change-detection accuracy) | `benchmarks/` | Temporal retrieval outperforms flat retrieval |
| 7.B.4 | Measure episodic metrics (event reconstruction accuracy, episode completeness, causal chain coverage) | `benchmarks/` | Episode-based retrieval outperforms fragment-based |
| 7.B.5 | Measure cost metrics (ingest latency, retrieval latency, LLM usage per 1000 memories) | `benchmarks/` | Latency within acceptable bounds |
| 7.B.6 | Update paper, README, and documentation with results | `docs/`, `README.md` | Results published with methodology |

**Phase 7 exit criteria:**
- [ ] Full ablation ladder completed
- [ ] Each component's contribution quantified
- [ ] Memory quality metrics computed
- [ ] Latency acceptable (<500ms retrieval p95)
- [ ] Results documented and reproducible

---

# 16B. Phase 8 — Project Oracle: Dream Cycle & Adaptive Scoring

**Codename:** Project Oracle
**Goal:** Replace LLM-as-judge reranking and LLM keyword bridges with a zero-LLM offline consolidation process that teaches ACT-R to produce better rankings through access pattern analysis and learned association strengths. This eliminates query-time LLM cost while producing compounding improvements.

**Motivation:** The ablation study revealed two key findings:
1. **Keyword bridges catastrophically fail** (nDCG@10 drops 95%) — generic LLM-extracted keywords flood graph expansion
2. **ACT-R underperforms on static benchmarks** (Full Pipeline 0.690 vs SPLADE+Graph 0.698) — uniform association strengths and no real access history handicap cognitive scoring

Project Oracle addresses both by teaching ACT-R through its own mechanism: access patterns and association weights. Like dream consolidation in cognitive science, an offline nightly process replays, rehearses, and reorganizes memory traces — no LLM required for the core loop.

### Cognitive Science Basis

In ACT-R theory, dream consolidation is modeled as offline rehearsal — the hippocampus replays important memory traces during slow-wave sleep, which effectively adds new access events and strengthens inter-chunk associations. Anderson (2007) shows that spaced rehearsal (distributed practice) produces optimal long-term retention. Project Oracle implements this directly:

- **Dream rehearsal** → synthetic access injection for decaying-but-important memories
- **Dream association** → PMI-based entity association strength learning from co-access patterns
- **Dream triage** → importance score adjustment based on access trend analysis

### Why ACT-R Fails on Static IR Benchmarks

Phase 7 tuning confirmed ACT-R weight ≤0.0 is optimal on BEIR (SciFact nDCG@10: 0.7053 without ACT-R vs 0.6903 with ACT-R=0.4). This is **expected** — BEIR is a single-shot benchmark where every document has identical access history (1 access, same timestamp). ACT-R's `ln(Σ(t^-d))` formula produces uniform scores across all candidates, contributing only noise.

ACT-R requires **differential access patterns** — some memories accessed more recently/frequently than others — to provide signal. These patterns emerge naturally from:
1. Agent work sessions (repeated queries about active topics)
2. Dream rehearsal (important memories replayed during sleep)
3. Consolidation (insights re-access their source memories)

### Dream Cycle Architecture

```
Agent Lifecycle with Dream Cycle
═════════════════════════════════

  ┌─────────────────────────────────────────────────┐
  │  WAKE PHASE (normal operation)                  │
  │                                                 │
  │  store() → creates access_log entry (t₁)        │
  │  search() → creates access_log entries (t₂..tₙ) │
  │  ask() → routes through bus, may access memories │
  │                                                 │
  │  Result: differential access patterns emerge     │
  │  - Working knowledge: 5-20 accesses/day         │
  │  - Background facts: 1 access (ingest only)     │
  │  - Stale knowledge: last access days ago        │
  └──────────────────────┬──────────────────────────┘
                         │ agent.sleep()
                         ▼
  ┌─────────────────────────────────────────────────┐
  │  DREAM PHASE 1: Rehearsal Selection             │
  │                                                 │
  │  Score all memories for rehearsal priority:      │
  │                                                 │
  │  rehearsal_priority(m) =                        │
  │    w₁ · graph_centrality(m)     [importance]    │
  │    + w₂ · access_trend(m)       [momentum]      │
  │    + w₃ · episode_membership(m) [context]       │
  │    + w₄ · entity_fan_out(m)     [connectivity]  │
  │    − w₅ · recency(m)           [already fresh]  │
  │                                                 │
  │  Select top-K for rehearsal (K ≈ 30% of active) │
  │  Cap: max 1 synthetic access per memory/night   │
  └──────────────────────┬──────────────────────────┘
                         │
                         ▼
  ┌─────────────────────────────────────────────────┐
  │  DREAM PHASE 2: Replay Execution               │
  │                                                 │
  │  For each selected memory:                      │
  │    record_access(memory_id, source="dream")     │
  │    → inserts access_log row with current time   │
  │    → ACT-R base-level recomputes on next query  │
  │                                                 │
  │  For episode members (contextual replay):       │
  │    replay all members of active episodes        │
  │    → strengthens episode coherence              │
  │                                                 │
  │  For conflicting states:                        │
  │    replay current state (NOT superseded)         │
  │    → widens activation gap between old/new      │
  └──────────────────────┬──────────────────────────┘
                         │
                         ▼
  ┌─────────────────────────────────────────────────┐
  │  DREAM PHASE 3: Association Learning            │
  │                                                 │
  │  Compute PMI from co-access patterns:           │
  │    PMI(e₁, e₂) = log(P(e₁,e₂) / P(e₁)·P(e₂)) │
  │                                                 │
  │  Where co-access = entities appearing in        │
  │  memories returned by the same search query     │
  │                                                 │
  │  Store in association_strengths table            │
  │  → feeds spreading_activation() at query time   │
  └──────────────────────┬──────────────────────────┘
                         │
                         ▼
  ┌─────────────────────────────────────────────────┐
  │  DREAM PHASE 4: Importance Drift                │
  │                                                 │
  │  Analyze 30-day access trends per memory:       │
  │    trend = linear_regression(access_counts/week) │
  │                                                 │
  │  Rising trend → boost importance                │
  │  Declining trend → reduce importance            │
  │  No accesses → natural ACT-R decay handles it   │
  └──────────────────────┬──────────────────────────┘
                         │ agent.wake()
                         ▼
  ┌─────────────────────────────────────────────────┐
  │  POST-DREAM QUERY                               │
  │                                                 │
  │  ACT-R now has differential signal:             │
  │  - Rehearsed: 3+ accesses (store + use + dream) │
  │  - Unrehearsed: 1 access (store only)           │
  │  - Stale: last access days ago, decayed         │
  │                                                 │
  │  Spreading activation uses learned PMI weights  │
  │  instead of uniform 1.0 for all associations    │
  └─────────────────────────────────────────────────┘
```

### Rehearsal Selector Algorithm

The rehearsal selector determines which memories get replayed during dream phase 1. This is the critical design decision — dreaming about the wrong memories wastes rehearsal budget and can over-boost irrelevant content.

```python
def select_for_rehearsal(
    memories: list[Memory],
    graph: NetworkXGraph,
    access_log: list[AccessEntry],
    episodes: list[MemoryNode],
    config: DreamConfig,
) -> list[str]:
    """Select memory IDs for dream rehearsal.

    Selection criteria (weighted scoring):
    1. Graph centrality — memories connected to many entities are
       structurally important (high PageRank in knowledge graph)
    2. Access momentum — memories with increasing access frequency
       are "hot" working knowledge worth reinforcing
    3. Episode membership — members of active (open) episodes are
       rehearsed together to strengthen episodic associations
    4. Entity fan-out — memories linking multiple entities serve as
       bridge nodes worth preserving
    5. Staleness penalty — recently-accessed memories don't need
       rehearsal (they're already fresh in ACT-R)

    Returns top-K memory IDs sorted by rehearsal priority.
    """
    scores: dict[str, float] = {}

    for mem in memories:
        # 1. Graph centrality (PageRank)
        centrality = graph.pagerank().get(mem.id, 0.0)

        # 2. Access momentum (slope of access frequency over last 7 days)
        recent_accesses = [a for a in access_log
                          if a.memory_id == mem.id
                          and a.age_days <= 7]
        momentum = _compute_access_trend(recent_accesses)

        # 3. Episode membership (boost if in active episode)
        in_active_episode = any(
            ep for ep in episodes
            if mem.id in ep.metadata.get("member_ids", [])
            and ep.metadata.get("status") == "open"
        )
        episode_bonus = 1.0 if in_active_episode else 0.0

        # 4. Entity fan-out (number of distinct entities)
        entity_count = len(graph.get_memory_entities(mem.id))
        fan_out = min(1.0, entity_count / 5.0)  # normalize, cap at 5

        # 5. Staleness (how long since last access)
        last_access_days = _days_since_last_access(mem.id, access_log)
        # Invert: recent = low priority, stale = high priority
        # But too stale (>30d) = probably irrelevant
        staleness = 0.0
        if 1.0 <= last_access_days <= 30.0:
            staleness = last_access_days / 30.0  # linear 0→1
        elif last_access_days < 1.0:
            staleness = -0.5  # penalty: already fresh

        scores[mem.id] = (
            config.w_centrality * centrality      # default 0.30
            + config.w_momentum * momentum        # default 0.25
            + config.w_episode * episode_bonus     # default 0.20
            + config.w_fan_out * fan_out           # default 0.10
            + config.w_staleness * staleness       # default 0.15
        )

    # Select top-K (cap at 30% of total memories)
    max_k = max(1, int(len(memories) * config.rehearsal_fraction))
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [mid for mid, _ in ranked[:max_k]]
```

### Rehearsal Selection Weights

| Signal | Weight | Rationale |
|--------|--------|-----------|
| `w_centrality` | 0.30 | Structurally important memories (many entity connections) are worth preserving |
| `w_momentum` | 0.25 | Rising access frequency indicates active working knowledge |
| `w_episode` | 0.20 | Episode members should be rehearsed together for coherence |
| `w_staleness` | 0.15 | Stale-but-important memories benefit most from rehearsal |
| `w_fan_out` | 0.10 | Bridge memories connecting multiple entities preserve graph structure |

### Conflict-Aware Rehearsal

When the dream cycle encounters entity states involved in supersession:
- **Current state**: always rehearsed (maintains activation advantage)
- **Superseded state**: never rehearsed (allowed to naturally decay via ACT-R)
- **Conflicting states**: both rehearsed, but current state gets priority slot

This creates a widening activation gap between current and obsolete knowledge over successive dream cycles — the correct answer becomes increasingly dominant without any explicit penalty mechanism.

### ACT-R Evaluation Protocol

Since BEIR benchmarks cannot evaluate ACT-R (static, no access history), Phase 8C introduces a purpose-built **Simulated Agent Workday** benchmark:

```
Protocol: Simulated Agent Workday
══════════════════════════════════

Day 1-3: Ingest Phase
  - Store 100 memories across 5 topics (auth, database, deploy, monitoring, API)
  - 20 memories per topic, mixed types (facts, state updates, episodes)

Day 4-7: Work Phase (simulated access patterns)
  - Agent queries auth + database topics heavily (5-10 searches/day)
  - Agent queries monitoring occasionally (1-2 searches/day)
  - Deploy + API topics: no queries (stale)
  - Each search creates access_log entries for returned memories

Day 7 (night): Dream Cycle
  - Run full dream cycle: rehearsal → association learning → importance drift
  - Rehearsal selector picks auth + database memories (high momentum)
  - Monitoring memories partially rehearsed (some centrality)
  - Deploy + API memories: unrehearsed (allowed to decay)

Day 8: Evaluation Queries
  - Query all 5 topics with identical queries
  - Measure: do rehearsed memories rank higher than unrehearsed?

Metrics:
  ┌───────────────────────────────────────────────┐
  │ Rehearsal Boost Rate (RBR):                   │
  │   % of queries where rehearsed memories       │
  │   rank above unrehearsed for same topic        │
  │   Target: ≥ 85%                               │
  │                                               │
  │ Recency Discrimination (RD):                  │
  │   Rank correlation between access recency     │
  │   and retrieval rank                          │
  │   Target: Spearman ρ ≥ 0.6                    │
  │                                               │
  │ Staleness Decay Rate (SDR):                   │
  │   Activation ratio: unrehearsed / rehearsed   │
  │   after N dream cycles                        │
  │   Target: < 0.5 after 3 cycles                │
  │                                               │
  │ Compounding Effect (CE):                      │
  │   RBR improvement per additional dream cycle  │
  │   Measure at 1, 3, 7, 14 cycles              │
  │   Target: monotonically increasing, plateau   │
  │                                               │
  │ ACT-R Weight Crossover:                       │
  │   Find optimal ACT-R weight on this benchmark │
  │   Compare: ACT-R=0 vs ACT-R=0.1..0.5        │
  │   Expect: positive weight now helps (unlike   │
  │   BEIR where ACT-R=0 was optimal)            │
  └───────────────────────────────────────────────┘

Control conditions:
  A) No dream cycle, ACT-R=0 (BEIR-optimal baseline)
  B) No dream cycle, ACT-R=0.4 (uniform access, should hurt)
  C) Dream cycle, ACT-R=0 (rehearsal but no scoring signal)
  D) Dream cycle, ACT-R=0.4 (full cognitive pipeline)

Expected result: D >> A > C > B
  - D wins because dream creates differential access + ACT-R exploits it
  - A is BEIR-optimal but can't distinguish working vs stale knowledge
  - C rehearses but doesn't use the signal
  - B has ACT-R noise with no differential access (worst case)
```

### Superseded State Demotion via Dream Cycles

Dream rehearsal provides a natural alternative to explicit supersession penalties for demoting obsolete states in retrieval:

```
State transition: "PostgreSQL 14" → "PostgreSQL 16" (supersedes)

Without dream:
  Both states have 1 access each → ACT-R scores identical
  Must rely on explicit penalty (−0.3) to demote old state

With dream:
  Cycle 1: current state rehearsed, old state not → 2 vs 1 accesses
  Cycle 3: current state 4 accesses, old state 1 → activation gap widens
  Cycle 7: old state activation decayed below threshold → naturally filtered

  ACT-R formula does the work:
    current: ln(t₁⁻⁰·⁵ + t₂⁻⁰·⁵ + t₃⁻⁰·⁵ + t₄⁻⁰·⁵) ≈ 0.8
    old:     ln(t₁⁻⁰·⁵)                              ≈ -1.2
    Gap: 2.0 activation units (no explicit penalty needed)
```

This is more cognitively faithful than hardcoded penalties — the system "forgets" obsolete knowledge through disuse, just as humans do.

### Phase 8A: Search Logging & Data Collection (estimated: 2-3 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 8.A.1 | Add `search_log` table: `(id, query, query_entities, returned_ids, timestamp, agent_id)` | `migrations.py` | Migration runs; schema_version incremented |
| 8.A.2 | Instrument `memory_service.search()` to log returned memory IDs before filtering | `memory_service.py` | Every search produces a search_log row |
| 8.A.3 | Add `get_recent_searches()` and `get_search_access_pairs()` to SQLiteStore | `sqlite_store.py` | Returns (query, returned_ids, accessed_ids) tuples |
| 8.A.4 | Add `association_strengths` table: `(entity_id_1, entity_id_2, strength, updated_at)` | `migrations.py` | Persisted association weights |
| 8.A.5 | Wire `association_strengths` loading into `memory_service.search()` → `spreading_activation()` | `memory_service.py` | Non-None strengths passed when table populated |

### Phase 8B: Dream Cycle — Non-LLM (estimated: 3-4 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 8.B.1 | Implement `run_dream_rehearsal()`: inject synthetic accesses for high-count + stale memories | `consolidation_service.py` | Decayed important memories get activation boost; recently-active memories untouched |
| 8.B.2 | Implement `learn_association_strengths()`: compute PMI from co-access entity pairs | `consolidation_service.py` | association_strengths table populated; frequently co-accessed entity pairs have positive PMI |
| 8.B.3 | Implement `adjust_importance_drift()`: raise importance for increasing-access memories, lower for declining | `consolidation_service.py` | importance field updated based on 30-day access trend |
| 8.B.4 | Add `run_dream_cycle()` orchestrator that runs all three passes in sequence | `consolidation_service.py` | Single entry point; idempotent; logs stats |
| 8.B.5 | Add config flags: `NCMS_DREAM_CYCLE_ENABLED`, `NCMS_DREAM_REHEARSAL_FRACTION` (0.3), `NCMS_DREAM_STALENESS_DAYS` (30), `NCMS_DREAM_MIN_ACCESS_COUNT` (2), `NCMS_DREAM_W_CENTRALITY` (0.30), `NCMS_DREAM_W_MOMENTUM` (0.25), `NCMS_DREAM_W_EPISODE` (0.20), `NCMS_DREAM_W_STALENESS` (0.15), `NCMS_DREAM_W_FAN_OUT` (0.10) | `config.py` | Feature flag controls; defaults disabled; rehearsal weights tunable |
| 8.B.6 | Unit tests: synthetic access injection, PMI computation, importance drift, edge cases (empty access log, single memory) | `tests/unit/` | All pass; deterministic with fixed timestamps |
| 8.B.7 | Integration test: full dream cycle on a populated memory store; verify activation scores improve for rehearsed memories | `tests/integration/` | Before/after activation comparison |

### Phase 8C: Evaluation & Ablation (estimated: 2-3 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 8.C.1 | Create Simulated Agent Workday benchmark: 100 memories across 5 topics, 7-day work simulation with differential access patterns, dream cycle, post-dream evaluation queries | `benchmarks/dream/` | Reproducible; deterministic with seeded timestamps |
| 8.C.2 | Implement 4 control conditions: (A) no dream + ACT-R=0, (B) no dream + ACT-R=0.4, (C) dream + ACT-R=0, (D) dream + ACT-R=0.4 | `benchmarks/dream/` | All 4 conditions runnable; D should outperform A |
| 8.C.3 | Compute ACT-R-specific metrics: Rehearsal Boost Rate (≥85%), Recency Discrimination (Spearman ρ≥0.6), Staleness Decay Rate (<0.5 after 3 cycles), ACT-R Weight Crossover point | `benchmarks/dream/` | Metrics demonstrate differential access value |
| 8.C.4 | Measure compounding effect: run dream cycle for 1, 3, 7, 14 simulated nights; plot RBR and activation gap curves | `benchmarks/dream/` | Monotonically increasing RBR, plateau identification |
| 8.C.5 | Run superseded state demotion test: compare explicit penalty (Phase 7) vs dream-based decay across N cycles | `benchmarks/dream/` | Dream-based demotion achieves ≥ penalty-based demotion after N cycles |
| 8.C.6 | Run ablation: SPLADE+Graph baseline → +Dream Rehearsal → +Association Learning → +Importance Drift | `benchmarks/dream/` | Each component measured independently |
| 8.C.7 | Update paper Section 6.5 and README with Project Oracle results | `docs/paper.md`, `README.md` | Results documented |

**Phase 8 exit criteria:**
- [ ] Dream cycle runs without LLM calls
- [ ] Rehearsal selector picks structurally important + actively used memories (centrality + momentum)
- [ ] Association strengths populated from co-access patterns (PMI-based)
- [ ] ACT-R weight crossover demonstrated: positive ACT-R weight helps on Simulated Workday (unlike BEIR where ACT-R=0 is optimal)
- [ ] Rehearsal Boost Rate ≥ 85% (rehearsed memories rank above unrehearsed)
- [ ] Compounding improvement demonstrated over multiple dream cycles (monotonic RBR increase)
- [ ] Superseded state demotion via dream decay matches or exceeds explicit penalty approach
- [ ] All features toggleable via config flags (10 new `NCMS_DREAM_*` params)
- [ ] Zero query-time latency impact (all work is offline)

---

# 17. Failure Modes to Watch

| Risk | Mitigation |
|------|------------|
| Episode over-clustering | Conservative anchor + 2-signal requirement; tune on labeled data |
| State slot explosion from poor normalization | Canonical entity resolution; state key normalization |
| Accidental supersession when scope differs | Require same entity + same state key + incompatible value; scope-aware comparison |
| Abstraction drift from stale summaries | `refresh_due_at` tracking; re-consolidate when sources change |
| Over-aggressive admission filtering | Start with low admission threshold (0.25); measure recall impact |
| Retrieval preferring abstracts over needed atomic evidence | HierarchyMatch should boost, not replace; intent classification routes to right level |
| LLM consolidation quality | All LLM features degrade gracefully; heuristic fallbacks for all consolidation levels |
| Dream rehearsal over-boosting | Cap synthetic accesses per cycle (max 1 per memory per night); decay naturally dampens over time |
| Association strength drift | Recompute PMI from 30-day rolling window; strengths auto-correct as access patterns change |
| Cold-start memories invisible to dream cycle | New memories get initial access at ingest (already implemented); optional LLM tier for semantic review |

---

# 18. Testing Strategy

## 18.1 Per-phase testing

Each phase includes:
- **Unit tests** for all new functions (pure functions, scoring, classification)
- **Integration tests** for service-level behavior (ingest → store → retrieve)
- **Regression tests** against existing NCMS retrieval baseline

## 18.2 Test conventions (matching existing NCMS)

- All tests use in-memory backends (`:memory:` SQLite, ephemeral Tantivy/NetworkX)
- Fixtures provide ready-to-use service instances
- No hardcoded expected values — use formula-computed expectations or relative assertions
- Test files mirror source structure

## 18.3 Evaluation datasets

| Dataset | Purpose | Size |
|---------|---------|------|
| SciFact (BEIR) | Baseline retrieval quality | 5,183 docs / 300 queries |
| NFCorpus (BEIR) | Cross-domain retrieval quality | 3,633 docs / 323 queries |
| Custom state-change dataset | State reconciliation accuracy | TBD (100+ state transitions) |
| Custom episode dataset | Episode formation accuracy | TBD (50+ incident scenarios) |
| Custom intent dataset | Intent classification accuracy | TBD (100+ labeled queries) |

---

# 19. Implementation Guidance

## 19.1 v1 bias

Prefer:
- Explicit rules and heuristics
- Typed Pydantic schemas
- Deterministic behavior
- Conservative thresholds
- Measurable outputs
- Feature flags for all new capabilities

Avoid:
- Premature end-to-end learning
- Complex ontology induction
- Heavy LLM dependency (everything must degrade gracefully)

## 19.2 Code conventions (matching existing NCMS)

- All models use Pydantic `BaseModel`
- All SQL is parameterized (never string-interpolate)
- All infrastructure contracts defined as `Protocol` in `domain/protocols.py`
- `model_dump(mode="json")` for serialization with datetimes
- `litellm` kwargs pattern for all LLM calls
- `think=False` for Ollama models
- Non-fatal LLM calls (degrade gracefully on error)
- `ruff` linting (line-length 100, py312 target)
- `mypy` strict mode
- `pytest-asyncio` with `asyncio_mode = "auto"`

## 19.3 v1 success criteria

The first release is successful if it:

1. Preserves current sparse retrieval performance (nDCG@10 ≥ 0.700 on SciFact)
2. Reduces duplicate and stale recall vs baseline
3. Supports current-state and change queries better than flat memory
4. Produces usable episode objects with correct member assignment
5. Supports clean ablation path showing each component's contribution
6. All features are toggleable via config flags
7. All features degrade gracefully without LLM

---

# 20. Phase Summary and Timeline

| Phase | Description | Codename | Estimated Duration | Key Deliverable |
|-------|-------------|----------|-------------------|-----------------|
| **0** | Pre-Work Cleanups | | 2–3 days | Protocol compliance, scalability fixes, split protocols |
| **1** | Typed Node Schema + Admission Scoring | | 9–12 days | Selective admission with feature-scored routing |
| **2** | State Reconciliation + Bitemporal | | 9–12 days | Truth-maintenance with supersession and temporal queries |
| **3** | Episode Formation | | 7–9 days | Automatic episode grouping and event reconstruction |
| **4** | Intent-Aware Retrieval | | 5–7 days | Query intent classification with type-filtered search |
| **5** | Hierarchical Consolidation | | 8–11 days | Four levels of abstraction from atomic to strategic |
| **6** | MCP + Dashboard + Demo | | 6–8 days | Full tooling and visualization |
| **7** | Tuning + Evaluation | | 5–7 days | Comprehensive ablation and documented results |
| **8** | Dream Cycle & Adaptive Scoring | **Project Oracle** | 7–10 days | Non-LLM offline learning that teaches ACT-R via access patterns |
| **Total** | | | **58–79 days** | Complete HTMG system with adaptive scoring and ablation-validated results |

Each phase is independently testable, deployable, and measurable. Phases can be paused between without loss of progress.
