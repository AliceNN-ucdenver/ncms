"""Core domain models for NCMS.

All models are Pydantic BaseModel instances for validation and serialization.
This module has zero infrastructure dependencies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return str(uuid4())


# ---------------------------------------------------------------------------
# HTMG Node & Edge Types (Phase 1)
# ---------------------------------------------------------------------------


class NodeType(StrEnum):
    """Type discriminator for memory_nodes table."""

    ATOMIC = "atomic"
    ENTITY_STATE = "entity_state"
    EPISODE = "episode"
    ABSTRACT = "abstract"


class EdgeType(StrEnum):
    """Typed edge categories for graph_edges table."""

    # Membership / hierarchy
    BELONGS_TO_EPISODE = "belongs_to_episode"
    ABSTRACTS = "abstracts"
    DERIVED_FROM = "derived_from"
    SUMMARIZES = "summarizes"
    # Semantic / support
    MENTIONS_ENTITY = "mentions_entity"
    RELATED_TO = "related_to"
    SUPPORTS = "supports"
    REFINES = "refines"
    # Truth maintenance
    SUPERSEDES = "supersedes"
    SUPERSEDED_BY = "superseded_by"
    CONFLICTS_WITH = "conflicts_with"
    CURRENT_STATE_OF = "current_state_of"
    # Temporal / causal
    PRECEDES = "precedes"
    # CTLG v8+: direct causation edges.  CAUSED_BY points from
    # effect to cause (``effect CAUSED_BY cause``); populated at
    # ingest-time by the cue-tagging pipeline when CAUSAL_EXPLICIT
    # or CAUSAL_ALTLEX spans connect two REFERENT entities.
    CAUSED_BY = "caused_by"
    # CTLG v8+: enabling conditions.  ENABLES points from the
    # enabler to the enabled; weaker than CAUSED_BY — the enabler
    # made the effect possible but didn't force it.  Example:
    # "availability of pgvector ENABLED the Postgres decision".
    ENABLES = "enables"


# ---------------------------------------------------------------------------
# Knowledge Bus Messages
# ---------------------------------------------------------------------------


class AskContext(BaseModel):
    """Context attached to a knowledge ask."""

    current_task: str | None = None
    relevant_code: str | None = None
    relevant_entities: list[str] = Field(default_factory=list)
    already_known: list[str] = Field(default_factory=list)
    max_results: int = 5


class KnowledgePayload(BaseModel):
    """The actual content being shared between agents."""

    type: Literal[
        "interface-spec",
        "code-snippet",
        "configuration",
        "architecture-decision",
        "constraint",
        "fact",
        "code-pattern",
        "convention",
        "bug-fix",
        "dependency",
        "pending-work",
    ] = "fact"
    content: str
    structured: dict[str, Any] | None = None
    references: list[str] = Field(default_factory=list)


class KnowledgeProvenance(BaseModel):
    """Tracks origin and chain of custody for knowledge."""

    source: Literal["direct-work", "memory-store", "documentation", "inferred"] = "direct-work"
    last_verified: datetime = Field(default_factory=_utcnow)
    trust_level: Literal["authoritative", "observed", "speculative"] = "observed"


class ImpactAssessment(BaseModel):
    """Describes the impact of a change announcement."""

    breaking_change: bool = False
    affected_domains: list[str] = Field(default_factory=list)
    severity: Literal["info", "warning", "critical"] = "info"
    description: str = ""


class SubscriptionFilter(BaseModel):
    """Filter for subscribing to announcements."""

    domains: list[str] | None = None
    severity_min: Literal["info", "warning", "critical"] | None = None
    tags: list[str] | None = None


class KnowledgeAsk(BaseModel):
    """A question routed through the Knowledge Bus."""

    ask_id: str = Field(default_factory=_uuid)
    from_agent: str
    question: str
    domains: list[str] = Field(default_factory=list)
    urgency: Literal["blocking", "important", "background"] = "important"
    context: AskContext = Field(default_factory=AskContext)
    response_format: str = "any"
    ttl_ms: int = 5000
    created_at: datetime = Field(default_factory=_utcnow)


class KnowledgeResponse(BaseModel):
    """A response to a KnowledgeAsk."""

    ask_id: str
    from_agent: str
    confidence: float = 0.5
    knowledge: KnowledgePayload
    provenance: KnowledgeProvenance = Field(default_factory=KnowledgeProvenance)
    freshness: datetime = Field(default_factory=_utcnow)
    source_mode: Literal["live", "warm", "cold"] = "live"
    snapshot_age_seconds: int | None = None
    original_agent: str | None = None
    staleness_warning: str | None = None


class KnowledgeAnnounce(BaseModel):
    """Fire-and-forget broadcast announcement."""

    announce_id: str = Field(default_factory=_uuid)
    from_agent: str
    event: Literal["created", "updated", "deprecated", "breaking-change"] = "updated"
    domains: list[str] = Field(default_factory=list)
    knowledge: KnowledgePayload
    impact: ImpactAssessment = Field(default_factory=ImpactAssessment)
    created_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Memory Storage
# ---------------------------------------------------------------------------


class Memory(BaseModel):
    """A single unit of persistent memory."""

    id: str = Field(default_factory=_uuid)
    content: str
    structured: dict[str, Any] | None = None
    type: Literal[
        "interface-spec",
        "code-snippet",
        "configuration",
        "architecture-decision",
        "constraint",
        "fact",
        "code-pattern",
        "convention",
        "bug-fix",
        "dependency",
        "pending-work",
        "insight",
        "section_index",
        "document_section",
        "document_chunk",
        "document",
        "document_profile",
    ] = "fact"
    importance: float = 5.0
    content_hash: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    # Bitemporal: when the source says the event happened (may be in the
    # past relative to created_at, which is always ingestion time).
    # Required for temporal queries to work on historical / replayed data
    # where ingestion date ≠ event date.
    observed_at: datetime | None = None
    source_agent: str | None = None
    project: str | None = None
    domains: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class Entity(BaseModel):
    """A node in the knowledge graph."""

    id: str = Field(default_factory=_uuid)
    name: str
    type: str  # "service", "endpoint", "table", "component", etc.
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Relationship(BaseModel):
    """A directed edge in the knowledge graph."""

    id: str = Field(default_factory=_uuid)
    source_entity_id: str
    target_entity_id: str
    type: str  # "depends_on", "exposes", "consumes", "supersedes"
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    source_memory_id: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class AccessRecord(BaseModel):
    """A log entry of a memory being accessed."""

    memory_id: str
    accessed_at: datetime = Field(default_factory=_utcnow)
    accessing_agent: str | None = None
    query_context: str | None = None


# ---------------------------------------------------------------------------
# HTMG Typed Nodes (Phase 1)
# ---------------------------------------------------------------------------


class MemoryNode(BaseModel):
    """A typed node in the HTMG hierarchy.

    Parallels the existing Memory model but adds node_type discriminator,
    parent linkage, and temporal fields for state reconciliation.

    Bitemporal fields (Phase 2B):
    - valid_from / valid_to: real-world validity interval
    - observed_at: when the source says the event happened
    - ingested_at: when NCMS stored this node
    """

    id: str = Field(default_factory=_uuid)
    memory_id: str  # FK to memories.id (the canonical Memory record)
    node_type: NodeType
    parent_id: str | None = None  # Episode or abstract parent
    importance: float = 5.0
    is_current: bool = True  # For entity states: current or superseded
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    observed_at: datetime | None = None  # When the source says event happened
    ingested_at: datetime = Field(default_factory=_utcnow)  # When NCMS stored it
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class GraphEdge(BaseModel):
    """A typed directed edge in the HTMG graph.

    ``retires_entities`` (schema v12 — TLG integration) carries the
    structural retirement set for the edge: entity IDs whose state
    this edge retires.  Populated by ``ReconciliationService`` when
    emitting SUPERSEDES edges (Phase 1 of the TLG integration — see
    ``docs/p1-plan.md``).  Empty by default so edges produced by
    code paths unaware of TLG remain correct.
    """

    id: str = Field(default_factory=_uuid)
    source_id: str  # memory_node or entity ID
    target_id: str  # memory_node or entity ID
    edge_type: EdgeType
    weight: float = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    retires_entities: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)


class EphemeralEntry(BaseModel):
    """A short-lived cache entry for low-admission-score content."""

    id: str = Field(default_factory=_uuid)
    content: str
    source_agent: str | None = None
    domains: list[str] = Field(default_factory=list)
    admission_score: float = 0.0
    ttl_seconds: int = 3600  # 1 hour default
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime | None = None


# ---------------------------------------------------------------------------
# State Reconciliation (Phase 2)
# ---------------------------------------------------------------------------


class RelationType(StrEnum):
    """Relation classification for state reconciliation."""

    SUPPORTS = "supports"
    REFINES = "refines"
    SUPERSEDES = "supersedes"
    CONFLICTS = "conflicts"
    UNRELATED = "unrelated"


class EntityStateMeta(BaseModel):
    """Validated accessor for entity-state metadata stored in MemoryNode.metadata.

    This is NOT a persistence model — it's a typed projection of the metadata dict.
    Use it to validate/extract entity state fields from MemoryNode.metadata.
    """

    entity_id: str
    state_key: str
    state_value: str
    state_scope: str | None = None
    revision_reason: str | None = None

    @classmethod
    def from_node(cls, node: MemoryNode) -> EntityStateMeta | None:
        """Extract EntityStateMeta from a MemoryNode's metadata dict.

        Returns None if the node's metadata lacks required fields.
        """
        meta = node.metadata
        entity_id = meta.get("entity_id")
        state_key = meta.get("state_key")
        state_value = meta.get("state_value")
        if not entity_id or not state_key or state_value is None:
            return None
        return cls(
            entity_id=str(entity_id),
            state_key=str(state_key),
            state_value=str(state_value),
            state_scope=meta.get("state_scope"),
            revision_reason=meta.get("revision_reason"),
        )


class ReconciliationResult(BaseModel):
    """Result of classifying the relation between a new state and an existing state."""

    relation: RelationType
    existing_node_id: str | None = None
    confidence: float = 1.0
    reason: str = ""


# ---------------------------------------------------------------------------
# Episode Formation (Phase 3)
# ---------------------------------------------------------------------------


class EpisodeStatus(StrEnum):
    """Lifecycle state for episode nodes."""

    OPEN = "open"
    CLOSED = "closed"


class EpisodeMeta(BaseModel):
    """Validated accessor for episode metadata stored in MemoryNode.metadata.

    This is NOT a persistence model — it's a typed projection of the metadata dict.
    Use it to validate/extract episode fields from MemoryNode.metadata.
    """

    episode_title: str
    status: EpisodeStatus = EpisodeStatus.OPEN
    anchor_type: str  # "entity_cluster", "structured:issue_id", "structured:release", etc.
    anchor_id: str | None = None  # topic key or structured ID (e.g., "JIRA-123")
    member_count: int = 0
    topic_entities: list[str] = Field(default_factory=list)  # entity names defining topic
    closed_reason: str | None = None

    @classmethod
    def from_node(cls, node: MemoryNode) -> EpisodeMeta | None:
        """Extract EpisodeMeta from a MemoryNode's metadata dict.

        Returns None if the node's metadata lacks required fields.
        """
        meta = node.metadata
        title = meta.get("episode_title")
        anchor_type = meta.get("anchor_type")
        if not title or not anchor_type:
            return None
        return cls(
            episode_title=str(title),
            status=EpisodeStatus(meta.get("status", "open")),
            anchor_type=str(anchor_type),
            anchor_id=meta.get("anchor_id"),
            member_count=int(meta.get("member_count", 0)),
            topic_entities=meta.get("topic_entities", []),
            closed_reason=meta.get("closed_reason"),
        )


# ---------------------------------------------------------------------------
# Knowledge Snapshots
# ---------------------------------------------------------------------------


class SnapshotEntry(BaseModel):
    """One piece of knowledge in a snapshot."""

    domain: str
    knowledge: KnowledgePayload
    confidence: float = 1.0
    last_verified: datetime = Field(default_factory=_utcnow)
    volatility: Literal["stable", "changing", "volatile"] = "changing"


class KnowledgeSnapshot(BaseModel):
    """Complete knowledge state for a sleeping agent."""

    snapshot_id: str = Field(default_factory=_uuid)
    agent_id: str
    timestamp: datetime = Field(default_factory=_utcnow)
    domains: list[str] = Field(default_factory=list)
    entries: list[SnapshotEntry] = Field(default_factory=list)
    is_incremental: bool = False
    supersedes: str | None = None
    ttl_hours: int = 168  # 7 days


# ---------------------------------------------------------------------------
# Search Logging (Phase 8 — Dream Cycles)
# ---------------------------------------------------------------------------


class SearchLogEntry(BaseModel):
    """A log entry recording a search query and its returned results.

    Used by dream cycles to compute PMI association strengths between
    entity pairs that co-occur in search results.
    """

    id: int | None = None  # Auto-assigned by SQLite
    query: str
    query_entities: list[str] = Field(default_factory=list)
    returned_ids: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=_utcnow)
    agent_id: str | None = None


# ---------------------------------------------------------------------------
# Search Results
# ---------------------------------------------------------------------------


class ScoredMemory(BaseModel):
    """A memory with its computed activation score components."""

    memory: Memory
    bm25_score: float = 0.0
    splade_score: float = 0.0
    base_level: float = 0.0
    spreading: float = 0.0
    total_activation: float = 0.0
    retrieval_prob: float = 1.0
    # Phase 2C: reconciliation annotations
    is_superseded: bool = False
    has_conflicts: bool = False
    superseded_by: str | None = None
    # Phase 4: intent-aware retrieval annotations
    node_types: list[str] = Field(default_factory=list)
    intent: str | None = None
    hierarchy_bonus: float = 0.0
    # Phase 4 temporal: temporal query match score
    temporal_score: float = 0.0
    # Phase H — per-query SLM-signal contributions (post-weight, the
    # actual additions to ``total_activation``).  Surfaced for the
    # query_diagnostic event so operators can see which heads moved
    # this candidate's rank.  CTLG (causal-temporal cue contributions)
    # will land alongside these as new fields.
    intent_alignment_contrib: float = 0.0
    state_change_alignment_contrib: float = 0.0
    role_grounding_contrib: float = 0.0
    # Phase G — reconciliation penalty applied (always >= 0; subtracted
    # from combined).  ``0.0`` means no supersession/conflict edges
    # OR the query intent didn't qualify for the penalty gate.
    reconciliation_penalty: float = 0.0


# ---------------------------------------------------------------------------
# Phase 11: Structured Recall — context-enriched retrieval results
# ---------------------------------------------------------------------------


class EntityStateSnapshot(BaseModel):
    """Current state of an entity mentioned in a recalled memory."""

    entity_id: str
    entity_name: str
    state_key: str = ""
    state_value: str = ""
    is_current: bool = True
    observed_at: datetime | None = None


class EpisodeContext(BaseModel):
    """Episode that a recalled memory belongs to."""

    episode_id: str
    episode_title: str = ""
    status: str = "open"
    member_count: int = 0
    topic_entities: list[str] = Field(default_factory=list)
    sibling_ids: list[str] = Field(default_factory=list)
    summary: str | None = None


class CausalChain(BaseModel):
    """Directed edges connecting a memory to related memories via HTMG."""

    supersedes: list[str] = Field(default_factory=list)
    superseded_by: list[str] = Field(default_factory=list)
    derived_from: list[str] = Field(default_factory=list)
    supports: list[str] = Field(default_factory=list)
    conflicts_with: list[str] = Field(default_factory=list)


class DocumentSectionContext(BaseModel):
    """Section context returned when a document profile is expanded."""

    doc_id: str
    doc_title: str
    doc_type: str | None = None
    from_agent: str | None = None
    section_heading: str
    section_content: str
    section_index: int
    relevance_score: float = 0.0


class RecallContext(BaseModel):
    """Structured context enriching a single recalled memory."""

    entity_states: list[EntityStateSnapshot] = Field(default_factory=list)
    episode: EpisodeContext | None = None
    causal_chain: CausalChain = Field(default_factory=CausalChain)
    temporal_neighbors: list[str] = Field(default_factory=list)
    document_sections: list[DocumentSectionContext] = Field(default_factory=list)


class RecallResult(BaseModel):
    """A recalled memory with full context graph — one call, complete picture."""

    memory: ScoredMemory
    context: RecallContext = Field(default_factory=RecallContext)
    retrieval_path: str = "fact_lookup"


# ---------------------------------------------------------------------------
# P1-Temporal-Experiment Phase B.5: Deterministic arithmetic resolver
# ---------------------------------------------------------------------------


class TemporalArithmeticResult(BaseModel):
    """Structured answer to an arithmetic temporal question.

    Produced by ``MemoryService.compute_temporal_arithmetic`` — no
    LLM involved.  Consumers (MCP tools, dashboard, RAG readers) can
    format the ``answer_text`` directly or drill into ``anchor_memories``
    for the underlying evidence.

    Fields:
      - ``answer_value`` — the computed delta as a float in ``unit``.
      - ``unit`` — the unit the caller asked for (days/weeks/months/
        years/hours).
      - ``answer_text`` — rounded, human-readable summary ("7 days").
      - ``operation`` — which arithmetic shape fired (``between``,
        ``since``, ``age_of``).
      - ``anchor_memories`` — the memories whose ``observed_at`` fed
        the calculation, in chronological order.
      - ``anchor_dates`` — parallel list of ISO-8601 timestamps (echo
        of the memory metadata; duplicative but avoids consumers
        re-reading the memory objects).
      - ``confidence`` — 0–1, lower when the anchor picks were
        ambiguous (multiple candidate memories per entity).
    """

    answer_value: float
    unit: str
    answer_text: str
    operation: str
    anchor_memories: list[Memory] = Field(default_factory=list)
    anchor_dates: list[datetime] = Field(default_factory=list)
    confidence: float = 1.0


# ---------------------------------------------------------------------------
# Phase 5: Level-First Retrieval & Synthesis
# ---------------------------------------------------------------------------


class TraversalMode(StrEnum):
    """Traversal strategy for hierarchical memory navigation."""

    TOP_DOWN = "top_down"          # Abstract → episodes → atomic
    BOTTOM_UP = "bottom_up"        # Atomic → episode → abstract
    TEMPORAL = "temporal"          # Entity state timeline
    LATERAL = "lateral"            # Episode siblings + related episodes


class SynthesisMode(StrEnum):
    """Synthesis output mode — controls how retrieved memories are combined."""

    SUMMARY = "summary"            # Brief overview of key points
    DETAIL = "detail"              # Exhaustive context with evidence
    TIMELINE = "timeline"          # Chronological narrative
    COMPARISON = "comparison"      # Before/after or multi-perspective
    EVIDENCE = "evidence"          # Fact-backed claims with citations


class TraversalResult(BaseModel):
    """Result of hierarchical traversal from a seed node."""

    seed_id: str                   # Starting memory/node ID
    traversal_mode: TraversalMode
    results: list[RecallResult] = Field(default_factory=list)
    levels_traversed: int = 0      # How many hierarchy levels covered
    path: list[str] = Field(default_factory=list)  # Node IDs in traversal order


class TopicCluster(BaseModel):
    """Emergent topic cluster from L4 abstract grouping."""

    topic_id: str = Field(default_factory=_uuid)
    label: str = ""                 # Human-readable topic label (from top entities)
    entity_keys: list[str] = Field(default_factory=list)  # Shared entities
    abstract_ids: list[str] = Field(default_factory=list)  # Abstract memory IDs
    episode_ids: list[str] = Field(default_factory=list)   # Contributing episodes
    confidence: float = 0.0         # Cluster quality score
    member_count: int = 0


class SynthesizedResponse(BaseModel):
    """Structured synthesis output with provenance and token accounting."""

    query: str
    mode: SynthesisMode
    content: str                    # The synthesized text
    sources: list[str] = Field(default_factory=list)  # Memory IDs used
    source_count: int = 0
    token_budget: int = 0           # Configured budget
    tokens_used: int = 0            # Approximate tokens in content
    traversal: TraversalMode | None = None  # If traversal was used
    topic_cluster: TopicCluster | None = None  # If topic-scoped
    intent: str = "fact_lookup"     # Classified intent


# ---------------------------------------------------------------------------
# Agent Registry
# ---------------------------------------------------------------------------


class AgentInfo(BaseModel):
    """Registration info for an agent on the Knowledge Bus."""

    agent_id: str
    domains: list[str] = Field(default_factory=list)
    status: Literal["online", "offline", "sleeping"] = "online"
    registered_at: datetime = Field(default_factory=_utcnow)
    last_seen: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Phase 2.5: Document Intelligence Persistence
# ---------------------------------------------------------------------------


class DocType(StrEnum):
    """Document types produced by the pipeline."""

    RESEARCH = "research"
    PRD = "prd"
    MANIFEST = "manifest"
    DESIGN = "design"
    REVIEW = "review"
    CONTRACT = "contract"


class DocLinkType(StrEnum):
    """Typed relationships between documents."""

    DERIVED_FROM = "derived_from"  # PRD derived from Research, Design from PRD
    REVIEWS = "reviews"            # Review report reviews a Design
    SUPERSEDES = "supersedes"      # Design v2 supersedes v1
    CITES = "cites"                # Document cites another as reference
    APPROVED_BY = "approved_by"    # Human approval linked to document


class User(BaseModel):
    """Local user for authentication and audit attribution."""

    id: str = Field(default_factory=lambda: f"usr-{uuid4().hex[:8]}")
    username: str
    password_hash: str
    display_name: str | None = None
    role: str = "reviewer"  # reviewer | admin
    created_at: datetime = Field(default_factory=_utcnow)


class Project(BaseModel):
    """Persistent project record — survives hub restarts."""

    id: str = Field(default_factory=lambda: f"PRJ-{uuid4().hex[:8]}")
    topic: str
    target: str = ""
    source_type: str = "research"
    repository_url: str | None = None
    scope: list[str] = Field(default_factory=list)
    status: str = "active"
    phase: str = "pending"
    quality_score: float | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Document(BaseModel):
    """Persistent, versioned, entity-enriched document."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    project_id: str | None = None
    title: str
    content: str
    from_agent: str | None = None
    doc_type: str | None = None
    version: int = 1
    parent_doc_id: str | None = None
    format: str = "markdown"
    size_bytes: int = 0
    content_hash: str | None = None
    entities: list[dict[str, str]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class DocumentLink(BaseModel):
    """Typed relationship between two documents."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    source_doc_id: str
    target_doc_id: str
    link_type: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class ReviewScore(BaseModel):
    """Structured review score from an expert agent."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    document_id: str
    project_id: str | None = None
    reviewer_agent: str
    review_round: int = 1
    score: int | None = None
    severity: str | None = None
    covered: str | None = None
    missing: str | None = None
    changes: str | None = None
    review_doc_id: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class ApprovalDecision(BaseModel):
    """Human approval/rejection of a document."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    project_id: str | None = None
    document_id: str
    decision: str  # approve | reject | request-changes
    approver: str
    comment: str | None = None
    policies_active: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_utcnow)


class GuardrailViolation(BaseModel):
    """Guardrail policy violation linked to a document."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    document_id: str | None = None
    project_id: str | None = None
    policy_type: str
    rule: str
    message: str | None = None
    escalation: str  # warn | block | reject
    overridden: bool = False
    override_reason: str | None = None
    timestamp: datetime = Field(default_factory=_utcnow)


class PendingApproval(BaseModel):
    """Guardrail approval gate — agent pauses, human approves or denies."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    project_id: str | None = None
    agent: str
    node: str
    violations: list[dict[str, str]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"  # pending | approved | denied | timeout
    decided_by: str | None = None
    comment: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    decided_at: datetime | None = None


class GroundingLogEntry(BaseModel):
    """Links a review citation to the actual NCMS memory retrieved."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    document_id: str
    review_score_id: str | None = None
    memory_id: str
    retrieval_score: float | None = None
    entity_query: str | None = None
    domain: str | None = None
    timestamp: datetime = Field(default_factory=_utcnow)


class LLMCallRecord(BaseModel):
    """Metadata for an LLM call + Phoenix trace link."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    project_id: str | None = None
    agent: str
    node: str
    prompt_hash: str | None = None
    prompt_size: int | None = None
    response_size: int | None = None
    reasoning_size: int = 0
    model: str | None = None
    thinking_enabled: bool = False
    duration_ms: int | None = None
    trace_id: str | None = None
    timestamp: datetime = Field(default_factory=_utcnow)


class AgentConfigSnapshot(BaseModel):
    """Agent configuration captured at pipeline start."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    project_id: str | None = None
    agent: str
    config_hash: str | None = None
    prompt_version: str | None = None
    model_name: str | None = None
    thinking_enabled: bool = False
    max_tokens: int | None = None
    timestamp: datetime = Field(default_factory=_utcnow)


class BusConversation(BaseModel):
    """Persistent record of a bus_ask/bus_respond exchange."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    project_id: str | None = None
    ask_id: str
    from_agent: str
    to_agent: str | None = None
    question_preview: str | None = None
    answer_preview: str | None = None
    confidence: float | None = None
    duration_ms: int | None = None
    timestamp: datetime = Field(default_factory=_utcnow)


class PipelineEvent(BaseModel):
    """Persistent pipeline node execution event."""

    project_id: str
    agent: str
    node: str
    status: str  # started | completed | failed | interrupted
    detail: str = ""
    event_subtype: str = ""  # research_plan | research_results | ""
    timestamp: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Intent-Slot Extraction (P2 ingest-side content understanding)
# ---------------------------------------------------------------------------


IntentLabel = Literal[
    "positive", "negative", "habitual", "difficulty", "choice", "none",
]

AdmissionDecision = Literal["persist", "ephemeral", "discard"]

StateChange = Literal["declaration", "retirement", "none"]

# The v6/v7.x ``ShapeIntent`` literal (a 13-class query-shape
# enum produced by the ``shape_intent_head``) was removed in v8.1
# and replaced by the CTLG sequence-labeled ``shape_cue_head`` +
# compositional synthesizer.  The synthesizer produces a
# :class:`ncms.domain.tlg.semantic_parser.TLGQuery`, which is the
# structured logical form the TLG dispatcher now consumes directly.
# Retrospective: ``docs/completed/failed-experiments/
# shape-intent-classification.md``.


class ExtractedLabel(BaseModel):
    """Output of an :class:`IntentSlotExtractor`.

    Five heads in one dataclass.  Per-head confidence is present so
    callers can gate admission / state-change / topic decisions on
    calibrated thresholds.  Backends that don't produce a given
    head (e.g. zero-shot baselines that score only intent) return
    ``None`` for the unavailable fields; ingest code treats ``None``
    as "abstain" and falls through to the next backend.

    The ``slots`` dict maps slot-name (domain taxonomy) to surface
    form extracted from the input text.  ``slot_confidences`` is
    per-slot when the backend exposes it.

    Topics are **dynamic per adapter** — no closed-vocabulary
    enum in this class.  The topic vocabulary lives in the adapter
    manifest at runtime, not in the codebase.  This lets each
    deployment ship its own taxonomy without code changes.
    """

    intent: IntentLabel = "none"
    intent_confidence: float = 0.0

    slots: dict[str, str] = Field(default_factory=dict)
    slot_confidences: dict[str, float] = Field(default_factory=dict)

    topic: str | None = None
    topic_confidence: float | None = None

    admission: AdmissionDecision | None = None
    admission_confidence: float | None = None

    state_change: StateChange | None = None
    state_change_confidence: float | None = None

    # v7+ role-classified gazetteer spans.  Each entry is a dict
    # with ``char_start``, ``char_end``, ``surface``, ``canonical``,
    # ``slot``, ``role`` — the serialised form of
    # :class:`ncms.application.adapters.schemas.RoleSpan`.  The
    # ``slots`` dict above is derived from these at inference time
    # (primary → typed slot, alternative → alternative slot).  Empty
    # list on pre-v7 adapters.  Dict-serialised (not the dataclass)
    # so this crosses the application↔infrastructure boundary
    # without pulling adapter schemas into the domain layer.
    role_spans: list[dict] = Field(default_factory=list)

    # v8+ CTLG cue tags (6th head — per-token BIO sequence labeler
    # over 33 causal / temporal / ordinal / modal / referent /
    # subject / scope labels).  Each entry is a dict with
    # ``char_start``, ``char_end``, ``surface``, ``cue_label``,
    # ``confidence`` — the serialised form of
    # :class:`ncms.domain.tlg.cue_taxonomy.TaggedToken`.  Serialises
    # directly as JSON into ``memory.structured["intent_slot"]
    # ["cue_tags"]`` so the ingest pipeline's
    # ``_extract_and_persist_causal_edges`` can consume it without
    # per-row conversion.  Empty list on pre-v8 adapters
    # (manifest.cue_labels empty) — callers treat an empty
    # cue_tags as "no CTLG signal".
    cue_tags: list[dict] = Field(default_factory=list)

    method: str = ""           # backend name that produced this label
    latency_ms: float = 0.0    # inference wall-time (populated by caller)

    def is_intent_confident(self, threshold: float = 0.7) -> bool:
        return self.intent_confidence >= threshold

    def is_topic_confident(self, threshold: float = 0.7) -> bool:
        return (
            self.topic_confidence is not None
            and self.topic_confidence >= threshold
        )

    def is_admission_confident(self, threshold: float = 0.7) -> bool:
        return (
            self.admission_confidence is not None
            and self.admission_confidence >= threshold
        )

    def is_state_change_confident(self, threshold: float = 0.7) -> bool:
        return (
            self.state_change_confidence is not None
            and self.state_change_confidence >= threshold
        )
