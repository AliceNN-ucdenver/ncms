"""Entity co-occurrence clustering for memory consolidation.

Groups memories that share entities in the knowledge graph, forming
clusters suitable for LLM-based pattern synthesis. Memories sharing
multiple entities indicate strong conceptual relationships worth
consolidating into insight records.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ncms.domain.models import Memory
from ncms.domain.protocols import GraphEngine


@dataclass
class MemoryCluster:
    """A group of memories linked by shared entities."""

    memories: list[Memory]
    shared_entity_ids: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)


def find_entity_clusters(
    memories: list[Memory],
    graph: GraphEngine,
    min_cluster_size: int = 3,
) -> list[MemoryCluster]:
    """Group memories by entity co-occurrence in the knowledge graph.

    Algorithm:
      1. For each memory, get its linked entity IDs from the graph.
      2. Build an entity → set[memory_index] index.
      3. For each entity with 2+ memories, those memories form a candidate group.
      4. Merge overlapping groups via union-find.
      5. Filter by *min_cluster_size* and exclude insight-type memories.
      6. Return clusters sorted by size (largest first).
    """
    # Filter out insight memories — don't re-consolidate insights
    source_memories = [m for m in memories if m.type != "insight"]
    if len(source_memories) < min_cluster_size:
        return []

    # Step 1-2: Build entity → memory indices mapping
    entity_to_mems: dict[str, set[int]] = defaultdict(set)
    mem_entities: dict[int, set[str]] = {}

    for idx, mem in enumerate(source_memories):
        eids = set(graph.get_entity_ids_for_memory(mem.id))
        mem_entities[idx] = eids
        for eid in eids:
            entity_to_mems[eid].add(idx)

    # Step 3-4: Union-find to merge overlapping groups
    parent: dict[int, int] = {i: i for i in range(len(source_memories))}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Merge memories that share at least one entity
    for _eid, mem_indices in entity_to_mems.items():
        indices = list(mem_indices)
        for i in range(1, len(indices)):
            union(indices[0], indices[i])

    # Step 5: Collect groups
    groups: dict[int, list[int]] = defaultdict(list)
    for idx in range(len(source_memories)):
        groups[find(idx)].append(idx)

    # Build clusters
    clusters: list[MemoryCluster] = []
    for member_indices in groups.values():
        if len(member_indices) < min_cluster_size:
            continue

        cluster_memories = [source_memories[i] for i in member_indices]

        # Find shared entities (entities linked to 2+ memories in the cluster)
        entity_counts: dict[str, int] = defaultdict(int)
        for idx in member_indices:
            for eid in mem_entities.get(idx, set()):
                entity_counts[eid] += 1
        shared = {eid for eid, count in entity_counts.items() if count >= 2}

        # Collect domains
        domains: set[str] = set()
        for m in cluster_memories:
            domains.update(m.domains)

        clusters.append(
            MemoryCluster(
                memories=cluster_memories,
                shared_entity_ids=shared,
                domains=domains,
            )
        )

    # Sort by cluster size (largest first)
    clusters.sort(key=lambda c: len(c.memories), reverse=True)
    return clusters
