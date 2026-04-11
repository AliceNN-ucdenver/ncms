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
                logger.debug(
                    "Memory %s below threshold: activation=%.2f", memory.id, bl
                )

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
        last_run = await self._store.get_consolidation_value(
            "last_knowledge_consolidation"
        )

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
                    await self._store.link_memory_entity(
                        insight_memory.id, entity.id
                    )
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

            # Create backing Memory(type="insight") for Tantivy/SPLADE indexing
            abstract_memory = Memory(
                content=result["summary"],
                type="insight",
                domains=list(member_domains),
                importance=result.get("confidence", 0.5) * 10,
                structured={
                    "abstract_type": "episode_summary",
                    "source_episode_id": ep_node.id,
                    "actors": result.get("actors", []),
                    "artifacts": result.get("artifacts", []),
                    "decisions": result.get("decisions", []),
                    "outcome": result.get("outcome", ""),
                    "confidence": result.get("confidence", 0.5),
                    "synthesis_model": self._config.consolidation_knowledge_model,
                },
            )
            await self._store.save_memory(abstract_memory)
            await self._store.log_access(
                AccessRecord(memory_id=abstract_memory.id, accessing_agent="consolidation")
            )
            self._index_memory(abstract_memory)

            # Create HTMG abstract node
            refresh_at = (datetime.now(UTC) + timedelta(
                days=self._config.abstract_refresh_days
            )).isoformat()
            abstract_node = MemoryNode(
                memory_id=abstract_memory.id,
                node_type=NodeType.ABSTRACT,
                importance=abstract_memory.importance,
                metadata={
                    "abstract_type": "episode_summary",
                    "source_episode_id": ep_node.id,
                    "episode_title": title,
                    "actors": result.get("actors", []),
                    "topic_entities": meta.get("topic_entities", []),
                    "refresh_due_at": refresh_at,
                },
            )
            await self._store.save_memory_node(abstract_node)

            # Create graph edges (SQLite HTMG)
            await self._store.save_graph_edge(GraphEdge(
                source_id=abstract_node.id,
                target_id=ep_node.id,
                edge_type=EdgeType.SUMMARIZES,
            ))
            for member in members:
                await self._store.save_graph_edge(GraphEdge(
                    source_id=abstract_node.id,
                    target_id=member.id,
                    edge_type=EdgeType.DERIVED_FROM,
                ))

            # Bridge entity links from source members to abstract memory
            # so graph traversal can discover summaries through shared entities
            if self._graph:
                member_mids = [m.memory_id for m in members]
                source_entities = _collect_entity_ids(self._graph, member_mids)
                for eid in source_entities:
                    self._graph.link_memory_entity(abstract_memory.id, eid)

            # Mark episode as summarized
            ep_node.metadata["summarized"] = True
            ep_node.metadata["summary_node_id"] = abstract_node.id
            await self._store.update_memory_node(ep_node)

            # Emit event
            self._emit_abstract_created("episode_summary", abstract_node.id, len(members))

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
            n.metadata.get("entity_id") for n in existing
            if not self._is_stale(n)
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

                # Create backing Memory
                abstract_memory = Memory(
                    content=result["narrative"],
                    type="insight",
                    importance=result.get("confidence", 0.5) * 10,
                    structured={
                        "abstract_type": "state_trajectory",
                        "entity_id": entity_id,
                        "entity_name": entity_name,
                        "state_key": state_key,
                        "trend": result.get("trend", "unknown"),
                        "key_transitions": result.get("key_transitions", []),
                        "confidence": result.get("confidence", 0.5),
                        "synthesis_model": self._config.consolidation_knowledge_model,
                    },
                )
                await self._store.save_memory(abstract_memory)
                await self._store.log_access(
                    AccessRecord(
                        memory_id=abstract_memory.id, accessing_agent="consolidation"
                    )
                )
                self._index_memory(abstract_memory)

                # Create HTMG abstract node
                refresh_at = (datetime.now(UTC) + timedelta(
                    days=self._config.abstract_refresh_days
                )).isoformat()
                abstract_node = MemoryNode(
                    memory_id=abstract_memory.id,
                    node_type=NodeType.ABSTRACT,
                    importance=abstract_memory.importance,
                    metadata={
                        "abstract_type": "state_trajectory",
                        "entity_id": entity_id,
                        "entity_name": entity_name,
                        "state_key": state_key,
                        "trend": result.get("trend", "unknown"),
                        "transition_count": len(key_states),
                        "refresh_due_at": refresh_at,
                    },
                )
                await self._store.save_memory_node(abstract_node)

                # DERIVED_FROM edges to each component state
                for state_node in key_states:
                    await self._store.save_graph_edge(GraphEdge(
                        source_id=abstract_node.id,
                        target_id=state_node.id,
                        edge_type=EdgeType.DERIVED_FROM,
                    ))

                # Bridge entity links from source states to trajectory abstract
                if self._graph:
                    state_mids = [s.memory_id for s in key_states]
                    source_entities = _collect_entity_ids(self._graph, state_mids)
                    for eid in source_entities:
                        self._graph.link_memory_entity(abstract_memory.id, eid)

                self._emit_abstract_created(
                    "state_trajectory", abstract_node.id, len(key_states)
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

            ep_ids = frozenset(
                n.metadata.get("source_episode_id", n.id) for n in cluster_nodes
            )
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

            # Create backing Memory
            abstract_memory = Memory(
                content=result["pattern"],
                type="insight",
                domains=list(all_domains),
                importance=confidence * 10,
                structured={
                    "abstract_type": abstract_type,
                    "pattern_type": result.get("pattern_type", "unknown"),
                    "recurrence_count": result.get("recurrence_count", len(cluster_nodes)),
                    "confidence": confidence,
                    "stability_score": round(stability, 3),
                    "key_entities": result.get("key_entities", []),
                    "source_episode_ids": list(ep_ids),
                    "synthesis_model": self._config.consolidation_knowledge_model,
                },
            )
            await self._store.save_memory(abstract_memory)
            await self._store.log_access(
                AccessRecord(memory_id=abstract_memory.id, accessing_agent="consolidation")
            )
            self._index_memory(abstract_memory)

            # Create HTMG abstract node
            refresh_at = (datetime.now(UTC) + timedelta(
                days=self._config.abstract_refresh_days
            )).isoformat()
            abstract_node = MemoryNode(
                memory_id=abstract_memory.id,
                node_type=NodeType.ABSTRACT,
                importance=abstract_memory.importance,
                metadata={
                    "abstract_type": abstract_type,
                    "pattern_type": result.get("pattern_type", "unknown"),
                    "stability_score": round(stability, 3),
                    "source_episode_ids": list(ep_ids),
                    "key_entities": result.get("key_entities", []),
                    "refresh_due_at": refresh_at,
                },
            )
            await self._store.save_memory_node(abstract_node)

            # DERIVED_FROM edges to each source episode summary
            for summary_node in cluster_nodes:
                await self._store.save_graph_edge(GraphEdge(
                    source_id=abstract_node.id,
                    target_id=summary_node.id,
                    edge_type=EdgeType.DERIVED_FROM,
                ))

            # Bridge entity links from source summaries to pattern abstract
            if self._graph:
                summary_mids = [s.memory_id for s in cluster_nodes]
                source_entities = _collect_entity_ids(self._graph, summary_mids)
                for eid in source_entities:
                    self._graph.link_memory_entity(abstract_memory.id, eid)

            self._emit_abstract_created(abstract_type, abstract_node.id, len(cluster_nodes))
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
        for abstract_type in ("episode_summary", "state_trajectory", "recurring_pattern",
                              "strategic_insight"):
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
        """Select important memories and inject synthetic access records.

        Uses a 5-signal weighted selector:
        - PageRank centrality (entity graph importance)
        - Staleness (days since last access)
        - Memory importance score
        - Access count
        - Inverse recency (penalise very recently accessed)

        Returns the number of memories rehearsed.
        """
        if not self._config.dream_cycle_enabled:
            return 0
        if not self._graph:
            logger.warning("Graph not available, skipping dream rehearsal")
            return 0

        # 1. Compute PageRank centrality over entity graph
        centrality = self._graph.pagerank()

        # 2. Load all memories
        memories = await self._store.list_memories(limit=100000)
        if not memories:
            return 0

        # 3. For each memory, compute selection signals
        candidates: list[dict[str, Any]] = []
        for memory in memories:
            access_ages = await self._store.get_access_times(memory.id)
            access_count = len(access_ages)

            # Skip memories with too few accesses
            if access_count < self._config.dream_min_access_count:
                continue

            # Centrality: average PageRank of linked entities
            entity_ids = self._graph.get_entity_ids_for_memory(memory.id)
            mem_centrality = 0.0
            if entity_ids and centrality:
                scores = [centrality.get(eid, 0.0) for eid in entity_ids]
                mem_centrality = sum(scores) / len(scores) if scores else 0.0

            # Staleness: days since last access
            last_access_age = min(access_ages) if access_ages else float("inf")
            staleness = last_access_age / 86400.0  # Convert seconds to days

            # Importance: memory.importance (already 0-10 scale)
            importance = memory.importance

            # Recency: invert — recently accessed get lower dream priority
            recency = last_access_age / 86400.0 if access_ages else 0.0

            candidates.append({
                "memory": memory,
                "centrality": mem_centrality,
                "staleness": staleness,
                "importance": importance,
                "access_count": float(access_count),
                "recency": recency,
            })

        if not candidates:
            return 0

        # 4. Rank-normalize each signal to [0, 1]
        for signal in ("centrality", "staleness", "importance", "access_count", "recency"):
            values = [c[signal] for c in candidates]
            min_val, max_val = min(values), max(values)
            range_val = max_val - min_val
            for c in candidates:
                c[f"{signal}_norm"] = (
                    (c[signal] - min_val) / range_val if range_val > 0 else 0.5
                )

        # 5. Compute weighted dream score
        cfg = self._config
        for c in candidates:
            c["dream_score"] = (
                cfg.dream_rehearsal_weight_centrality * c["centrality_norm"]
                + cfg.dream_rehearsal_weight_staleness * c["staleness_norm"]
                + cfg.dream_rehearsal_weight_importance * c["importance_norm"]
                + cfg.dream_rehearsal_weight_access_count * c["access_count_norm"]
                + cfg.dream_rehearsal_weight_recency * c["recency_norm"]
            )

        # 6. Select top fraction
        candidates.sort(key=lambda c: c["dream_score"], reverse=True)
        n_rehearse = max(1, int(len(candidates) * self._config.dream_rehearsal_fraction))
        selected = candidates[:n_rehearse]

        # 7. Inject differential synthetic access records
        # Scale accesses proportionally to dream score: top memories get 5 accesses,
        # lowest get 1. This creates the differential access patterns that ACT-R needs
        # instead of uniform injection that compresses the activation range.
        rehearsed = 0
        if selected:
            max_dream = selected[0]["dream_score"]  # Already sorted descending
            min_dream = selected[-1]["dream_score"]
            dream_range = max_dream - min_dream

        for c in selected:
            memory = c["memory"]
            # Map dream_score to 1-5 accesses (linear interpolation)
            if dream_range > 0:
                normalized = (c["dream_score"] - min_dream) / dream_range
                n_accesses = 1 + int(normalized * 4)  # 1 to 5
            else:
                n_accesses = 1

            for _i in range(n_accesses):
                await self._store.log_access(
                    AccessRecord(
                        memory_id=memory.id,
                        accessing_agent="dream_rehearsal",
                        query_context=(
                            f"dream_cycle:score={c['dream_score']:.3f}"
                            f":accesses={n_accesses}"
                        ),
                    )
                )
            rehearsed += 1

        logger.info(
            "Dream rehearsal: rehearsed %d/%d eligible memories (from %d total)",
            rehearsed, len(candidates), len(memories),
        )
        return rehearsed

    async def learn_association_strengths(self) -> int:
        """Compute PMI association strengths from search co-access patterns.

        For each search result set, entities that co-occur in returned
        memories are associated.  PMI(a,b) = log(P(a,b) / P(a)*P(b)).

        Returns the number of association pairs saved.
        """
        if not self._config.dream_cycle_enabled:
            return 0

        import math

        # Get last run timestamp
        last_run = await self._store.get_consolidation_value(
            "last_association_learning"
        )

        # Get search-result pairs since last run
        pairs = await self._store.get_search_access_pairs(since=last_run)
        if not pairs:
            return 0

        # Collect entity co-occurrences from search results
        entity_count: dict[str, int] = defaultdict(int)  # P(entity)
        pair_count: dict[tuple[str, str], int] = defaultdict(int)  # P(a, b)
        total_searches = 0

        for _query, returned_ids in pairs:
            if not returned_ids:
                continue
            total_searches += 1

            # Collect all entities from returned memories
            search_entities: set[str] = set()
            for memory_id in returned_ids:
                entity_ids = (
                    self._graph.get_entity_ids_for_memory(memory_id)
                    if self._graph else set()
                )
                search_entities.update(entity_ids)

            # Count individual entities
            for eid in search_entities:
                entity_count[eid] += 1

            # Count co-occurring pairs (canonical ordering)
            entity_list = sorted(search_entities)
            for i, e1 in enumerate(entity_list):
                for e2 in entity_list[i + 1:]:
                    pair_count[(e1, e2)] += 1

        if total_searches == 0 or not pair_count:
            return 0

        # Compute PMI for each pair
        saved = 0
        for (e1, e2), co_count in pair_count.items():
            p_ab = co_count / total_searches
            p_a = entity_count[e1] / total_searches
            p_b = entity_count[e2] / total_searches

            if p_a > 0 and p_b > 0 and p_ab > 0:
                pmi = math.log(p_ab / (p_a * p_b))
                # Clamp to [0, 10] and normalize to [0, 1]
                pmi_clamped = max(0.0, min(10.0, pmi))
                strength = pmi_clamped / 10.0

                if strength > 0.01:  # Skip negligible associations
                    await self._store.save_association_strength(e1, e2, strength)
                    saved += 1

        # Update last run timestamp
        await self._store.set_consolidation_value(
            "last_association_learning",
            datetime.now(UTC).isoformat(),
        )

        logger.info(
            "Association learning: saved %d pairs from %d searches",
            saved, total_searches,
        )

        # Bridge learned associations into the NetworkX graph for spreading activation
        if saved > 0 and self._graph:
            bridged = self._bridge_pmi_to_graph(pair_count, total_searches, entity_count)
            logger.info(
                "PMI-to-graph bridge: updated %d edge weights", bridged,
            )

        return saved

    def _bridge_pmi_to_graph(
        self,
        pair_count: dict[tuple[str, str], int],
        total_searches: int,
        entity_count: dict[str, int],
    ) -> int:
        """Bridge dream PMI associations into NetworkX graph edge weights.

        For existing co-occurrence edges, updates their weight with the learned
        PMI value (blended with structural PMI). For high-PMI pairs without
        edges, creates new edges so graph traversal can discover them.

        Returns the number of edge weights updated or edges created.
        """
        import math as _math

        if not pair_count or total_searches == 0:
            return 0

        # Compute PMI for each pair and find max for normalization
        pmi_values: list[tuple[str, str, float]] = []
        max_pmi = 0.01

        for (e1, e2), co_count in pair_count.items():
            p_ab = co_count / total_searches
            p_a = entity_count.get(e1, 1) / total_searches
            p_b = entity_count.get(e2, 1) / total_searches

            if p_a > 0 and p_b > 0 and p_ab > 0:
                pmi = _math.log2(p_ab / (p_a * p_b))
                pmi = max(pmi, 0.0)
            else:
                pmi = 0.0

            if pmi > 0.01:
                pmi_values.append((e1, e2, pmi))
                if pmi > max_pmi:
                    max_pmi = pmi

        if self._graph is None:
            return 0

        updated = 0
        for e1, e2, pmi in pmi_values:
            weight = max(0.01, pmi / max_pmi)

            # Update existing edge weight (blend: 0.3 structural + 0.7 learned)
            existing_fwd = self._graph.get_edge_weight(e1, e2)
            existing_rev = self._graph.get_edge_weight(e2, e1)

            if existing_fwd > 0:
                blended = 0.3 * existing_fwd + 0.7 * weight
                self._graph.set_edge_weight(e1, e2, blended)
                updated += 1
            if existing_rev > 0:
                blended = 0.3 * existing_rev + 0.7 * weight
                self._graph.set_edge_weight(e2, e1, blended)
                updated += 1

            # For top-PMI pairs without edges, create new edges
            # (only if PMI is in top 10% to avoid graph explosion)
            if existing_fwd == 0 and existing_rev == 0 and weight > 0.9:
                from ncms.domain.models import Relationship

                self._graph.add_relationship(Relationship(
                    source_entity_id=e1,
                    target_entity_id=e2,
                    type="learned_association",
                    source_memory_id="dream_pmi",
                ))
                self._graph.set_edge_weight(e1, e2, weight)
                updated += 1

        return updated

    async def adjust_importance_drift(self) -> int:
        """Adjust memory importance based on access rate trends.

        Compares recent access rate (last window/2) vs older rate (window/2
        before that). Memories accessed more recently get importance bumped
        up; memories accessed less get bumped down.

        Returns the number of memories adjusted.
        """
        if not self._config.dream_cycle_enabled:
            return 0

        half_window = timedelta(
            days=self._config.dream_importance_drift_window_days / 2.0
        )
        window = timedelta(days=self._config.dream_importance_drift_window_days)
        drift_rate = self._config.dream_importance_drift_rate

        memories = await self._store.list_memories(limit=100000)
        adjusted = 0

        for memory in memories:
            access_ages = await self._store.get_access_times(memory.id)
            if len(access_ages) < 2:
                continue

            # Split accesses into recent (last half window) and older (before that)
            recent_count = sum(
                1 for age in access_ages if age < half_window.total_seconds()
            )
            older_count = sum(
                1 for age in access_ages
                if half_window.total_seconds() <= age < window.total_seconds()
            )

            # Compute rates (accesses per day)
            half_days = half_window.total_seconds() / 86400.0
            recent_rate = recent_count / half_days if half_days > 0 else 0
            older_rate = older_count / half_days if half_days > 0 else 0

            # Determine drift direction
            if recent_rate > older_rate * 1.5:
                # Momentum up — accessed more recently
                delta = drift_rate
            elif older_rate > recent_rate * 1.5:
                # Momentum down — access dropping off
                delta = -drift_rate
            else:
                continue

            # Apply drift (clamp importance to [0.0, 10.0])
            new_importance = max(0.0, min(10.0, memory.importance + delta))
            if abs(new_importance - memory.importance) > 0.001:
                memory.importance = new_importance
                await self._store.update_memory(memory)
                adjusted += 1

        logger.info(
            "Importance drift: adjusted %d/%d memories", adjusted, len(memories),
        )
        return adjusted

    async def build_query_expansion_dict(self) -> int:
        """Build PMI-based query expansion dictionary (Phase 9 REM phase).

        For each high-PMI entity pair, creates bidirectional expansion entries:
        entity_name_a → [entity_name_b, ...] and vice versa.

        Stored as JSON in consolidation_state for use by the search pipeline.

        Returns the number of entity pairs included.
        """
        if not self._config.dream_query_expansion_enabled:
            return 0

        import json as _json

        min_pmi = self._config.dream_expansion_min_pmi
        max_terms = self._config.dream_expansion_max_terms

        # Load only strong associations (SQL-filtered, not full 3M+ table)
        strong = await self._store.get_strong_associations(
            min_strength=min_pmi, limit=100_000,
        )
        if not strong:
            logger.info("Query expansion: no associations above min_pmi=%.2f", min_pmi)
            return 0

        # Build bidirectional expansion map (entity_id → [(entity_id, strength)])
        raw_expansions: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for e1, e2, strength in strong:
            raw_expansions[e1].append((e2, strength))
            raw_expansions[e2].append((e1, strength))

        if not raw_expansions:
            return 0

        # Resolve entity IDs to names for BM25 query expansion
        entity_name_cache: dict[str, str] = {}
        if self._graph:
            all_eids = set(raw_expansions.keys())
            for candidates in raw_expansions.values():
                for eid, _ in candidates:
                    all_eids.add(eid)
            for eid in all_eids:
                name = self._graph.get_entity_name(eid)
                if name:
                    entity_name_cache[eid] = name

        # Sort by strength (descending) and take top-K per entity, storing names
        expansion_dict: dict[str, list[str]] = {}
        total_pairs = 0
        for eid, candidates in raw_expansions.items():
            candidates.sort(key=lambda x: x[1], reverse=True)
            # Map target entity IDs to names for BM25 matching
            top_names = [
                entity_name_cache[c[0]]
                for c in candidates[:max_terms]
                if c[0] in entity_name_cache
            ]
            if top_names:
                expansion_dict[eid] = top_names
                total_pairs += len(top_names)

        # Store as JSON in consolidation_state
        await self._store.set_consolidation_value(
            "query_expansion_dict",
            _json.dumps(expansion_dict),
        )

        logger.info(
            "Query expansion dict: %d entities with %d total expansion terms",
            len(expansion_dict), total_pairs,
        )
        return total_pairs

    async def active_forgetting(self) -> int:
        """Suppress superseded/conflicting memories (Phase 9 — SleepGate-inspired).

        - Superseded memories (is_current=False): reduce importance per cycle
        - Prune old access records for superseded memories
        - Conflicting memories with unresolved conflicts: reduce importance

        Returns the number of memories affected.
        """
        if not self._config.dream_active_forgetting_enabled:
            return 0

        decay_rate = self._config.dream_forgetting_decay_rate
        access_prune_days = self._config.dream_forgetting_access_prune_days
        conflict_age_days = self._config.dream_forgetting_conflict_age_days

        affected = 0

        # Phase 4a: Decay superseded memories
        try:
            from ncms.domain.models import NodeType
            superseded_nodes = await self._store.get_memory_nodes_by_type(
                NodeType.ENTITY_STATE.value
            )
            for node in superseded_nodes:
                if node.is_current:
                    continue
                # Reduce importance of the source memory
                memory = await self._store.get_memory(node.memory_id)
                if not memory:
                    continue
                new_importance = max(0.0, memory.importance - decay_rate)
                if abs(new_importance - memory.importance) > 0.001:
                    memory.importance = new_importance
                    await self._store.update_memory(memory)
                    affected += 1

                # Prune old access records
                try:
                    pruned = await self._store.prune_access_records(
                        node.memory_id, access_prune_days,
                    )
                    if pruned > 0:
                        logger.debug(
                            "Pruned %d access records for superseded memory %s",
                            pruned, node.memory_id,
                        )
                except Exception:
                    pass  # prune_access_records may not be implemented yet

        except Exception:
            logger.debug("Active forgetting (supersession) failed", exc_info=True)

        # Phase 4b: Reduce importance of unresolved conflict memories
        try:
            from ncms.domain.models import EdgeType, NodeType

            cutoff = (
                datetime.now(UTC) - timedelta(days=conflict_age_days)
            ).isoformat()

            entity_state_nodes = await self._store.get_memory_nodes_by_type(
                NodeType.ENTITY_STATE.value
            )
            for node in entity_state_nodes:
                if not node.is_current:
                    continue
                conflict_edges = await self._store.get_graph_edges(
                    node.id, EdgeType.CONFLICTS_WITH,
                )
                if not conflict_edges:
                    continue
                # Check if conflict is old enough
                if node.metadata.get("ingested_at", "") < cutoff:
                    memory = await self._store.get_memory(node.memory_id)
                    if memory:
                        new_importance = max(0.0, memory.importance - decay_rate * 0.5)
                        if abs(new_importance - memory.importance) > 0.001:
                            memory.importance = new_importance
                            await self._store.update_memory(memory)
                            affected += 1
        except Exception:
            logger.debug("Active forgetting (conflicts) failed", exc_info=True)

        logger.info("Active forgetting: affected %d memories", affected)
        return affected

    async def run_dream_cycle(self) -> dict[str, int]:
        """Run full dream cycle: rehearsal → associations → expansion → forgetting → drift.

        Each phase is wrapped in suppress(Exception) so failures are non-fatal.
        Returns a dict mapping phase names to counts.
        """
        if not self._config.dream_cycle_enabled:
            return {"rehearsal": 0, "associations": 0, "drift": 0}

        results: dict[str, int] = {}

        with contextlib.suppress(Exception):
            results["rehearsal"] = await self.run_dream_rehearsal()

        with contextlib.suppress(Exception):
            results["associations"] = await self.learn_association_strengths()

        # Phase 9: Query expansion dict (REM phase)
        try:
            results["query_expansion"] = await self.build_query_expansion_dict()
        except Exception:
            logger.warning("Query expansion dict failed", exc_info=True)

        # Phase 9: Active forgetting
        try:
            results["forgetting"] = await self.active_forgetting()
        except Exception:
            logger.warning("Active forgetting failed", exc_info=True)

        with contextlib.suppress(Exception):
            results["drift"] = await self.adjust_importance_drift()

        # Fill defaults for any phases that raised
        results.setdefault("rehearsal", 0)
        results.setdefault("associations", 0)
        results.setdefault("query_expansion", 0)
        results.setdefault("forgetting", 0)
        results.setdefault("drift", 0)

        self._emit_dream_cycle_complete(results)
        logger.info("Dream cycle complete: %s", results)
        return results

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
        self, abstract_type: str, node_id: str, source_count: int,
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
