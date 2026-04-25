# NeMo Cognitive Memory System

## Design Specification

**Vector-Free Retrieval / Embedded Knowledge Bus / NeMo Agent Templates**

Version 0.1 Draft | March 2026 | Shawn McCarthy / Chief Archeologist

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Architecture Overview](#3-architecture-overview)
4. [Vector-Free Retrieval Pipeline](#4-vector-free-retrieval-pipeline)
5. [Embedded Knowledge Bus](#5-embedded-knowledge-bus)
6. [NeMo Agent Template with Knowledge Callbacks](#6-nemo-agent-template-with-knowledge-callbacks)
7. [Memory Lifecycle and Consolidation](#7-memory-lifecycle-and-consolidation)
8. [Deployment Modes](#8-deployment-modes)
9. [Knowledge Persistence and Surrogate Response](#9-knowledge-persistence-and-surrogate-response)
10. [MCP Server Integration](#10-mcp-server-integration)
11. [Coding Agent Integration: Hooks and Commit Patterns](#11-coding-agent-integration-hooks-and-commit-patterns)
12. [Storage Architecture and Rehydration](#12-storage-architecture-and-rehydration)
13. [Implementation Roadmap](#13-implementation-roadmap)
14. [Appendix A: Comparison with Existing Systems](#appendix-a-comparison-with-existing-systems)
15. [Appendix B: Key References](#appendix-b-key-references)

---

## 1. Executive Summary

This document specifies the NeMo Cognitive Memory System (NCMS), a next-generation persistent memory architecture for NVIDIA's NeMo Agent Toolkit. NCMS addresses three fundamental gaps in the current agentic AI landscape: the absence of GPU-accelerated memory services, the lack of real-time collaborative knowledge sharing between agents, and the dependency on vector embedding pipelines that sacrifice precision for speed.

The system introduces three core innovations:

- **Vector-Free GPU-Accelerated Retrieval.** A three-tier pipeline combining SPLADE learned sparse retrieval, knowledge graph traversal via cuGraph, and LLM-as-judge reasoning. No embedding vectors. Full semantic understanding at every tier.
- **Embedded Knowledge Bus.** An in-process, zero-dependency event bus enabling non-blocking broadcast/ask/subscribe patterns between collaborative agents. No external message brokers required. Agents discover knowledge osmotically without interrupting their current tasks.
- **NeMo Agent Template with Knowledge Callbacks.** A standardized base class for NeMo agents that implements knowledge provider/consumer interfaces, automatic memory capture, and lifecycle hooks for the Knowledge Bus.

The architecture is packaged as a NIM-compatible container with Helm charts, deployable alongside existing NeMo infrastructure. It integrates with NeMo Agent Toolkit v1.4+ through the MemoryEditor and MemoryManager plugin interfaces, making it immediately compatible with all NAT agent types including ReAct, ReWOO, Tool Calling, and Router agents.

---

## 2. Problem Statement

The current state of agent memory across the industry suffers from five structural problems that this system addresses.

### No GPU-Native Memory Service Exists

NVIDIA's agentic AI stack includes inference (NIM), training (NeMo Customizer), evaluation (NeMo Evaluator), retrieval (NeMo Retriever), safety (NeMo Guardrails), and orchestration (NeMo Agent Toolkit). There is no persistent memory service. Current NAT memory integrations (Mem0, Zep, Redis) are third-party backends that do not leverage NVIDIA GPU infrastructure, do not follow NIM packaging conventions, and do not integrate with the broader NeMo microservice ecosystem.

### Vector Embedding Creates a Precision Ceiling

Traditional vector search compresses entire documents or memories into single dense vectors, losing fine-grained token-level information. When a frontend agent asks "What is the interface contract for the user service /profile endpoint?", cosine similarity between a query embedding and a memory embedding may rank a memory about "user authentication flows" higher than one containing the actual OpenAPI specification. The semantic proximity is high but the factual relevance is low. Google's Always-On Memory Agent demonstrated that having the LLM reason directly over structured memory records produces more accurate retrieval than vector similarity, but at the cost of linear scaling.

### Agents Cannot Share Knowledge in Real Time

In multi-agent coding workflows, agents working on different parts of a system (frontend, API, database, testing) accumulate knowledge that other agents need. A frontend agent building a React component needs to know the API contract. The API agent changing an endpoint signature needs to notify downstream consumers. Today, this requires explicit tool calls, polling shared files, or human coordination. There is no mechanism for non-blocking, event-driven knowledge diffusion between agents.

### External Service Dependencies Create Deployment Friction

Production memory solutions like Zep require Neo4j. Mem0 requires a vector store plus optionally a graph store. Even Redis-based solutions require a separate Redis cluster. For development, local testing, and edge deployments, these dependencies create friction. An embedded memory system that can run entirely in-process while optionally scaling to distributed backends would eliminate this barrier.

### No Standard Pattern for Knowledge-Aware Agents

NeMo Agent Toolkit provides agent orchestration and memory hooks, but no standardized pattern for agents that participate in collaborative knowledge networks. Each team builds ad-hoc solutions for inter-agent communication, knowledge registration, and memory lifecycle management. A template that codifies these patterns would accelerate adoption and ensure interoperability.

---

## 3. Architecture Overview

The NeMo Cognitive Memory System consists of three layers, each independently useful but designed to compose into a unified cognitive infrastructure.

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 3: Knowledge Bus (Embedded)                              │
│  [Ask/Respond] [Announce/Subscribe] [Domain Router] [Inbox Mgr]│
│  Transport: In-Process EventEmitter | Optional: Redis | NATS    │
├─────────────────────────────────────────────────────────────────┤
│  Layer 2: Memory Core (Vector-Free Retrieval)                   │
│  Tier 1: SPLADE Sparse Neural Index (GPU-accelerated)           │
│  Tier 2: Knowledge Graph Traversal (cuGraph / NetworkX)         │
│  Tier 3: LLM-as-Judge Reasoning (NIM / local model)            │
│  ACT-R Scorer | Provenance Engine | Contradiction Detector      │
├─────────────────────────────────────────────────────────────────┤
│  Layer 1: Storage Backends (Pluggable)                          │
│  Embedded: SQLite + Tantivy | Scaled: Milvus + Neo4j + Redis   │
│  Background Workers: Consolidation | Distillation | Decay       │
└─────────────────────────────────────────────────────────────────┘
```

### Integration Surfaces

NCMS exposes three integration surfaces, each targeting a different consumer:

- **NAT MemoryEditor Plugin:** Implements the MemoryEditor and MemoryManager abstract interfaces from NeMo Agent Toolkit. This makes NCMS a drop-in replacement for Mem0, Zep, or Redis memory backends. Compatible with auto_memory_agent and all NAT agent types.
- **MCP Server:** Exposes store_memory, search_memory, ask_knowledge, announce_knowledge, and get_provenance as MCP tools. Memory contents are browsable as MCP resources. Compatible with any MCP client including Claude, VS Code extensions, and third-party agents.
- **REST/gRPC API:** OpenAI-compatible endpoint structure following NIM conventions. For service-to-service integration, microservice deployments, and non-Python consumers.

---

## 4. Vector-Free Retrieval Pipeline

The retrieval pipeline eliminates dense vector embeddings entirely. Instead, it uses three complementary mechanisms that each exploit GPU acceleration differently and together provide higher precision than any single-vector approach.

### Tier 1: SPLADE Learned Sparse Retrieval

SPLADE (Sparse Lexical and Expansion Model) uses BERT's Masked Language Model head not for token prediction but for estimating term importance across the full vocabulary. This produces sparse representations that are compatible with traditional inverted indexes while capturing semantic relationships that BM25 misses.

Key properties that make SPLADE superior to vector search for memory retrieval:

- **Learned query expansion:** When an agent asks about "API interface specification", SPLADE expands the query to include related terms like "endpoint", "contract", "schema", "REST", "OpenAPI" based on learned associations. This bridges vocabulary mismatch without embedding similarity.
- **Exact term matching preserved:** Unlike dense vectors, SPLADE retains exact lexical matching. If a memory contains "UserProfileService.getProfile()", a query for "getProfile" will match directly. Vector search would dilute this signal across the embedding dimensions.
- **Inverted index efficiency:** Sparse representations use inverted indexes, the same data structure powering web search engines for decades. Retrieval is sublinear in corpus size. The expansion and scoring phases benefit from GPU batch processing.
- **Interpretability:** Every match can be explained in terms of which terms contributed to the score. This is critical for provenance tracking in enterprise settings.

For the embedded deployment mode, we use the Tantivy search library (Rust-based, Python bindings via tantivy-py) as the inverted index. SPLADE expansion runs on GPU via a NIM model or a local ONNX-optimized checkpoint. For scaled deployments, Elasticsearch or OpenSearch provide the inverted index with SPLADE-generated sparse vectors stored as term weights.

### Tier 2: Knowledge Graph Traversal

Memories are not isolated documents. They form a graph of entities, relationships, and temporal connections. Tier 2 exploits this structure for multi-hop retrieval that no flat search can replicate.

The knowledge graph stores three types of nodes:

- **Entity Nodes:** Named entities extracted from memories: services, endpoints, people, configurations, architecture decisions, code modules. Each entity carries a type, attributes, and temporal metadata.
- **Memory Nodes:** The memories themselves, linked to the entities they reference. Each memory node carries importance score, recency, access frequency, and provenance.
- **Relationship Edges:** Typed, directed edges with bi-temporal validity (valid_at, invalid_at). Examples: "UserService EXPOSES /profile endpoint", "FrontendApp DEPENDS_ON UserService", "ArchitectureDecision SUPERSEDES PreviousDecision".

For embedded mode, the graph uses NetworkX (in-memory, zero dependencies). For GPU-accelerated deployments, NVIDIA cuGraph provides massively parallel graph traversal on GPU. For persistent scaled deployments, Neo4j or FalkorDB serve as the graph backend. The PathHD hyperdimensional computing approach provides an encoder-free alternative for path retrieval, using simple cosine similarity over GHRR-encoded relation paths instead of neural scorers.

### Tier 3: LLM-as-Judge Reasoning

The final tier uses an LLM to reason over the candidate set produced by Tiers 1 and 2. This is the approach that Google's Always-On Memory Agent uses for its entire pipeline, but we apply it only to a small, pre-filtered candidate set (typically 5 to 20 memories), making it both accurate and efficient.

The LLM receives the original query, the candidate memories with their provenance and graph context, and a structured prompt that asks it to: (a) rank candidates by relevance to the query, (b) identify any contradictions between candidates, (c) synthesize a coherent response with citations to specific memories, and (d) flag any knowledge gaps where no candidate adequately addresses the query.

For embedded mode, this tier uses a local small language model (e.g., Phi-3 or Mistral 7B via llama.cpp). For production, a NIM-hosted model handles the reasoning with enterprise-grade throughput and latency guarantees.

### ACT-R Inspired Scoring

All three tiers contribute to a unified scoring function inspired by the ACT-R cognitive architecture. The activation of a memory is computed as:

```
activation(m) = base_level(m) + spreading_activation(m, query) + noise

base_level(m) = ln( sum_i( (t_now - t_access_i) ^ -0.5 ) )
  where t_access_i are all previous access timestamps

spreading_activation(m, query) = sum_j( w_j * S_ji )
  where w_j is the weight of context element j (query terms, active entities)
  and S_ji is the strength of association between element j and memory m

retrieval_probability(m) = 1 / (1 + exp(-activation(m) / tau))
  where tau is a temperature parameter (default 0.4)
```

This scoring function naturally implements human-like memory dynamics: frequently and recently accessed memories have higher base-level activation, contextually relevant memories receive spreading activation from the current query, and the noise parameter introduces stochastic variability that prevents the system from always returning the same memories for similar queries.

### Implementation Status (2026-04-12)

The retrieval pipeline as implemented differs from the original design in several ways:

- **Tier 1** is fully implemented: BM25 (Tantivy) + SPLADE v3 (sentence-transformers SparseEncoder, naver/splade-v3, 110M params) + RRF fusion. SPLADE uses asymmetric encoding (`encode_document()` at ingest, `encode_query()` at search) with MPS/CUDA auto-detection.
- **Tier 2** is fully implemented: NetworkX knowledge graph with GLiNER entity extraction (urchade/gliner_medium-v2.1, 209M params). Graph spreading activation uses BFS traversal with per-hop decay, PMI-weighted co-occurrence edges, and IDF-weighted entity matching. Co-occurrence edges persist to SQLite `relationships` table.
- **Tier 3** was redesigned: LLM-as-Judge (Section 4 above) was **not implemented**. Instead, a 22M-parameter cross-encoder reranker (`cross-encoder/ms-marco-MiniLM-L-6-v2`, not an LLM) provides Tier 3 reranking. It runs selectively based on classified intent — enabled for fact_lookup, pattern_lookup, strategic_reflection; disabled for temporal/state queries where it destroys ordering. No LLM is used at query time.
- **Scoring** is fully implemented: ACT-R with Jaccard-normalized spreading activation, per-query min-max normalization of all signals, and reconciliation penalties on the combined score. ACT-R weight defaults to 0.0 (activates after dream cycles build differential access patterns).
- **Additional signals** added post-design: intent classification (7 classes via BM25 exemplar index), hierarchy bonus, temporal scoring, domain-scoped filtering.
- **Structured recall** wraps the full search pipeline with entity state snapshots, episode context, causal chains, and document section expansion.
- **Content-aware ingestion**: two-class gate (ATOMIC vs NAVIGABLE) routes content. NAVIGABLE documents produce a single document profile memory + sections in the document store. Content-hash dedup at the store boundary.

See `CLAUDE.md` Key Design Decisions #1-26 and `docs/ncms-resilience-update.md` for full implementation details.

### Tier 4 (optional): Temporal Linguistic Geometry

Added in P1 (2026-04), Temporal Linguistic Geometry (TLG)
provides a **structural-proof retrieval layer** that composes
with Tiers 1–3.  It is the canonical path for state-change /
temporal queries where lexical + semantic + graph scoring
cannot recover the correct answer — e.g. "what is the current
authentication scheme?", "what came before MFA?", "what
eventually led to passkeys?".  On a 32-query validation
corpus spanning 11 intent shapes, TLG delivers 32/32 top-5
and rank-1 vs. BM25's 41%/16%; see
`docs/completed/tlg-history/tlg-validation-findings.md`.

Gated behind `NCMS_TEMPORAL_ENABLED` (default `False`).

**Architecture.**

- **Grammar layer** (`domain/tlg/`) — pure, infrastructure-free.
  Retirement extractor, L1 vocabulary induction, L2 state-change
  markers, content markers, aliases, zones, structural query
  parser, shape cache, composition rules, four-level confidence
  label.  No dependencies beyond stdlib.
- **Application wiring** (`application/tlg/`) — `VocabularyCache`
  (L1 + aliases + domain nouns + content markers),
  `ShapeCacheStore` (persistent skeleton memo in
  `grammar_shape_cache` table, schema v12), `dispatch.retrieve_lg`
  (12-intent switch), `induction` (L2 marker pipeline).
- **O(1) entity index.**
  `SQLiteStore.find_memory_ids_by_entity` plus a stem-index in
  `InducedVocabulary` shrinks the `lookup_subject` /
  `lookup_entity` fast path from O(|vocab|) regex iteration to
  O(|query_words|) hash fetch + O(1) subset test.
- **Reconciliation extension.**
  `ReconciliationService._apply_supersedes` emits
  `retires_entities` on SUPERSEDES edges via the structural
  retirement extractor, so TLG zone computation is populated
  on ingest, not on query.

**Composition with BM25 / SPLADE / graph.**  TLG runs *alongside*
the Tier-1/2/3 pipeline via `MemoryService.search`.  On every
query:

1. The grammar layer produces an answer + confidence label.
2. If `has_confident_answer()` is true, TLG's rank-1 answer is
   composed onto the head of the BM25 ranking; the rest of the
   BM25 ordering is preserved verbatim.
3. If confidence is low or the grammar abstains, the BM25 +
   SPLADE + graph ranking is returned unchanged.

This composition satisfies a **zero-confidently-wrong invariant**
(Proposition 1 in `docs/temporal-linguistic-geometry.md` §3.4):
TLG never overrides BM25 with a confidently-wrong answer.
Abstention is a first-class primitive, not a failure mode.

**Observability.**  Every dispatch emits a `grammar.dispatched`
event (intent, subject, entity, confidence, grammar_answer, proof
preview) on the dashboard event log.  Every composition emits
`grammar.composed` tracking the bm25-vs-composed ranking delta.

**Maintenance.**  A periodic `tlg_induction` task refreshes the
L1 vocabulary + L2 markers + aliases so the cache doesn't
stale-drift as new memories land.  CLI commands `ncms tlg
status` and `ncms tlg induce` expose manual control.  The
`--tlg` flag on the LongMemEval benchmark flips
`NCMS_TEMPORAL_ENABLED` for benchmark runs.

**Scope note.**  TLG targets the *state-evolution* axis.  On
conversational corpora like LongMemEval — no state
declarations, no retirement markers — L1 induction yields 0
subjects, TLG falls through, and retrieval runs unchanged
through the Tier 1–3 pipeline.  LongMemEval therefore serves
as a non-regression check, not a headline benchmark.  The
at-scale benchmark on the state-evolution axis is the SWE
state-evolution corpus planned in `docs/p3-state-evolution-benchmark.md`.

**Deprecated in favour of TLG.**  The following modules carry
`DeprecationWarning` on use and will be removed one release
later: `domain/temporal/intent.py::classify_query_intent`,
`application/retrieval/apply_ordinal_ordering`,
`application/retrieval/apply_range_filter`,
`domain/tlg/query_classifier.py` (the pre-TLG heuristic
classifier).  The `temporal_range_filter_enabled` config flag
is retained as a baseline path and will retire with the next
major release.

### Tier 5 (ingest-side): Intent-Slot Distillation ✅ SHIPPED 2026-04-20

The ingest-side complement to TLG's query-side grammar: a
**LoRA multi-head classifier** that runs at `store_memory()`
time and replaces five brittle pattern-matching code paths with
one forward pass.  Shipped end-to-end in P2 Sprint 4; three
reference adapters (conversational / software_dev / clinical)
at F1 = 1.000 on gold across every head are published at
`~/.ncms/adapters/<domain>/v4/` (2.4 MB each).  Integration
findings:
[`intent-slot-sprint-4-findings.md`](intent-slot-sprint-4-findings.md).
Plan: [`p2-plan.md`](p2-plan.md).  Sprint 1–3 research:
[`intent-slot-sprints-1-3.md`](intent-slot-sprints-1-3.md).

Gated behind `NCMS_SLM_ENABLED` (default `False` at
ship; flips to `True` one release later).

**Architecture.**  One shared `bert-base-uncased` encoder +
per-deployment LoRA adapter + five classification heads:

| Head | Output | Replaces today |
|---|---|---|
| `intent_head` | positive / negative / habitual / difficulty / choice / none | Never-shipped regex preference extractor |
| `slot_head` | BIO tags over domain slot taxonomy | Regex slot fills |
| `topic_head` | Domain taxonomy label (e.g. `framework`, `medication`, `food_pref`) | `infrastructure/extraction/label_detector.py` (LLM topic detection) |
| `admission_head` | persist / ephemeral / discard | `application/admission_service.py` (4-feature heuristic) |
| `state_change_head` | declaration / retirement / none | `application/index_worker.py::_has_state_declaration` regex |

**One forward pass, 20–65 ms on MPS, 2.4 MB per adapter.**
Swap adapter = swap domain behaviour.  Topic output optionally
auto-populates `Memory.domains` (replacing the
"user-hands-us-a-domain-string" flow with
"SLM-classifies-content-against-learned-taxonomy").

**Composition with TLG.**  The `state_change_head` output is the
*ingest-side* signal that drives TLG's retirement extractor at
zone-induction time.  Two systems on different axes share one
fact: the classifier decides *whether* a memory is a state
transition; TLG decides *what the transition means structurally*.

**Fallback chain.**  Confidence-gated degradation:

```
JointLoraExtractor (custom adapter)
    ↓ if adapter missing / confidence < threshold
GlinerPlusE5Extractor (zero-shot, always available)
    ↓ if GLiNER unavailable
E5ZeroShotExtractor (pure E5)
    ↓ if E5 unavailable
heuristic null-output (old admission_service path)
```

Same zero-confidently-wrong invariant as TLG — the classifier
abstains rather than emit a confidently-wrong label.

**Per-deployment adaptation.**  Operators train their own
adapter against their corpus in one command:

```bash
ncms train-adapter --corpus ./my-docs \
  --taxonomy ./my-topics.yaml \
  --domain my_domain \
  --output ./adapters/my_domain/v1/
```

The CLI runs the four-phase pipeline (bootstrap → SDG expand →
adversarial augment → train + gate) and refuses to promote an
adapter that fails the gate.  See
[`intent-slot-sprints-1-3.md`](intent-slot-sprints-1-3.md) for
the gate design.

**Deprecated in favour of the SLM.**  One release after
`NCMS_SLM_ENABLED` defaults to true, the following
retire entirely: `application/admission_service.py`,
`application/index_worker._has_state_declaration`,
`infrastructure/extraction/label_detector.py`.

---

## 5. Embedded Knowledge Bus

The Knowledge Bus is the real-time coordination layer that enables osmotic knowledge sharing between agents. It is designed as an embedded, zero-dependency component that runs in-process with no external message brokers, databases, or services required. For multi-process or distributed deployments, the transport layer can optionally swap to Redis Pub/Sub or NATS, but the default mode is a Python asyncio EventEmitter that works anywhere Python runs.

### Design Principles

- **Non-blocking by default.** An agent asking a question gets an ask_id back immediately and continues its current task. Responses arrive asynchronously in the agent's inbox. Only "blocking" urgency asks pause the requesting agent.
- **Domain-routed, not agent-addressed.** Agents publish their expertise domains when they register. Asks are routed to domains, not to specific agents. The bus resolves which agents can answer. This decouples knowledge consumers from knowledge providers.
- **Osmotic absorption.** Announcements flow to subscribed agents without explicit queries. When the API agent changes an endpoint, it announces. Frontend agents subscribed to that domain absorb the change. Next time they need that knowledge, it is already in their inbox. They "just know."
- **Embedded first, distributed optional.** The default transport is an in-process Python asyncio event bus. No Redis. No Kafka. No NATS. For multi-process deployments, swap the transport adapter without changing any agent code.

### Bus Architecture

```
+--Agent A--+   +--Agent B--+   +--Agent C--+
| Provider: |   | Provider: |   | Consumer  |
| api:*     |   | db:*      |   | (frontend)|
| [Inbox]   |   | [Inbox]   |   | [Inbox]   |
+-----+-----+   +-----+-----+   +-----+-----+
      |               |               |
+-----v---------------v---------------v-----+
|            KNOWLEDGE BUS CORE              |
|  [Domain Registry] [Ask Router] [Inbox Mgr]|
|  [Subscription Mgr] [Announce Dispatcher]  |
+-------------------+------------------------+
                    |
+-------------------v------------------------+
|          TRANSPORT ADAPTER                  |
|  Default: AsyncIO EventEmitter (in-process) |
|  Optional: Redis Pub/Sub | NATS | Kafka    |
+---------------------------------------------+
```

### Core Interfaces

#### KnowledgeAsk: Non-Blocking Query

```python
@dataclass
class KnowledgeAsk:
    ask_id: str                    # Auto-generated UUID
    from_agent: str                # Requesting agent ID
    question: str                  # Natural language question
    domains: list[str]             # Routing hints: ["api:user-service"]
    urgency: Literal["blocking", "important", "background"]
    context: AskContext            # What the agent is currently doing
    response_format: str = "any"   # "openapi" | "typescript" | "json" | "any"
    ttl_ms: int = 30000           # Time-to-live. Stale after this.

@dataclass
class AskContext:
    current_task: str              # "Building React user profile component"
    relevant_code: str | None      # Optional: snippet of current work
    already_known: list[str]       # Prevent redundant info
```

#### KnowledgeResponse: Answering an Ask

```python
@dataclass
class KnowledgeResponse:
    ask_id: str                    # Correlates to the Ask
    from_agent: str                # Responding agent ID
    confidence: float              # 0.0-1.0
    knowledge: KnowledgePayload
    provenance: KnowledgeProvenance
    freshness: datetime            # When this knowledge was last verified
    source_mode: Literal["live", "warm", "cold"]
    snapshot_age_seconds: int | None      # None if live, age if warm/cold
    original_agent: str | None            # Who published the snapshot
    staleness_warning: str | None         # "Published 6h ago by api-agent"

@dataclass
class KnowledgePayload:
    type: Literal["interface-spec", "code-snippet", "configuration",
                  "architecture-decision", "constraint", "fact"]
    content: str                   # The actual knowledge (natural language)
    structured: dict | None        # Optional: OpenAPI spec, JSON schema, etc.
    references: list[str]          # File paths, URLs, doc links

@dataclass
class KnowledgeProvenance:
    source: Literal["direct-work", "memory-store", "documentation", "inferred"]
    last_verified: datetime
    trust_level: Literal["authoritative", "observed", "speculative"]
```

#### KnowledgeAnnounce: Proactive Broadcasting

```python
@dataclass
class KnowledgeAnnounce:
    announce_id: str
    from_agent: str
    event: Literal["created", "updated", "deprecated", "breaking-change"]
    domains: list[str]
    knowledge: KnowledgePayload
    impact: ImpactAssessment

@dataclass
class ImpactAssessment:
    breaking_change: bool
    affected_domains: list[str]    # Who else might care
    severity: Literal["info", "warning", "critical"]
    description: str               # Human-readable impact summary
```

#### KnowledgeBus: The Service Interface

```python
class KnowledgeBus(Protocol):
    # Registration
    def register_provider(self, agent_id: str, domains: list[str]) -> None: ...
    def update_availability(self, agent_id: str, status: str) -> None: ...

    # Ask (non-blocking by default)
    async def ask(self, ask: KnowledgeAsk) -> str:  # Returns ask_id immediately
        ...
    def on_response(self, ask_id: str, cb: Callable[[KnowledgeResponse], None]) -> None:
        ...

    # Announce (fire-and-forget broadcast)
    async def announce(self, announcement: KnowledgeAnnounce) -> None: ...

    # Subscribe (passive osmosis)
    def subscribe(self, agent_id: str, domains: list[str],
                  filter_policy: SubscriptionFilter | None = None) -> None: ...

    # Inbox (non-interrupting, check when ready)
    def get_inbox(self, agent_id: str) -> list[KnowledgeResponse]: ...
    def get_announcements(self, agent_id: str) -> list[KnowledgeAnnounce]: ...
    def drain_inbox(self, agent_id: str) -> list[KnowledgeResponse]: ...
```

### Transport Adapters

| Transport | Use Case | Dependencies | Latency |
|-----------|----------|-------------|---------|
| AsyncIO EventEmitter | Single-process, dev, testing | None (stdlib) | < 1ms |
| Redis Pub/Sub | Multi-process, same host | redis-py | 1-5ms |
| NATS | Distributed, multi-node | nats-py | 2-10ms |
| Kafka | Enterprise, durable, audit trail | confluent-kafka | 10-50ms |

### Scenario Walkthrough: Collaborative Coding

Consider a multi-agent coding team with three agents: a Frontend Agent building React components, an API Agent developing Express.js endpoints, and a Database Agent managing PostgreSQL schemas.

1. API Agent registers as provider for domains: **api:user-service, api:auth-service, api:payment-service**
2. Frontend Agent registers as provider for domains: ui:components, ui:pages, ui:state-management
3. Database Agent registers as provider for domains: db:user-schema, db:payment-schema, db:migrations
4. Frontend Agent is building a user profile component and needs the API contract. It issues a KnowledgeAsk with domains=["api:user-service"] and urgency="important". The Bus routes to the API Agent.
5. The API Agent's on_ask callback fires. Without stopping its current work (it is building a new endpoint), it checks its working memory, finds the OpenAPI spec for /profile, and sends a KnowledgeResponse with the structured spec.
6. The Frontend Agent's inbox receives the response. On its next planning step, it checks the inbox, extracts the TypeScript interface from the OpenAPI spec, and continues building the component with real types.
7. Later, the API Agent changes the /profile response shape. It issues a KnowledgeAnnounce with event="breaking-change" and domains=["api:user-service"]. The Frontend Agent's subscription catches it. The announcement sits in its inbox until its next review cycle.

---

## 6. NeMo Agent Template with Knowledge Callbacks

This section defines the KnowledgeAgent base class, a standardized template for NeMo Agent Toolkit agents that participate in the Knowledge Bus and Cognitive Memory System.

### Base Class: KnowledgeAgent

```python
from abc import ABC, abstractmethod
from nemo_agent_toolkit.agents import AgentBase
from nemo_agent_toolkit.memory import MemoryEditor, MemoryManager
from ncms.bus import KnowledgeBus, KnowledgeAsk, KnowledgeResponse,
                      KnowledgeAnnounce, KnowledgePayload
from ncms.memory import CognitiveMemoryEditor


class KnowledgeAgent(AgentBase, ABC):
    """
    Base class for NeMo agents that participate in the Knowledge Bus.
    Extends AgentBase with knowledge provider/consumer capabilities
    and automatic cognitive memory integration.
    """

    def __init__(self, agent_id: str, bus: KnowledgeBus,
                 memory: CognitiveMemoryEditor, **kwargs):
        super().__init__(**kwargs)
        self.agent_id = agent_id
        self._bus = bus
        self._memory = memory
        self._expertise_domains: list[str] = []
        self._subscriptions: list[str] = []

        # Register lifecycle hooks
        self._bus.register_provider(agent_id, self.declare_expertise())
        self._bus.subscribe(agent_id, self.declare_subscriptions())

        # Wire up the ask callback
        self._bus.on_ask(agent_id, self._handle_ask)

    # ──────────────────────────────────────────────
    # KNOWLEDGE DECLARATION (override in subclass)
    # ──────────────────────────────────────────────

    @abstractmethod
    def declare_expertise(self) -> list[str]:
        """Declare knowledge domains this agent can provide."""
        ...

    @abstractmethod
    def declare_subscriptions(self) -> list[str]:
        """Declare knowledge domains this agent wants to receive."""
        ...
```

### Knowledge Callbacks

The template provides four callback hooks that agents override to participate in knowledge exchange. These are designed to be lightweight and non-blocking.

#### on_ask: Responding to Knowledge Queries

```python
    async def on_ask(self, ask: KnowledgeAsk) -> KnowledgeResponse | None:
        """
        Called when another agent asks a question matching your expertise.
        Return a KnowledgeResponse if you have relevant knowledge,
        or None to indicate you cannot answer.

        This runs in a background task. Your main work loop is not
        interrupted. Default implementation checks memory store.
        """
        results = await self._memory.search(
            query=ask.question,
            domains=ask.domains,
            limit=5,
        )
        if results:
            return KnowledgeResponse(
                ask_id=ask.ask_id,
                from_agent=self.agent_id,
                confidence=results[0].score,
                knowledge=KnowledgePayload(
                    type="fact",
                    content=results[0].content,
                    references=results[0].references,
                ),
                provenance=KnowledgeProvenance(
                    source="memory-store",
                    last_verified=results[0].last_accessed,
                    trust_level="observed",
                ),
                freshness=results[0].last_accessed,
            )
        return None
```

#### on_announcement: Absorbing Broadcast Knowledge

```python
    async def on_announcement(self, announcement: KnowledgeAnnounce) -> None:
        """
        Called when a subscribed domain receives an announcement.
        Default implementation stores in cognitive memory for later use.
        Override to add custom processing (e.g., invalidate cache,
        trigger rebuild, flag for human review).
        """
        await self._memory.add_items([{
            "content": announcement.knowledge.content,
            "structured": announcement.knowledge.structured,
            "domains": announcement.domains,
            "event_type": announcement.event,
            "source_agent": announcement.from_agent,
            "impact": announcement.impact.description,
            "is_breaking": announcement.impact.breaking_change,
        }])

        if announcement.impact.breaking_change:
            self._flag_breaking_change(announcement)
```

#### on_inbox_ready: Processing Queued Knowledge

```python
    async def on_inbox_ready(self) -> None:
        """
        Called during the agent's planning phase (between task steps).
        Processes any queued responses and announcements.
        """
        responses = self._bus.drain_inbox(self.agent_id)
        for response in responses:
            await self._integrate_response(response)

        announcements = self._bus.get_announcements(self.agent_id)
        for ann in announcements:
            await self.on_announcement(ann)
```

#### Convenience Methods: ask and announce

```python
    async def ask_knowledge(self, question: str, domains: list[str],
                            urgency: str = "important",
                            response_format: str = "any") -> str:
        """
        Ask the knowledge network a question. Returns ask_id.
        Response will arrive in inbox asynchronously.
        """
        ask = KnowledgeAsk(
            ask_id=str(uuid4()),
            from_agent=self.agent_id,
            question=question,
            domains=domains,
            urgency=urgency,
            context=self._build_context(),
            response_format=response_format,
        )
        return await self._bus.ask(ask)

    async def announce_knowledge(self, event: str, domains: list[str],
                                 content: str, structured: dict = None,
                                 breaking: bool = False,
                                 severity: str = "info") -> None:
        """Broadcast knowledge to the network."""
        announcement = KnowledgeAnnounce(
            announce_id=str(uuid4()),
            from_agent=self.agent_id,
            event=event,
            domains=domains,
            knowledge=KnowledgePayload(
                type="interface-spec" if structured else "fact",
                content=content,
                structured=structured,
            ),
            impact=ImpactAssessment(
                breaking_change=breaking,
                affected_domains=domains,
                severity=severity,
                description=content,
            ),
        )
        await self._bus.announce(announcement)
```

### Example: API Agent Implementation

```python
class ApiAgent(KnowledgeAgent):
    """Agent responsible for building and maintaining API endpoints."""

    def declare_expertise(self) -> list[str]:
        return ["api:user-service", "api:auth-service",
                "api:user-service:endpoints", "api:auth-service:endpoints"]

    def declare_subscriptions(self) -> list[str]:
        return ["db:user-schema", "db:auth-schema",
                "config:environment"]

    async def on_ask(self, ask: KnowledgeAsk) -> KnowledgeResponse | None:
        if "user-service" in str(ask.domains):
            spec = self._get_current_openapi_spec("user-service")
            if spec:
                return KnowledgeResponse(
                    ask_id=ask.ask_id,
                    from_agent=self.agent_id,
                    confidence=0.95,
                    knowledge=KnowledgePayload(
                        type="interface-spec",
                        content=f"OpenAPI spec for user-service",
                        structured=spec,
                        references=["src/api/user-service/openapi.yaml"],
                    ),
                    provenance=KnowledgeProvenance(
                        source="direct-work",
                        last_verified=datetime.now(),
                        trust_level="authoritative",
                    ),
                    freshness=datetime.now(),
                )
        return await super().on_ask(ask)

    async def on_announcement(self, ann: KnowledgeAnnounce) -> None:
        if ann.impact.breaking_change and "db:" in str(ann.domains):
            self._flag_for_review(
                f"Schema change in {ann.domains}: {ann.impact.description}"
            )
        await super().on_announcement(ann)
```

---

## 7. Memory Lifecycle and Consolidation

### Ingestion

When a memory enters the system (via agent interaction, Knowledge Bus announcement, or direct API call), it undergoes structured extraction. The LLM extracts entities, relationships, topics, and an importance score (1 to 10). The memory is stored as a structured record in the sparse index and as nodes/edges in the knowledge graph. Raw text is never stored as the primary representation; structured records are the unit of storage.

### Active Use and Activation Tracking

Every retrieval updates the memory's access history: timestamp, accessing agent, and query context. These access events feed the ACT-R base-level activation function. Memories that are accessed frequently and recently maintain high activation. Memories that go unaccessed decay according to the power law of forgetting.

### Consolidation Pipeline

Unlike Google's fixed 30-minute consolidation timer, NCMS triggers consolidation based on importance-score thresholds. When the accumulated importance scores of unconsolidated memories exceeds a configurable threshold (default: 50), the consolidation worker runs. Consolidation performs three operations:

1. **Cross-linking:** The LLM examines new memories against existing ones and creates relationship edges in the knowledge graph. "API Agent changed /profile endpoint" gets linked to "Frontend Agent uses /profile endpoint" via a DEPENDS_ON relationship.
2. **Abstraction:** Episodic memories (specific events and interactions) are generalized into semantic knowledge (reusable facts). Multiple memories about "added field X to endpoint Y" consolidate into a general understanding of that endpoint's current shape.
3. **Contradiction Detection:** The LLM compares new facts against existing knowledge. If a new memory claims "/profile returns {name, email}" but existing knowledge says "/profile returns {name, email, avatar_url}", the contradiction is flagged with provenance metadata showing which is more recent and from a more authoritative source.

### Decay and Pruning

Memories with activation below a configurable threshold (default: -2.0) are candidates for pruning. Before deletion, the decay engine checks whether the memory has any exclusive knowledge (facts not captured elsewhere in the graph). If so, the memory's key facts are extracted and merged into a more general semantic node before the original episodic memory is pruned. This ensures no knowledge is lost, only the verbose episodic wrapper.

### Implementation Status (2026-04-12)

Consolidation is fully implemented with three batch passes:
- **Episode summaries (5A)**: Synthesize closed episodes into searchable narratives via LLM. Feature-flagged: `NCMS_EPISODE_CONSOLIDATION_ENABLED`.
- **State trajectories (5B)**: Generate temporal progression narratives for entities with ≥N state transitions. Feature-flagged: `NCMS_TRAJECTORY_CONSOLIDATION_ENABLED`.
- **Recurring patterns (5C)**: Cluster episode summaries by entity Jaccard overlap, promote stable clusters to `strategic_insight`. Feature-flagged: `NCMS_PATTERN_CONSOLIDATION_ENABLED`.

Dream cycles (Phase 8) add three non-LLM passes: rehearsal (synthetic access injection), PMI association learning from search logs, and importance drift. Feature-flagged: `NCMS_DREAM_CYCLE_ENABLED`.

A maintenance scheduler (`application/maintenance_scheduler.py`) runs consolidation, dream cycles, episode closure, and decay passes on configurable background intervals. CLI: `ncms maintenance status|run`. Feature-flagged: `NCMS_MAINTENANCE_ENABLED`.

---

## 8. Deployment Modes

### Embedded Mode (Zero Dependencies)

For development, testing, and single-agent deployments, NCMS runs entirely in-process:

- Storage: SQLite for structured memory records, Tantivy for the sparse inverted index, NetworkX for the knowledge graph
- Knowledge Bus: AsyncIO EventEmitter, in-process only
- LLM: Local model via llama.cpp, ONNX Runtime, or HTTP call to NIM
- SPLADE: ONNX-optimized checkpoint for CPU inference, or GPU via PyTorch

Installation is a single pip install with no external services required. The entire system initializes in under 3 seconds.

### Scaled Mode (NIM-Compatible Container)

For production multi-agent deployments, NCMS packages as a Docker container following NIM conventions:

- Storage: Milvus (sparse vector index with GPU acceleration), Neo4j or FalkorDB (knowledge graph), Redis (KV cache and pub/sub transport)
- Knowledge Bus: Redis Pub/Sub or NATS for cross-process communication
- LLM: NIM-hosted model for consolidation, scoring, and reasoning
- SPLADE: NIM-hosted or GPU-local for expansion and scoring

### Configuration

```yaml
# ncms_config.yaml
ncms:
  mode: embedded  # or "scaled"

  storage:
    backend: sqlite  # sqlite | milvus | postgres
    path: ./ncms_data/memories.db

  graph:
    backend: networkx  # networkx | neo4j | falkordb

  sparse_index:
    backend: tantivy  # tantivy | elasticsearch | opensearch
    splade_model: naver/splade-cocondenser-ensembledistil
    device: auto  # auto | cpu | cuda

  knowledge_bus:
    transport: asyncio  # asyncio | redis | nats | kafka

  retrieval:
    tier1_candidates: 50
    tier2_candidates: 20
    tier3_judge_top_k: 10
    act_r_decay: 0.5
    act_r_temperature: 0.4

  consolidation:
    importance_threshold: 50
    llm_model: meta/llama-3.1-8b
    contradiction_check: true

  llm:
    provider: nim  # nim | local | openai-compatible
```

---

## 9. Knowledge Persistence and Surrogate Response

A fundamental challenge in multi-agent systems is liveness: the agent that holds critical knowledge may be offline, sleeping, or not yet started. A frontend developer using Copilot at 2 AM should still be able to query the API contract even though the API agent last ran during business hours.

### Three Knowledge Availability Modes

| Mode | Description | Response Source | Freshness Guarantee |
|------|-------------|----------------|-------------------|
| **Live** | Agent is running and registered on the Knowledge Bus | Agent responds directly via on_ask callback | Real-time. Agent can check current working state. |
| **Warm** | Agent is offline but published a Knowledge Snapshot before sleeping | Memory Core responds as surrogate using the snapshot | As of last snapshot timestamp. May be hours or days old. |
| **Cold** | No agent has ever published to this domain, or snapshot expired | Memory Core searches general memory store for any relevant knowledge | Unknown. Best-effort from historical interactions. |

### Knowledge Snapshots: The Last Will Pattern

A Knowledge Snapshot is a structured export of an agent's current working knowledge, published to the Memory Core before the agent goes offline. Think of it as the agent's "last will and testament."

Snapshots are triggered in three ways:

- **Lifecycle hooks (on_suspend, on_shutdown).** When an agent is shutting down gracefully or being suspended, the KnowledgeAgent base class automatically calls publish_snapshot().
- **Periodic heartbeat (configurable interval).** Long-running agents publish incremental snapshots at configurable intervals (default: every 15 minutes). Only knowledge that has changed since the last snapshot is published. This protects against ungraceful termination.
- **Explicit publish (agent-initiated).** An agent can call publish_snapshot() at any time, for example after completing a major task or making a breaking change.

#### Snapshot Structure

```python
@dataclass
class KnowledgeSnapshot:
    snapshot_id: str
    agent_id: str
    timestamp: datetime
    domains: list[str]
    entries: list[SnapshotEntry]
    is_incremental: bool = False
    supersedes: str | None = None
    ttl_hours: int = 168                  # Default: 7 days

@dataclass
class SnapshotEntry:
    domain: str                           # "api:user-service:profile"
    knowledge: KnowledgePayload
    confidence: float                     # 0.0-1.0
    last_verified: datetime
    volatility: Literal["stable", "changing", "volatile"]
    #   stable   = unlikely to change (architecture decisions, conventions)
    #   changing = changes occasionally (API contracts, schema definitions)
    #   volatile = changes frequently (config values, feature flags)
```

### Agent Lifecycle Hooks

```python
class KnowledgeAgent(AgentBase, ABC):

    async def on_startup(self) -> None:
        """Called when agent starts. Load previous snapshot if available."""
        previous = await self._memory.get_latest_snapshot(self.agent_id)
        if previous:
            self._restore_from_snapshot(previous)
        self._bus.register_provider(self.agent_id, self.declare_expertise())

    async def on_suspend(self) -> None:
        """Called before agent goes to sleep. Publish snapshot."""
        await self.publish_snapshot(reason="suspend")
        self._bus.update_availability(self.agent_id, "offline")

    async def on_shutdown(self) -> None:
        """Called on graceful shutdown. Publish final snapshot."""
        await self.publish_snapshot(reason="shutdown")
        self._bus.deregister_provider(self.agent_id)

    async def publish_snapshot(self, reason: str = "periodic") -> str:
        """Serialize current working knowledge and publish to Memory Core."""
        entries = await self.collect_working_knowledge()
        snapshot = KnowledgeSnapshot(
            snapshot_id=str(uuid4()),
            agent_id=self.agent_id,
            timestamp=datetime.now(),
            domains=self.declare_expertise(),
            entries=entries,
            supersedes=self._last_snapshot_id,
        )
        await self._memory.store_snapshot(snapshot)
        self._last_snapshot_id = snapshot.snapshot_id
        return snapshot.snapshot_id

    @abstractmethod
    async def collect_working_knowledge(self) -> list[SnapshotEntry]:
        """Override to declare what your agent currently knows."""
        ...
```

### Surrogate Response Priority

1. **Live agent responds (Mode: Live).** If any agent registered for the requested domain is online, route to that agent.
2. **Snapshot-based surrogate (Mode: Warm).** If no live agent matches but a Knowledge Snapshot exists, the Memory Core reads the relevant SnapshotEntry and wraps it in a KnowledgeResponse with source_mode="warm".
3. **General memory search (Mode: Cold).** If no snapshot exists, fall back to standard memory search. Response carries source_mode="cold" and lower confidence.

---

## 10. MCP Server Integration

The MCP server makes the entire NCMS system available to any MCP-compatible client: Copilot, Cursor, Claude Code, VS Code extensions, or any external agent framework.

### MCP Tool Surface

| MCP Tool | Description | Maps To |
|----------|-------------|---------|
| search_memory | Search cognitive memory with SPLADE + graph + LLM pipeline | Memory Core retrieval |
| store_memory | Store a new memory with automatic entity extraction | Memory Core ingestion |
| ask_knowledge | Non-blocking ask routed to live agents or surrogate | Knowledge Bus ask |
| ask_knowledge_sync | Blocking variant that waits for response (with timeout) | Knowledge Bus ask (blocking) |
| announce_knowledge | Broadcast a knowledge update to all subscribed agents | Knowledge Bus announce |
| commit_knowledge | Store knowledge learned during a coding session | Memory Core ingestion (coding-optimized) |
| get_provenance | Trace origin, modification history, confidence chain | Memory Core provenance |
| list_domains | List all registered knowledge domains with availability mode | Knowledge Bus registry |
| get_snapshot | Retrieve latest Knowledge Snapshot for agent or domain | Memory Core snapshots |

### MCP Resources

```
ncms://domains                          # List all knowledge domains
ncms://domains/{domain}/status          # Live/warm/cold status
ncms://domains/{domain}/snapshots       # All snapshots for a domain
ncms://agents                           # List all registered agents
ncms://agents/{agent_id}/status         # Agent availability and last snapshot
ncms://agents/{agent_id}/expertise      # Domains this agent provides
ncms://agents/{agent_id}/snapshot       # Latest snapshot
ncms://graph/entities                   # Browse knowledge graph entities
ncms://graph/entities/{entity}/related  # Related entities and relationships
ncms://memories/{memory_id}             # Individual memory with provenance
ncms://memories/{memory_id}/provenance  # Provenance chain
```

### Copilot Scenario: End-to-End Flow

```
Copilot/MCP Client                      NCMS MCP Server
       |                                      |
       |  ask_knowledge_sync(                  |
       |    question="user-service /profile?", |
       |    domains=["api:user-service"]        |
       |  )                                    |
       |--------------------------------------->|
       |                                      |
       |    1. Check Knowledge Bus: any live   |
       |       agent for api:user-service?     |
       |       -> NO (API agent is sleeping)   |
       |                                      |
       |    2. Check snapshots: any snapshot   |
       |       for api:user-service domain?    |
       |       -> YES (6h old, from api-agent) |
       |                                      |
       |  <-- KnowledgeResponse(              |
       |        source_mode="warm",            |
       |        snapshot_age=21600s,           |
       |        original_agent="api-agent",    |
       |        knowledge={openapi_spec...},   |
       |        staleness_warning=              |
       |         "From api-agent snapshot 6h ago"|
       |      )                               |
```

### MCP Server Configuration

```yaml
ncms:
  mcp:
    enabled: true
    transport: stdio        # stdio (for Claude/Cursor) or http (for remote)
    http_port: 8080
    auth:
      enabled: false
      method: bearer
    surrogate:
      enabled: true
      max_snapshot_age_hours: 168
      cold_fallback: true
```

---

## 11. Coding Agent Integration: Hooks and Commit Patterns

Claude Code, Copilot, Cursor, and similar coding agents do not extend KnowledgeAgent. They connect via MCP, do work, and disconnect. They need a simple, low-friction mechanism to persist what they learned. NCMS supports this through two complementary patterns: an explicit MCP tool (commit_knowledge) and pre-built hook configurations for both Claude Code and GitHub Copilot.

### The commit_knowledge MCP Tool

```
Input Schema:
  content: string          # Required. What was learned or changed.
  domains: list[string]    # Optional. Knowledge domains.
  type: string             # Optional. "interface-spec" | "architecture-decision" |
                           # "code-pattern" | "configuration" | "convention" |
                           # "bug-fix" | "dependency" | "fact"
  structured: object       # Optional. OpenAPI spec, JSON schema, etc.
  project: string          # Optional. Project/repo context.
  tags: list[string]       # Optional. Free-form tags.
  session_id: string       # Optional. Links to a specific coding session.

Output:
  memory_id: string
  entities_extracted: int
  domains_detected: list
```

Minimal and rich usage examples:

```python
# Minimal: just tell NCMS what you learned
commit_knowledge(
  content="The user-service /profile endpoint now returns avatar_url as a string field"
)

# Rich: provide structured data for high-fidelity storage
commit_knowledge(
  content="Updated OpenAPI spec for user-service",
  domains=["api:user-service:profile"],
  type="interface-spec",
  structured={"openapi": "3.0", "paths": {"/profile": {...}}},
  project="acme-platform",
  tags=["api", "user-service", "v2.3"]
)
```

### Claude Code Hook Configuration

NCMS ships a ready-to-use Claude Code hook configuration for `.claude/settings.json`:

| Hook Event | When It Fires | What Gets Committed |
|------------|---------------|-------------------|
| Stop | Claude finishes responding (task complete) | Summary of accomplishments, files modified, decisions made |
| TaskCompleted | A subagent finishes a delegated task | Task outcome and new knowledge |
| PreCompact | Before context window compaction | Full session knowledge dump. Critical: compaction destroys context. |
| SessionEnd | Session ends (exit, sigint, crash) | Final summary, unfinished work, pending decisions |

```json
// .claude/settings.json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [{
          "type": "command",
          "command": "ncms-commit-hook --event stop --transcript $CLAUDE_TRANSCRIPT_PATH"
        }]
      }
    ],
    "PreCompact": [
      {
        "hooks": [{
          "type": "command",
          "command": "ncms-commit-hook --event pre-compact --transcript $CLAUDE_TRANSCRIPT_PATH"
        }]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [{
          "type": "command",
          "command": "ncms-commit-hook --event session-end --transcript $CLAUDE_TRANSCRIPT_PATH"
        }]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [{
          "type": "command",
          "command": "ncms-commit-hook --event file-changed --tool-input"
        }]
      }
    ]
  }
}
```

### GitHub Copilot Coding Agent Hook Configuration

GitHub Copilot's coding agent supports hooks via a `hooks.json` file in `.github/hooks/` on the repository's default branch. Copilot hooks support sessionStart, sessionEnd, userPromptSubmitted, preToolUse, postToolUse, and errorOccurred events with separate bash and powershell command keys.

```json
// .github/hooks/ncms-hooks.json
{
  "version": 1,
  "hooks": {
    "sessionStart": [
      {
        "type": "command",
        "bash": "ncms-context-loader --project $(pwd)",
        "powershell": "ncms-context-loader --project $PWD",
        "cwd": ".",
        "timeoutSec": 15
      }
    ],
    "sessionEnd": [
      {
        "type": "command",
        "bash": "ncms-commit-hook --event session-end",
        "powershell": "ncms-commit-hook --event session-end",
        "cwd": ".",
        "timeoutSec": 30
      }
    ],
    "postToolUse": [
      {
        "type": "command",
        "bash": "ncms-commit-hook --event post-tool",
        "powershell": "ncms-commit-hook --event post-tool",
        "cwd": ".",
        "timeoutSec": 10,
        "env": {
          "NCMS_DEPTH": "shallow"
        }
      }
    ],
    "errorOccurred": [
      {
        "type": "command",
        "bash": "ncms-commit-hook --event error",
        "powershell": "ncms-commit-hook --event error",
        "cwd": ".",
        "timeoutSec": 10
      }
    ]
  }
}
```

The ncms-commit-hook script is the same for both agents. It reads JSON from stdin, detects which agent is calling based on the input shape, and routes to the appropriate extraction logic. Copilot hooks live in the repository (shared across the team via the default branch) while Claude Code hooks can be per-user or per-project. For team-wide NCMS integration, the Copilot `.github/hooks/` path is ideal.

### Hook Compatibility Matrix

| NCMS Event | Claude Code Hook | Copilot Hook |
|------------|-----------------|--------------|
| Load context on start | SessionStart | sessionStart |
| Commit on task complete | Stop | sessionEnd (nearest equivalent) |
| Commit before compaction | PreCompact | N/A (Copilot manages context internally) |
| Commit on session end | SessionEnd | sessionEnd |
| Track file changes | PostToolUse (matcher: Write\|Edit) | postToolUse |
| Log errors for debugging | PostToolUseFailure | errorOccurred |
| Gate dangerous operations | PreToolUse (exit code 2 = deny) | preToolUse (exit code 1 = block) |

### The ncms-commit-hook Script

```python
# ncms-commit-hook (simplified pseudocode)
# Installed via: pip install ncms-tools  /  npx @ncms/commit-hook

def handle_stop(transcript_path):
    last_exchange = read_last_exchange(transcript_path)
    knowledge = extract_knowledge(last_exchange, depth="shallow")
    ncms_client.commit_knowledge(knowledge)

def handle_pre_compact(transcript_path):
    # CRITICAL: Compaction destroys context. Extract everything.
    full_transcript = read_full_transcript(transcript_path)
    knowledge = extract_knowledge(full_transcript, depth="deep")
    ncms_client.commit_knowledge(knowledge)

def handle_session_end(transcript_path):
    full_transcript = read_full_transcript(transcript_path)
    knowledge = extract_knowledge(full_transcript, depth="deep")
    pending = extract_pending_work(full_transcript)
    if pending:
        ncms_client.commit_knowledge(pending, type="pending-work")
    ncms_client.commit_knowledge(knowledge)

def handle_file_changed(tool_input):
    file_path = tool_input.get("file_path")
    ncms_client.commit_knowledge(
        content=f"Modified {file_path}",
        type="code-pattern",
        tags=["file-change"],
    )
```

### SessionStart: Reloading Context

```json
// SessionStart hook for Claude Code in .claude/settings.json
"SessionStart": [
  {
    "hooks": [{
      "type": "command",
      "command": "ncms-context-loader --project $CLAUDE_PROJECT_DIR"
    }]
  }
]
```

The ncms-context-loader outputs to stdout, which the agent automatically injects as session context. It queries NCMS for: recent knowledge commits for this project, pending/unfinished work from previous sessions, breaking change announcements since last session, and relevant architecture decisions and conventions.

This creates a full cycle: SessionStart loads context from NCMS, the coding session accumulates knowledge, and Stop/PreCompact/SessionEnd commits it back. The next session starts with everything the previous session learned.

### Generic MCP Pattern (Cursor, Other Agents)

For coding agents that do not support hooks, the MCP tool description itself acts as the prompt:

```yaml
commit_knowledge:
  description: |
    Store knowledge learned during this session for future retrieval.
    Call this tool when you have completed a task, made an architecture
    decision, discovered a pattern, changed an interface, or learned
    something that would be useful in future sessions.
    Include structured data (OpenAPI specs, TypeScript interfaces,
    JSON schemas) when available.
```

---

## 12. Storage Architecture and Rehydration

If NCMS restarts, how does it rebuild the in-memory knowledge graph, the sparse index, and the bus state? The answer depends on a clear separation between what is durable and what is derived.

### Durable vs. Derived: The Storage Map

| Component | Durability | Storage Backend | On Restart |
|-----------|-----------|-----------------|------------|
| Memory Records | Durable | SQLite / Postgres | Source of truth. Loaded directly. |
| Knowledge Graph | Derived | In-memory (NetworkX) | Rebuilt from memory records and relationship table. |
| SPLADE Index | Durable | Tantivy (file-based) | Persists to disk. Loaded on startup. |
| Knowledge Snapshots | Durable | SQLite / Postgres | Source of truth for surrogate responses. |
| ACT-R Access History | Durable | SQLite / Postgres | Rebuilt into activation scores on startup. |
| Entity Registry | Durable | SQLite / Postgres | Used to rebuild graph nodes. |
| Relationship Table | Durable | SQLite / Postgres | Used to rebuild graph edges. |
| Bus Registrations | Ephemeral | In-memory only | Rebuilt as agents register on startup. |
| Bus Inboxes | Ephemeral | In-memory only | Lost on restart. |
| Consolidation State | Durable | SQLite / Postgres | Accumulated importance scores persisted. |

### SQLite Schema (Embedded Mode)

```sql
-- Core memory records
CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    structured JSON,
    type TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 5,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    source_agent TEXT,
    project TEXT,
    domains JSON NOT NULL DEFAULT '[]',
    tags JSON DEFAULT '[]'
);

-- Entity registry (nodes in the knowledge graph)
CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    attributes JSON DEFAULT '{}',
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
CREATE INDEX idx_entities_name ON entities(name);
CREATE INDEX idx_entities_type ON entities(type);

-- Relationships (edges in the knowledge graph)
CREATE TABLE relationships (
    id TEXT PRIMARY KEY,
    source_entity TEXT NOT NULL REFERENCES entities(id),
    target_entity TEXT NOT NULL REFERENCES entities(id),
    type TEXT NOT NULL,
    valid_at TIMESTAMP,
    invalid_at TIMESTAMP,
    source_memory TEXT REFERENCES memories(id),
    created_at TIMESTAMP NOT NULL
);
CREATE INDEX idx_rel_source ON relationships(source_entity);
CREATE INDEX idx_rel_target ON relationships(target_entity);

-- Memory-to-entity links
CREATE TABLE memory_entities (
    memory_id TEXT NOT NULL REFERENCES memories(id),
    entity_id TEXT NOT NULL REFERENCES entities(id),
    PRIMARY KEY (memory_id, entity_id)
);

-- ACT-R access history
CREATE TABLE access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL REFERENCES memories(id),
    accessed_at TIMESTAMP NOT NULL,
    accessing_agent TEXT,
    query_context TEXT
);
CREATE INDEX idx_access_memory ON access_log(memory_id, accessed_at);

-- Knowledge snapshots
CREATE TABLE snapshots (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    domains JSON NOT NULL,
    entries JSON NOT NULL,
    is_incremental BOOLEAN DEFAULT FALSE,
    supersedes TEXT,
    ttl_hours INTEGER DEFAULT 168,
    created_at TIMESTAMP NOT NULL
);
CREATE INDEX idx_snapshots_agent ON snapshots(agent_id, timestamp DESC);
CREATE INDEX idx_snapshots_domains ON snapshots(domains);

-- Consolidation state
CREATE TABLE consolidation_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    accumulated_importance REAL DEFAULT 0,
    last_consolidation_at TIMESTAMP,
    last_decay_pass_at TIMESTAMP
);
```

### Rehydration Process

On startup, NCMS rebuilds all derived structures from the durable SQLite store:

1. **Load consolidation state.** Read the singleton consolidation_state row.
2. **Rebuild knowledge graph.** Query all entities and relationships. Create NetworkX nodes and directed edges with bi-temporal metadata. For edges where invalid_at is set, mark as historical. For 10,000 memories with 50,000 relationships: approximately 2 to 5 seconds.
3. **Verify SPLADE/Tantivy index.** Tantivy persists its inverted index to disk. Verify document count matches memory table. If they diverge (crash during write), perform incremental re-index of the delta.
4. **Load snapshots for surrogate response.** Query the most recent non-expired snapshot per agent/domain combination. Hold in memory for fast surrogate resolution.
5. **Precompute ACT-R base-level activations.** For each memory, query access_log entries and compute activation. Cache in memory, update incrementally.
6. **Start MCP server and Knowledge Bus.** Bus starts empty. Agents populate via register_provider(). MCP server is immediately available for surrogate responses from snapshots.

### Startup Time Budget

| Step | Time (10K memories) | Notes |
|------|-------------------|-------|
| Load consolidation state | < 1ms | Single row read |
| Rebuild knowledge graph | 2-5s | Bulk load from SQLite into NetworkX |
| Verify Tantivy index | 50-200ms | File stat + count comparison |
| Re-index delta (if needed) | 0-30s | Only for crash recovery; typically 0 |
| Load snapshots | 10-50ms | One query per agent |
| Precompute ACT-R activations | 1-3s | Aggregate access_log per memory |
| Start MCP server + Bus | < 100ms | Async server startup |
| **TOTAL (clean start)** | **3-8s** | **Normal startup** |
| **TOTAL (crash recovery)** | **5-40s** | **Includes delta re-indexing** |

### Implementation Status (2026-04-12)

The storage schema has grown from the 7-table design above to 27 tables (schema version 9, single-pass creation). Key additions since the original design:

| Table | Purpose |
|-------|---------|
| `memory_nodes` | HTMG typed nodes (atomic, entity_state, episode, abstract) + bitemporal fields |
| `graph_edges` | Typed directed edges in the HTMG |
| `ephemeral_cache` | Short-lived entries below admission threshold |
| `search_log` | Query → result associations for PMI computation (dream cycles) |
| `association_strengths` | Learned entity co-occurrence strengths (PMI-based) |
| `documents` | Full document content + sections (parent_doc_id links) |
| `document_links` | Typed links between documents (derived_from, supersedes) |
| `dashboard_events` | SSE event stream persistence for observability |
| `projects` | NemoClaw project tracking |
| + 10 more | Pipeline events, review scores, approvals, guardrails, grounding, LLM calls, agent configs, bus conversations, pending approvals, users |

The `memories` table gained a `content_hash TEXT` column for dedup. Rehydration now also loads co-occurrence edges from the `relationships` table and association strengths from `association_strengths` into the NetworkX graph on startup.

Co-occurrence edges are persisted during `store_memory()` (not just built in-memory), so the graph survives container restarts. The Document Store (`documents` table) stores full document content and sections separately from the memory store, with the Document Profile model providing a single vocabulary-dense profile memory for BM25/SPLADE indexing.

### Scaled Mode: Persistent Graph

In scaled mode, the knowledge graph is stored in Neo4j or FalkorDB rather than rebuilt in-memory. This eliminates the graph rehydration step entirely. The trade-off is that graph queries go over the network (1 to 5ms per traversal). For deployments with more than 100,000 memories, the persistent graph backend is recommended.

### Backup and Disaster Recovery

In embedded mode, the entire NCMS state is contained in two locations: the SQLite database file and the Tantivy index directory. A minimal backup requires only the SQLite file (first startup after restore will re-index, adding 10 to 30 seconds).

For scaled mode, standard database backup procedures apply: Neo4j dump, PostgreSQL pg_dump, Elasticsearch snapshots. NCMS provides an `ncms-backup` CLI tool that orchestrates all backends into a consistent snapshot.

---

## 13. Implementation Roadmap

### Phase 1: Foundation (Weeks 1 to 4)

Deliver the embedded-mode core with Knowledge Bus, basic memory CRUD, Knowledge Persistence lifecycle, and the NeMo agent template.

- Implement KnowledgeBus with AsyncIO transport and domain routing
- Build CognitiveMemoryEditor implementing NAT's MemoryEditor interface
- Create KnowledgeAgent base class with all four callback hooks plus lifecycle hooks (on_startup, on_suspend, on_shutdown)
- Implement Knowledge Snapshot serialization and surrogate response resolution (live/warm/cold)
- SQLite storage backend with Tantivy sparse index
- Basic SPLADE integration (ONNX checkpoint, CPU inference)
- MCP server with stdio transport exposing core tools (search, store, ask_knowledge_sync, commit_knowledge)
- ncms-commit-hook script for Claude Code Stop/PreCompact/SessionEnd events
- ncms-context-loader script for Claude Code SessionStart
- SQLite schema implementation with full rehydration sequence
- Unit tests and integration tests with a three-agent coding scenario including agent sleep/wake cycles

### Phase 2: Retrieval Pipeline (Weeks 5 to 8)

Build the full three-tier vector-free retrieval pipeline with knowledge graph, LLM reasoning, and complete MCP surface.

- NetworkX knowledge graph with entity extraction and relationship creation
- ACT-R scoring function with access tracking and decay
- LLM-as-judge tier with structured prompting
- Consolidation background worker with importance-threshold triggering
- Contradiction detection engine
- Full MCP server with all 9 tools and browsable ncms:// resources
- Periodic snapshot scheduler with incremental delta publishing
- Snapshot TTL management and stale snapshot cleanup

### Phase 3: GPU Acceleration (Weeks 9 to 12)

Add GPU acceleration paths for SPLADE, graph traversal, and batch operations.

- SPLADE expansion and scoring on GPU via NIM or PyTorch
- cuGraph integration for GPU-accelerated graph traversal
- Milvus backend for GPU-accelerated sparse vector search
- NIM integration for consolidation and reasoning LLMs
- Hardware-aware memory tiering (VRAM / RAM / disk)

### Phase 4: Production Packaging (Weeks 13 to 16)

Package as NIM-compatible container with full observability and enterprise features.

- Docker container with Helm charts and Kubernetes operator
- Redis and NATS transport adapters for Knowledge Bus
- Neo4j and FalkorDB graph backends
- REST/gRPC API following NIM conventions
- Prometheus metrics, OpenTelemetry traces, structured logging
- RBAC and multi-tenant memory isolation
- GitHub Copilot .github/hooks/ configuration
- Benchmark suite comparing against Mem0, Zep, and raw vector search

---

## Appendix A: Comparison with Existing Systems

| Capability | NCMS | Mem0 | Zep | MemGPT | Google ADK | LangGraph |
|-----------|------|------|-----|--------|-----------|-----------|
| Vector-free retrieval | Yes | No | No | No | Yes | No |
| GPU-accelerated | Yes | No | No | No | No | No |
| Knowledge graph | Yes | Partial | Yes | No | No | No |
| Temporal model | Yes | No | Yes | No | No | No |
| Agent broadcast/ask | Yes | No | No | No | No | No |
| Knowledge snapshots | Yes | No | No | No | No | No |
| Surrogate response | Yes | No | No | No | No | No |
| Coding agent hooks | Yes | No | No | No | No | No |
| Crash-safe rehydration | Yes | Partial | Yes | No | No | Partial |
| Embedded mode | Yes | Yes | No | Yes | Yes | Yes |
| NeMo native | Yes | Plugin | Plugin | No | No | No |
| NIM packaging | Yes | No | No | No | No | No |
| MCP server | Yes | No | No | No | No | No |
| ACT-R scoring | Yes | No | No | No | Partial | No |
| Consolidation | Yes | No | Partial | Partial | Yes | No |
| Provenance tracking | Yes | No | Partial | No | No | No |
| Contradiction detect | Yes | No | No | No | No | No |
| Multi-agent RBAC | Yes | Partial | No | No | No | No |

---

## Appendix B: Key References

- Park et al. (2023). Generative Agents: Interactive Simulacra of Human Behavior. Stanford.
- Shinn et al. (2023). Reflexion: Language Agents with Verbal Reinforcement Learning. NeurIPS.
- Packer et al. (2023). MemGPT: Towards LLMs as Operating Systems. NeurIPS.
- Rezazadeh et al. (2025). Collaborative Memory: Multi-User Memory Sharing in LLM Agents. arXiv.
- Zep/Graphiti (2025). A Temporal Knowledge Graph Architecture for Agent Memory. arXiv.
- Xu et al. (2025). A-MEM: Agentic Memory for LLM Agents. NeurIPS.
- Formal et al. (2021-2022). SPLADE: Sparse Lexical and Expansion Model. SIGIR.
- Khattab & Zaharia (2020). ColBERT: Efficient and Effective Passage Search. SIGIR.
- Sumers et al. (2024). Cognitive Architectures for Language Agents (CoALA). Princeton.
- Anderson (2007). How Can the Human Mind Occur in the Physical Universe? (ACT-R). Oxford.
- NVIDIA NeMo Agent Toolkit v1.4 Documentation. docs.nvidia.com.
- Google ADK Always-On Memory Agent. GoogleCloudPlatform/generative-ai.
