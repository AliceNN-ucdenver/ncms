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

### Episode Affinity — episode membership signals
- Same entities/artifacts as active episode
- Same workflow/thread/issue ID
- Temporal proximity to open episode
- Sequence/causal markers

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

# 10. Episode Formation

## 10.1 Purpose

Group traces and state transitions into bounded events or workflow arcs for coherent event-level recall.

## 10.2 Episode anchors

An episode begins with an **anchor** — a strong signal of a bounded event:

- Explicit issue/ticket/PR ID
- Release ID or version bump
- Incident marker (outage, error spike, rollback)
- Migration marker
- Significant state transition for a tracked entity

## 10.3 Episode assignment rules (conservative v1)

A candidate joins an existing episode when it has:
- **At least one anchor match** (shared workflow ID, shared entity+artifact, linked state transition)
- **Plus at least two supporting signals** (temporal proximity, participant overlap, same source thread, causal cue words, retrieval neighborhood overlap)

A candidate starts a new episode when it has an anchor but no matching open episode.

## 10.4 Episode closure

Close an episode when:
- Explicit resolution marker appears
- No new member arrives within window `T_close` (configurable, default 24h)
- A new episode supersedes it
- A release/migration/incident is marked complete

## 10.5 Episode lifecycle

```
[*] → Open → Open (add fragment/state)
Open → Consolidating (threshold met or closure signal)
Consolidating → Closed (summary created)
Closed → Reopened (new linked fragment arrives)
Closed → Superseded (replaced by higher-level or merged episode)
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
- [ ] All application services use Protocol types, not concrete implementations
- [ ] `memory_count()` uses SQL COUNT
- [ ] `MemoryStore` protocol split into cohesive sub-protocols
- [ ] Surrogate discovery works for deregistered agents
- [ ] `uv run mypy src/` and `uv run pytest tests/` both pass
- [ ] Zero functional changes — all existing behavior preserved

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

**Phase 1A verification:** All existing tests pass. New models serialize/deserialize. New tables exist. Schema migration runs idempotently.

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

**Phase 1B verification:** `uv run pytest tests/unit/application/test_admission_service.py` — all 10+ scenarios pass. Feature computation is deterministic and traceable.

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

**Phase 1C verification:** `uv run pytest tests/` — zero regressions. Admission can be toggled. Ablation shows retrieval quality maintained.

**Phase 1 exit criteria:**
- [ ] All existing tests pass
- [ ] New models, tables, and admission service fully tested
- [ ] Feature flag allows clean toggle
- [ ] BEIR ablation shows no regression with admission enabled
- [ ] Dashboard shows admission routing decisions

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
- [ ] Entity state supersession chain works end-to-end
- [ ] Bitemporal queries return correct results at any point in time
- [ ] Superseded states penalized in retrieval ranking
- [ ] Conflict detection replaces simple contradiction detection
- [ ] BEIR ablation shows no regression, ideally reduced stale recall

---

## Phase 3: Episode Formation

**Goal:** Automatically group related traces and state transitions into bounded event arcs.

**Research question addressed:** Does episodic grouping improve event reconstruction queries?

### Phase 3A: Episode Builder (estimated: 4-5 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 3.A.1 | Create `application/episode_service.py` with `EpisodeService` class | New file | Constructor takes `MemoryStore`, `GraphEngine` |
| 3.A.2 | Implement anchor detection — identify episode-starting signals (issue IDs, incident markers, release markers) | `application/episode_service.py` | Unit test: "INCIDENT-234: API outage" → anchor detected; "general observation" → no anchor |
| 3.A.3 | Implement `_compute_episode_affinity()` — score candidate against open episodes | `application/episode_service.py` | Unit test: same entities + temporal proximity → high affinity |
| 3.A.4 | Implement `assign_episode()` — assign to existing or create new | `application/episode_service.py` | Unit test: matching open episode → assign; anchor + support signals → new episode |
| 3.A.5 | Implement `_check_supporting_signals()` — temporal proximity, participant overlap, source thread, causal cues | `application/episode_service.py` | Unit test: candidate with 2+ support signals → assignable |
| 3.A.6 | Implement `close_episode()` — closure heuristics (resolution marker, timeout, supersession) | `application/episode_service.py` | Unit test: episode with no new members after T_close → closed |
| 3.A.7 | Store episodes in `memory_nodes` table with `node_type='episode'` | `application/episode_service.py` | Episodes persisted with member lists |
| 3.A.8 | Create `BELONGS_TO_EPISODE` edges in graph when fragments assigned | `application/episode_service.py` | Graph traversal from episode returns all member nodes |

**Phase 3A verification:** `uv run pytest tests/unit/application/test_episode_service.py` — episode creation, assignment, and closure all pass.

### Phase 3B: Episode Integration (estimated: 3-4 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 3.B.1 | Wire episode assignment into admission routing — `episode_fragment` route calls `EpisodeService` | `application/memory_service.py` | Fragments auto-assigned to episodes during ingest |
| 3.B.2 | Add periodic episode closure check (background task) | `application/episode_service.py` | Open episodes older than T_close auto-closed |
| 3.B.3 | Index episode title + summary in Tantivy for searchability | `application/memory_service.py` | Episodes appear in BM25 search results |
| 3.B.4 | Add episode expansion in retrieval — when episode matched, also return member fragments | `application/memory_service.py` | "What happened during the API outage?" returns episode + all related fragments |
| 3.B.5 | Add `EpisodeCompleteness` scoring factor — episodes with more members score higher for reconstruction queries | `domain/scoring.py` | Multi-member episodes ranked above single fragments |
| 3.B.6 | Add `NCMS_EPISODES_ENABLED` config toggle | `config.py` | Default false; existing behavior preserved |
| 3.B.7 | Add episode events to observability log | `infrastructure/observability/event_log.py` | Dashboard shows episode creation/assignment/closure |
| 3.B.8 | Run event reconstruction test: store 10 traces for an incident → query "what happened" → measure completeness | Custom test | Episode returns ≥80% of related traces |

**Phase 3 exit criteria:**
- [ ] Episodes auto-created from anchored events
- [ ] Fragments correctly assigned to matching episodes
- [ ] Episode closure works with timeout and explicit resolution
- [ ] Event reconstruction queries return episode + members
- [ ] BEIR ablation shows no regression

---

## Phase 4: Intent-Aware Retrieval

**Goal:** Classify query intent and route to appropriate node types for more precise retrieval.

**Research question addressed:** Does intent-aware routing outperform uniform scoring?

### Phase 4A: Intent Classifier (estimated: 2-3 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 4.A.1 | Create `application/intent_classifier.py` with heuristic rule-based classifier | New file | 7 intent classes classified from query text |
| 4.A.2 | Implement keyword pattern matching for each intent class | `application/intent_classifier.py` | Unit tests: 20+ queries → correct intent classification |
| 4.A.3 | Add intent confidence score (how strongly the patterns match) | `application/intent_classifier.py` | Low-confidence intents fall back to `fact_lookup` |
| 4.A.4 | Add `NCMS_INTENT_CLASSIFICATION_ENABLED` config toggle | `config.py` | Default false; fallback to existing uniform retrieval |

**Phase 4A verification:** `uv run pytest tests/unit/application/test_intent_classifier.py` — ≥85% accuracy on hand-crafted test queries.

### Phase 4B: Type-Filtered Retrieval (estimated: 3-4 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 4.B.1 | Implement retrieval routing — each intent class specifies which node types to search | `application/memory_service.py` | `current_state_lookup` only searches `EntityState` with `is_current=true` |
| 4.B.2 | Implement `HierarchyMatch` scoring factor — bonus when result node_type matches intent expectation | `domain/scoring.py` | Entity states score higher for state queries; episodes score higher for event queries |
| 4.B.3 | Implement temporal filter integration — `historical_lookup` applies valid-time filter | `application/memory_service.py` | Historical queries only return temporally valid results |
| 4.B.4 | Implement change detection retrieval — follow `SUPERSEDES` chains for entity | `application/memory_service.py` | "What changed" returns ordered supersession chain |
| 4.B.5 | Update `ScoredMemory` result type with `node_type`, `intent`, and hierarchy metadata | `domain/models.py` | Results include type information for downstream consumers |
| 4.B.6 | Run BEIR ablation: B3 vs B4 (with intent classification) | Benchmark scripts | Measure per-intent-class retrieval quality |

**Phase 4 exit criteria:**
- [ ] Intent classifier achieves ≥85% accuracy on test set
- [ ] Type-filtered retrieval correctly narrows candidate space
- [ ] State queries return current entity states
- [ ] Historical queries apply temporal filtering
- [ ] Change detection follows supersession chains
- [ ] BEIR ablation shows improvement on appropriate query types

---

## Phase 5: Hierarchical Consolidation

**Goal:** Generate reusable higher-order knowledge from lower-level traces.

**Research question addressed:** Does hierarchical consolidation produce useful abstractions?

### Phase 5A: Episode Summary Consolidation (estimated: 3-4 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 5.A.1 | Extend `ConsolidationService` with episode summary generation | `application/consolidation_service.py` | Closed episodes trigger summary generation |
| 5.A.2 | Implement episode summary prompt — LLM generates narrative from member fragments | `infrastructure/consolidation/synthesizer.py` | Summary covers actors, artifacts, decisions, outcome |
| 5.A.3 | Store summary as `AbstractMemory` with `abstract_type='episode_summary'` | `application/consolidation_service.py` | Summary node linked to episode via `DERIVED_FROM` |
| 5.A.4 | Index summary in Tantivy + SPLADE for retrieval | `application/memory_service.py` | Summaries appear in search results |
| 5.A.5 | Add `NCMS_EPISODE_CONSOLIDATION_ENABLED` config toggle | `config.py` | Default false |

**Phase 5A verification:** Close an episode → summary generated → summary searchable → summary contains key details from member fragments.

### Phase 5B: State Trajectory Consolidation (estimated: 2-3 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 5.B.1 | Implement state trajectory detection — find entities with ≥3 state transitions | `application/consolidation_service.py` | Identifies entities with rich state histories |
| 5.B.2 | Implement trajectory summary — LLM generates temporal progression narrative | `infrastructure/consolidation/synthesizer.py` | Summary covers major transitions and current state |
| 5.B.3 | Store trajectory as `AbstractMemory` with `abstract_type='state_trajectory'` | `application/consolidation_service.py` | Trajectory linked to all component states |

**Phase 5B verification:** Entity with 5 state transitions → trajectory summary generated → summary accurately describes progression.

### Phase 5C: Pattern and Insight Consolidation (estimated: 3-4 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 5.C.1 | Implement similar episode clustering — find episodes with overlapping entities/types | `infrastructure/consolidation/clusterer.py` | Clusters of ≥3 similar episodes identified |
| 5.C.2 | Implement recurring pattern detection — LLM generates generalized pattern from episode cluster | `infrastructure/consolidation/synthesizer.py` | Pattern describes what recurs and why |
| 5.C.3 | Store pattern as `AbstractMemory` with `abstract_type='recurring_pattern'` | `application/consolidation_service.py` | Pattern linked to source episodes |
| 5.C.4 | Implement strategic insight generation — stable patterns → durable lessons | `infrastructure/consolidation/synthesizer.py` | Insights only from patterns with `stability_score > 0.7` |
| 5.C.5 | Implement `refresh_due_at` tracking — flag abstractions for refresh when source nodes change | `application/consolidation_service.py` | Stale abstractions re-generated when sources updated |
| 5.C.6 | Run ablation: retrieval with vs without consolidated abstractions | Benchmark scripts | Measure abstract memory contribution to pattern/strategic queries |

**Phase 5 exit criteria:**
- [ ] Episode summaries generated from closed episodes
- [ ] State trajectories generated for entities with rich histories
- [ ] Recurring patterns detected from similar episode clusters
- [ ] Abstractions indexed and retrievable
- [ ] Stale abstractions flagged for refresh
- [ ] Consolidation is LLM-optional (degrades gracefully without LLM)

---

## Phase 6: MCP Tools + Dashboard + Demo

**Goal:** Expose NCMS-Next capabilities through MCP tools, update dashboard, and create a comprehensive demo.

### Phase 6A: MCP Tool Updates (estimated: 3-4 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 6.A.1 | Add `ncms_store_with_admission` MCP tool — store with admission scoring visible | `interfaces/mcp/tools.py` | MCP client can see admission score and routing decision |
| 6.A.2 | Add `ncms_get_current_state` MCP tool — entity state lookup | `interfaces/mcp/tools.py` | Returns current state for entity/key |
| 6.A.3 | Add `ncms_get_state_history` MCP tool — temporal state chain | `interfaces/mcp/tools.py` | Returns ordered state history with transitions |
| 6.A.4 | Add `ncms_list_episodes` MCP tool — open/closed episodes | `interfaces/mcp/tools.py` | Returns episodes with member counts |
| 6.A.5 | Add `ncms_get_episode` MCP tool — episode with all members | `interfaces/mcp/tools.py` | Returns episode + member fragments + state transitions |
| 6.A.6 | Update `ncms_search` to support intent-aware retrieval | `interfaces/mcp/tools.py` | Optional `intent` parameter for type-filtered search |
| 6.A.7 | Add `ncms://entities/{id}/state` resource | `interfaces/mcp/resources.py` | Resource returns current state and recent history |

**Phase 6A verification:** All new MCP tools callable from Claude Code. Existing tools unchanged.

### Phase 6B: Dashboard + Demo Updates (estimated: 3-4 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 6.B.1 | Update dashboard to show memory node types with color coding | `interfaces/http/static/index.html` | Atomic=blue, EntityState=green, Episode=orange, Abstract=purple |
| 6.B.2 | Add episode timeline view to dashboard | `interfaces/http/static/index.html` | Episodes shown on timeline with member fragments |
| 6.B.3 | Add state history view to dashboard | `interfaces/http/static/index.html` | Entity state transitions shown as timeline |
| 6.B.4 | Add admission scoring panel to dashboard | `interfaces/http/static/index.html` | Real-time admission scores and routing visible |
| 6.B.5 | Create NCMS-Next demo scenario exercising admission, episodes, states, and consolidation | `demo/` | Demo shows all Phase 1-5 features in action |
| 6.B.6 | Update CLI with `ncms state`, `ncms episodes` subcommands | `interfaces/cli/main.py` | CLI access to state and episode data |

**Phase 6 exit criteria:**
- [ ] All MCP tools working and tested
- [ ] Dashboard shows new node types and relationships
- [ ] Demo scenario exercises full pipeline
- [ ] CLI provides state and episode access

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
| 8.B.5 | Add config flags: `NCMS_DREAM_CYCLE_ENABLED`, `NCMS_DREAM_REHEARSAL_STALENESS_DAYS`, `NCMS_DREAM_MIN_ACCESS_COUNT` | `config.py` | Feature flag controls; defaults disabled |
| 8.B.6 | Unit tests: synthetic access injection, PMI computation, importance drift, edge cases (empty access log, single memory) | `tests/unit/` | All pass; deterministic with fixed timestamps |
| 8.B.7 | Integration test: full dream cycle on a populated memory store; verify activation scores improve for rehearsed memories | `tests/integration/` | Before/after activation comparison |

### Phase 8C: Evaluation & Ablation (estimated: 2-3 days)

| # | Task | Files | Verification |
|---|------|-------|--------------|
| 8.C.1 | Create temporal benchmark: SciFact with synthetic access patterns (simulate 30 days of agent usage) | `benchmarks/` | Reproducible access pattern generation |
| 8.C.2 | Run ablation: SPLADE+Graph baseline → +Dream Rehearsal → +Association Learning → +Importance Drift | `benchmarks/` | Each component measured independently |
| 8.C.3 | Compare vs. LLM-as-judge: Dream-consolidated ACT-R vs. Tier 3 judge on same queries | `benchmarks/` | Quantify quality gap (if any) vs. cost savings |
| 8.C.4 | Measure compounding effect: run dream cycle for 1, 7, 30 simulated nights | `benchmarks/` | Retrieval quality improves with more dream cycles |
| 8.C.5 | Update paper Section 6.5 and README with Project Oracle results | `docs/paper.md`, `README.md` | Results documented |

**Phase 8 exit criteria:**
- [ ] Dream cycle runs without LLM calls
- [ ] Association strengths populated from co-access patterns
- [ ] ACT-R scoring improves with dream cycle (measured on temporal benchmark)
- [ ] Compounding improvement demonstrated over multiple dream cycles
- [ ] All features toggleable via config flags
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
