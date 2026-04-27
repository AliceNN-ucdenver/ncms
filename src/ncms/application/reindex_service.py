"""Reindex service — rebuild BM25, SPLADE, entity, and graph indexes from SQLite.

Reads all persisted memories from the store and rebuilds the specified
indexes from scratch.  Useful after enabling SPLADE on an existing database,
recovering a corrupted Tantivy index, or re-extracting entities with
the configured entity extraction lane.

Each rebuild method is independent and can be called individually or
combined via ``rebuild_all()``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ncms.application.entity_extraction_mode import (
    structured_slm_entities,
    use_gliner_entities,
)
from ncms.config import NCMSConfig
from ncms.domain.models import Entity, Memory
from ncms.domain.protocols import GraphEngine, MemoryStore
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine

logger = logging.getLogger(__name__)


@dataclass
class ReindexResult:
    """Summary of a reindex operation."""

    bm25_indexed: int = 0
    splade_indexed: int = 0
    entities_extracted: int = 0
    graph_rebuilt: bool = False
    errors: int = 0
    duration_ms: float = 0.0
    error_details: list[str] = field(default_factory=list)


ProgressCallback = Callable[[int, int], None]
"""Callback(current, total) for progress reporting."""


class ReindexService:
    """Rebuilds search indexes and entity graph from persisted memories."""

    def __init__(
        self,
        store: MemoryStore,
        tantivy: TantivyEngine,
        splade: object | None,  # SpladeEngine or None
        graph: GraphEngine,
        config: NCMSConfig,
    ) -> None:
        self._store = store
        self._tantivy = tantivy
        self._splade = splade
        self._graph = graph
        self._config = config

    async def _load_all_memories(self) -> list[Memory]:
        """Load all memories from the store."""
        return await self._store.list_memories(limit=1_000_000)

    # ── BM25 rebuild ───────────────────────────────────────────────

    async def rebuild_bm25(
        self,
        memories: list[Memory] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> int:
        """Rebuild the Tantivy BM25 index from scratch.

        Deletes the existing index and re-indexes all memories.
        Returns the number of memories indexed.
        """
        if memories is None:
            memories = await self._load_all_memories()

        # Re-initialize the Tantivy index (creates a fresh schema).
        # TantivyEngine.initialize() creates the directory and opens a new
        # index.  For a full rebuild we need to clear any existing data by
        # removing old documents first, then re-adding.  The simplest
        # approach is to re-initialize which resets the writer.
        import shutil
        from pathlib import Path

        index_dir = self._tantivy._index_dir
        if index_dir:
            path = Path(index_dir)
            if path.exists():
                shutil.rmtree(path)
        # Re-initialize with original path
        self._tantivy._index = None
        self._tantivy._schema = None
        self._tantivy._index_dir = None
        self._tantivy.initialize(path=self._tantivy._path)

        indexed = 0
        total = len(memories)
        for i, memory in enumerate(memories):
            self._tantivy.index_memory(memory)
            indexed += 1
            if progress_callback:
                progress_callback(i + 1, total)

        logger.info("BM25 reindex complete: %d memories indexed", indexed)
        return indexed

    # ── SPLADE rebuild ─────────────────────────────────────────────

    async def rebuild_splade(
        self,
        memories: list[Memory] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> int:
        """Rebuild the SPLADE sparse vector index from scratch.

        Clears the in-memory vector store and re-indexes all memories.
        Returns the number of memories indexed.
        """
        if self._splade is None:
            logger.warning("SPLADE is not enabled, skipping rebuild")
            return 0

        if memories is None:
            memories = await self._load_all_memories()

        # Clear existing vectors
        self._splade._vectors.clear()  # type: ignore[attr-defined]

        indexed = 0
        total = len(memories)
        for i, memory in enumerate(memories):
            try:
                await asyncio.to_thread(
                    self._splade.index_memory,  # type: ignore[attr-defined]
                    memory,
                )
                indexed += 1
            except Exception:
                logger.warning(
                    "SPLADE indexing failed for %s, skipping",
                    memory.id,
                    exc_info=True,
                )
            if progress_callback:
                progress_callback(i + 1, total)

        logger.info("SPLADE reindex complete: %d memories indexed", indexed)
        return indexed

    # ── Entity re-extraction ───────────────────────────────────────

    async def rebuild_entities(
        self,
        memories: list[Memory] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> int:
        """Re-extract entities via configured lane and re-link to memories.

        In ``gliner_only`` mode this reruns GLiNER.  In ``slm_only`` mode
        it recovers the SLM entities baked into ``memory.structured`` at
        ingest time, avoiding a hidden GLiNER fallback during reindex.
        """
        if memories is None:
            memories = await self._load_all_memories()

        total_entities = 0
        total = len(memories)

        for i, memory in enumerate(memories):
            try:
                entities = await self._extract_reindex_entities(memory)

                # Link entities to memory
                for e_data in entities:
                    entity = await self._find_or_create_entity(
                        name=e_data["name"],
                        entity_type=e_data.get("type", "concept"),
                    )
                    await self._store.link_memory_entity(memory.id, entity.id)
                    self._graph.link_memory_entity(memory.id, entity.id)

                total_entities += len(entities)

            except Exception:
                logger.warning(
                    "Entity extraction failed for %s, skipping",
                    memory.id,
                    exc_info=True,
                )

            if progress_callback:
                progress_callback(i + 1, total)

        logger.info(
            "Entity reindex complete: %d entities from %d memories",
            total_entities,
            total,
        )
        return total_entities

    async def _extract_reindex_entities(self, memory: Memory) -> list[dict]:
        if not use_gliner_entities(self._config):
            return structured_slm_entities(memory.structured)

        from ncms.application.label_cache import load_cached_labels
        from ncms.domain.entity_extraction import resolve_labels
        from ncms.infrastructure.extraction.gliner_extractor import (
            extract_entities_gliner,
        )

        cached = await load_cached_labels(
            self._store,
            memory.domains,
        )
        labels = resolve_labels(memory.domains, cached_labels=cached)
        return await asyncio.to_thread(
            extract_entities_gliner,
            memory.content,
            model_name=self._config.gliner_model,
            threshold=self._config.gliner_threshold,
            labels=labels,
            cache_dir=self._config.model_cache_dir,
        )

    # ── Full rebuild ───────────────────────────────────────────────

    async def rebuild_all(
        self,
        *,
        bm25: bool = True,
        splade: bool = True,
        entities: bool = False,
        graph: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> ReindexResult:
        """Rebuild selected indexes from scratch.

        Loads all memories once, then rebuilds the requested indexes.
        Graph rebuild uses ``GraphService.rebuild_from_store()`` which
        reads entities and relationships from SQLite.

        Args:
            bm25: Rebuild the Tantivy BM25 index.
            splade: Rebuild the SPLADE sparse vector index.
            entities: Re-extract entities via configured lane and re-link.
            graph: Rebuild the NetworkX graph from SQLite.
            progress_callback: Optional (current, total) callback.

        Returns:
            ReindexResult with counts and timing.
        """
        t0 = time.perf_counter()
        result = ReindexResult()

        # Load all memories once for shared use
        memories = await self._load_all_memories()

        # BM25
        if bm25:
            try:
                result.bm25_indexed = await self.rebuild_bm25(
                    memories=memories,
                    progress_callback=progress_callback,
                )
            except Exception as e:
                logger.error("BM25 rebuild failed: %s", e, exc_info=True)
                result.errors += 1
                result.error_details.append(f"BM25: {e}")

        # SPLADE
        if splade and self._splade is not None:
            try:
                result.splade_indexed = await self.rebuild_splade(
                    memories=memories,
                    progress_callback=progress_callback,
                )
            except Exception as e:
                logger.error("SPLADE rebuild failed: %s", e, exc_info=True)
                result.errors += 1
                result.error_details.append(f"SPLADE: {e}")

        # Entities
        if entities:
            try:
                result.entities_extracted = await self.rebuild_entities(
                    memories=memories,
                    progress_callback=progress_callback,
                )
            except Exception as e:
                logger.error("Entity rebuild failed: %s", e, exc_info=True)
                result.errors += 1
                result.error_details.append(f"Entities: {e}")

        # Graph (uses its own data source — reads from SQLite)
        if graph:
            try:
                from ncms.application.graph_service import GraphService

                graph_svc = GraphService(store=self._store, graph=self._graph)
                await graph_svc.rebuild_from_store()
                result.graph_rebuilt = True
            except Exception as e:
                logger.error("Graph rebuild failed: %s", e, exc_info=True)
                result.errors += 1
                result.error_details.append(f"Graph: {e}")

        result.duration_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "Reindex complete in %.0fms: bm25=%d splade=%d entities=%d graph=%s errors=%d",
            result.duration_ms,
            result.bm25_indexed,
            result.splade_indexed,
            result.entities_extracted,
            result.graph_rebuilt,
            result.errors,
        )
        return result

    # ── Helpers ─────────────────────────────────────────────────────

    async def _find_or_create_entity(
        self,
        name: str,
        entity_type: str,
    ) -> Entity:
        """Find existing entity by name, or create a new one."""
        existing = await self._store.find_entity_by_name(name)
        if existing:
            return existing

        entity = Entity(name=name, type=entity_type, attributes={})
        await self._store.save_entity(entity)
        self._graph.add_entity(entity)
        return entity
