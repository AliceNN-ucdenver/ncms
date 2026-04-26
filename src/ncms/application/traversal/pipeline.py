"""Traversal pipeline: HTMG hierarchy walks and topic clustering.

Four traversal strategies walk the hierarchical typed memory graph
from a seed memory:

- **top_down**: abstract → episodes it summarizes → atomic members
- **bottom_up**: atomic → episode membership → abstract summaries
- **temporal**: entity state timeline for entities mentioned in seed
- **lateral**: episode siblings + related episodes via shared entities

Plus ``get_topic_map`` — Jaccard-overlap clustering of L4 abstract
nodes into emergent topics.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ncms.domain.models import (
    EdgeType,
    EpisodeMeta,
    Memory,
    RecallContext,
    RecallResult,
    ScoredMemory,
    TopicCluster,
)

if TYPE_CHECKING:
    from ncms.config import NCMSConfig
    from ncms.domain.protocols import GraphEngine, MemoryStore

logger = logging.getLogger(__name__)


class TraversalPipeline:
    """HTMG traversal strategies and topic clustering.

    Dependencies are injected via constructor; every method takes its
    inputs explicitly so stages can be tested in isolation.
    """

    def __init__(
        self,
        store: MemoryStore,
        graph: GraphEngine,
        config: NCMSConfig,
    ) -> None:
        self._store = store
        self._graph = graph
        self._config = config

    # ── Top-Down: Abstract → Episodes → Atomic ──────────────────────────

    async def traverse_top_down(
        self,
        seed_memory: Memory,
        seed_nodes: list,
        limit: int,
    ) -> tuple[list, int, list]:
        """Abstract → episodes it summarizes → atomic members."""
        results: list[RecallResult] = []
        path: list[str] = [seed_memory.id]
        levels = 0
        seen: set[str] = {seed_memory.id}

        # Level 1: Episodes this abstract summarizes
        episode_node_ids: list[str] = []
        for node in seed_nodes:
            edges = await self._store.get_graph_edges(node.id)
            for edge in edges:
                if (
                    edge.edge_type
                    in (
                        EdgeType.SUMMARIZES,
                        EdgeType.ABSTRACTS,
                    )
                    and edge.target_id not in seen
                ):
                    episode_node_ids.append(edge.target_id)
                    seen.add(edge.target_id)

        if episode_node_ids:
            levels += 1
            for ep_node_id in episode_node_ids[:limit]:
                ep_node = await self._store.get_memory_node(ep_node_id)
                if ep_node:
                    mem = await self._store.get_memory(ep_node.memory_id)
                    if mem and mem.id not in seen:
                        seen.add(mem.id)
                        path.append(mem.id)
                        results.append(
                            RecallResult(
                                memory=ScoredMemory(memory=mem),
                                context=RecallContext(),
                                retrieval_path="top_down:episode",
                            )
                        )

        # Level 2: Atomic members of those episodes
        if episode_node_ids and len(results) < limit:
            levels += 1
            for ep_node_id in episode_node_ids:
                members = await self._store.get_episode_members(ep_node_id)
                for member in members:
                    if len(results) >= limit:
                        break
                    if member.memory_id not in seen:
                        seen.add(member.memory_id)
                        mem = await self._store.get_memory(member.memory_id)
                        if mem:
                            path.append(mem.id)
                            results.append(
                                RecallResult(
                                    memory=ScoredMemory(memory=mem),
                                    context=RecallContext(),
                                    retrieval_path="top_down:atomic",
                                )
                            )

        return results, levels, path

    # ── Bottom-Up: Atomic → Episode → Abstract ──────────────────────────

    async def traverse_bottom_up(
        self,
        seed_memory: Memory,
        seed_nodes: list,
        limit: int,
    ) -> tuple[list, int, list]:
        """Atomic → episode membership → abstract summaries."""
        results: list[RecallResult] = []
        path: list[str] = [seed_memory.id]
        levels = 0
        seen: set[str] = {seed_memory.id}

        # Level 1: Episode(s) this memory belongs to
        episode_node_ids: list[str] = []
        for node in seed_nodes:
            edges = await self._store.get_graph_edges(node.id)
            for edge in edges:
                if edge.edge_type == EdgeType.BELONGS_TO_EPISODE:
                    episode_node_ids.append(edge.target_id)

        if episode_node_ids:
            levels += 1
            for ep_node_id in episode_node_ids[:limit]:
                ep_node = await self._store.get_memory_node(ep_node_id)
                if ep_node and ep_node.memory_id not in seen:
                    seen.add(ep_node.memory_id)
                    mem = await self._store.get_memory(ep_node.memory_id)
                    if mem:
                        path.append(mem.id)
                        results.append(
                            RecallResult(
                                memory=ScoredMemory(memory=mem),
                                context=RecallContext(),
                                retrieval_path="bottom_up:episode",
                            )
                        )

        # Level 2: Abstracts that summarize those episodes
        if episode_node_ids and len(results) < limit:
            levels += 1
            for ep_node_id in episode_node_ids:
                ep_edges = await self._store.get_graph_edges(ep_node_id)
                for edge in ep_edges:
                    if edge.edge_type == EdgeType.SUMMARIZES:
                        abs_node = await self._store.get_memory_node(
                            edge.source_id,
                        )
                        if abs_node and abs_node.memory_id not in seen:
                            seen.add(abs_node.memory_id)
                            mem = await self._store.get_memory(
                                abs_node.memory_id,
                            )
                            if mem:
                                path.append(mem.id)
                                results.append(
                                    RecallResult(
                                        memory=ScoredMemory(memory=mem),
                                        context=RecallContext(),
                                        retrieval_path="bottom_up:abstract",
                                    )
                                )

        return results, levels, path

    # ── Temporal: Entity State Timeline ─────────────────────────────────

    async def traverse_temporal(
        self,
        seed_memory: Memory,
        seed_nodes: list,
        limit: int,
    ) -> tuple[list, int, list]:
        """Entity state timeline for entities mentioned in the seed."""
        results: list[RecallResult] = []
        path: list[str] = [seed_memory.id]
        seen: set[str] = {seed_memory.id}

        # Find entities linked to seed memory
        entity_ids = self._graph.get_entity_ids_for_memory(seed_memory.id)

        for entity_id in entity_ids[:5]:  # Cap to avoid explosion
            states = await self._store.get_entity_states_by_entity(entity_id)
            # Sort by observed_at for timeline ordering
            states.sort(key=lambda s: s.observed_at or s.created_at)
            for state_node in states:
                if len(results) >= limit:
                    break
                if state_node.memory_id not in seen:
                    seen.add(state_node.memory_id)
                    mem = await self._store.get_memory(
                        state_node.memory_id,
                    )
                    if mem:
                        path.append(mem.id)
                        results.append(
                            RecallResult(
                                memory=ScoredMemory(memory=mem),
                                context=RecallContext(),
                                retrieval_path="temporal:state_timeline",
                            )
                        )

        levels = 1 if results else 0
        return results, levels, path

    # ── Lateral: Episode Siblings + Related Episodes ────────────────────

    async def traverse_lateral(
        self,
        seed_memory: Memory,
        seed_nodes: list,
        limit: int,
    ) -> tuple[list, int, list]:
        """Episode siblings + related episodes via shared entities."""
        results: list[RecallResult] = []
        path: list[str] = [seed_memory.id]
        levels = 0
        seen: set[str] = {seed_memory.id}

        # Level 1: Sibling memories in the same episode(s)
        episode_node_ids = await self._collect_seed_episodes(seed_nodes)
        if episode_node_ids:
            levels += 1
            await self._collect_episode_siblings(
                episode_node_ids,
                results,
                path,
                seen,
                limit,
            )

        # Level 2: Related episodes via shared topic entities
        if episode_node_ids and len(results) < limit:
            levels += 1
            await self._collect_related_episodes(
                episode_node_ids,
                results,
                path,
                seen,
                limit,
            )

        return results, levels, path

    async def _collect_seed_episodes(
        self,
        seed_nodes: list,
    ) -> list[str]:
        episode_node_ids: list[str] = []
        for node in seed_nodes:
            edges = await self._store.get_graph_edges(node.id)
            for edge in edges:
                if edge.edge_type == EdgeType.BELONGS_TO_EPISODE:
                    episode_node_ids.append(edge.target_id)
        return episode_node_ids

    async def _collect_episode_siblings(
        self,
        episode_node_ids: list[str],
        results: list,
        path: list[str],
        seen: set[str],
        limit: int,
    ) -> None:
        for ep_node_id in episode_node_ids:
            members = await self._store.get_episode_members(ep_node_id)
            for member in members:
                if len(results) >= limit:
                    return
                if member.memory_id not in seen:
                    seen.add(member.memory_id)
                    mem = await self._store.get_memory(member.memory_id)
                    if mem:
                        path.append(mem.id)
                        results.append(
                            RecallResult(
                                memory=ScoredMemory(memory=mem),
                                context=RecallContext(),
                                retrieval_path="lateral:sibling",
                            )
                        )

    async def _collect_related_episodes(
        self,
        episode_node_ids: list[str],
        results: list,
        path: list[str],
        seen: set[str],
        limit: int,
    ) -> None:
        seed_entities: set[str] = set()
        for ep_id in episode_node_ids:
            ep_node = await self._store.get_memory_node(ep_id)
            if ep_node:
                meta = EpisodeMeta.from_node(ep_node)
                if meta:
                    seed_entities.update(meta.topic_entities)

        if not seed_entities:
            return

        all_episodes = await self._store.get_memory_nodes_by_type("episode")
        for ep in all_episodes:
            if ep.id in episode_node_ids:
                continue
            meta = EpisodeMeta.from_node(ep)
            if not meta:
                continue
            overlap = seed_entities & set(meta.topic_entities)
            if overlap and ep.memory_id not in seen:
                seen.add(ep.memory_id)
                mem = await self._store.get_memory(ep.memory_id)
                if mem:
                    path.append(mem.id)
                    results.append(
                        RecallResult(
                            memory=ScoredMemory(memory=mem),
                            context=RecallContext(),
                            retrieval_path="lateral:related_episode",
                        )
                    )
                    if len(results) >= limit:
                        return

    # ── Topic Map (L4 clustering) ───────────────────────────────────────

    async def get_topic_map(self) -> list[TopicCluster]:
        """Generate emergent topic map from L4 abstract clustering.

        Clusters abstract nodes by shared topic_entities using Jaccard
        overlap.  Returns topic clusters ordered by size.
        """
        if not self._config.topic_map_enabled:
            return []

        abstracts = await self._store.get_memory_nodes_by_type("abstract")
        if len(abstracts) < self._config.topic_map_min_abstracts:
            return []

        # Extract entity sets per abstract
        abstract_entities: dict[str, set[str]] = {}
        abstract_episodes: dict[str, list[str]] = {}
        for node in abstracts:
            meta = node.metadata or {}
            entities = set(meta.get("topic_entities", []) or meta.get("key_entities", []))
            if entities:
                abstract_entities[node.memory_id] = entities
                src_eps = meta.get("source_episode_ids", [])
                abstract_episodes[node.memory_id] = src_eps if src_eps else []

        if not abstract_entities:
            return []

        clusters = self._cluster_abstracts_by_overlap(
            abstract_entities,
            abstract_episodes,
            len(abstracts),
        )
        clusters.sort(key=lambda c: c.member_count, reverse=True)
        logger.info(
            "[topic_map] Generated %d topic clusters from %d abstracts",
            len(clusters),
            len(abstracts),
        )
        return clusters

    def _cluster_abstracts_by_overlap(
        self,
        abstract_entities: dict[str, set[str]],
        abstract_episodes: dict[str, list[str]],
        total_abstracts: int,
    ) -> list[TopicCluster]:
        """Greedy clustering of abstracts by Jaccard entity overlap."""
        threshold = self._config.topic_map_entity_overlap
        unclustered = set(abstract_entities.keys())
        clusters: list[TopicCluster] = []

        while unclustered:
            seed_id = next(iter(unclustered))
            unclustered.discard(seed_id)
            cluster_ids = [seed_id]
            cluster_entities = set(abstract_entities[seed_id])

            # Grow cluster greedily until no new members
            changed = True
            while changed:
                changed = False
                for mid in list(unclustered):
                    e = abstract_entities[mid]
                    union = cluster_entities | e
                    overlap = cluster_entities & e
                    jaccard = len(overlap) / len(union) if union else 0
                    if jaccard >= threshold:
                        cluster_ids.append(mid)
                        cluster_entities |= e
                        unclustered.discard(mid)
                        changed = True

            if len(cluster_ids) < self._config.topic_map_min_abstracts:
                continue

            clusters.append(
                self._build_cluster(
                    cluster_ids,
                    abstract_entities,
                    abstract_episodes,
                    total_abstracts,
                ),
            )

        return clusters

    @staticmethod
    def _build_cluster(
        cluster_ids: list[str],
        abstract_entities: dict[str, set[str]],
        abstract_episodes: dict[str, list[str]],
        total_abstracts: int,
    ) -> TopicCluster:
        """Build a TopicCluster from a set of clustered abstract IDs."""
        entity_freq: dict[str, int] = {}
        all_episode_ids: list[str] = []
        for mid in cluster_ids:
            for ent in abstract_entities.get(mid, set()):
                entity_freq[ent] = entity_freq.get(ent, 0) + 1
            all_episode_ids.extend(abstract_episodes.get(mid, []))

        top_entities = sorted(
            entity_freq,
            key=entity_freq.get,  # type: ignore[arg-type]
            reverse=True,
        )[:5]
        label = " / ".join(top_entities) if top_entities else "Unnamed Topic"

        return TopicCluster(
            label=label,
            entity_keys=top_entities,
            abstract_ids=cluster_ids,
            episode_ids=list(set(all_episode_ids)),
            confidence=len(cluster_ids) / total_abstracts,
            member_count=len(cluster_ids),
        )
