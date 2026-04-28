"""Tests for memory hierarchy domain models."""

from datetime import UTC, datetime

from ncms.domain.models import (
    EdgeType,
    EphemeralEntry,
    GraphEdge,
    MemoryNode,
    NodeType,
)


class TestNodeType:
    def test_values_match_spec(self):
        """All four HTMG node types from design spec §7.1 are present."""
        assert NodeType.ATOMIC == "atomic"
        assert NodeType.ENTITY_STATE == "entity_state"
        assert NodeType.EPISODE == "episode"
        assert NodeType.ABSTRACT == "abstract"

    def test_is_string(self):
        """NodeType values behave as strings (StrEnum)."""
        assert isinstance(NodeType.ATOMIC, str)
        assert NodeType.ATOMIC.upper() == "ATOMIC"


class TestEdgeType:
    def test_membership_edges(self):
        """Design spec §7.2: membership/hierarchy edges."""
        assert EdgeType.BELONGS_TO_EPISODE == "belongs_to_episode"
        assert EdgeType.ABSTRACTS == "abstracts"
        assert EdgeType.DERIVED_FROM == "derived_from"
        assert EdgeType.SUMMARIZES == "summarizes"

    def test_semantic_edges(self):
        """Design spec §7.2: semantic/support edges."""
        assert EdgeType.MENTIONS_ENTITY == "mentions_entity"
        assert EdgeType.RELATED_TO == "related_to"
        assert EdgeType.SUPPORTS == "supports"
        assert EdgeType.REFINES == "refines"

    def test_truth_maintenance_edges(self):
        """Design spec §7.2: truth-maintenance edges."""
        assert EdgeType.SUPERSEDES == "supersedes"
        assert EdgeType.SUPERSEDED_BY == "superseded_by"
        assert EdgeType.CONFLICTS_WITH == "conflicts_with"
        assert EdgeType.CURRENT_STATE_OF == "current_state_of"

    def test_temporal_edges(self):
        """Design spec §7.2: temporal/causal edges."""
        assert EdgeType.PRECEDES == "precedes"
        assert EdgeType.CAUSED_BY == "caused_by"


class TestMemoryNode:
    def test_construct_minimal(self):
        node = MemoryNode(memory_id="mem-1", node_type=NodeType.ATOMIC)
        assert node.memory_id == "mem-1"
        assert node.node_type == NodeType.ATOMIC
        assert node.is_current is True
        assert node.importance == 5.0
        assert node.parent_id is None
        assert node.valid_from is None
        assert node.valid_to is None
        assert node.metadata == {}

    def test_construct_entity_state(self):
        now = datetime.now(UTC)
        node = MemoryNode(
            memory_id="mem-2",
            node_type=NodeType.ENTITY_STATE,
            parent_id="episode-1",
            importance=8.0,
            is_current=False,
            valid_from=now,
            metadata={"entity_id": "ent-1", "state_key": "status"},
        )
        assert node.node_type == NodeType.ENTITY_STATE
        assert node.parent_id == "episode-1"
        assert node.is_current is False
        assert node.valid_from == now
        assert node.metadata["state_key"] == "status"

    def test_auto_id(self):
        a = MemoryNode(memory_id="m1", node_type=NodeType.ATOMIC)
        b = MemoryNode(memory_id="m1", node_type=NodeType.ATOMIC)
        assert a.id != b.id  # UUIDs are unique

    def test_serialize_roundtrip(self):
        node = MemoryNode(memory_id="m1", node_type=NodeType.EPISODE)
        data = node.model_dump(mode="json")
        restored = MemoryNode(**data)
        assert restored.memory_id == node.memory_id
        assert restored.node_type == node.node_type
        assert restored.id == node.id


class TestGraphEdge:
    def test_construct_minimal(self):
        edge = GraphEdge(
            source_id="node-1",
            target_id="node-2",
            edge_type=EdgeType.BELONGS_TO_EPISODE,
        )
        assert edge.source_id == "node-1"
        assert edge.target_id == "node-2"
        assert edge.edge_type == EdgeType.BELONGS_TO_EPISODE
        assert edge.weight == 1.0
        assert edge.metadata == {}

    def test_construct_with_metadata(self):
        edge = GraphEdge(
            source_id="s",
            target_id="t",
            edge_type=EdgeType.SUPERSEDES,
            weight=0.95,
            metadata={"reason": "version_update"},
        )
        assert edge.weight == 0.95
        assert edge.metadata["reason"] == "version_update"

    def test_serialize_roundtrip(self):
        edge = GraphEdge(source_id="a", target_id="b", edge_type=EdgeType.SUPPORTS)
        data = edge.model_dump(mode="json")
        restored = GraphEdge(**data)
        assert restored.edge_type == EdgeType.SUPPORTS
        assert restored.id == edge.id


class TestEphemeralEntry:
    def test_construct_minimal(self):
        entry = EphemeralEntry(content="some transient info")
        assert entry.content == "some transient info"
        assert entry.admission_score == 0.0
        assert entry.ttl_seconds == 3600
        assert entry.domains == []

    def test_construct_with_fields(self):
        entry = EphemeralEntry(
            content="temp note",
            source_agent="agent-1",
            domains=["frontend"],
            admission_score=0.35,
            ttl_seconds=1800,
        )
        assert entry.source_agent == "agent-1"
        assert entry.admission_score == 0.35
        assert entry.ttl_seconds == 1800

    def test_serialize_roundtrip(self):
        entry = EphemeralEntry(content="test", domains=["api"])
        data = entry.model_dump(mode="json")
        restored = EphemeralEntry(**data)
        assert restored.content == "test"
        assert restored.domains == ["api"]
