"""Tests for domain models."""

import pytest
from pydantic import ValidationError

from ncms.domain.models import (
    AccessRecord,
    AgentInfo,
    AskContext,
    Entity,
    ImpactAssessment,
    KnowledgeAnnounce,
    KnowledgeAsk,
    KnowledgePayload,
    KnowledgeProvenance,
    KnowledgeResponse,
    KnowledgeSnapshot,
    Memory,
    Relationship,
    ScoredMemory,
    SnapshotEntry,
    SubscriptionFilter,
)


class TestMemory:
    def test_auto_generated_fields(self):
        """Memory should auto-generate id and timestamps."""
        m = Memory(content="test content")
        assert m.id  # non-empty string
        assert m.created_at is not None
        assert m.updated_at is not None

    def test_defaults(self):
        """Memory defaults should match the model definition."""
        m = Memory(content="test")
        assert m.content == "test"
        assert m.type == Memory.model_fields["type"].default
        assert m.importance == Memory.model_fields["importance"].default
        assert m.domains == []
        assert m.tags == []
        assert m.source_agent is None

    def test_full_construction(self):
        """Memory should accept all fields."""
        m = Memory(
            content="API endpoint spec",
            type="interface-spec",
            importance=8.0,
            domains=["api", "auth"],
            tags=["rest", "v2"],
            source_agent="api-agent",
            project="acme",
        )
        assert m.type == "interface-spec"
        assert m.importance == 8.0
        assert set(m.domains) == {"api", "auth"}
        assert m.project == "acme"

    def test_json_round_trip(self):
        """Memory should survive JSON serialization/deserialization."""
        m = Memory(
            content="round trip test",
            domains=["a", "b"],
            type="code-snippet",
            importance=7.5,
            source_agent="test",
        )
        data = m.model_dump(mode="json")
        m2 = Memory(**data)
        assert m2.id == m.id
        assert m2.content == m.content
        assert m2.domains == m.domains
        assert m2.type == m.type
        assert m2.importance == m.importance

    def test_unique_ids(self):
        """Each Memory should get a unique id."""
        m1 = Memory(content="first")
        m2 = Memory(content="second")
        assert m1.id != m2.id


class TestKnowledgeAsk:
    def test_auto_id(self):
        """Ask should auto-generate a unique ask_id."""
        ask = KnowledgeAsk(from_agent="test", question="what?")
        assert ask.ask_id  # non-empty
        assert ask.urgency == KnowledgeAsk.model_fields["urgency"].default

    def test_domains(self):
        """Ask should accept multiple domains."""
        ask = KnowledgeAsk(
            from_agent="test",
            question="what?",
            domains=["api", "db"],
        )
        assert set(ask.domains) == {"api", "db"}

    def test_context_defaults(self):
        """AskContext should have sensible defaults."""
        ask = KnowledgeAsk(from_agent="t", question="q")
        assert ask.context.current_task is None
        assert ask.context.max_results == AskContext.model_fields["max_results"].default

    def test_unique_ask_ids(self):
        """Each ask should get a unique id."""
        a1 = KnowledgeAsk(from_agent="t", question="q1")
        a2 = KnowledgeAsk(from_agent="t", question="q2")
        assert a1.ask_id != a2.ask_id


class TestKnowledgeResponse:
    def test_live_mode_default(self):
        """Default source_mode should be 'live'."""
        resp = KnowledgeResponse(
            ask_id="test-ask",
            from_agent="agent",
            knowledge=KnowledgePayload(content="answer"),
        )
        assert resp.source_mode == KnowledgeResponse.model_fields["source_mode"].default
        assert resp.snapshot_age_seconds is None

    def test_surrogate_mode(self):
        """Surrogate responses should carry warm mode and age."""
        age = 3600
        resp = KnowledgeResponse(
            ask_id="test-ask",
            from_agent="agent",
            knowledge=KnowledgePayload(content="cached answer"),
            source_mode="warm",
            snapshot_age_seconds=age,
            original_agent="original",
            staleness_warning="stale",
        )
        assert resp.source_mode == "warm"
        assert resp.snapshot_age_seconds == age
        assert resp.original_agent == "original"


class TestKnowledgeAnnounce:
    def test_defaults(self):
        """Announcement should have auto-generated id and timestamp."""
        ann = KnowledgeAnnounce(
            from_agent="db-agent",
            knowledge=KnowledgePayload(content="schema change"),
        )
        assert ann.announce_id  # non-empty
        assert ann.event == KnowledgeAnnounce.model_fields["event"].default
        assert ann.created_at is not None

    def test_breaking_change(self):
        """Breaking change announcement should carry impact assessment."""
        ann = KnowledgeAnnounce(
            from_agent="db-agent",
            event="breaking-change",
            domains=["db"],
            knowledge=KnowledgePayload(content="added column"),
            impact=ImpactAssessment(
                breaking_change=True,
                affected_domains=["db", "api"],
                severity="critical",
                description="new required column",
            ),
        )
        assert ann.impact.breaking_change is True
        assert ann.impact.severity == "critical"
        assert set(ann.impact.affected_domains) == {"db", "api"}


class TestKnowledgePayload:
    def test_default_type(self):
        """Payload type should default to 'fact'."""
        p = KnowledgePayload(content="some fact")
        assert p.type == KnowledgePayload.model_fields["type"].default

    def test_structured_data(self):
        """Payload should accept structured data."""
        p = KnowledgePayload(
            type="interface-spec",
            content="GET /users",
            structured={"method": "GET", "path": "/users"},
            references=["docs/api.md"],
        )
        assert p.structured["method"] == "GET"
        assert len(p.references) == 1


class TestKnowledgeSnapshot:
    def test_with_entries(self):
        """Snapshot should hold entries and derive domains."""
        entry = SnapshotEntry(
            domain="api",
            knowledge=KnowledgePayload(content="endpoint spec"),
            confidence=0.9,
        )
        snap = KnowledgeSnapshot(
            agent_id="test",
            domains=["api"],
            entries=[entry],
        )
        assert len(snap.entries) == 1
        assert snap.entries[0].confidence == entry.confidence
        # TTL should match model default
        assert snap.ttl_hours == KnowledgeSnapshot.model_fields["ttl_hours"].default

    def test_json_serializable(self):
        """Snapshot should be fully JSON serializable."""
        snap = KnowledgeSnapshot(
            agent_id="test",
            domains=["api"],
            entries=[
                SnapshotEntry(
                    domain="api",
                    knowledge=KnowledgePayload(content="data"),
                ),
            ],
        )
        data = snap.model_dump(mode="json")
        assert isinstance(data["timestamp"], str)
        assert isinstance(data["entries"], list)
        # Round-trip
        snap2 = KnowledgeSnapshot(**data)
        assert snap2.snapshot_id == snap.snapshot_id
        assert len(snap2.entries) == len(snap.entries)

    def test_unique_snapshot_ids(self):
        """Each snapshot should get a unique id."""
        s1 = KnowledgeSnapshot(agent_id="a", domains=[])
        s2 = KnowledgeSnapshot(agent_id="a", domains=[])
        assert s1.snapshot_id != s2.snapshot_id


class TestScoredMemory:
    def test_score_components(self):
        """ScoredMemory should carry all score components."""
        m = Memory(content="test")
        bm25, base, spread, total = 2.5, 1.0, 0.5, 3.0
        sm = ScoredMemory(
            memory=m,
            bm25_score=bm25,
            base_level=base,
            spreading=spread,
            total_activation=total,
        )
        assert sm.bm25_score == bm25
        assert sm.base_level == base
        assert sm.spreading == spread
        assert sm.total_activation == total
        assert sm.memory.id == m.id


class TestEntity:
    def test_construction(self):
        """Entity should accept name and type."""
        e = Entity(name="UserService", type="service")
        assert e.name == "UserService"
        assert e.type == "service"
        assert e.id  # auto-generated

    def test_attributes(self):
        """Entity should accept arbitrary attributes."""
        e = Entity(
            name="users",
            type="table",
            attributes={"columns": ["id", "name", "email"]},
        )
        assert "columns" in e.attributes


class TestRelationship:
    def test_construction(self):
        """Relationship should link two entities."""
        r = Relationship(
            source_entity_id="e1",
            target_entity_id="e2",
            type="depends_on",
        )
        assert r.source_entity_id == "e1"
        assert r.target_entity_id == "e2"
        assert r.type == "depends_on"
        assert r.id  # auto-generated


class TestSubscriptionFilter:
    def test_defaults(self):
        """All filter fields should default to None."""
        f = SubscriptionFilter()
        assert f.domains is None
        assert f.severity_min is None
        assert f.tags is None

    def test_domain_filter(self):
        f = SubscriptionFilter(domains=["api", "db"])
        assert set(f.domains) == {"api", "db"}


class TestAgentInfo:
    def test_defaults(self):
        info = AgentInfo(agent_id="test")
        assert info.status == AgentInfo.model_fields["status"].default
        assert info.domains == []
        assert info.registered_at is not None
