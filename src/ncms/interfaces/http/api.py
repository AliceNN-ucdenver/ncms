"""NCMS HTTP REST API server.

Exposes NCMS memory, knowledge bus, and agent lifecycle operations
as HTTP endpoints for multi-agent deployments.

Usage:
    ncms serve --transport http --port 8080
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from ncms.application.bus_service import BusService
from ncms.application.memory_service import MemoryService
from ncms.application.snapshot_service import SnapshotService
from ncms.domain.models import (
    KnowledgeAnnounce,
    KnowledgeAsk,
    KnowledgePayload,
    SnapshotEntry,
)
from ncms.infrastructure.observability.event_log import EventLog, NullEventLog

logger = logging.getLogger(__name__)


def create_api_app(
    memory_svc: MemoryService,
    bus_svc: BusService,
    snapshot_svc: SnapshotService,
    consolidation_svc: object | None = None,
    event_log: EventLog | None = None,
    auth_token: str | None = None,
) -> Starlette:
    """Create the NCMS HTTP REST API application."""

    _event_log = event_log or NullEventLog()

    # -- Auth middleware -----------------------------------------------------

    async def auth_middleware(request: Request, call_next):
        if auth_token and request.url.path != "/api/v1/health":
            token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if token != auth_token:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)

    # -- Health --------------------------------------------------------------

    async def health(request: Request) -> JSONResponse:
        count = await memory_svc.memory_count()
        agents = bus_svc.get_all_agents()
        return JSONResponse({
            "status": "healthy",
            "memory_count": count,
            "agent_count": len(agents),
        })

    # -- Memory operations ---------------------------------------------------

    async def store_memory(request: Request) -> JSONResponse:
        body = await request.json()
        content = body.get("content", "")
        if not content:
            return JSONResponse({"error": "content is required"}, status_code=400)

        agent_id = request.headers.get("X-Agent-ID")
        memory = await memory_svc.store_memory(
            content=content,
            memory_type=body.get("type", "fact"),
            domains=body.get("domains"),
            tags=body.get("tags"),
            importance=body.get("importance", 5.0),
            source_agent=agent_id or body.get("source_agent"),
            structured=body.get("structured"),
        )
        return JSONResponse({
            "memory_id": memory.id,
            "content": memory.content[:200],
            "domains": memory.domains,
            "created_at": memory.created_at.isoformat() if memory.created_at else None,
        }, status_code=201)

    async def search_memory(request: Request) -> JSONResponse:
        query = request.query_params.get("q", "")
        if not query:
            return JSONResponse({"error": "q parameter is required"}, status_code=400)

        domain = request.query_params.get("domain")
        limit = int(request.query_params.get("limit", "10"))
        intent = request.query_params.get("intent")

        results = await memory_svc.search(
            query=query, domain=domain, limit=limit, intent_override=intent,
        )
        return JSONResponse({
            "results": [
                {
                    "memory_id": r.memory.id,
                    "content": r.memory.content,
                    "type": r.memory.type,
                    "domains": r.memory.domains,
                    "combined_score": r.combined_score,
                    "bm25_score": r.bm25_score,
                    "created_at": (
                        r.memory.created_at.isoformat() if r.memory.created_at else None
                    ),
                }
                for r in results
            ],
            "count": len(results),
        })

    async def recall_memory(request: Request) -> JSONResponse:
        query = request.query_params.get("q", "")
        if not query:
            return JSONResponse({"error": "q parameter is required"}, status_code=400)

        domain = request.query_params.get("domain")
        limit = int(request.query_params.get("limit", "10"))

        results = await memory_svc.recall(
            query=query, domain=domain, limit=limit,
        )
        return JSONResponse({
            "results": [
                {
                    "memory_id": r.memory.memory.id,
                    "content": r.memory.memory.content,
                    "combined_score": r.memory.combined_score,
                    "retrieval_path": r.retrieval_path,
                    "episode": {
                        "episode_id": r.context.episode.episode_id,
                        "episode_title": r.context.episode.episode_title,
                        "status": r.context.episode.status,
                        "member_count": r.context.episode.member_count,
                    } if r.context.episode else None,
                    "entity_states": [
                        {
                            "entity_name": s.entity_name,
                            "state_key": s.state_key,
                            "state_value": s.state_value,
                            "is_current": s.is_current,
                        }
                        for s in r.context.entity_states
                    ],
                    "causal_chain": {
                        "supersedes": r.context.causal_chain.supersedes,
                        "superseded_by": r.context.causal_chain.superseded_by,
                        "derived_from": r.context.causal_chain.derived_from,
                        "supports": r.context.causal_chain.supports,
                        "conflicts_with": r.context.causal_chain.conflicts_with,
                    },
                }
                for r in results
            ],
            "count": len(results),
        })

    async def delete_memory_endpoint(request: Request) -> JSONResponse:
        memory_id = request.path_params["memory_id"]
        deleted = await memory_svc.delete(memory_id)
        if not deleted:
            return JSONResponse({"error": "Memory not found"}, status_code=404)
        return JSONResponse({"deleted": True, "memory_id": memory_id})

    async def get_provenance(request: Request) -> JSONResponse:
        memory_id = request.path_params["memory_id"]
        memory = await memory_svc.get_memory(memory_id)
        if memory is None:
            return JSONResponse({"error": "Memory not found"}, status_code=404)

        entity_ids = await memory_svc.store.get_memory_entities(memory_id)
        return JSONResponse({
            "memory_id": memory.id,
            "content": memory.content,
            "type": memory.type,
            "domains": memory.domains,
            "tags": memory.tags,
            "source_agent": memory.source_agent,
            "importance": memory.importance,
            "access_count": memory.access_count,
            "created_at": memory.created_at.isoformat() if memory.created_at else None,
            "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
            "linked_entities": entity_ids,
        })

    # -- Knowledge Bus -------------------------------------------------------

    async def bus_ask(request: Request) -> JSONResponse:
        body = await request.json()
        question = body.get("question", "")
        if not question:
            return JSONResponse({"error": "question is required"}, status_code=400)

        agent_id = request.headers.get("X-Agent-ID", "http-client")
        ask_obj = KnowledgeAsk(
            question=question,
            domains=body.get("domains", []),
            from_agent=agent_id,
        )
        timeout_ms = body.get("timeout_ms", 5000)
        response = await bus_svc.ask_sync(ask_obj, timeout_ms=timeout_ms)

        if response is None:
            return JSONResponse({"answered": False})

        return JSONResponse({
            "answered": True,
            "content": response.knowledge.content,
            "from_agent": response.from_agent,
            "source_mode": response.source_mode,
            "confidence": response.confidence,
            "staleness_warning": response.staleness_warning,
        })

    async def bus_announce(request: Request) -> JSONResponse:
        body = await request.json()
        content = body.get("content", "")
        domains = body.get("domains", [])
        if not content or not domains:
            return JSONResponse(
                {"error": "content and domains are required"}, status_code=400,
            )

        agent_id = request.headers.get("X-Agent-ID", "http-client")
        announcement = KnowledgeAnnounce(
            knowledge=KnowledgePayload(content=content),
            domains=domains,
            from_agent=agent_id,
            event=body.get("event", "updated"),
        )
        await bus_svc.announce(announcement)
        return JSONResponse({"announced": True, "domains": domains})

    async def bus_events(request: Request) -> StreamingResponse:
        """SSE stream of Knowledge Bus events."""
        domain_filter = request.query_params.get("domains", "").split(",")
        domain_filter = [d.strip() for d in domain_filter if d.strip()]

        async def generate():
            queue = await _event_log.subscribe()
            try:
                while True:
                    try:
                        evt = await asyncio.wait_for(queue.get(), timeout=30.0)
                        # Filter by domain if specified
                        if domain_filter:
                            evt_domains = getattr(evt, "domains", [])
                            if evt_domains and not any(
                                d in domain_filter for d in evt_domains
                            ):
                                continue
                        yield evt.to_sse()
                    except TimeoutError:
                        yield ": keepalive\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                _event_log.unsubscribe(queue)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def bus_domains(request: Request) -> JSONResponse:
        domains = bus_svc.list_domains()
        return JSONResponse({"domains": domains, "total": len(domains)})

    # -- Agent lifecycle -----------------------------------------------------

    async def agent_wake(request: Request) -> JSONResponse:
        agent_id = request.path_params["agent_id"]
        body = await request.json()
        domains = body.get("domains", [])

        # Register agent as provider
        await bus_svc.register_provider(agent_id, domains)

        # Subscribe to domains
        subscribe_to = body.get("subscribe_to", [])
        if subscribe_to:
            await bus_svc.subscribe(agent_id, subscribe_to)

        # Check inbox for messages received while sleeping
        inbox = await bus_svc.drain_inbox(agent_id)
        announcements = await bus_svc.drain_announcements(agent_id)

        return JSONResponse({
            "agent_id": agent_id,
            "status": "live",
            "domains": domains,
            "inbox_count": len(inbox),
            "announcement_count": len(announcements),
            "inbox": [
                {
                    "content": r.knowledge.content,
                    "from_agent": r.from_agent,
                    "source_mode": r.source_mode,
                }
                for r in inbox
            ],
            "announcements": [
                {
                    "content": a.knowledge.content,
                    "from_agent": a.from_agent,
                    "event": a.event,
                }
                for a in announcements
            ],
        })

    async def agent_sleep(request: Request) -> JSONResponse:
        agent_id = request.path_params["agent_id"]
        body = await request.json()

        # Create snapshot from provided entries
        entries = []
        for entry_data in body.get("entries", []):
            entries.append(SnapshotEntry(
                domain=entry_data.get("domain", "general"),
                knowledge=KnowledgePayload(content=entry_data["content"]),
                confidence=entry_data.get("confidence", 0.8),
            ))

        if entries:
            domains = list({e.domain for e in entries})
            await snapshot_svc.create_snapshot(
                agent_id=agent_id,
                entries=entries,
                domains=domains,
            )

        # Update agent status
        await bus_svc.update_availability(agent_id, "sleeping")

        return JSONResponse({
            "agent_id": agent_id,
            "status": "sleeping",
            "snapshot_entries": len(entries),
        })

    async def agent_snapshot(request: Request) -> JSONResponse:
        agent_id = request.path_params["agent_id"]
        snapshot = await snapshot_svc.get_snapshot(agent_id)
        if snapshot is None:
            return JSONResponse({"exists": False, "agent_id": agent_id})

        return JSONResponse({
            "exists": True,
            "agent_id": agent_id,
            "snapshot_id": snapshot.snapshot_id,
            "timestamp": snapshot.timestamp.isoformat() if snapshot.timestamp else None,
            "domains": snapshot.domains,
            "entry_count": len(snapshot.entries),
        })

    async def list_agents(request: Request) -> JSONResponse:
        agents = bus_svc.get_all_agents()
        return JSONResponse({
            "agents": [
                {
                    "agent_id": a.agent_id,
                    "status": a.status,
                    "domains": a.domains,
                }
                for a in agents
            ],
            "count": len(agents),
        })

    # -- Entity state & episodes ---------------------------------------------

    async def entity_state(request: Request) -> JSONResponse:
        entity_id = request.path_params["entity_id"]
        state_key = request.query_params.get("key", "state")
        node = await memory_svc.store.get_current_state(entity_id, state_key)
        if node is None:
            return JSONResponse({"found": False, "entity_id": entity_id})
        return JSONResponse({
            "found": True,
            "entity_id": entity_id,
            "node_id": node.id,
            "state_key": node.metadata.get("state_key", ""),
            "state_value": node.metadata.get("state_value", ""),
            "observed_at": node.metadata.get("observed_at", ""),
        })

    async def entity_history(request: Request) -> JSONResponse:
        entity_id = request.path_params["entity_id"]
        state_key = request.query_params.get("key", "state")
        nodes = await memory_svc.store.get_state_history(entity_id, state_key)
        return JSONResponse({
            "entity_id": entity_id,
            "state_key": state_key,
            "count": len(nodes),
            "states": [
                {
                    "node_id": n.id,
                    "state_value": n.metadata.get("state_value", ""),
                    "is_current": n.is_current,
                    "observed_at": n.metadata.get("observed_at", ""),
                    "created_at": n.created_at.isoformat() if n.created_at else None,
                }
                for n in nodes
            ],
        })

    async def list_episodes_endpoint(request: Request) -> JSONResponse:
        episodes = list(await memory_svc.store.get_open_episodes())
        return JSONResponse({
            "count": len(episodes),
            "episodes": [
                {
                    "episode_id": ep.id,
                    "memory_id": ep.memory_id,
                    "status": ep.metadata.get("status", "open"),
                    "title": ep.metadata.get("episode_title", ""),
                    "created_at": ep.created_at.isoformat() if ep.created_at else None,
                }
                for ep in episodes
            ],
        })

    async def get_episode_endpoint(request: Request) -> JSONResponse:
        episode_id = request.path_params["episode_id"]
        episode_node = await memory_svc.store.get_memory_node(episode_id)
        if episode_node is None:
            return JSONResponse({"error": "Episode not found"}, status_code=404)

        members = await memory_svc.store.get_episode_members(episode_id)
        return JSONResponse({
            "episode_id": episode_node.id,
            "status": episode_node.metadata.get("status", "unknown"),
            "title": episode_node.metadata.get("episode_title", ""),
            "member_count": len(members),
            "members": [
                {
                    "node_id": m.id,
                    "memory_id": m.memory_id,
                    "node_type": m.node_type,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in members
            ],
        })

    # -- Consolidation -------------------------------------------------------

    async def run_consolidation_endpoint(request: Request) -> JSONResponse:
        if consolidation_svc is None:
            return JSONResponse({"error": "Consolidation not available"}, status_code=503)
        result = await consolidation_svc.run_consolidation_pass()  # type: ignore[union-attr]
        return JSONResponse(result)

    # -- Routes --------------------------------------------------------------

    routes = [
        # Health
        Route("/api/v1/health", health, methods=["GET"]),

        # Memory operations
        Route("/api/v1/memories", store_memory, methods=["POST"]),
        Route("/api/v1/memories/search", search_memory, methods=["GET"]),
        Route("/api/v1/memories/recall", recall_memory, methods=["GET"]),
        Route("/api/v1/memories/{memory_id}", get_provenance, methods=["GET"]),
        Route("/api/v1/memories/{memory_id}", delete_memory_endpoint, methods=["DELETE"]),

        # Knowledge Bus
        Route("/api/v1/bus/ask", bus_ask, methods=["POST"]),
        Route("/api/v1/bus/announce", bus_announce, methods=["POST"]),
        Route("/api/v1/bus/events", bus_events, methods=["GET"]),
        Route("/api/v1/bus/domains", bus_domains, methods=["GET"]),

        # Agent lifecycle
        Route("/api/v1/agents", list_agents, methods=["GET"]),
        Route("/api/v1/agents/{agent_id}/wake", agent_wake, methods=["POST"]),
        Route("/api/v1/agents/{agent_id}/sleep", agent_sleep, methods=["POST"]),
        Route("/api/v1/agents/{agent_id}/snapshot", agent_snapshot, methods=["GET"]),

        # Entity state & episodes
        Route("/api/v1/entities/{entity_id}/state", entity_state, methods=["GET"]),
        Route("/api/v1/entities/{entity_id}/history", entity_history, methods=["GET"]),
        Route("/api/v1/episodes", list_episodes_endpoint, methods=["GET"]),
        Route("/api/v1/episodes/{episode_id}", get_episode_endpoint, methods=["GET"]),

        # Consolidation
        Route("/api/v1/consolidation/run", run_consolidation_endpoint, methods=["POST"]),
    ]

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            return await auth_middleware(request, call_next)

    middlewares = []
    if auth_token:
        middlewares.append(Middleware(AuthMiddleware))

    app = Starlette(routes=routes, middleware=middlewares)
    return app


async def run_http_server(
    config: Any = None,
    host: str = "0.0.0.0",
    port: int = 8080,
    auth_token: str | None = None,
) -> None:
    """Start the NCMS HTTP API server."""
    import signal

    import uvicorn

    from ncms.config import NCMSConfig
    from ncms.infrastructure.observability.event_log import EventLog
    from ncms.interfaces.mcp.server import create_ncms_services

    config = config or NCMSConfig()

    # Use the same service creation as MCP server
    memory_svc, bus_svc, snapshot_svc, consolidation_svc = await create_ncms_services(config)

    event_log = EventLog(max_events=200)
    # Wire event log into memory service if possible
    if hasattr(memory_svc, "_event_log"):
        memory_svc._event_log = event_log

    app = create_api_app(
        memory_svc=memory_svc,
        bus_svc=bus_svc,
        snapshot_svc=snapshot_svc,
        consolidation_svc=consolidation_svc,
        event_log=event_log,
        auth_token=auth_token,
    )

    config_uvicorn = uvicorn.Config(
        app, host=host, port=port, log_level="info",
    )
    server = uvicorn.Server(config_uvicorn)

    def _handle_shutdown(sig: int, frame: object) -> None:
        server.should_exit = True

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    logger.info("NCMS HTTP API server starting on %s:%d", host, port)
    await server.serve()
