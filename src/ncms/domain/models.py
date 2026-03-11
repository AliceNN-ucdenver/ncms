"""Core domain models for NCMS.

All models are Pydantic BaseModel instances for validation and serialization.
This module has zero infrastructure dependencies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return str(uuid4())


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
    ] = "fact"
    importance: float = 5.0
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
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
# Search Results
# ---------------------------------------------------------------------------


class ScoredMemory(BaseModel):
    """A memory with its computed activation score components."""

    memory: Memory
    bm25_score: float = 0.0
    base_level: float = 0.0
    spreading: float = 0.0
    total_activation: float = 0.0
    retrieval_prob: float = 1.0


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
