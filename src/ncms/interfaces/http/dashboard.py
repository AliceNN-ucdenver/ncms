"""NCMS Dashboard - Starlette web server for real-time observability.

Provides:
- SSE event stream for real-time bus/memory activity
- REST endpoints for agents, domains, graph, stats
- Single-page app served from static/index.html

Requires: pip install ncms[dashboard]
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from ncms.application.bus_service import BusService
from ncms.application.memory_service import MemoryService
from ncms.infrastructure.observability.event_log import EventLog

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_dashboard_app(
    memory_service: MemoryService,
    bus_service: BusService,
    event_log: EventLog,
) -> Starlette:
    """Create the Starlette dashboard application."""

    # ── SSE Stream ────────────────────────────────────────────────────────

    async def event_stream(request: Request) -> StreamingResponse:
        """Server-Sent Events stream of live dashboard events."""

        async def generate():
            queue = await event_log.subscribe()
            try:
                # Send recent events as initial burst
                for evt in reversed(event_log.recent(50)):
                    yield evt.to_sse()

                # Then stream live events
                while True:
                    try:
                        evt = await asyncio.wait_for(queue.get(), timeout=30.0)
                        yield evt.to_sse()
                    except TimeoutError:
                        # Send keepalive comment every 30s
                        yield ": keepalive\n\n"
                    except asyncio.CancelledError:
                        break
            finally:
                event_log.unsubscribe(queue)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ── REST Endpoints ────────────────────────────────────────────────────

    async def api_agents(request: Request) -> JSONResponse:
        """Return all registered agents with status and domains."""
        agents = bus_service.get_all_agents()
        return JSONResponse([
            {
                "agent_id": a.agent_id,
                "domains": a.domains,
                "status": a.status,
                "last_seen": a.last_seen.isoformat() if a.last_seen else None,
            }
            for a in agents
        ])

    async def api_domains(request: Request) -> JSONResponse:
        """Return domain -> provider agent mapping."""
        domains = bus_service.list_domains()
        result: dict[str, list[dict[str, Any]]] = {}
        for domain, agent_ids in domains.items():
            result[domain] = [
                {
                    "agent_id": aid,
                    "online": bus_service.is_agent_online(aid),
                }
                for aid in agent_ids
            ]
        return JSONResponse(result)

    async def api_graph_entity_detail(request: Request) -> JSONResponse:
        """Return detailed info about a single entity and its connections."""
        entity_id = request.path_params["entity_id"]
        entity = await memory_service._store.get_entity(entity_id)
        if not entity:
            return JSONResponse({"error": "Entity not found"}, status_code=404)

        graph = memory_service.graph

        # Connected memories (via graph._entity_memories)
        mem_ids = graph._entity_memories.get(entity_id, set())
        connected_memories: list[dict[str, Any]] = []
        for mid in list(mem_ids)[:50]:  # Cap at 50
            memory = await memory_service.get_memory(mid)
            if memory:
                structured = memory.structured or {}
                connected_memories.append({
                    "id": memory.id,
                    "content": memory.content[:200],
                    "type": memory.type,
                    "domains": memory.domains,
                    "source_agent": memory.source_agent,
                    "is_insight": memory.type == "insight",
                    "has_contradictions": bool(structured.get("contradictions")),
                    "is_contradicted": bool(structured.get("contradicted_by")),
                    "created_at": memory.created_at.isoformat() if memory.created_at else None,
                })

        # Connected entities (via relationships)
        relationships = await memory_service._store.get_relationships(entity_id)
        connected_entities: list[dict[str, Any]] = []
        seen_entity_ids: set[str] = set()
        for rel in relationships:
            other_id = (
                rel.target_entity_id if rel.source_entity_id == entity_id
                else rel.source_entity_id
            )
            if other_id in seen_entity_ids:
                continue
            seen_entity_ids.add(other_id)
            other = await memory_service._store.get_entity(other_id)
            if other:
                connected_entities.append({
                    "id": other.id,
                    "name": other.name,
                    "type": other.type,
                    "relationship_type": rel.type,
                    "direction": "outgoing" if rel.source_entity_id == entity_id else "incoming",
                })

        return JSONResponse({
            "entity": {
                "id": entity.id,
                "name": entity.name,
                "type": entity.type,
                "attributes": entity.attributes,
                "created_at": entity.created_at.isoformat() if entity.created_at else None,
            },
            "connected_memories": connected_memories,
            "connected_entities": connected_entities,
        })

    async def api_graph(request: Request) -> JSONResponse:
        """Return graph data for D3 force-directed visualization."""
        graph = memory_service.graph
        nodes: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []

        # Entity nodes
        entities = await memory_service.list_entities()
        entity_ids = set()
        for entity in entities[:200]:  # Cap at 200 for performance
            nodes.append({
                "id": entity.id,
                "name": entity.name,
                "type": entity.type,
                "group": "entity",
                "attributes": entity.attributes,
            })
            entity_ids.add(entity.id)

        # Memory nodes (only those linked to entities)
        memory_ids_seen: set[str] = set()
        for entity in entities[:200]:
            mem_ids = graph._entity_memories.get(entity.id, set())
            for mid in mem_ids:
                if mid not in memory_ids_seen:
                    memory = await memory_service.get_memory(mid)
                    if memory:
                        structured = memory.structured or {}
                        nodes.append({
                            "id": mid,
                            "name": memory.content[:60],
                            "type": memory.type,
                            "group": "memory",
                            "domains": memory.domains,
                            "source_agent": memory.source_agent,
                            "is_insight": memory.type == "insight",
                            "has_contradictions": bool(structured.get("contradictions")),
                            "is_contradicted": bool(structured.get("contradicted_by")),
                        })
                        memory_ids_seen.add(mid)
                # Entity -> Memory link
                links.append({
                    "source": entity.id,
                    "target": mid,
                    "type": "linked",
                })

        # Entity -> Entity relationships (from graph edges)
        nx_graph = graph._graph
        for source, target, data in nx_graph.edges(data=True):
            if source in entity_ids and target in entity_ids:
                links.append({
                    "source": source,
                    "target": target,
                    "type": data.get("type", "related_to"),
                })

        return JSONResponse({"nodes": nodes, "links": links})

    async def api_memories(request: Request) -> JSONResponse:
        """Return recent memories."""
        limit = int(request.query_params.get("limit", "30"))
        memories = await memory_service.list_memories(limit=limit)
        return JSONResponse([
            {
                "id": m.id,
                "content": m.content[:200],
                "type": m.type,
                "domains": m.domains,
                "source_agent": m.source_agent,
                "importance": m.importance,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in memories
        ])

    async def api_stats(request: Request) -> JSONResponse:
        """Return system statistics."""
        agents = bus_service.get_all_agents()
        online = sum(1 for a in agents if a.status == "online")
        return JSONResponse({
            "memory_count": await memory_service.memory_count(),
            "entity_count": memory_service.entity_count(),
            "relationship_count": memory_service.relationship_count(),
            "agent_count": len(agents),
            "agents_online": online,
            "agents_sleeping": sum(1 for a in agents if a.status == "sleeping"),
            "domain_count": len(bus_service.list_domains()),
            "event_count": event_log.count(),
        })

    async def api_events(request: Request) -> JSONResponse:
        """Return recent events as JSON (non-streaming)."""
        from dataclasses import asdict

        limit = int(request.query_params.get("limit", "100"))
        events = event_log.recent(limit)
        return JSONResponse([asdict(e) for e in events])

    # ── SPA ───────────────────────────────────────────────────────────────

    async def index(request: Request) -> HTMLResponse:
        """Serve the single-page dashboard app."""
        html_path = STATIC_DIR / "index.html"
        if html_path.exists():
            return HTMLResponse(html_path.read_text())
        return HTMLResponse("<h1>NCMS Dashboard</h1><p>index.html not found</p>")

    # ── App ───────────────────────────────────────────────────────────────

    routes = [
        Route("/", index),
        Route("/api/events/stream", event_stream),
        Route("/api/events", api_events),
        Route("/api/agents", api_agents),
        Route("/api/domains", api_domains),
        Route("/api/graph/entity/{entity_id}", api_graph_entity_detail),
        Route("/api/graph", api_graph),
        Route("/api/memories", api_memories),
        Route("/api/stats", api_stats),
    ]

    return Starlette(routes=routes)


async def run_dashboard(
    host: str = "0.0.0.0",
    port: int = 8420,
    run_demo: bool = False,
) -> None:
    """Start the dashboard web server with full NCMS services.

    Args:
        host: Bind address.
        port: Port number.
        run_demo: If True, also run demo agents for instant visual activity.
    """
    import uvicorn

    from ncms.application.graph_service import GraphService
    from ncms.application.snapshot_service import SnapshotService
    from ncms.config import NCMSConfig
    from ncms.infrastructure.bus.async_bus import AsyncKnowledgeBus
    from ncms.infrastructure.graph.networkx_store import NetworkXGraph
    from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
    from ncms.infrastructure.storage.sqlite_store import SQLiteStore

    # Create shared EventLog
    event_log = EventLog(max_events=5000)

    # Wire up infrastructure with event_log
    config = NCMSConfig(db_path=":memory:")
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    bus = AsyncKnowledgeBus(
        ask_timeout_ms=config.bus_ask_timeout_ms,
        event_log=event_log,
    )

    memory_svc = MemoryService(
        store=store, index=index, graph=graph, config=config,
        event_log=event_log,
    )
    snapshot_svc = SnapshotService(
        store=store,
        max_entries=config.snapshot_max_entries,
        ttl_hours=config.snapshot_ttl_hours,
    )
    bus_svc = BusService(
        bus=bus, snapshot_service=snapshot_svc,
        surrogate_enabled=config.bus_surrogate_enabled,
        event_log=event_log,
    )

    # Rebuild graph from store (for persistent DB mode)
    graph_svc = GraphService(store=store, graph=graph)
    await graph_svc.rebuild_from_store()

    app = create_dashboard_app(memory_svc, bus_svc, event_log)

    # Optionally run demo agents in background
    demo_task = None
    if run_demo:
        from ncms.interfaces.http.demo_runner import run_demo_loop

        demo_task = asyncio.create_task(
            run_demo_loop(memory_svc, bus_svc, snapshot_svc, event_log)
        )

    config_uvicorn = uvicorn.Config(
        app, host=host, port=port, log_level="info",
    )
    server = uvicorn.Server(config_uvicorn)

    # Install signal handlers so Ctrl+C triggers graceful shutdown
    # instead of abrupt termination (which leaves the port in TIME_WAIT)
    import signal

    def _handle_shutdown(sig: int, frame: object) -> None:
        server.should_exit = True

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    try:
        await server.serve()
    finally:
        if demo_task:
            demo_task.cancel()
        await store.close()
