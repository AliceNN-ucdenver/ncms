"""Tests for Phase 2 domain models: RelationType, EntityStateMeta, ReconciliationResult."""

from __future__ import annotations

from ncms.domain.models import (
    EntityStateMeta,
    MemoryNode,
    NodeType,
    ReconciliationResult,
    RelationType,
)


# ── RelationType ──────────────────────────────────────────────────────


class TestRelationType:
    def test_all_five_values(self) -> None:
        assert set(RelationType) == {
            RelationType.SUPPORTS,
            RelationType.REFINES,
            RelationType.SUPERSEDES,
            RelationType.CONFLICTS,
            RelationType.UNRELATED,
        }

    def test_str_values(self) -> None:
        assert str(RelationType.SUPPORTS) == "supports"
        assert str(RelationType.SUPERSEDES) == "supersedes"

    def test_is_strenum(self) -> None:
        assert RelationType.CONFLICTS == "conflicts"


# ── EntityStateMeta ───────────────────────────────────────────────────


class TestEntityStateMeta:
    def _make_node(self, **meta_overrides: object) -> MemoryNode:
        meta = {
            "entity_id": "auth-service",
            "state_key": "status",
            "state_value": "deployed",
        }
        meta.update(meta_overrides)
        return MemoryNode(
            memory_id="mem-1",
            node_type=NodeType.ENTITY_STATE,
            metadata=meta,
        )

    def test_from_node_extracts_required_fields(self) -> None:
        node = self._make_node()
        esm = EntityStateMeta.from_node(node)
        assert esm is not None
        assert esm.entity_id == "auth-service"
        assert esm.state_key == "status"
        assert esm.state_value == "deployed"
        assert esm.state_scope is None

    def test_from_node_extracts_optional_scope(self) -> None:
        node = self._make_node(state_scope="us-east-1")
        esm = EntityStateMeta.from_node(node)
        assert esm is not None
        assert esm.state_scope == "us-east-1"

    def test_from_node_extracts_revision_reason(self) -> None:
        node = self._make_node(revision_reason="hotfix applied")
        esm = EntityStateMeta.from_node(node)
        assert esm is not None
        assert esm.revision_reason == "hotfix applied"

    def test_from_node_returns_none_missing_entity_id(self) -> None:
        node = MemoryNode(
            memory_id="mem-1",
            node_type=NodeType.ENTITY_STATE,
            metadata={"state_key": "status", "state_value": "deployed"},
        )
        assert EntityStateMeta.from_node(node) is None

    def test_from_node_returns_none_missing_state_key(self) -> None:
        node = MemoryNode(
            memory_id="mem-1",
            node_type=NodeType.ENTITY_STATE,
            metadata={"entity_id": "svc-1", "state_value": "deployed"},
        )
        assert EntityStateMeta.from_node(node) is None

    def test_from_node_returns_none_missing_state_value(self) -> None:
        node = MemoryNode(
            memory_id="mem-1",
            node_type=NodeType.ENTITY_STATE,
            metadata={"entity_id": "svc-1", "state_key": "status"},
        )
        assert EntityStateMeta.from_node(node) is None

    def test_from_node_empty_metadata(self) -> None:
        node = MemoryNode(
            memory_id="mem-1",
            node_type=NodeType.ENTITY_STATE,
            metadata={},
        )
        assert EntityStateMeta.from_node(node) is None

    def test_from_node_coerces_to_str(self) -> None:
        """Numeric or other types in metadata get str()-coerced."""
        node = MemoryNode(
            memory_id="mem-1",
            node_type=NodeType.ENTITY_STATE,
            metadata={"entity_id": 123, "state_key": "port", "state_value": 8080},
        )
        esm = EntityStateMeta.from_node(node)
        assert esm is not None
        assert esm.entity_id == "123"
        assert esm.state_value == "8080"


# ── ReconciliationResult ──────────────────────────────────────────────


class TestReconciliationResult:
    def test_defaults(self) -> None:
        r = ReconciliationResult(relation=RelationType.SUPPORTS)
        assert r.existing_node_id is None
        assert r.confidence == 1.0
        assert r.reason == ""

    def test_full_fields(self) -> None:
        r = ReconciliationResult(
            relation=RelationType.SUPERSEDES,
            existing_node_id="node-42",
            confidence=0.9,
            reason="Value changed: running -> stopped",
        )
        assert r.relation == RelationType.SUPERSEDES
        assert r.existing_node_id == "node-42"
        assert r.confidence == 0.9
        assert "Value changed" in r.reason

    def test_serialization_roundtrip(self) -> None:
        r = ReconciliationResult(
            relation=RelationType.CONFLICTS,
            existing_node_id="n-1",
            confidence=0.7,
            reason="different scope",
        )
        data = r.model_dump(mode="json")
        restored = ReconciliationResult.model_validate(data)
        assert restored == r
