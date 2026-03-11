"""Consolidation Service - background memory maintenance.

Handles memory decay, pruning, and knowledge consolidation.
Knowledge consolidation discovers emergent cross-memory patterns
and stores them as insight memories (Phase 4).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ncms.config import NCMSConfig
from ncms.domain.models import AccessRecord, Memory
from ncms.domain.scoring import base_level_activation
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class ConsolidationService:
    """Background maintenance for memory health and knowledge consolidation."""

    def __init__(
        self,
        store: SQLiteStore,
        index: TantivyEngine | None = None,
        graph: NetworkXGraph | None = None,
        config: NCMSConfig | None = None,
    ):
        self._store = store
        self._index = index
        self._graph = graph
        self._config = config or NCMSConfig()

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
