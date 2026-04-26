"""Reconciliation Service — classifies and applies relations between memory states.

When a new L2 entity_state node is created (triggered by state change signal
or structured state declaration in memory_service), this service:
1. Finds existing states for the same entity + state_key
2. Classifies the relation (supports, refines, supersedes, conflicts, unrelated)
3. Applies the appropriate action (create edges, flip is_current, set valid_to)

All classification is heuristic-based (no LLM required).
Feature-flagged via config.temporal_enabled (default False).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ncms.application.tlg import load_retirement_verbs
from ncms.config import NCMSConfig
from ncms.domain.models import (
    EdgeType,
    EntityStateMeta,
    GraphEdge,
    MemoryNode,
    ReconciliationResult,
    RelationType,
)
from ncms.domain.tlg import extract_retired
from ncms.infrastructure.observability.event_log import NullEventLog

logger = logging.getLogger(__name__)


class ReconciliationService:
    """Classifies relations between incoming and existing entity states."""

    def __init__(
        self,
        store: object,
        config: NCMSConfig | None = None,
        event_log: object | None = None,
    ) -> None:
        self._store = store
        self._config = config or NCMSConfig()
        self._event_log = event_log or NullEventLog()

    # ── Find Related States ──────────────────────────────────────────

    async def _find_related_states(
        self,
        entity_id: str,
        state_key: str,
    ) -> list[MemoryNode]:
        """Retrieve current entity states for the same entity + state_key."""
        return await self._store.get_current_entity_states(entity_id, state_key)  # type: ignore[attr-defined]

    # ── Classify Relation ────────────────────────────────────────────

    def classify_relation(
        self,
        new_meta: EntityStateMeta,
        existing_node: MemoryNode,
        existing_meta: EntityStateMeta,
    ) -> ReconciliationResult:
        """Classify the relation between a new state and an existing state.

        Classification rules (from design spec section 9.2):
        1. Same entity+key, same value, same scope → SUPPORTS
        2. Same entity+key, same value, narrower scope → REFINES
        3. Same entity+key, different value, same/no scope → SUPERSEDES
        4. Same entity+key, different value, different scope → CONFLICTS
        5. Different entity or key → UNRELATED
        """
        # Different entity or key → unrelated
        if new_meta.entity_id != existing_meta.entity_id:
            return ReconciliationResult(
                relation=RelationType.UNRELATED,
                existing_node_id=existing_node.id,
                reason="Different entity_id",
            )
        if new_meta.state_key != existing_meta.state_key:
            return ReconciliationResult(
                relation=RelationType.UNRELATED,
                existing_node_id=existing_node.id,
                reason="Different state_key",
            )

        # Same entity + same key
        values_match = (
            new_meta.state_value.strip().lower() == existing_meta.state_value.strip().lower()
        )

        if values_match:
            # Same value — supports or refines
            new_scope = (new_meta.state_scope or "").strip().lower()
            existing_scope = (existing_meta.state_scope or "").strip().lower()

            if new_scope and existing_scope and new_scope != existing_scope:
                # Different scope with same value → refines (more specific)
                return ReconciliationResult(
                    relation=RelationType.REFINES,
                    existing_node_id=existing_node.id,
                    confidence=0.8,
                    reason=f"Same value, different scope: {new_scope} vs {existing_scope}",
                )

            return ReconciliationResult(
                relation=RelationType.SUPPORTS,
                existing_node_id=existing_node.id,
                confidence=0.9,
                reason="Same entity+key+value",
            )

        # Different value — supersedes or conflicts
        new_scope = (new_meta.state_scope or "").strip().lower()
        existing_scope = (existing_meta.state_scope or "").strip().lower()

        if new_scope and existing_scope and new_scope != existing_scope:
            # Different scope with different value → conflict (parallel truths)
            return ReconciliationResult(
                relation=RelationType.CONFLICTS,
                existing_node_id=existing_node.id,
                confidence=0.7,
                reason=(
                    f"Different values in different scopes: "
                    f"{new_meta.state_value} ({new_scope}) "
                    f"vs {existing_meta.state_value} ({existing_scope})"
                ),
            )

        # Same scope (or no scope), different value → supersedes
        return ReconciliationResult(
            relation=RelationType.SUPERSEDES,
            existing_node_id=existing_node.id,
            confidence=0.9,
            reason=f"Value changed: {existing_meta.state_value} -> {new_meta.state_value}",
        )

    # ── Apply Actions ────────────────────────────────────────────────

    async def _apply_supports(
        self,
        new_node: MemoryNode,
        existing_node: MemoryNode,
    ) -> None:
        """Create SUPPORTS edge, boost importance of both nodes."""
        boost = self._config.reconciliation_importance_boost

        edge = GraphEdge(
            source_id=new_node.id,
            target_id=existing_node.id,
            edge_type=EdgeType.SUPPORTS,
            metadata={"reason": "same_entity_state_value"},
        )
        await self._store.save_graph_edge(edge)  # type: ignore[attr-defined]

        # Boost importance (capped at 10.0)
        existing_node.importance = min(10.0, existing_node.importance + boost)
        await self._store.update_memory_node(existing_node)  # type: ignore[attr-defined]
        new_node.importance = min(10.0, new_node.importance + boost)
        await self._store.update_memory_node(new_node)  # type: ignore[attr-defined]

        self._event_log.reconciliation_applied(  # type: ignore[attr-defined]
            new_node_id=new_node.id,
            existing_node_id=existing_node.id,
            relation="supports",
        )

    async def _apply_refines(
        self,
        new_node: MemoryNode,
        existing_node: MemoryNode,
    ) -> None:
        """Create REFINES edge. Source (broader) remains valid."""
        edge = GraphEdge(
            source_id=new_node.id,
            target_id=existing_node.id,
            edge_type=EdgeType.REFINES,
            metadata={"reason": "narrower_scope"},
        )
        await self._store.save_graph_edge(edge)  # type: ignore[attr-defined]

        self._event_log.reconciliation_applied(  # type: ignore[attr-defined]
            new_node_id=new_node.id,
            existing_node_id=existing_node.id,
            relation="refines",
        )

    async def _apply_supersedes(
        self,
        new_node: MemoryNode,
        existing_node: MemoryNode,
        reason: str = "",
    ) -> None:
        """Close prior valid_to, flip is_current, create bidirectional edges.

        When :attr:`NCMSConfig.temporal_enabled` is True, the outgoing
        SUPERSEDES edge also carries a structurally-extracted
        ``retires_entities`` set (Phase 1 of the TLG integration — see
        ``docs/p1-plan.md``).  Extraction never raises to the caller:
        on any error we fall back to the empty set so reconciliation
        semantics are preserved.
        """
        now = datetime.now(UTC)

        # Close existing state
        existing_node.is_current = False
        existing_node.valid_to = now
        existing_meta = dict(existing_node.metadata)
        existing_meta["superseded_by"] = new_node.id
        existing_meta["revision_reason"] = reason
        existing_node.metadata = existing_meta
        await self._store.update_memory_node(existing_node)  # type: ignore[attr-defined]

        # Set new state as current
        new_node.is_current = True
        if new_node.valid_from is None:
            new_node.valid_from = now
        new_meta = dict(new_node.metadata)
        new_meta["supersedes"] = existing_node.id
        new_node.metadata = new_meta
        await self._store.update_memory_node(new_node)  # type: ignore[attr-defined]

        # TLG Phase 1: compute retires_entities via structural extractor.
        # Disabled path returns an empty list so downstream code sees the
        # same shape whether TLG is on or off.
        retires = await self._compute_retires_entities(new_node, existing_node)

        # Create SUPERSEDES edge (new → old)
        await self._store.save_graph_edge(
            GraphEdge(  # type: ignore[attr-defined]
                source_id=new_node.id,
                target_id=existing_node.id,
                edge_type=EdgeType.SUPERSEDES,
                metadata={"reason": reason},
                retires_entities=retires,
            )
        )

        # Create SUPERSEDED_BY edge (old → new)
        await self._store.save_graph_edge(
            GraphEdge(  # type: ignore[attr-defined]
                source_id=existing_node.id,
                target_id=new_node.id,
                edge_type=EdgeType.SUPERSEDED_BY,
                metadata={"reason": reason},
                retires_entities=retires,
            )
        )

        self._event_log.reconciliation_applied(  # type: ignore[attr-defined]
            new_node_id=new_node.id,
            existing_node_id=existing_node.id,
            relation="supersedes",
        )

    async def _compute_retires_entities(
        self,
        new_node: MemoryNode,
        existing_node: MemoryNode,
    ) -> list[str]:
        """Populate ``retires_entities`` when TLG is enabled; else empty.

        Fetches the memory content for the new (superseding) node plus
        entity sets for both sides, then delegates to the pure
        ``extract_retired`` extractor in ``ncms.domain.tlg``.  On any
        missing data or exception we return an empty list so the caller
        can proceed unchanged — TLG-enablement is a best-effort
        annotation, never a hard dependency for reconciliation.
        """
        if not getattr(self._config, "temporal_enabled", False):
            return []
        try:
            new_memory = await self._store.get_memory(new_node.memory_id)  # type: ignore[attr-defined]
            if new_memory is None or not new_memory.content:
                return []
            src_entities = frozenset(
                await self._store.get_memory_entities(existing_node.memory_id)  # type: ignore[attr-defined]
            )
            dst_entities = frozenset(
                await self._store.get_memory_entities(new_node.memory_id)  # type: ignore[attr-defined]
            )
            # Prefer induced retirement verbs from
            # ``grammar_transition_markers`` (populated by
            # ``application.tlg.induction.induce_and_persist_markers``).
            # Falls back to SEED_RETIREMENT_VERBS on a cold store.
            verbs = await load_retirement_verbs(self._store)
            retired = extract_retired(
                new_memory.content,
                src_entities,
                dst_entities,
                retirement_verbs=verbs,
                domain_entities=frozenset(),
            )
            return sorted(retired)
        except Exception as exc:  # pragma: no cover — defensive guard
            logger.warning(
                "TLG retirement extraction failed for %s → %s: %s",
                new_node.id,
                existing_node.id,
                exc,
            )
            return []

    async def _apply_conflicts(
        self,
        new_node: MemoryNode,
        existing_node: MemoryNode,
        reason: str = "",
    ) -> None:
        """Create bidirectional CONFLICTS_WITH edges, flag for review."""
        # new → existing
        await self._store.save_graph_edge(
            GraphEdge(  # type: ignore[attr-defined]
                source_id=new_node.id,
                target_id=existing_node.id,
                edge_type=EdgeType.CONFLICTS_WITH,
                metadata={"reason": reason, "flagged_for_review": True},
            )
        )
        # existing → new
        await self._store.save_graph_edge(
            GraphEdge(  # type: ignore[attr-defined]
                source_id=existing_node.id,
                target_id=new_node.id,
                edge_type=EdgeType.CONFLICTS_WITH,
                metadata={"reason": reason, "flagged_for_review": True},
            )
        )

        self._event_log.reconciliation_applied(  # type: ignore[attr-defined]
            new_node_id=new_node.id,
            existing_node_id=existing_node.id,
            relation="conflicts",
        )

    # ── Orchestrator ─────────────────────────────────────────────────

    async def reconcile(
        self,
        new_node: MemoryNode,
    ) -> list[ReconciliationResult]:
        """Run reconciliation for an incoming entity state node.

        1. Extract entity_id + state_key from new_node.metadata
        2. Find existing current states for the same entity + key
        3. Classify relation with each existing state
        4. Apply the appropriate action

        Returns list of ReconciliationResult (one per existing state compared).
        """
        new_meta = EntityStateMeta.from_node(new_node)
        if new_meta is None:
            logger.warning(
                "Cannot reconcile node %s: missing entity state metadata",
                new_node.id,
            )
            return []

        related_states = await self._find_related_states(
            new_meta.entity_id,
            new_meta.state_key,
        )

        results: list[ReconciliationResult] = []
        for existing_node in related_states:
            # Skip comparing node to itself
            if existing_node.id == new_node.id:
                continue

            existing_meta = EntityStateMeta.from_node(existing_node)
            if existing_meta is None:
                continue

            result = self.classify_relation(new_meta, existing_node, existing_meta)
            results.append(result)

            # Apply the action
            if result.relation == RelationType.SUPPORTS:
                await self._apply_supports(new_node, existing_node)
            elif result.relation == RelationType.REFINES:
                await self._apply_refines(new_node, existing_node)
            elif result.relation == RelationType.SUPERSEDES:
                await self._apply_supersedes(
                    new_node,
                    existing_node,
                    reason=result.reason,
                )
            elif result.relation == RelationType.CONFLICTS:
                await self._apply_conflicts(
                    new_node,
                    existing_node,
                    reason=result.reason,
                )
            # UNRELATED: no action needed

        return results
