"""Consolidation Service - background memory maintenance.

Handles memory decay, pruning, and knowledge consolidation.

Phase 5 adds hierarchical consolidation:
- Episode summary generation from closed episodes (5A)
- State trajectory narratives from entity histories (5B)
- Recurring pattern detection from similar episode clusters (5C)
- Staleness tracking and abstract refresh
"""

from __future__ import annotations

import contextlib
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ncms.config import NCMSConfig
from ncms.domain.models import (
    AccessRecord,
    EdgeType,
    GraphEdge,
    Memory,
    MemoryNode,
    NodeType,
)
from ncms.domain.protocols import GraphEngine, IndexEngine, MemoryStore
from ncms.domain.scoring import base_level_activation

if TYPE_CHECKING:
    from ncms.infrastructure.indexing.splade_engine import SpladeEngine
    from ncms.infrastructure.observability.event_log import EventLog, NullEventLog

logger = logging.getLogger(__name__)


def _collect_entity_ids(
    graph: GraphEngine | None,
    memory_ids: list[str],
) -> set[str]:
    """Collect all entity IDs linked to a set of memories."""
    entity_ids: set[str] = set()
    if not graph:
        return entity_ids
    for mid in memory_ids:
        entity_ids.update(graph.get_entity_ids_for_memory(mid))
    return entity_ids


class ConsolidationService:
    """Background maintenance for memory health and knowledge consolidation."""

    def __init__(
        self,
        store: MemoryStore,
        index: IndexEngine | None = None,
        graph: GraphEngine | None = None,
        config: NCMSConfig | None = None,
        event_log: EventLog | NullEventLog | None = None,
        splade: SpladeEngine | None = None,
    ):
        self._store = store
        self._index = index
        self._graph = graph
        self._config = config or NCMSConfig()
        self._event_log = event_log
        self._splade = splade

    # ── Decay ────────────────────────────────────────────────────────────

    async def run_decay_pass(self) -> int:
        """Recompute activation scores and flag low-activation memories.

        Returns the number of memories below threshold.
        """
        memories = await self._store.list_memories(limit=100000)
        below_threshold = 0

        for memory in memories:
            access_ages = await self._store.get_access_times(memory.id)
            bl = base_level_activation(access_ages, decay=self._config.actr_decay)

            if bl < self._config.actr_threshold:
                below_threshold += 1
                logger.debug("Memory %s below threshold: activation=%.2f", memory.id, bl)

        logger.info(
            "Decay pass complete: %d/%d memories below threshold",
            below_threshold,
            len(memories),
        )
        return below_threshold

    # ── Entity Co-occurrence Knowledge Consolidation ─────────────────────

    async def consolidate_knowledge(self) -> int:
        """Discover cross-memory patterns and create insight memories.

        Uses entity co-occurrence clustering to find groups of related
        memories, then synthesizes emergent patterns via LLM. Insights
        are stored as ``Memory(type="insight")`` records, indexed in
        Tantivy, and linked to key entities in the knowledge graph.

        Returns the number of insights created.
        """
        if not self._config.consolidation_knowledge_enabled:
            return 0
        if not self._graph:
            logger.warning("Graph not available, skipping knowledge consolidation")
            return 0

        # 1. Get last run timestamp from consolidation_state
        last_run = await self._store.get_consolidation_value("last_knowledge_consolidation")

        # 2. Fetch memories since last run (or all if first run)
        memories = await self._store.list_memories(since=last_run, limit=10000)
        # Exclude existing insights from clustering input
        memories = [m for m in memories if m.type != "insight"]

        if len(memories) < self._config.consolidation_knowledge_min_cluster_size:
            logger.debug(
                "Too few memories for consolidation: %d < %d",
                len(memories),
                self._config.consolidation_knowledge_min_cluster_size,
            )
            return 0

        # 3. Cluster by entity co-occurrence
        from ncms.infrastructure.consolidation.clusterer import find_entity_clusters

        clusters = find_entity_clusters(
            memories,
            self._graph,
            min_cluster_size=self._config.consolidation_knowledge_min_cluster_size,
        )

        if not clusters:
            logger.debug("No clusters found for consolidation")
            return 0

        # 4. Synthesize insights from top clusters
        from ncms.infrastructure.consolidation.synthesizer import synthesize_insight

        insights_created = 0
        max_insights = self._config.consolidation_knowledge_max_insights_per_run
        for cluster in clusters[:max_insights]:
            result = await synthesize_insight(
                cluster,
                model=self._config.consolidation_knowledge_model,
                api_base=self._config.consolidation_knowledge_api_base,
            )
            if not result:
                continue

            # 5. Store insight as Memory(type="insight")
            insight_memory = Memory(
                content=result["insight"],
                type="insight",
                domains=list(cluster.domains),
                importance=result.get("confidence", 0.5) * 10,
                structured={
                    "source_memory_ids": [m.id for m in cluster.memories],
                    "pattern_type": result.get("pattern_type", "unknown"),
                    "confidence": result.get("confidence", 0.5),
                    "key_entities": result.get("key_entities", []),
                    "synthesis_model": self._config.consolidation_knowledge_model,
                },
            )
            await self._store.save_memory(insight_memory)
            # Log initial access so ACT-R scoring doesn't filter out new insights
            await self._store.log_access(
                AccessRecord(memory_id=insight_memory.id, accessing_agent="consolidation")
            )
            if self._index:
                self._index.index_memory(insight_memory)

            # Link insight to key entities in graph
            for entity_name in result.get("key_entities", []):
                entity = await self._store.find_entity_by_name(entity_name)
                if entity:
                    await self._store.link_memory_entity(insight_memory.id, entity.id)
                    self._graph.link_memory_entity(insight_memory.id, entity.id)

            insights_created += 1

        # 6. Update last run timestamp
        await self._store.set_consolidation_value(
            "last_knowledge_consolidation",
            datetime.now(UTC).isoformat(),
        )

        logger.info(
            "Knowledge consolidation: created %d insights from %d clusters",
            insights_created,
            len(clusters),
        )
        return insights_created

    # ── Phase 5A: Episode Summary Consolidation ──────────────────────────

    async def consolidate_episodes(self) -> int:
        """Generate narrative summaries from closed episodes.

        Queries closed episodes that haven't been summarized yet, generates
        LLM summaries, and stores them as abstract memory nodes linked to
        the source episode and its members.

        Returns the number of summaries created.
        """
        if not self._config.episode_consolidation_enabled:
            return 0

        from ncms.infrastructure.consolidation.abstract_synthesizer import (
            synthesize_episode_summary,
        )

        episodes = await self._store.get_closed_unsummarized_episodes()
        if not episodes:
            return 0

        cap = self._config.consolidation_max_abstracts_per_run
        summaries_created = 0

        for ep_node in episodes[:cap]:
            meta = ep_node.metadata
            title = meta.get("episode_title", "Untitled episode")

            # Gather member content
            members = await self._store.get_episode_members(ep_node.id)
            member_contents: list[str] = []
            member_domains: set[str] = set()
            for member in members:
                mem = await self._store.get_memory(member.memory_id)
                if mem:
                    member_contents.append(mem.content)
                    member_domains.update(mem.domains)

            if not member_contents:
                continue

            # Synthesize via LLM
            result = await synthesize_episode_summary(
                episode_title=title,
                member_contents=member_contents,
                model=self._config.consolidation_knowledge_model,
                api_base=self._config.consolidation_knowledge_api_base,
            )
            if not result:
                continue

            # Store insight + HTMG abstract node + edges
            structured_extra = {
                "source_episode_id": ep_node.id,
                "episode_title": title,
                "actors": result.get("actors", []),
                "artifacts": result.get("artifacts", []),
                "decisions": result.get("decisions", []),
                "outcome": result.get("outcome", ""),
                "topic_entities": meta.get("topic_entities", []),
            }
            member_node_ids = [m.id for m in members]
            _, abstract_node = await self._store_abstract(
                content=result["summary"],
                abstract_type="episode_summary",
                domains=list(member_domains),
                confidence=result.get("confidence", 0.5),
                structured_extra=structured_extra,
                source_node_ids=member_node_ids,
                source_memory_ids=[m.memory_id for m in members],
                edge_type="derived_from",
            )
            # SUMMARIZES edge: abstract → episode (separate from DERIVED_FROM to members)
            await self._store.save_graph_edge(
                GraphEdge(
                    source_id=abstract_node.id,
                    target_id=ep_node.id,
                    edge_type=EdgeType.SUMMARIZES,
                )
            )

            # Mark episode as summarized
            ep_node.metadata["summarized"] = True
            ep_node.metadata["summary_node_id"] = abstract_node.id
            await self._store.update_memory_node(ep_node)

            summaries_created += 1

        await self._store.set_consolidation_value(
            "last_episode_consolidation",
            datetime.now(UTC).isoformat(),
        )

        logger.info(
            "Episode consolidation: created %d summaries from %d closed episodes",
            summaries_created,
            len(episodes),
        )
        return summaries_created

    # ── Phase 5B: State Trajectory Consolidation ─────────────────────────

    async def consolidate_trajectories(self) -> int:
        """Generate trajectory narratives for entities with rich state histories.

        Finds entities with >= trajectory_min_transitions state transitions,
        synthesizes temporal progression narratives, and stores them as
        abstract memory nodes.

        Returns the number of trajectories created.
        """
        if not self._config.trajectory_consolidation_enabled:
            return 0

        from ncms.infrastructure.consolidation.abstract_synthesizer import (
            synthesize_state_trajectory,
        )

        entity_counts = await self._store.get_entities_with_state_count(
            self._config.trajectory_min_transitions,
        )
        if not entity_counts:
            return 0

        # Check which entities already have trajectory abstracts
        existing = await self._store.get_abstract_nodes_by_type("state_trajectory")
        existing_entity_ids = {
            n.metadata.get("entity_id") for n in existing if not self._is_stale(n)
        }

        cap = self._config.consolidation_max_abstracts_per_run
        trajectories_created = 0

        for entity_id, _count in entity_counts:
            if trajectories_created >= cap:
                break
            if entity_id in existing_entity_ids:
                continue

            # Get all states for this entity, grouped by state_key
            all_states = await self._store.get_entity_states_by_entity(entity_id)
            if not all_states:
                continue

            # Group by state_key
            by_key: dict[str, list[MemoryNode]] = defaultdict(list)
            for state in all_states:
                sk = state.metadata.get("state_key", "default")
                by_key[sk].append(state)

            # Resolve entity name
            entity_name = entity_id
            if self._graph:
                entity = await self._store.get_entity(entity_id)
                if entity:
                    entity_name = entity.name

            for state_key, key_states in by_key.items():
                if len(key_states) < self._config.trajectory_min_transitions:
                    continue
                if trajectories_created >= cap:
                    break

                # Build chronological state list
                key_states.sort(key=lambda n: n.created_at)
                state_dicts = [
                    {
                        "value": s.metadata.get("state_value", ""),
                        "timestamp": s.created_at.isoformat(),
                    }
                    for s in key_states
                ]

                result = await synthesize_state_trajectory(
                    entity_name=entity_name,
                    state_key=state_key,
                    states=state_dicts,
                    model=self._config.consolidation_knowledge_model,
                    api_base=self._config.consolidation_knowledge_api_base,
                )
                if not result:
                    continue

                await self._store_abstract(
                    content=result["narrative"],
                    abstract_type="state_trajectory",
                    domains=[],
                    confidence=result.get("confidence", 0.5),
                    structured_extra={
                        "entity_id": entity_id,
                        "entity_name": entity_name,
                        "state_key": state_key,
                        "trend": result.get("trend", "unknown"),
                        "key_transitions": result.get(
                            "key_transitions",
                            [],
                        ),
                        "transition_count": len(key_states),
                    },
                    source_node_ids=[s.id for s in key_states],
                    source_memory_ids=[s.memory_id for s in key_states],
                )
                trajectories_created += 1

        await self._store.set_consolidation_value(
            "last_trajectory_consolidation",
            datetime.now(UTC).isoformat(),
        )

        logger.info(
            "Trajectory consolidation: created %d trajectories",
            trajectories_created,
        )
        return trajectories_created

    # ── Phase 5C: Pattern & Insight Consolidation ────────────────────────

    async def consolidate_patterns(self) -> int:
        """Detect recurring patterns from similar episode clusters.

        Clusters episode summaries by topic_entities overlap, synthesizes
        recurring patterns via LLM, and promotes stable patterns to
        strategic insights.

        Returns the number of patterns/insights created.
        """
        if not self._config.pattern_consolidation_enabled:
            return 0

        from ncms.infrastructure.consolidation.abstract_synthesizer import (
            synthesize_recurring_pattern,
        )

        # Load episode summary abstracts
        summaries = await self._store.get_abstract_nodes_by_type("episode_summary")
        if len(summaries) < self._config.pattern_min_episodes:
            return 0

        # Cluster by topic_entities Jaccard overlap
        clusters = self._cluster_by_entity_overlap(summaries)
        if not clusters:
            return 0

        # Check existing patterns to avoid duplicates
        existing_patterns = await self._store.get_abstract_nodes_by_type("recurring_pattern")
        existing_insights = await self._store.get_abstract_nodes_by_type("strategic_insight")
        existing_sigs = {
            frozenset(n.metadata.get("source_episode_ids", []))
            for n in [*existing_patterns, *existing_insights]
            if not self._is_stale(n)
        }

        cap = self._config.consolidation_max_abstracts_per_run
        patterns_created = 0

        for cluster_nodes, shared_entities in clusters:
            if patterns_created >= cap:
                break

            ep_ids = frozenset(n.metadata.get("source_episode_id", n.id) for n in cluster_nodes)
            if ep_ids in existing_sigs:
                continue

            # Get summary content from backing memories
            summary_texts: list[str] = []
            all_domains: set[str] = set()
            for node in cluster_nodes:
                mem = await self._store.get_memory(node.memory_id)
                if mem:
                    summary_texts.append(mem.content)
                    all_domains.update(mem.domains)

            if not summary_texts:
                continue

            result = await synthesize_recurring_pattern(
                episode_summaries=summary_texts,
                shared_entities=list(shared_entities),
                model=self._config.consolidation_knowledge_model,
                api_base=self._config.consolidation_knowledge_api_base,
            )
            if not result:
                continue

            # Stability-based promotion
            confidence = result.get("confidence", 0.5)
            stability = min(1.0, len(cluster_nodes) / 5) * confidence
            if stability >= self._config.pattern_stability_threshold:
                abstract_type = "strategic_insight"
            else:
                abstract_type = "recurring_pattern"

            await self._store_abstract(
                content=result["pattern"],
                abstract_type=abstract_type,
                domains=list(all_domains),
                confidence=confidence,
                structured_extra={
                    "pattern_type": result.get("pattern_type", "unknown"),
                    "recurrence_count": result.get(
                        "recurrence_count",
                        len(cluster_nodes),
                    ),
                    "stability_score": round(stability, 3),
                    "key_entities": result.get("key_entities", []),
                    "source_episode_ids": list(ep_ids),
                },
                source_node_ids=[n.id for n in cluster_nodes],
                source_memory_ids=[n.memory_id for n in cluster_nodes],
            )
            patterns_created += 1

        await self._store.set_consolidation_value(
            "last_pattern_consolidation",
            datetime.now(UTC).isoformat(),
        )

        logger.info(
            "Pattern consolidation: created %d patterns/insights",
            patterns_created,
        )
        return patterns_created

    # ── Staleness Refresh ────────────────────────────────────────────────

    async def refresh_stale_abstracts(self) -> int:
        """Re-synthesize abstract nodes past their refresh_due_at.

        Returns the number of abstracts refreshed.
        """
        refreshed = 0
        for abstract_type in (
            "episode_summary",
            "state_trajectory",
            "recurring_pattern",
            "strategic_insight",
        ):
            nodes = await self._store.get_abstract_nodes_by_type(abstract_type)
            for node in nodes:
                if not self._is_stale(node):
                    continue

                # Reset refresh_due_at to push staleness forward
                node.metadata["refresh_due_at"] = (
                    datetime.now(UTC) + timedelta(days=self._config.abstract_refresh_days)
                ).isoformat()
                await self._store.update_memory_node(node)
                refreshed += 1

        if refreshed:
            logger.info("Refreshed %d stale abstract nodes", refreshed)
        return refreshed

    # ── Phase 8: Dream Cycle ────────────────────────────────────────────

    async def run_dream_rehearsal(self) -> int:
        """Pass-through to :mod:`ncms.application.consolidation.dream_pass`."""
        from ncms.application.consolidation import dream_pass as _dp

        return await _dp.run_dream_rehearsal(
            store=self._store, graph=self._graph, config=self._config
        )

    async def learn_association_strengths(self) -> int:
        """Pass-through to :mod:`ncms.application.consolidation.dream_pass`."""
        from ncms.application.consolidation import dream_pass as _dp

        return await _dp.learn_association_strengths(
            store=self._store, graph=self._graph, config=self._config
        )

    async def adjust_importance_drift(self) -> int:
        """Pass-through to :mod:`ncms.application.consolidation.dream_pass`."""
        from ncms.application.consolidation import dream_pass as _dp

        return await _dp.adjust_importance_drift(store=self._store, config=self._config)

    async def build_query_expansion_dict(self) -> int:
        """Pass-through to :mod:`ncms.application.consolidation.dream_pass`."""
        from ncms.application.consolidation import dream_pass as _dp

        return await _dp.build_query_expansion_dict(
            store=self._store, graph=self._graph, config=self._config
        )

    async def active_forgetting(self) -> int:
        """Pass-through to :mod:`ncms.application.consolidation.dream_pass`."""
        from ncms.application.consolidation import dream_pass as _dp

        return await _dp.active_forgetting(store=self._store, config=self._config)

    async def run_dream_cycle(self) -> dict[str, int]:
        """Pass-through to :mod:`ncms.application.consolidation.dream_pass`."""
        from ncms.application.consolidation import dream_pass as _dp

        return await _dp.run_dream_cycle(
            store=self._store,
            graph=self._graph,
            config=self._config,
            event_log=self._event_log,
        )

    # ── Orchestrator ─────────────────────────────────────────────────────

    async def run_consolidation_pass(self) -> dict[str, int]:
        """Run all consolidation subtasks in sequence.

        Returns a dict mapping task names to counts.
        """
        results: dict[str, int] = {}
        results["decay"] = await self.run_decay_pass()
        results["knowledge"] = await self.consolidate_knowledge()
        results["episodes"] = await self.consolidate_episodes()
        results["trajectories"] = await self.consolidate_trajectories()
        results["patterns"] = await self.consolidate_patterns()
        results["refresh"] = await self.refresh_stale_abstracts()

        # Phase 8: Dream cycle
        if self._config.dream_cycle_enabled:
            dream_results = await self.run_dream_cycle()
            results.update({f"dream_{k}": v for k, v in dream_results.items()})

        self._emit_pass_complete(results)
        logger.info("Consolidation pass complete: %s", results)
        return results

    # ── Private Helpers ──────────────────────────────────────────────────

    async def _store_abstract(
        self,
        content: str,
        abstract_type: str,
        domains: list[str],
        confidence: float,
        structured_extra: dict,
        source_node_ids: list[str],
        source_memory_ids: list[str],
        edge_type: str = "derived_from",
    ) -> tuple[Memory, MemoryNode]:
        """Shared tail for all consolidation methods: store insight + node + edges.

        Creates Memory(type=insight), MemoryNode(type=ABSTRACT), graph edges
        from abstract to source nodes, and bridges entity links from source
        memories to the abstract. Returns (memory, node).
        """
        abstract_memory = Memory(
            content=content,
            type="insight",
            domains=domains,
            importance=max(confidence * 10, 1.0),
            structured={
                "abstract_type": abstract_type,
                "confidence": confidence,
                "synthesis_model": self._config.consolidation_knowledge_model,
                **structured_extra,
            },
        )
        await self._store.save_memory(abstract_memory)
        await self._store.log_access(
            AccessRecord(memory_id=abstract_memory.id, accessing_agent="consolidation")
        )
        self._index_memory(abstract_memory)

        refresh_at = (
            datetime.now(UTC)
            + timedelta(
                days=self._config.abstract_refresh_days,
            )
        ).isoformat()
        abstract_node = MemoryNode(
            memory_id=abstract_memory.id,
            node_type=NodeType.ABSTRACT,
            importance=abstract_memory.importance,
            metadata={
                "abstract_type": abstract_type,
                "refresh_due_at": refresh_at,
                **structured_extra,
            },
        )
        await self._store.save_memory_node(abstract_node)

        # Graph edges to source nodes
        for src_id in source_node_ids:
            await self._store.save_graph_edge(
                GraphEdge(
                    source_id=abstract_node.id,
                    target_id=src_id,
                    edge_type=EdgeType(edge_type),
                )
            )

        # Bridge entity links from source memories to abstract
        if self._graph and source_memory_ids:
            source_entities = _collect_entity_ids(self._graph, source_memory_ids)
            for eid in source_entities:
                self._graph.link_memory_entity(abstract_memory.id, eid)

        self._emit_abstract_created(abstract_type, abstract_node.id, len(source_node_ids))
        return abstract_memory, abstract_node

    def _index_memory(self, memory: Memory) -> None:
        """Index a memory in Tantivy and optionally SPLADE."""
        if self._index:
            self._index.index_memory(memory)
        if self._splade is not None:
            try:
                self._splade.index_memory(memory)
            except Exception:
                logger.debug("SPLADE indexing failed for %s", memory.id, exc_info=True)

    def _is_stale(self, node: MemoryNode) -> bool:
        """Check if an abstract node has passed its refresh_due_at."""
        refresh_at = node.metadata.get("refresh_due_at")
        if not refresh_at:
            return False
        try:
            due = datetime.fromisoformat(refresh_at)
            return datetime.now(UTC) >= due
        except (ValueError, TypeError):
            return False

    def _cluster_by_entity_overlap(
        self,
        summary_nodes: list[MemoryNode],
    ) -> list[tuple[list[MemoryNode], set[str]]]:
        """Cluster episode summary nodes by topic_entities Jaccard overlap.

        Returns list of (cluster_nodes, shared_entities) tuples.
        """
        threshold = self._config.pattern_entity_overlap_threshold
        min_size = self._config.pattern_min_episodes

        # Build entity sets per node
        node_entities: list[tuple[MemoryNode, set[str]]] = []
        for node in summary_nodes:
            entities = set(node.metadata.get("topic_entities", []))
            if entities:
                node_entities.append((node, entities))

        if len(node_entities) < min_size:
            return []

        # Union-find for clustering
        n = len(node_entities)
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # Merge nodes with Jaccard overlap above threshold
        for i in range(n):
            for j in range(i + 1, n):
                e_i = node_entities[i][1]
                e_j = node_entities[j][1]
                intersection = len(e_i & e_j)
                union_size = len(e_i | e_j)
                if union_size > 0 and intersection / union_size >= threshold:
                    union(i, j)

        # Collect groups
        groups: dict[int, list[int]] = defaultdict(list)
        for idx in range(n):
            groups[find(idx)].append(idx)

        # Build clusters with shared entities
        clusters: list[tuple[list[MemoryNode], set[str]]] = []
        for member_indices in groups.values():
            if len(member_indices) < min_size:
                continue
            cluster_nodes = [node_entities[i][0] for i in member_indices]
            # Shared = intersection of all entity sets in cluster
            shared = node_entities[member_indices[0]][1].copy()
            for i in member_indices[1:]:
                shared &= node_entities[i][1]
            if not shared:
                # Fall back to entities appearing in 2+ nodes
                entity_counts: dict[str, int] = defaultdict(int)
                for i in member_indices:
                    for e in node_entities[i][1]:
                        entity_counts[e] += 1
                shared = {e for e, c in entity_counts.items() if c >= 2}
            clusters.append((cluster_nodes, shared))

        # Sort by cluster size (largest first)
        clusters.sort(key=lambda c: len(c[0]), reverse=True)
        return clusters

    def _emit_abstract_created(
        self,
        abstract_type: str,
        node_id: str,
        source_count: int,
    ) -> None:
        """Emit an abstract creation event if event_log is available."""
        if self._event_log is not None:
            with contextlib.suppress(Exception):
                self._event_log.consolidation_abstract_created(
                    abstract_type=abstract_type,
                    node_id=node_id,
                    source_count=source_count,
                )

    def _emit_pass_complete(self, results: dict[str, Any]) -> None:
        """Emit a consolidation pass complete event."""
        if self._event_log is not None:
            with contextlib.suppress(Exception):
                self._event_log.consolidation_pass_complete(results=results)

    def _emit_dream_cycle_complete(self, results: dict[str, int]) -> None:
        """Emit a dream cycle complete event."""
        if self._event_log is not None:
            with contextlib.suppress(Exception):
                self._event_log.dream_cycle_complete(results=results)
