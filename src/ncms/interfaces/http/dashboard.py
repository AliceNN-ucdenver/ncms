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
from typing import Any, cast

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ncms.application.bus_service import BusService
from ncms.application.memory_service import MemoryService
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.observability.event_log import EventLog
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

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

        graph = cast(NetworkXGraph, memory_service.graph)

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
        graph = cast(NetworkXGraph, memory_service.graph)
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

        # Batch-lookup HTMG node types for memories
        if memory_ids_seen:
            store = cast(SQLiteStore, memory_service._store)
            mem_nodes = await store.get_memory_nodes_for_memories(
                list(memory_ids_seen)
            )
            for node in nodes:
                if node.get("group") == "memory":
                    node_list = mem_nodes.get(node["id"], [])
                    node["node_type"] = (
                        node_list[0].node_type if node_list else None
                    )

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
        """Return system statistics and active feature flags."""
        agents = bus_service.get_all_agents()
        online = sum(1 for a in agents if a.status == "online")

        # Active feature flags for dashboard badges
        features: list[str] = []
        cfg = memory_service._config
        if cfg.splade_enabled:
            features.append("SPLADE")
        if cfg.admission_enabled:
            features.append("Admission")
        if cfg.reconciliation_enabled:
            features.append("Reconciliation")
        if cfg.episodes_enabled:
            features.append("Episodes")
        if cfg.intent_classification_enabled:
            features.append("Intent")
        if cfg.reranker_enabled:
            features.append("Reranker")
        if cfg.temporal_enabled:
            features.append("Temporal")
        if cfg.content_classification_enabled:
            features.append("Sections")
        if cfg.contradiction_detection_enabled:
            features.append("Contradictions")
        if cfg.dream_cycle_enabled:
            features.append("Dream")
        if cfg.maintenance_enabled:
            features.append("Maintenance")
        if cfg.search_feedback_enabled:
            features.append("Feedback")

        return JSONResponse({
            "memory_count": await memory_service.memory_count(),
            "entity_count": memory_service.entity_count(),
            "relationship_count": memory_service.relationship_count(),
            "agent_count": len(agents),
            "agents_online": online,
            "agents_sleeping": sum(1 for a in agents if a.status == "sleeping"),
            "domain_count": len(bus_service.list_domains()),
            "event_count": event_log.count(),
            "features": features,
        })

    async def api_topics(request: Request) -> JSONResponse:
        """Return cached entity labels (topics) for all domains."""
        import json

        from ncms.domain.entity_extraction import UNIVERSAL_LABELS

        store = cast(SQLiteStore, memory_service._store)
        # Query all entity_labels:* keys from consolidation_state
        rows = await store.db.execute_fetchall(
            "SELECT key, value FROM consolidation_state"
            " WHERE key LIKE 'entity_labels:%'"
        )
        domains: dict[str, list[str]] = {}
        for key, value in rows:
            domain = key.removeprefix("entity_labels:")
            try:
                labels = json.loads(value)
                if isinstance(labels, list):
                    domains[domain] = labels
            except Exception:
                pass

        return JSONResponse({
            "domains": domains,
            "universal_labels": UNIVERSAL_LABELS,
        })

    # ── Phase 6: HTMG Endpoints ─────────────────────────────────────────

    async def api_episodes(request: Request) -> JSONResponse:
        """Return list of episodes with member counts."""
        from ncms.domain.models import NodeType

        store = cast(SQLiteStore, memory_service._store)
        all_episodes = await store.get_memory_nodes_by_type(NodeType.EPISODE.value)

        result = []
        for ep in all_episodes:
            members = await store.get_episode_members(ep.id)
            result.append({
                "episode_id": ep.id,
                "memory_id": ep.memory_id,
                "status": ep.metadata.get("status", "unknown"),
                "title": ep.metadata.get("episode_title", ""),
                "member_count": len(members),
                "created_at": ep.created_at.isoformat() if ep.created_at else None,
                "closed_at": ep.metadata.get("closed_at"),
            })
        return JSONResponse(result)

    async def api_episode_detail(request: Request) -> JSONResponse:
        """Return episode detail with members."""
        episode_id = request.path_params["episode_id"]
        store = cast(SQLiteStore, memory_service._store)

        episode = await store.get_memory_node(episode_id)
        if not episode:
            return JSONResponse({"error": "Episode not found"}, status_code=404)

        members = await store.get_episode_members(episode_id)
        member_details = []
        for m in members:
            mem = await memory_service.get_memory(m.memory_id)
            member_details.append({
                "node_id": m.id,
                "memory_id": m.memory_id,
                "node_type": m.node_type,
                "content": mem.content[:200] if mem else None,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            })

        return JSONResponse({
            "episode_id": episode.id,
            "status": episode.metadata.get("status", "unknown"),
            "title": episode.metadata.get("episode_title", ""),
            "metadata": episode.metadata,
            "member_count": len(member_details),
            "members": member_details,
        })

    async def api_entity_states(request: Request) -> JSONResponse:
        """Return current states for an entity."""
        entity_id = request.path_params["entity_id"]
        store = cast(SQLiteStore, memory_service._store)

        states = await store.get_entity_states_by_entity(entity_id)
        current = [s for s in states if s.is_current]
        return JSONResponse({
            "entity_id": entity_id,
            "current_states": [s.model_dump(mode="json") for s in current],
            "total_states": len(states),
        })

    async def api_entity_state_history(request: Request) -> JSONResponse:
        """Return state history for an entity."""
        entity_id = request.path_params["entity_id"]
        state_key = request.query_params.get("key", "state")
        store = cast(SQLiteStore, memory_service._store)

        history = await store.get_state_history(entity_id, state_key)
        return JSONResponse({
            "entity_id": entity_id,
            "state_key": state_key,
            "count": len(history),
            "states": [n.model_dump(mode="json") for n in history],
        })

    async def api_entities_with_states(request: Request) -> JSONResponse:
        """Return entities that have state nodes, with state counts and keys."""
        from ncms.domain.models import NodeType

        store = cast(SQLiteStore, memory_service._store)
        nodes = await store.get_memory_nodes_by_type(NodeType.ENTITY_STATE.value)

        # Group by entity_id → collect state keys and counts
        entities: dict[str, dict[str, Any]] = {}
        for n in nodes:
            eid = n.metadata.get("entity_id", "unknown")
            if eid not in entities:
                entities[eid] = {
                    "entity_id": eid,
                    "state_count": 0,
                    "current_count": 0,
                    "state_keys": set(),
                }
            entities[eid]["state_count"] += 1
            if n.is_current:
                entities[eid]["current_count"] += 1
            key = n.metadata.get("state_key", "state")
            entities[eid]["state_keys"].add(key)

        # Convert sets to sorted lists for JSON
        result = []
        for info in sorted(entities.values(), key=lambda x: -x["state_count"]):
            info["state_keys"] = sorted(info["state_keys"])
            result.append(info)

        return JSONResponse(result)

    async def api_bus_snapshot(request: Request) -> JSONResponse:
        """Return current bus state snapshot (agents, domains, subscriptions)."""
        agents = bus_service.get_all_agents()
        domains = bus_service.list_domains()
        subs = bus_service.get_subscriptions()
        return JSONResponse({
            "agents": [
                {
                    "agent_id": a.agent_id,
                    "domains": a.domains,
                    "status": a.status,
                    "last_seen": a.last_seen.isoformat() if a.last_seen else None,
                }
                for a in agents
            ],
            "domains": {d: aids for d, aids in domains.items()},
            "subscriptions": {
                aid: {
                    "domains": sf.domains,
                    "severity_min": sf.severity_min,
                }
                for aid, sf in subs.items()
            },
        })

    async def api_events(request: Request) -> JSONResponse:
        """Return recent events as JSON (non-streaming)."""
        from dataclasses import asdict

        limit = int(request.query_params.get("limit", "100"))
        events = event_log.recent(limit)
        return JSONResponse([asdict(e) for e in events])

    async def api_events_history(request: Request) -> JSONResponse:
        """Return persisted events for time-travel replay.

        Query params:
          after_seq: cursor — return events after this seq (default 0 = all)
          after: ISO timestamp — return events after this time
          before: ISO timestamp — return events before this time
          limit: max events to return (default 5000)
        """
        after_ts = request.query_params.get("after")
        before_ts = request.query_params.get("before")
        limit = int(request.query_params.get("limit", "5000"))

        if after_ts or before_ts:
            # Timestamp-based range query
            events = await event_log.query_time_range(
                start=after_ts or "1970-01-01T00:00:00",
                end=before_ts or "2999-12-31T23:59:59",
                limit=limit,
            )
            last_seq = events[-1]["seq"] if events else 0
        else:
            # Sequence-based cursor query (default)
            after_seq = int(request.query_params.get("after_seq", "0"))
            events = await event_log.query_events(after_seq=after_seq, limit=limit)
            last_seq = events[-1]["seq"] if events else after_seq

        return JSONResponse({
            "events": events,
            "last_seq": last_seq,
            "count": len(events),
        })

    async def api_events_count(request: Request) -> JSONResponse:
        """Return count of persisted events."""
        count = await event_log.event_count_persisted()
        return JSONResponse({"count": count})

    # ── SPA ───────────────────────────────────────────────────────────────

    async def index(request: Request) -> HTMLResponse:
        """Serve the single-page dashboard app."""
        html_path = STATIC_DIR / "index.html"
        if html_path.exists():
            return HTMLResponse(html_path.read_text())
        return HTMLResponse("<h1>NCMS Dashboard</h1><p>index.html not found</p>")

    # ── App ───────────────────────────────────────────────────────────────

    async def prometheus_metrics(request: Request) -> Any:
        """Prometheus metrics endpoint."""
        from ncms.infrastructure.observability.metrics import metrics_endpoint

        return await metrics_endpoint(request)

    routes = [
        Route("/", index),
        Route("/metrics", prometheus_metrics),
        Route("/api/events/stream", event_stream),
        Route("/api/events", api_events),
        Route("/api/events/history", api_events_history),
        Route("/api/events/count", api_events_count),
        Route("/api/agents", api_agents),
        Route("/api/domains", api_domains),
        Route("/api/graph/entity/{entity_id}", api_graph_entity_detail),
        Route("/api/graph", api_graph),
        Route("/api/memories", api_memories),
        Route("/api/topics", api_topics),
        Route("/api/stats", api_stats),
        Route("/api/bus/snapshot", api_bus_snapshot),
        Route("/api/episodes", api_episodes),
        Route("/api/episodes/{episode_id}", api_episode_detail),
        Route("/api/entities-with-states", api_entities_with_states),
        Route("/api/entity-states/{entity_id}", api_entity_states),
        Route("/api/entity-states/{entity_id}/history", api_entity_state_history),
        Mount("/js", StaticFiles(directory=str(STATIC_DIR / "js")), name="js"),
        Mount("/css", StaticFiles(directory=str(STATIC_DIR / "css")), name="css"),
        Mount("/img", StaticFiles(directory=str(STATIC_DIR / "img")), name="img"),
        Mount("/documents", StaticFiles(directory=str(STATIC_DIR / "documents")), name="documents"),
    ]

    # Ensure directories exist for static serving
    (STATIC_DIR / "documents").mkdir(exist_ok=True)
    (STATIC_DIR / "img").mkdir(exist_ok=True)

    return Starlette(routes=routes)


async def run_dashboard(
    host: str = "0.0.0.0",
    port: int = 8420,
    run_demo: bool = False,
    pipeline_debug: bool = False,
    demo_mode: str | None = None,
) -> None:
    """Start the dashboard web server with full NCMS services.

    Args:
        host: Bind address.
        port: Port number.
        run_demo: If True, also run demo agents for instant visual activity.
        pipeline_debug: If True, emit candidate details in pipeline events.
        demo_mode: "nd" for Architect/Security/Builder, "classic" for standard demo,
            None to auto-select based on run_demo flag.
    """
    import uvicorn

    from ncms.application.graph_service import GraphService
    from ncms.application.snapshot_service import SnapshotService
    from ncms.config import NCMSConfig
    from ncms.infrastructure.bus.async_bus import AsyncKnowledgeBus
    from ncms.infrastructure.graph.networkx_store import NetworkXGraph
    from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
    from ncms.infrastructure.storage.sqlite_store import SQLiteStore

    # Wire up infrastructure with event_log
    config = NCMSConfig(db_path=":memory:", pipeline_debug=pipeline_debug)
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()

    # Create shared EventLog with SQLite persistence for time-travel replay
    event_log = EventLog(max_events=5000, db=store.db)
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()
    bus = AsyncKnowledgeBus(
        ask_timeout_ms=config.bus_ask_timeout_ms,
        event_log=event_log,
    )

    # SPLADE sparse neural retrieval (disabled by default)
    splade = None
    if config.splade_enabled:
        from ncms.infrastructure.indexing.splade_engine import SpladeEngine

        splade = SpladeEngine(
            model_name=config.splade_model,
            cache_dir=config.model_cache_dir,
        )
        # Eager-load model at startup to avoid race condition when
        # multiple concurrent store_memory calls trigger lazy load
        splade._ensure_model()
        logger.info("SPLADE model pre-loaded at startup")

    # Admission scoring (Phase 1, disabled by default)
    admission = None
    if config.admission_enabled:
        from ncms.application.admission_service import AdmissionService

        admission = AdmissionService(store=store, index=index, graph=graph, config=config)

    # Reconciliation service (Phase 2, disabled by default)
    reconciliation = None
    if config.reconciliation_enabled:
        from ncms.application.reconciliation_service import ReconciliationService

        reconciliation = ReconciliationService(
            store=store, config=config, event_log=event_log,
        )

    # Episode formation (Phase 3, disabled by default)
    episode = None
    if config.episodes_enabled:
        from ncms.application.episode_service import EpisodeService

        episode = EpisodeService(
            store=store, index=index, config=config,
            event_log=event_log, splade=splade,
        )

    # Intent classifier (Phase 4, disabled by default)
    intent_classifier = None
    if config.intent_classification_enabled:
        from ncms.infrastructure.indexing.exemplar_intent_index import (
            ExemplarIntentIndex,
        )

        intent_classifier = ExemplarIntentIndex()
        logger.info("BM25 exemplar intent classifier enabled")

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

    memory_svc = MemoryService(
        store=store, index=index, graph=graph, config=config,
        event_log=event_log, splade=splade, admission=admission,
        reconciliation=reconciliation, episode=episode,
        intent_classifier=intent_classifier,
        reranker=reranker,
    )
    snapshot_svc = SnapshotService(
        store=store,
        max_entries=config.snapshot_max_entries,
        ttl_hours=config.snapshot_ttl_hours,
    )
    bus_svc = BusService(
        bus=bus, snapshot_service=snapshot_svc,
        surrogate_enabled=True,  # Always on (retired flag)
        event_log=event_log,
    )

    # Rebuild graph from store (for persistent DB mode)
    graph_svc = GraphService(store=store, graph=graph)
    await graph_svc.rebuild_from_store()

    app = create_dashboard_app(memory_svc, bus_svc, event_log)

    # Start event persistence background task for time-travel replay
    persist_task = asyncio.create_task(event_log.start_persistence())

    # Optionally run demo agents in background
    demo_task = None
    effective_mode = demo_mode if demo_mode else ("classic" if run_demo else None)
    if effective_mode == "nd":
        from ncms.interfaces.http.demo_runner_nd import run_nd_demo_loop

        demo_task = asyncio.create_task(
            run_nd_demo_loop(memory_svc, bus_svc, snapshot_svc, event_log)
        )
    elif effective_mode == "classic":
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
        persist_task.cancel()
        if demo_task:
            demo_task.cancel()
        await store.close()
