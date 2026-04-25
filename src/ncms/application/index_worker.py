"""Background indexing worker — decouples store_memory() from indexing.

After store_memory() persists to SQLite and runs admission scoring, the
expensive work (BM25, SPLADE, GLiNER, entity linking, co-occurrence edges,
episode formation, contradiction detection) is enqueued as an IndexTask.

A pool of async workers drains the queue, running indexing stages with
parallelism where possible. Failures retry with exponential backoff.
Memory is safe in SQLite regardless — indexing is enrichment, not a gate.

Queue depth and worker status are observable via ncms://indexing/status.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ncms.domain.models import MemoryNode

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class IndexTask:
    """Unit of work for the background indexing pipeline."""

    memory_id: str
    content: str
    memory_type: str
    domains: list[str]
    tags: list[str]
    source_agent: str | None
    importance: float
    entities_manual: list[dict] = field(default_factory=list)
    relationships: list[dict] = field(default_factory=list)
    admission_features: object | None = None
    admission_route: str | None = None
    # Option D' Part 4: caller-asserted entity-subject.  When set,
    # ENTITY_STATE node creation uses this as ``entity_id`` directly
    # instead of inferring via regex / SLM state-change detection.
    subject: str | None = None
    # SLM slot head primary / GLiNER fallback: when True, the SLM
    # slot head already produced confident typed entities for this
    # memory; skip GLiNER to keep the entity graph clean on
    # trained-domain deployments.
    skip_gliner: bool = False

    # Internal tracking
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    attempt: int = 0
    max_attempts: int = 3
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: TaskStatus = TaskStatus.PENDING
    error: str | None = None


@dataclass(frozen=True)
class IndexingStats:
    """Snapshot of indexing pipeline health."""

    queue_depth: int
    queue_capacity: int
    workers: int
    workers_busy: int
    processed_total: int
    failed_total: int
    retried_total: int
    avg_process_ms: float
    oldest_pending_age_ms: float


class IndexWorkerPool:
    """Bounded async worker pool for background indexing.

    Usage::

        pool = IndexWorkerPool(memory_service=svc, num_workers=3, queue_size=1000)
        await pool.start()
        # ... enqueue tasks via pool.enqueue(task)
        await pool.shutdown()  # drains queue, waits for workers
    """

    def __init__(
        self,
        memory_service: Any,  # MemoryService — forward ref to avoid circular import
        num_workers: int = 3,
        queue_size: int = 1000,
        max_retries: int = 3,
        drain_timeout_seconds: int = 30,
    ) -> None:
        self._svc = memory_service
        self._num_workers = num_workers
        self._max_retries = max_retries
        self._drain_timeout = drain_timeout_seconds

        self._queue: asyncio.Queue[IndexTask] = asyncio.Queue(maxsize=queue_size)
        self._queue_capacity = queue_size
        self._workers: list[asyncio.Task[None]] = []
        self._running = False
        self._shutting_down = False

        # Episode formation lock — keyed by episode ID
        self._episode_locks: dict[str, asyncio.Lock] = {}
        self._episode_locks_lock = asyncio.Lock()

        # Stats
        self._workers_busy = 0
        self._processed_total = 0
        self._failed_total = 0
        self._retried_total = 0
        self._process_times: list[float] = []  # Last 100 processing times (ms)

    async def start(self) -> None:
        """Start the worker pool."""
        if self._running:
            return
        self._running = True
        self._shutting_down = False
        for i in range(self._num_workers):
            task = asyncio.create_task(
                self._worker_loop(i), name=f"index-worker-{i}",
            )
            self._workers.append(task)
        logger.info(
            "IndexWorkerPool started: %d workers, queue capacity %d",
            self._num_workers, self._queue_capacity,
        )

    async def shutdown(self, timeout: float | None = None) -> None:
        """Drain the queue and stop workers.

        Sends poison pills (None) to each worker, then waits for them
        to finish with an optional timeout.
        """
        if not self._running:
            return
        self._shutting_down = True
        timeout = timeout or self._drain_timeout

        # Poison pill for each worker
        for _ in self._workers:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)  # type: ignore[arg-type]

        # Wait for workers to finish
        if self._workers:
            done, pending = await asyncio.wait(
                self._workers, timeout=timeout,
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        self._workers.clear()
        self._running = False
        logger.info(
            "IndexWorkerPool shut down. processed=%d failed=%d",
            self._processed_total, self._failed_total,
        )

    def enqueue(self, task: IndexTask) -> bool:
        """Enqueue a task. Returns True if enqueued, False if queue is full.

        Caller should fall back to inline indexing when this returns False.
        """
        try:
            self._queue.put_nowait(task)
            return True
        except asyncio.QueueFull:
            logger.warning(
                "Index queue full (%d/%d), task %s must run inline",
                self._queue.qsize(), self._queue_capacity, task.task_id,
            )
            return False

    def stats(self) -> IndexingStats:
        """Return a snapshot of indexing pipeline health."""
        # Oldest pending age
        oldest_age_ms = 0.0
        if not self._queue.empty() and self._process_times:
            avg_ms = sum(self._process_times) / len(self._process_times)
            oldest_age_ms = self._queue.qsize() * avg_ms / max(self._workers_busy, 1)

        avg_ms = 0.0
        if self._process_times:
            avg_ms = sum(self._process_times) / len(self._process_times)

        return IndexingStats(
            queue_depth=self._queue.qsize(),
            queue_capacity=self._queue_capacity,
            workers=self._num_workers,
            workers_busy=self._workers_busy,
            processed_total=self._processed_total,
            failed_total=self._failed_total,
            retried_total=self._retried_total,
            avg_process_ms=round(avg_ms, 1),
            oldest_pending_age_ms=round(oldest_age_ms, 1),
        )

    async def get_episode_lock(self, episode_id: str) -> asyncio.Lock:
        """Get or create a per-episode lock for concurrent safety."""
        async with self._episode_locks_lock:
            if episode_id not in self._episode_locks:
                self._episode_locks[episode_id] = asyncio.Lock()
            return self._episode_locks[episode_id]

    # ── Worker loop ─────────────────────────────────────────────────

    async def _worker_loop(self, worker_id: int) -> None:
        """Main loop for a single worker."""
        logger.debug("index-worker-%d started", worker_id)
        while self._running:
            try:
                # Wait for a task (with timeout so we can check _running)
                try:
                    task = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0,
                    )
                except TimeoutError:
                    continue

                # Poison pill = shutdown
                if task is None:
                    break

                self._workers_busy += 1
                try:
                    await self._process_task(task, worker_id)
                finally:
                    self._workers_busy -= 1
                    self._queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception:
                logger.error(
                    "index-worker-%d unexpected error", worker_id,
                    exc_info=True,
                )

        logger.debug("index-worker-%d stopped", worker_id)

    async def _process_task(self, task: IndexTask, worker_id: int) -> None:
        """Process a single IndexTask through the indexing pipeline."""
        t0 = time.perf_counter()
        task.status = TaskStatus.PROCESSING
        pipeline_id = f"idx-{task.task_id}"

        def _emit(stage: str, duration_ms: float, data: dict | None = None) -> None:
            self._svc._event_log.pipeline_stage(
                pipeline_id=pipeline_id, pipeline_type="index",
                stage=stage, duration_ms=duration_ms,
                data=data, agent_id=task.source_agent,
                memory_id=task.memory_id,
            )

        _emit("started", 0.0, {
            "worker_id": worker_id,
            "attempt": task.attempt,
            "queue_depth": self._queue.qsize(),
        })

        try:
            # Retrieve the memory from SQLite (already persisted)
            memory = await self._svc._store.get_memory(task.memory_id)
            if memory is None:
                logger.error("IndexTask: memory %s not found in store", task.memory_id)
                task.status = TaskStatus.FAILED
                task.error = "memory_not_found"
                self._failed_total += 1
                _emit("failed", 0.0, {"error": "memory_not_found"})
                return

            # ── Stage 1: Parallel indexing (BM25 + SPLADE + GLiNER) ──
            # All three are independent — run concurrently.  GLiNER
            # is SKIPPED when the SLM slot head already produced
            # confident typed entities for this memory (see
            # ``task.skip_gliner``, set upstream in
            # ``MemoryService.store_memory``).  Keeps the entity
            # graph clean on trained-domain deployments.
            t1 = time.perf_counter()
            if task.skip_gliner:
                async def _noop_gliner() -> tuple[list[dict[str, str]], float]:
                    return [], 0.0
                bm25_ms, splade_ms, (auto_entities, extract_ms) = await asyncio.gather(
                    self._do_bm25(memory),
                    self._do_splade(memory),
                    _noop_gliner(),
                )
            else:
                bm25_ms, splade_ms, (auto_entities, extract_ms) = await asyncio.gather(
                    self._do_bm25(memory),
                    self._do_splade(memory),
                    self._do_gliner(memory, task.domains),
                )
            parallel_ms = (time.perf_counter() - t1) * 1000

            _emit("parallel_indexing", parallel_ms, {
                "bm25_ms": round(bm25_ms, 1),
                "splade_ms": round(splade_ms, 1),
                "gliner_ms": round(extract_ms, 1),
                "entity_count": len(auto_entities),
            })

            # Merge manual + auto entities
            manual = list(task.entities_manual)
            manual_names = {e["name"].lower() for e in manual}
            all_entities = manual + [
                e for e in auto_entities if e["name"].lower() not in manual_names
            ]

            # ── Stage 2: Entity linking + co-occurrence ──────────────
            t2 = time.perf_counter()
            linked_entity_ids = await self._link_entities(
                memory, all_entities, pipeline_id, task.source_agent,
            )
            self._build_cooccurrence_edges(
                memory, linked_entity_ids, pipeline_id, task.source_agent,
            )
            link_ms = (time.perf_counter() - t2) * 1000
            _emit("entity_linking", link_ms, {
                "entities_linked": len(linked_entity_ids),
            })

            # ── Stage 3: Memory nodes + reconciliation + episodes ────
            t3 = time.perf_counter()
            await self._create_nodes_and_episodes(
                memory=memory,
                all_entities=all_entities,
                linked_entity_ids=linked_entity_ids,
                admission_features=task.admission_features,
                admission_route=task.admission_route,
                pipeline_id=pipeline_id,
                source_agent=task.source_agent,
                subject=task.subject,
            )
            nodes_ms = (time.perf_counter() - t3) * 1000
            _emit("nodes_and_episodes", nodes_ms)

            # ── Stage 4: Process relationships ───────────────────────
            if task.relationships:
                from ncms.domain.models import Relationship
                for r_data in task.relationships:
                    rel = Relationship(
                        source_entity_id=r_data["source"],
                        target_entity_id=r_data["target"],
                        type=r_data.get("type", "related_to"),
                        source_memory_id=memory.id,
                    )
                    await self._svc._store.save_relationship(rel)
                    self._svc._graph.add_relationship(rel)

            # ── Stage 5: Contradiction detection (fire-and-forget) ───
            if self._svc._config.contradiction_detection_enabled:
                asyncio.create_task(
                    self._svc._deferred_contradiction_check(
                        memory=memory,
                        all_entities=all_entities,
                        pipeline_id=pipeline_id,
                        source_agent=task.source_agent,
                    )
                )

            # ── Log access + emit completion ─────────────────────────
            from ncms.domain.models import AccessRecord
            await self._svc._store.log_access(
                AccessRecord(memory_id=memory.id, accessing_agent=task.source_agent),
            )

            total_ms = (time.perf_counter() - t0) * 1000
            task.status = TaskStatus.COMPLETED
            self._processed_total += 1
            self._process_times.append(total_ms)
            if len(self._process_times) > 100:
                self._process_times = self._process_times[-100:]

            _emit("complete", total_ms, {
                "entity_count": len(all_entities),
                "parallel_ms": round(parallel_ms, 1),
                "link_ms": round(link_ms, 1),
                "nodes_ms": round(nodes_ms, 1),
            })

            self._svc._event_log.memory_stored(
                memory_id=memory.id,
                content_preview=memory.content,
                memory_type=memory.type,
                domains=memory.domains,
                entity_count=len(all_entities),
                agent_id=task.source_agent,
            )

            logger.info(
                "Indexed memory %s: %.0fms (worker-%d, attempt %d)",
                memory.id, total_ms, worker_id, task.attempt,
            )

        except Exception as e:
            total_ms = (time.perf_counter() - t0) * 1000
            task.attempt += 1

            if task.attempt < task.max_attempts:
                # Retry with exponential backoff
                backoff = 1.0 * (5 ** (task.attempt - 1))  # 1s, 5s, 25s
                task.status = TaskStatus.PENDING
                task.error = str(e)
                self._retried_total += 1

                logger.warning(
                    "IndexTask %s failed (attempt %d/%d), retrying in %.0fs: %s",
                    task.task_id, task.attempt, task.max_attempts, backoff, e,
                )
                _emit("retry", total_ms, {
                    "attempt": task.attempt,
                    "backoff_seconds": backoff,
                    "error": str(e),
                })

                await asyncio.sleep(backoff)
                # Re-enqueue (best effort)
                try:
                    self._queue.put_nowait(task)
                except asyncio.QueueFull:
                    logger.error("Cannot retry task %s — queue full", task.task_id)
                    task.status = TaskStatus.FAILED
                    self._failed_total += 1
            else:
                # Dead letter
                task.status = TaskStatus.FAILED
                task.error = str(e)
                self._failed_total += 1

                logger.error(
                    "IndexTask %s failed permanently after %d attempts: %s",
                    task.task_id, task.max_attempts, e, exc_info=True,
                )
                _emit("failed", total_ms, {
                    "attempt": task.attempt,
                    "error": str(e),
                })

    # ── Indexing sub-stages ─────────────────────────────────────────

    async def _do_bm25(self, memory: Any) -> float:
        t = time.perf_counter()
        await asyncio.to_thread(self._svc._index.index_memory, memory)
        return (time.perf_counter() - t) * 1000

    async def _do_splade(self, memory: Any) -> float:
        if self._svc._splade is None:
            return 0.0
        t = time.perf_counter()
        try:
            await asyncio.to_thread(self._svc._splade.index_memory, memory)
        except Exception:
            logger.warning(
                "SPLADE indexing failed for %s, continuing", memory.id,
                exc_info=True,
            )
        return (time.perf_counter() - t) * 1000

    async def _do_gliner(
        self, memory: Any, domains: list[str],
    ) -> tuple[list[dict[str, str]], float]:
        from ncms.application.label_cache import load_cached_labels
        from ncms.domain.entity_extraction import resolve_labels
        from ncms.infrastructure.extraction.gliner_extractor import extract_entities_gliner

        t = time.perf_counter()
        cached = await load_cached_labels(self._svc._store, domains)
        gliner_labels = resolve_labels(domains, cached_labels=cached)
        result = await asyncio.to_thread(
            extract_entities_gliner,
            memory.content,
            model_name=self._svc._config.gliner_model,
            threshold=self._svc._config.gliner_threshold,
            labels=gliner_labels,
            cache_dir=self._svc._config.model_cache_dir,
        )
        return result, (time.perf_counter() - t) * 1000

    async def _link_entities(
        self,
        memory: Any,
        all_entities: list[dict],
        pipeline_id: str,
        source_agent: str | None,
    ) -> list[str]:
        """Link extracted entities to the memory in graph + SQLite."""
        linked_entity_ids: list[str] = []
        for e_data in all_entities:
            entity = await self._svc.add_entity(
                name=e_data["name"],
                entity_type=e_data.get("type", "concept"),
                attributes=e_data.get("attributes", {}),
            )
            linked_entity_ids.append(entity.id)
            await self._svc._store.link_memory_entity(memory.id, entity.id)
            self._svc._graph.link_memory_entity(memory.id, entity.id)
        return linked_entity_ids

    def _build_cooccurrence_edges(
        self,
        memory: Any,
        linked_entity_ids: list[str],
        pipeline_id: str,
        source_agent: str | None,
    ) -> None:
        """Build in-memory co-occurrence edges between entities."""
        from ncms.domain.models import Relationship

        config = self._svc._config
        if len(linked_entity_ids) <= 1:  # Co-occurrence always on
            return

        cooc_ids = linked_entity_ids[:config.cooccurrence_max_entities]
        for i, a in enumerate(cooc_ids):
            for b in cooc_ids[i + 1:]:
                existing_count = self._svc._graph.get_edge_cooccurrence(a, b)
                if existing_count > 0:
                    self._svc._graph.increment_edge_cooccurrence(a, b)
                    self._svc._graph.increment_edge_cooccurrence(b, a)
                else:
                    rel_ab = Relationship(
                        source_entity_id=a, target_entity_id=b,
                        type="co_occurs", source_memory_id=memory.id,
                    )
                    rel_ba = Relationship(
                        source_entity_id=b, target_entity_id=a,
                        type="co_occurs", source_memory_id=memory.id,
                    )
                    self._svc._graph.add_relationship(rel_ab)
                    self._svc._graph.add_relationship(rel_ba)

    async def _create_nodes_and_episodes(
        self,
        memory: Any,
        all_entities: list[dict],
        linked_entity_ids: list[str],
        admission_features: object | None,
        admission_route: str | None,
        pipeline_id: str,
        source_agent: str | None,
        subject: str | None = None,
    ) -> None:
        """Create L1/L2 memory nodes, run reconciliation, form episodes."""
        from ncms.domain.models import MemoryNode, NodeType

        config = self._svc._config

        _should_create_node = (
            admission_route == "persist"
            or admission_route is None
            or (
                config.temporal_enabled
                and self._svc._episode is not None
            )
        )
        if not _should_create_node:
            return

        # L1: ALWAYS create atomic node
        l1_node = MemoryNode(
            memory_id=memory.id,
            node_type=NodeType.ATOMIC,
            importance=memory.importance,
            observed_at=memory.observed_at,
        )
        await self._svc._store.save_memory_node(l1_node)

        # L2: entity_state if state change detected OR caller asserted subject
        await self._detect_and_create_l2_node(
            memory, all_entities, admission_features, l1_node,
            subject=subject,
        )

        # Episode formation
        await self._run_episode_formation(
            memory, l1_node, linked_entity_ids,
        )

    async def _detect_and_create_l2_node(
        self,
        memory: Any,
        all_entities: list[dict],
        admission_features: object | None,
        l1_node: MemoryNode,
        subject: str | None = None,
    ) -> None:
        """Detect state changes and create L2 entity_state node.

        Checks admission signal threshold and regex-based state
        declaration patterns. Validates detected entity exists in
        GLiNER extraction set. Runs reconciliation if enabled.

        When ``subject`` is provided (Option D' Part 4), the caller
        has asserted the entity-subject and we create the ENTITY_STATE
        node unconditionally with ``entity_id = subject`` — bypassing
        the state-change / regex / GLiNER-validation fork.
        """
        import re

        from ncms.domain.models import (
            EdgeType,
            GraphEdge,
            MemoryNode,
            NodeType,
        )

        # ── Caller-asserted subject + SLM state_change gate ──────────
        # See pipeline.py::_detect_and_create_l2_node for the full
        # rationale.  Summary: subject kwarg alone is not enough;
        # L2 creation also requires the SLM state_change head to
        # fire declaration/retirement with confidence.  Otherwise
        # we'd create one L2 per memory (measured on MSEB mini:
        # 186 L2 from 186 memories, which floods reconciliation
        # and turns subject entities into graph hubs).  The
        # subject is already linked as an entity upstream; that's
        # enough for TLG vocabulary seeding.  NO REGEX.
        slm_label = (memory.structured or {}).get("intent_slot") or {}
        _caller_slm_state = slm_label.get("state_change")
        _caller_slm_state_conf = slm_label.get("state_change_confidence") or 0.0
        _caller_slm_confident = (
            _caller_slm_state in {"declaration", "retirement"}
            and _caller_slm_state_conf
            >= self._svc._config.slm_confidence_threshold
        )
        if subject and _caller_slm_confident:
            snippet = memory.content.strip()[:200] or "(empty)"
            node_metadata = {
                "entity_id": subject,
                "state_key": "status",
                "state_value": snippet,
                "source": "caller_subject_slm_state_change",
                "slm_state_change": _caller_slm_state,
            }
            l2_node = MemoryNode(
                memory_id=memory.id,
                node_type=NodeType.ENTITY_STATE,
                importance=memory.importance,
                metadata=node_metadata,
            )
            await self._svc._store.save_memory_node(l2_node)
            await self._svc._store.save_graph_edge(GraphEdge(
                source_id=l2_node.id,
                target_id=l1_node.id,
                edge_type=EdgeType.DERIVED_FROM,
            ))
            config = self._svc._config
            if config.temporal_enabled:
                try:
                    from ncms.application.reconciliation_service import (
                        ReconciliationService,
                    )
                    recon = ReconciliationService(
                        store=self._svc._store, config=config,
                    )
                    await recon.reconcile(l2_node)
                except Exception:
                    logger.warning(
                        "Reconciliation failed for %s", l2_node.id,
                        exc_info=True,
                    )
                self._svc.invalidate_tlg_vocabulary()
            return
        if subject:
            # Subject provided but SLM did not declare state change.
            # No L2 node; subject entity link is sufficient.
            return

        # Skip document sections — structural content triggers
        # false positives on state-declaration regexes.
        _is_section_content = memory.type in (
            "document_section", "document_chunk",
            "section_index", "document",
        )

        # Phase I.2 — SLM-first via the shared
        # ``slm_state_change_decision`` helper.  Same retirement
        # discipline as IngestionPipeline.create_memory_nodes: when
        # the LoRA adapter ran confidently, its verdict (incl. "none")
        # is authoritative.  Section content always skips L2 to avoid
        # false-positives on structural document content.
        from ncms.domain.intent_slot_taxonomy import (
            slm_state_change_decision,
        )

        _slm_label = (memory.structured or {}).get("intent_slot") or {}
        _slm_decision = slm_state_change_decision(
            _slm_label,
            threshold=self._svc._config.slm_confidence_threshold,
        )
        if _is_section_content:
            _has_state_change = False
            _has_state_declaration = False
        elif _slm_decision is not None:
            _has_state_change, _has_state_declaration = _slm_decision
        else:
            # Cold-start regex/heuristic fallback — LoRA adapter
            # missing for this domain.
            _has_state_change = (
                admission_features is not None
                and hasattr(admission_features, "state_change_signal")
                and admission_features.state_change_signal >= 0.35
            )
            _has_state_declaration = bool(
                re.search(
                    r"^[a-zA-Z0-9_\-]+\s*:\s*[a-zA-Z0-9_\-]+\s*=\s*.+$",
                    memory.content,
                    re.MULTILINE,
                )
                or re.search(
                    r"(?:^|\n)##?\s*[Ss]tatus\s*[\n:]\s*\w+",
                    memory.content,
                )
                or re.search(
                    r"^\s*status\s*:\s*\w+",
                    memory.content,
                    re.MULTILINE | re.IGNORECASE,
                )
            )

        if not (_has_state_change or _has_state_declaration):
            return

        # v7+ async indexing path: also source canonical state_value
        # from the persisted SLM role_spans when present.  The ingest
        # stage stashed the full dict under ``memory.structured["intent_slot"]``.
        slm_label_bg = (memory.structured or {}).get("intent_slot") or None
        node_metadata = self._svc._ingestion.extract_entity_state_meta(
            memory.content, all_entities, slm_label=slm_label_bg,
        )

        # Validate detected entity exists in GLiNER extraction
        # (skip for SLM-sourced metadata — the primary canonical is
        # authoritative even when GLiNER didn't echo it verbatim).
        if node_metadata.get("source") != "slm_role_span":
            _entity_names_lower = {
                e["name"].lower() for e in all_entities
            }
            _detected_entity = node_metadata.get("entity_id", "")
            if _detected_entity.lower() not in _entity_names_lower:
                return  # suppress L2 creation

        if not node_metadata:
            return

        l2_node = MemoryNode(
            memory_id=memory.id,
            node_type=NodeType.ENTITY_STATE,
            importance=memory.importance,
            metadata=node_metadata,
        )
        await self._svc._store.save_memory_node(l2_node)

        edge = GraphEdge(
            source_id=l2_node.id,
            target_id=l1_node.id,
            edge_type=EdgeType.DERIVED_FROM,
        )
        await self._svc._store.save_graph_edge(edge)

        # Reconciliation
        config = self._svc._config
        if config.temporal_enabled:
            try:
                from ncms.application.reconciliation_service import (
                    ReconciliationService,
                )
                recon = ReconciliationService(
                    store=self._svc._store, config=config,
                )
                await recon.reconcile(l2_node)
            except Exception:
                logger.warning(
                    "Reconciliation failed for %s", l2_node.id,
                    exc_info=True,
                )

        # TLG Phase 3c — background path also creates ENTITY_STATE
        # nodes.  Invalidate the L1 vocabulary cache so the next
        # retrieve_lg call sees the new subject / entity tokens.
        if config.temporal_enabled:
            self._svc.invalidate_tlg_vocabulary()

    async def _run_episode_formation(
        self,
        memory: Any,
        l1_node: MemoryNode,
        linked_entity_ids: list[str],
    ) -> None:
        """Assign memory to an episode and check for closure."""
        config = self._svc._config
        if (
            self._svc._episode is None
            or not config.temporal_enabled
        ):
            return

        try:
            pool = self._svc._index_pool
            episode_node = (
                await self._svc._episode.assign_or_create(
                    fragment_node=l1_node,
                    fragment_memory=memory,
                    entity_ids=linked_entity_ids,
                )
            )
            if episode_node is not None:
                if pool is not None:
                    lock = await pool.get_episode_lock(
                        episode_node.id,
                    )
                    async with lock:
                        await self._svc._episode \
                            .check_resolution_closure(
                                memory.content, episode_node,
                            )
                else:
                    await self._svc._episode \
                        .check_resolution_closure(
                            memory.content, episode_node,
                        )
        except Exception:
            logger.warning(
                "Episode formation failed for node %s",
                l1_node.id,
                exc_info=True,
            )
