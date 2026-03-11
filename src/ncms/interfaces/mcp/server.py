"""MCP Server composition root for NCMS.

Creates and wires together all services, then exposes them
via the FastMCP server with tools and resources.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from ncms.application.bus_service import BusService
from ncms.application.graph_service import GraphService
from ncms.application.memory_service import MemoryService
from ncms.application.snapshot_service import SnapshotService
from ncms.config import NCMSConfig
from ncms.infrastructure.bus.async_bus import AsyncKnowledgeBus
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore
from ncms.interfaces.mcp.resources import register_resources
from ncms.interfaces.mcp.tools import register_tools

logger = logging.getLogger(__name__)


async def create_ncms_services(
    config: NCMSConfig | None = None,
) -> tuple[MemoryService, BusService, SnapshotService]:
    """Initialize all NCMS services. Returns (memory_svc, bus_svc, snapshot_svc)."""
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
        logger.info("SPLADE engine enabled with model: %s", config.splade_model)

    # Application services
    memory_svc = MemoryService(
        store=store, index=index, graph=graph, config=config, splade=splade,
    )
    snapshot_svc = SnapshotService(
        store=store,
        max_entries=config.snapshot_max_entries,
        ttl_hours=config.snapshot_ttl_hours,
    )
    bus_svc = BusService(
        bus=bus,
        snapshot_service=snapshot_svc,
        surrogate_enabled=config.bus_surrogate_enabled,
    )

    # Rebuild in-memory graph from persistent store (rehydrate after restart)
    graph_svc = GraphService(store=store, graph=graph)
    await graph_svc.rebuild_from_store()

    return memory_svc, bus_svc, snapshot_svc


def create_mcp_server(
    memory_svc: MemoryService,
    bus_svc: BusService,
    snapshot_svc: SnapshotService,
) -> FastMCP:
    """Create a FastMCP server with all NCMS tools and resources registered."""
    mcp = FastMCP(
        name="ncms",
    )

    register_tools(mcp, memory_svc, bus_svc, snapshot_svc)
    register_resources(mcp, memory_svc, bus_svc, snapshot_svc)

    return mcp


async def run_server(config: NCMSConfig | None = None) -> None:
    """Create and run the NCMS MCP server."""
    memory_svc, bus_svc, snapshot_svc = await create_ncms_services(config)
    mcp = create_mcp_server(memory_svc, bus_svc, snapshot_svc)
    await mcp.run_stdio_async()
