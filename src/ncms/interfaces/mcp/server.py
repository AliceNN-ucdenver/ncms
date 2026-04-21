"""MCP Server composition root for NCMS.

Creates and wires together all services, then exposes them
via the FastMCP server with tools and resources.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from ncms.application.bus_service import BusService
from ncms.application.consolidation_service import ConsolidationService
from ncms.application.graph_service import GraphService
from ncms.application.maintenance_scheduler import MaintenanceScheduler
from ncms.application.memory_service import MemoryService
from ncms.application.snapshot_service import SnapshotService
from ncms.config import NCMSConfig
from ncms.infrastructure.bus.async_bus import AsyncKnowledgeBus
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.observability.event_log import EventLog, NullEventLog
from ncms.infrastructure.storage.sqlite_store import SQLiteStore
from ncms.interfaces.mcp.resources import register_resources
from ncms.interfaces.mcp.tools import register_tools

logger = logging.getLogger(__name__)


async def create_ncms_services(
    config: NCMSConfig | None = None,
    event_log: EventLog | NullEventLog | None = None,
) -> tuple[MemoryService, BusService, SnapshotService, ConsolidationService, MaintenanceScheduler]:
    """Initialize all NCMS services. Returns (memory_svc, bus_svc, snapshot_svc, ...)."""
    config = config or NCMSConfig()

    # Infrastructure
    store = SQLiteStore(db_path=config.db_path)
    await store.initialize()

    index = TantivyEngine(path=config.index_path)
    index.initialize()

    graph = NetworkXGraph()
    bus = AsyncKnowledgeBus(ask_timeout_ms=config.bus_ask_timeout_ms)

    # SPLADE sparse neural retrieval (disabled by default)
    splade = None
    if config.splade_enabled:
        from ncms.infrastructure.indexing.splade_engine import SpladeEngine

        splade = SpladeEngine(
            model_name=config.splade_model,
            cache_dir=config.model_cache_dir,
        )
        splade._ensure_model()
        logger.info("SPLADE engine enabled and pre-loaded: %s", config.splade_model)

    # Admission scoring (Phase 1, disabled by default)
    admission = None
    if config.admission_enabled:
        from ncms.application.admission_service import AdmissionService

        admission = AdmissionService(store=store, index=index, graph=graph, config=config)
        logger.info("Admission scoring enabled")

    # Reconciliation service (Phase 2, disabled by default)
    reconciliation = None
    if config.temporal_enabled:
        from ncms.application.reconciliation_service import ReconciliationService

        reconciliation = ReconciliationService(store=store, config=config)
        logger.info("Reconciliation service enabled")

    # Episode formation (Phase 3, disabled by default)
    episode = None
    if config.temporal_enabled:
        from ncms.application.episode_service import EpisodeService

        episode = EpisodeService(
            store=store, index=index, config=config,
            event_log=event_log, splade=splade,
        )
        logger.info("Episode formation enabled")

    # Intent classifier (Phase 4, uses BM25 exemplar index when enabled)
    intent_classifier = None
    if config.temporal_enabled:
        from ncms.infrastructure.indexing.exemplar_intent_index import (
            ExemplarIntentIndex,
        )

        intent_classifier = ExemplarIntentIndex()
        logger.info("BM25 exemplar intent classifier enabled")

    # Consolidation service (Phase 5 hierarchical consolidation)
    from ncms.application.consolidation_service import ConsolidationService

    consolidation_svc = ConsolidationService(
        store=store, index=index, graph=graph, config=config,
        event_log=event_log,
        splade=splade,
    )

    # Cross-encoder reranker (Phase 10)
    reranker = None
    if config.reranker_enabled:
        from ncms.infrastructure.reranking.cross_encoder_reranker import (
            CrossEncoderReranker,
        )

        reranker = CrossEncoderReranker(
            model_name=config.reranker_model,
            cache_dir=config.model_cache_dir,
        )
        logger.info("Cross-encoder reranker enabled: %s", config.reranker_model)

    # Application services
    memory_svc = MemoryService(
        store=store, index=index, graph=graph, config=config,
        splade=splade, admission=admission,
        reconciliation=reconciliation, episode=episode,
        intent_classifier=intent_classifier,
        reranker=reranker,
    )

    # Section service (Phase 4 content-aware ingestion)
    # Created after MemoryService because of circular dependency (duck-typed).
    # document_service is not available in the MCP path — section_service falls
    # back to legacy ingestion (section_index + section children in memory store).
    if config.content_classification_enabled:
        from ncms.application.section_service import SectionService

        section_svc = SectionService(
            memory_service=memory_svc, config=config, document_service=None,
        )
        memory_svc._section_svc = section_svc
        logger.info("Content classification + section extraction enabled")
    snapshot_svc = SnapshotService(
        store=store,
        max_entries=config.snapshot_max_entries,
        ttl_hours=config.snapshot_ttl_hours,
    )
    bus_svc = BusService(
        bus=bus,
        snapshot_service=snapshot_svc,
        surrogate_enabled=True,  # Always on (retired flag)
    )

    # Rebuild in-memory graph from persistent store (rehydrate after restart)
    graph_svc = GraphService(store=store, graph=graph)
    await graph_svc.rebuild_from_store()

    # Start background indexing pool if enabled (default: True)
    await memory_svc.start_index_pool()

    # Maintenance scheduler (background periodic tasks)
    from ncms.application.maintenance_scheduler import MaintenanceScheduler

    scheduler = MaintenanceScheduler(
        consolidation_svc=consolidation_svc,
        episode_svc=episode,
        config=config,
        event_log=event_log,
        memory_svc=memory_svc,
    )
    await scheduler.start()

    # Phase 6: Start heartbeat monitor for bus agents
    async def _auto_snapshot(agent_id: str) -> None:
        await snapshot_svc.create_snapshot(agent_id, [])

    await bus_svc.start_heartbeat_monitor(
        interval_seconds=config.bus_heartbeat_interval_seconds,
        timeout_seconds=config.bus_heartbeat_timeout_seconds,
        auto_snapshot=config.auto_snapshot_on_disconnect,
        snapshot_callback=_auto_snapshot if config.auto_snapshot_on_disconnect else None,
    )
    logger.info(
        "[phase6] Heartbeat monitor started: interval=%ds timeout=%ds auto_snapshot=%s",
        config.bus_heartbeat_interval_seconds,
        config.bus_heartbeat_timeout_seconds,
        config.auto_snapshot_on_disconnect,
    )

    return memory_svc, bus_svc, snapshot_svc, consolidation_svc, scheduler


def create_mcp_server(
    memory_svc: MemoryService,
    bus_svc: BusService,
    snapshot_svc: SnapshotService,
    consolidation_svc: ConsolidationService | None = None,
) -> FastMCP:
    """Create a FastMCP server with all NCMS tools and resources registered."""
    mcp = FastMCP(
        name="ncms",
    )

    register_tools(mcp, memory_svc, bus_svc, snapshot_svc, consolidation_svc)
    register_resources(mcp, memory_svc, bus_svc, snapshot_svc)

    return mcp


async def run_server(config: NCMSConfig | None = None) -> None:
    """Create and run the NCMS MCP server."""
    memory_svc, bus_svc, snapshot_svc, consolidation_svc, _scheduler = (
        await create_ncms_services(config)
    )
    mcp = create_mcp_server(memory_svc, bus_svc, snapshot_svc, consolidation_svc)
    await mcp.run_stdio_async()
