"""NCMS HTTP REST API server.

Exposes NCMS memory, knowledge bus, and agent lifecycle operations
as HTTP endpoints for multi-agent deployments.

Usage:
    ncms serve --transport http --port 8080
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
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
    extra_routes: list[Route] | None = None,
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

        domain_param = request.query_params.get("domain")
        limit = int(request.query_params.get("limit", "10"))
        intent = request.query_params.get("intent")

        # Support comma-separated domains: ?domain=architecture,security
        if domain_param and "," in domain_param:
            domains = [d.strip() for d in domain_param.split(",") if d.strip()]
            # Search each domain, merge + deduplicate by memory_id, re-sort
            seen: dict[str, Any] = {}
            for d in domains:
                domain_results = await memory_svc.search(
                    query=query, domain=d, limit=limit, intent_override=intent,
                )
                for r in domain_results:
                    mid = r.memory.id
                    if mid not in seen or r.combined_score > seen[mid].combined_score:
                        seen[mid] = r
            results = sorted(seen.values(), key=lambda r: r.combined_score, reverse=True)[:limit]
        else:
            results = await memory_svc.search(
                query=query, domain=domain_param, limit=limit, intent_override=intent,
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
                    "score": r.memory.total_activation,
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

        agent_id = request.headers.get("X-Agent-ID", body.get("from_agent", "http-client"))
        timeout_ms = body.get("timeout_ms", 60000)
        ask_obj = KnowledgeAsk(
            question=question,
            domains=body.get("domains", []),
            from_agent=agent_id,
            ttl_ms=timeout_ms,
        )
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

        agent_id = request.headers.get(
            "X-Agent-ID", body.get("from_agent", "http-client"),
        )
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

    # -- Agent Chat Proxy ----------------------------------------------------
    # Proxies chat requests to NAT agents' /generate endpoint.
    # Avoids CORS issues (dashboard and hub share the same origin).
    # Agent port mapping: architect=8001, security=8002, builder=8003
    _AGENT_PORTS = {"architect": 8001, "security": 8002, "builder": 8003, "product_owner": 8004, "researcher": 8005}

    async def agent_chat(request: Request) -> JSONResponse:
        import httpx as _httpx

        agent_id = request.path_params["agent_id"]
        port = _AGENT_PORTS.get(agent_id)
        if port is None:
            return JSONResponse(
                {"error": f"Unknown agent: {agent_id}"}, status_code=404,
            )

        body = await request.json()
        input_message = body.get("input_message", body.get("question", ""))
        if not input_message:
            return JSONResponse({"error": "Missing input_message"}, status_code=400)

        try:
            async with _httpx.AsyncClient(
                timeout=_httpx.Timeout(300.0, connect=10.0),
            ) as client:
                # Turn 1: send the user's message
                resp = await client.post(
                    f"http://host.docker.internal:{port}/generate",
                    json={"input_message": input_message},
                )
                resp.raise_for_status()
                data = resp.json()
                content = data.get("value", data.get("output", str(data)))

                return JSONResponse({
                    "from_agent": agent_id,
                    "content": content,
                    "answered": True,
                })
        except _httpx.TimeoutException:
            return JSONResponse(
                {"error": "Agent timed out", "answered": False}, status_code=504,
            )
        except Exception as exc:
            return JSONResponse(
                {"error": f"Agent unreachable: {exc}", "answered": False},
                status_code=502,
            )

    # -- Document Store -------------------------------------------------------

    _documents: dict[str, dict[str, Any]] = {}
    _documents_dir = Path(__file__).parent / "static" / "documents"
    _documents_dir.mkdir(parents=True, exist_ok=True)

    async def store_document(request: Request) -> JSONResponse:
        """Store a design document (markdown) and return a download URL."""
        body = await request.json()
        title = body.get("title", "Untitled")
        content = body.get("content", "")
        from_agent = body.get("from_agent")
        plan_id = body.get("plan_id")

        if not content:
            return JSONResponse({"error": "content is required"}, status_code=400)

        doc_id = uuid.uuid4().hex[:12]
        filename = f"{doc_id}.md"
        filepath = _documents_dir / filename
        filepath.write_text(content, encoding="utf-8")

        meta = {
            "document_id": doc_id,
            "title": title,
            "from_agent": from_agent,
            "plan_id": plan_id,
            "format": body.get("format", "markdown"),
            "url": f"/documents/{filename}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "size_bytes": len(content.encode("utf-8")),
        }
        _documents[doc_id] = meta

        # Emit SSE event so dashboard auto-refreshes documents
        if event_log:
            from ncms.infrastructure.observability.event_log import DashboardEvent
            event_log.emit(DashboardEvent(
                type="document.published",
                data={
                    "document_id": doc_id,
                    "title": title,
                    "from_agent": from_agent,
                    "content": content[:200],
                },
                agent_id=from_agent,
            ))

        return JSONResponse(meta, status_code=201)

    async def list_documents(request: Request) -> JSONResponse:
        """List all published documents."""
        docs = sorted(
            _documents.values(),
            key=lambda d: d.get("created_at", ""),
            reverse=True,
        )
        return JSONResponse(docs)

    async def get_document(request: Request) -> JSONResponse:
        """Return a single document with its full content."""
        doc_id = request.path_params["doc_id"]
        meta = _documents.get(doc_id)
        if not meta:
            return JSONResponse({"error": "Document not found"}, status_code=404)

        filepath = _documents_dir / f"{doc_id}.md"
        content = ""
        if filepath.exists():
            content = filepath.read_text(encoding="utf-8")

        return JSONResponse({**meta, "content": content})

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

        # Agent chat proxy (NAT /generate)
        Route("/api/v1/agent/{agent_id}/chat", agent_chat, methods=["POST"]),

        # Consolidation
        Route("/api/v1/consolidation/run", run_consolidation_endpoint, methods=["POST"]),

        # Documents
        Route("/api/v1/documents", store_document, methods=["POST"]),
        Route("/api/v1/documents", list_documents, methods=["GET"]),
        Route("/api/v1/documents/{doc_id}", get_document, methods=["GET"]),
    ]

    # Mount transport or other extra routes (e.g. HttpBusTransport SSE)
    if extra_routes:
        routes.extend(extra_routes)

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            return await auth_middleware(request, call_next)

    from starlette.middleware.cors import CORSMiddleware

    middlewares = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        ),
    ]
    if auth_token:
        middlewares.append(Middleware(AuthMiddleware))

    app = Starlette(routes=routes, middleware=middlewares)
    return app


async def run_http_server(
    config: Any = None,
    host: str = "0.0.0.0",
    port: int = 8080,
    auth_token: str | None = None,
    dashboard_port: int | None = None,
) -> None:
    """Start the NCMS HTTP API server.

    Args:
        dashboard_port: If set, also start the dashboard on this port,
            sharing the same EventLog and services (single process).
    """
    import signal

    import uvicorn

    from ncms.config import NCMSConfig
    from ncms.infrastructure.bus.async_bus import AsyncKnowledgeBus
    from ncms.infrastructure.bus.http_transport import HttpBusTransport
    from ncms.infrastructure.observability.event_log import EventLog
    from ncms.interfaces.mcp.server import create_ncms_services

    config = config or NCMSConfig()
    event_log = EventLog(max_events=5000)

    # Create services — then wrap bus with HTTP transport
    memory_svc, bus_svc, snapshot_svc, consolidation_svc = (
        await create_ncms_services(config)
    )

    # Wire event log into services for dashboard visibility
    if hasattr(memory_svc, "_event_log"):
        memory_svc._event_log = event_log
    if hasattr(bus_svc, "_event_log"):
        bus_svc._event_log = event_log

    # Wrap the inner bus with HttpBusTransport for remote agent support
    inner_bus = bus_svc.bus
    if isinstance(inner_bus, AsyncKnowledgeBus):
        inner_bus._event_log = event_log
        transport = HttpBusTransport(inner=inner_bus, event_log=event_log)
        bus_svc._bus = transport  # Swap transport — BusService is unaware
    else:
        transport = None

    app = create_api_app(
        memory_svc=memory_svc,
        bus_svc=bus_svc,
        snapshot_svc=snapshot_svc,
        consolidation_svc=consolidation_svc,
        event_log=event_log,
        auth_token=auth_token,
        extra_routes=transport.starlette_routes() if transport else None,
    )

    config_uvicorn = uvicorn.Config(
        app, host=host, port=port, log_level="info",
    )
    server = uvicorn.Server(config_uvicorn)

    # Optionally start dashboard on a separate port, sharing services
    dashboard_server = None
    if dashboard_port:
        try:
            from ncms.interfaces.http.dashboard import create_dashboard_app

            dashboard_app = create_dashboard_app(
                memory_service=memory_svc,
                bus_service=bus_svc,
                event_log=event_log,
            )
            dashboard_config = uvicorn.Config(
                dashboard_app, host=host, port=dashboard_port,
                log_level="info",
            )
            dashboard_server = uvicorn.Server(dashboard_config)
            logger.info(
                "Dashboard will start on %s:%d (shared EventLog)",
                host, dashboard_port,
            )
        except ImportError:
            logger.warning(
                "Dashboard dependencies not installed, skipping"
            )

    def _handle_shutdown(sig: int, frame: object) -> None:
        server.should_exit = True
        if dashboard_server:
            dashboard_server.should_exit = True

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    logger.info("NCMS HTTP API server starting on %s:%d", host, port)

    if dashboard_server:
        # Run both servers concurrently in the same event loop
        await asyncio.gather(
            server.serve(),
            dashboard_server.serve(),
        )
    else:
        await server.serve()
