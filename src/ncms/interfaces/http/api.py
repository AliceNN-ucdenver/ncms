"""NCMS HTTP REST API server.

Exposes NCMS memory, knowledge bus, and agent lifecycle operations
as HTTP endpoints for multi-agent deployments.

Usage:
    ncms serve --transport http --port 8080
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
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
    doc_svc: object | None = None,  # DocumentService (Phase 2.5)
    extra_routes: list[Route] | None = None,
    maintenance_scheduler: object | None = None,
) -> Starlette:
    """Create the NCMS HTTP REST API application."""

    _event_log = event_log or NullEventLog()

    # -- Auth (JWT) -----------------------------------------------------------

    import os as _os
    import secrets as _secrets
    _jwt_secret = _os.environ.get("NCMS_JWT_SECRET") or _secrets.token_hex(32)

    # Unprotected paths (no JWT required)
    _public_paths = {
        "/api/v1/health", "/api/v1/auth/login",
        "/api/v1/bus/events", "/api/v1/bus/register",
        "/api/v1/maintenance/status",
    }
    _public_prefixes = ("/js/", "/css/", "/api/stats", "/api/v1/agents")

    async def auth_middleware(request: Request, call_next):
        path = request.url.path

        # Public paths: no auth needed
        if path in _public_paths or any(path.startswith(p) for p in _public_prefixes):
            return await call_next(request)

        # GET requests: allow without auth (dashboard viewing)
        if request.method == "GET":
            # Still extract user if token provided (for identity display)
            _try_extract_user(request)
            return await call_next(request)

        # Legacy static auth_token support (for agent→hub API calls)
        if auth_token:
            bearer = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if bearer == auth_token:
                return await call_next(request)

        # JWT auth for mutations (POST/PUT/DELETE)
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if bearer:
            try:
                import jwt
                payload = jwt.decode(bearer, _jwt_secret, algorithms=["HS256"])
                request.state.user = payload
                return await call_next(request)
            except Exception:
                pass

        # Agent-to-hub calls (from sandboxes) use X-Agent-ID header, no JWT
        if request.headers.get("X-Agent-ID"):
            return await call_next(request)

        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    def _try_extract_user(request: Request):
        """Extract user from JWT if present (non-blocking)."""
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if bearer:
            try:
                import jwt
                request.state.user = jwt.decode(bearer, _jwt_secret, algorithms=["HS256"])
            except Exception:
                pass

    async def login(request: Request) -> JSONResponse:
        """Authenticate with username/password, return JWT token."""
        body = await request.json()
        username = body.get("username", "")
        password = body.get("password", "")
        if not username or not password:
            return JSONResponse({"error": "username and password required"}, status_code=400)

        if not doc_svc:
            return JSONResponse({"error": "auth not available"}, status_code=503)

        user = await doc_svc._store.get_user_by_username(username)
        if not user:
            return JSONResponse({"error": "invalid credentials"}, status_code=401)

        import bcrypt as _bcrypt
        if not _bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
            return JSONResponse({"error": "invalid credentials"}, status_code=401)

        import jwt
        payload = {
            "sub": user["username"],
            "role": user["role"],
            "display_name": user["display_name"],
            "exp": int(time.time()) + 86400,  # 24 hours
        }
        token = jwt.encode(payload, _jwt_secret, algorithm="HS256")

        return JSONResponse({
            "token": token,
            "expires_in": 86400,
            "user": {
                "username": user["username"],
                "display_name": user["display_name"],
                "role": user["role"],
            },
        })

    async def auth_me(request: Request) -> JSONResponse:
        """Return current user from JWT."""
        user = getattr(request.state, "user", None)
        if not user:
            _try_extract_user(request)
            user = getattr(request.state, "user", None)
        if not user:
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        return JSONResponse(user)

    # -- Health --------------------------------------------------------------

    async def health(request: Request) -> JSONResponse:
        count = await memory_svc.memory_count()
        agents = bus_svc.get_all_agents()
        online = sum(1 for a in agents if a.status == "online")

        result: dict[str, Any] = {
            "status": "healthy",
            "memory_count": count,
            "agent_count": len(agents),
            "agents_online": online,
        }

        # Indexing stats (from background worker pool)
        idx_stats = memory_svc.index_pool_stats()
        if idx_stats is not None:
            result["indexing"] = {
                "queue_depth": idx_stats["queue_depth"],
                "workers": idx_stats["workers"],
                "workers_busy": idx_stats["workers_busy"],
                "processed_total": idx_stats["processed_total"],
                "failed_total": idx_stats["failed_total"],
            }

        # Graph stats (entity + edge counts from in-memory graph)
        result["graph"] = {
            "entity_count": memory_svc.entity_count(),
            "edge_count": memory_svc.relationship_count(),
        }

        # Maintenance scheduler status (None until scheduler is wired)
        if maintenance_scheduler is not None:
            try:
                result["maintenance"] = maintenance_scheduler.status()
            except Exception:
                logger.debug(
                    "Failed to get maintenance scheduler status",
                    exc_info=True,
                )

        return JSONResponse(result)

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
        # Support both GET (query params) and POST (JSON body) for large queries
        if request.method == "POST":
            body = await request.json()
            query = body.get("q", body.get("query", ""))
            domain_param = body.get("domain")
            limit = int(body.get("limit", 10))
            intent = body.get("intent")
        else:
            query = request.query_params.get("q", "")
            domain_param = request.query_params.get("domain")
            limit = int(request.query_params.get("limit", "10"))
            intent = request.query_params.get("intent")

        if not query:
            return JSONResponse({"error": "q parameter is required"}, status_code=400)
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
                    if mid not in seen or r.total_activation > seen[mid].combined_score:
                        seen[mid] = r
            results = sorted(seen.values(), key=lambda r: r.total_activation, reverse=True)[:limit]
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
                    "score": r.total_activation,
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
        if request.method == "POST":
            body = await request.json()
            query = body.get("q", body.get("query", ""))
            domain = body.get("domain")
            limit = int(body.get("limit", 10))
        else:
            query = request.query_params.get("q", "")
            domain = request.query_params.get("domain")
            limit = int(request.query_params.get("limit", "10"))

        if not query:
            return JSONResponse({"error": "q parameter is required"}, status_code=400)

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
        t0 = time.monotonic()
        response = await bus_svc.ask_sync(ask_obj, timeout_ms=timeout_ms)
        duration_ms = int((time.monotonic() - t0) * 1000)

        # Record bus conversation for audit trail (#41)
        if doc_svc and response:
            try:
                # Extract project_id from question text if present
                import re as _re
                _pid_match = _re.search(r"\(project_id:\s*(PRJ-[a-f0-9]{8})\)", question)
                _project_id = _pid_match.group(1) if _pid_match else None
                await doc_svc.record_bus_conversation(
                    project_id=_project_id,
                    ask_id=ask_obj.ask_id,
                    from_agent=agent_id,
                    to_agent=response.from_agent,
                    question_preview=question[:500],
                    answer_preview=(response.knowledge.content or "")[:500],
                    confidence=response.confidence,
                    duration_ms=duration_ms,
                )
            except Exception:
                pass  # Non-fatal

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

    # -- Phase 5: Level-First Retrieval & Synthesis -------------------------

    async def search_level_endpoint(request: Request) -> JSONResponse:
        body = await request.json()
        query = body.get("query", "")
        if not query:
            return JSONResponse({"error": "query is required"}, status_code=400)
        node_types = body.get("node_types")
        domain = body.get("domain")
        limit = int(body.get("limit", 10))
        results = await memory_svc.search_level(
            query, node_types=node_types, domain=domain, limit=limit,
        )
        return JSONResponse([
            {
                "memory_id": sm.memory.id,
                "content": sm.memory.content[:500],
                "score": round(sm.total_activation, 4),
                "node_types": sm.node_types,
                "memory_type": sm.memory.memory_type,
            }
            for sm in results
        ])

    async def traverse_endpoint(request: Request) -> JSONResponse:
        body = await request.json()
        seed_id = body.get("seed_memory_id", "")
        if not seed_id:
            return JSONResponse(
                {"error": "seed_memory_id is required"}, status_code=400,
            )
        mode = body.get("mode", "bottom_up")
        limit = int(body.get("limit", 20))
        result = await memory_svc.traverse(seed_id, mode=mode, limit=limit)
        return JSONResponse({
            "seed_id": result.seed_id,
            "traversal_mode": result.traversal_mode,
            "levels_traversed": result.levels_traversed,
            "result_count": len(result.results),
            "path": result.path[:20],
            "results": [
                {
                    "memory_id": rr.memory.memory.id,
                    "content": rr.memory.memory.content[:300],
                    "retrieval_path": rr.retrieval_path,
                }
                for rr in result.results
            ],
        })

    async def topic_map_endpoint(request: Request) -> JSONResponse:
        clusters = await memory_svc.get_topic_map()
        return JSONResponse([
            {
                "topic_id": c.topic_id,
                "label": c.label,
                "entity_keys": c.entity_keys,
                "abstract_count": len(c.abstract_ids),
                "episode_count": len(c.episode_ids),
                "confidence": round(c.confidence, 3),
            }
            for c in clusters
        ])

    async def synthesize_endpoint(request: Request) -> JSONResponse:
        body = await request.json()
        query = body.get("query", "")
        if not query:
            return JSONResponse({"error": "query is required"}, status_code=400)
        result = await memory_svc.synthesize(
            query=query,
            mode=body.get("mode", "summary"),
            domain=body.get("domain"),
            limit=int(body.get("limit", 10)),
            token_budget=body.get("token_budget"),
            traversal=body.get("traversal"),
            seed_memory_id=body.get("seed_memory_id"),
        )
        return JSONResponse({
            "query": result.query,
            "mode": result.mode,
            "content": result.content,
            "sources": result.sources,
            "source_count": result.source_count,
            "token_budget": result.token_budget,
            "tokens_used": result.tokens_used,
            "traversal": result.traversal,
            "intent": result.intent,
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
    _agent_ports = {
        "archeologist": 8001, "architect": 8002, "security": 8003,
        "product_owner": 8004, "designer": 8005,
    }

    async def agent_chat(request: Request) -> JSONResponse:
        import httpx as _httpx

        agent_id = request.path_params["agent_id"]
        port = _agent_ports.get(agent_id)
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

    # -- Project Store (Phase 2.5 — persistent in SQLite) ---------------------

    async def create_project(request: Request) -> JSONResponse:
        body = await request.json()
        topic = body.get("topic", "")
        if not topic:
            return JSONResponse({"error": "topic is required"}, status_code=400)

        source_type = body.get("source_type", "research")
        repository_url = body.get("repository_url", "")

        if doc_svc:
            project = await doc_svc.create_project(
                topic=topic,
                target=body.get("target", ""),
                source_type=source_type,
                repository_url=repository_url or None,
                scope=body.get("scope", ["research", "prd", "design"]),
            )
            project_id = project.id
        else:
            # Fallback: in-memory (backwards compat if doc_svc not wired)
            project_id = "PRJ-" + uuid.uuid4().hex[:8]

        # Also store as NCMS memory (non-blocking)
        try:
            await memory_svc.store_memory(
                content=f"Project {project_id}: {topic}",
                memory_type="fact",
                domains=body.get("scope", []),
                tags=["project", project_id],
                importance=7.0,
            )
        except Exception as e:
            logger.warning("Failed to store project memory: %s", e)

        if event_log:
            from ncms.infrastructure.observability.event_log import DashboardEvent
            event_log.emit(DashboardEvent(
                type="project.created",
                data={"project_id": project_id, "topic": topic},
            ))

        # Fire-and-forget: trigger the first agent via bus announcement.
        # The SSE listener in each agent detects trigger-{agent_id} domains
        # and self-calls /generate inside the sandbox. No port forward needed.
        # Archaeology → archeologist, Research → researcher (mutually exclusive)
        target = body.get("target", "")
        if source_type == "archaeology" and repository_url:
            trigger_msg = (
                f"Analyze repository: {repository_url}\n"
                f"Goal: {topic}"
                + (f" for {target}" if target else "")
                + f"\n(project_id: {project_id})"
            )
            trigger_domain = "trigger-archeologist"
            phase = "archaeology"
        elif "research" in body.get("scope", []):
            trigger_msg = (
                f"Research {topic}"
                + (f" for {target}" if target else "")
                + f" (project_id: {project_id})"
            )
            trigger_domain = "trigger-archeologist"
            phase = "research"
        else:
            trigger_msg = ""
            trigger_domain = ""
            phase = "pending"

        if trigger_msg and trigger_domain:
            announcement = KnowledgeAnnounce(
                knowledge=KnowledgePayload(content=trigger_msg),
                domains=[trigger_domain],
                from_agent="hub",
                event="created",
            )
            await bus_svc.announce(announcement)
            logger.info(
                "Triggered %s for %s via bus announce",
                trigger_domain, project_id,
            )

        # Update project phase
        if doc_svc:
            await doc_svc.update_project_phase(project_id, phase)

        # Return project data
        if doc_svc:
            proj = await doc_svc.get_project(project_id)
            if proj:
                d = proj.model_dump(mode="json")
                d["project_id"] = d.pop("id", project_id)
                return JSONResponse(d, status_code=201)
        return JSONResponse({"project_id": project_id, "topic": topic}, status_code=201)

    async def list_projects(request: Request) -> JSONResponse:
        status_filter = request.query_params.get("status")
        if doc_svc:
            projects = await doc_svc.list_projects(status=status_filter)
            # Map "id" → "project_id" for dashboard compatibility
            result = []
            for p in projects:
                d = p.model_dump(mode="json")
                d["project_id"] = d.pop("id", d.get("project_id"))
                result.append(d)
            return JSONResponse(result)
        return JSONResponse([])

    async def get_project(request: Request) -> JSONResponse:
        project_id = request.path_params["project_id"]
        if doc_svc:
            summary = await doc_svc.get_project_summary(project_id)
            if "error" in summary:
                return JSONResponse(summary, status_code=404)
            return JSONResponse(summary)
        return JSONResponse({"error": "Project not found"}, status_code=404)

    async def archive_project(request: Request) -> JSONResponse:
        project_id = request.path_params["project_id"]
        if doc_svc:
            await doc_svc.update_project_status(project_id, "archived")
            project = await doc_svc.get_project(project_id)
            if not project:
                return JSONResponse({"error": "Project not found"}, status_code=404)

            if event_log:
                from ncms.infrastructure.observability.event_log import DashboardEvent
                event_log.emit(DashboardEvent(
                    type="project.archived",
                    data={"project_id": project_id},
                ))

            return JSONResponse(project.model_dump(mode="json"))
        return JSONResponse({"error": "Not found"}, status_code=404)

    async def fail_project(request: Request) -> JSONResponse:
        """Mark project as failed (manually or by timeout)."""
        project_id = request.path_params["project_id"]
        if doc_svc:
            await doc_svc.update_project_status(project_id, "failed")
            if event_log:
                from ncms.infrastructure.observability.event_log import DashboardEvent
                event_log.emit(DashboardEvent(
                    type="project.failed",
                    data={"project_id": project_id},
                ))
            return JSONResponse({"status": "failed", "project_id": project_id})
        return JSONResponse({"error": "Not found"}, status_code=404)

    async def complete_project(request: Request) -> JSONResponse:
        """Mark project as completed when designer verify passes."""
        project_id = request.path_params["project_id"]
        if doc_svc:
            body = await request.json()
            quality_score = body.get("quality_score")
            await doc_svc.update_project_status(project_id, "completed")
            if quality_score is not None:
                project = await doc_svc.get_project(project_id)
                if project:
                    project.quality_score = quality_score
                    from datetime import UTC, datetime
                    project.updated_at = datetime.now(UTC)
                    await doc_svc._store.update_project(project)

            if event_log:
                from ncms.infrastructure.observability.event_log import DashboardEvent
                event_log.emit(DashboardEvent(
                    type="project.completed",
                    data={"project_id": project_id, "quality_score": quality_score},
                ))

            return JSONResponse({"status": "completed", "project_id": project_id})
        return JSONResponse({"error": "Not found"}, status_code=404)

    # -- Pipeline Telemetry ----------------------------------------------------

    async def post_pipeline_event(request: Request) -> JSONResponse:
        body = await request.json()
        project_id = body.get("project_id", "")
        if not project_id:
            return JSONResponse({"error": "project_id is required"}, status_code=400)

        detail_raw = body.get("detail", "")

        evt = {
            "project_id": project_id,
            "agent": body.get("agent", ""),
            "node": body.get("node", ""),
            "status": body.get("status", ""),
            "detail": detail_raw,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        # Persist to DB (Phase 2.5)
        if doc_svc:
            await doc_svc.record_pipeline_event(
                project_id=project_id,
                agent=body.get("agent", ""),
                node=body.get("node", ""),
                status=body.get("status", ""),
                detail=detail_raw,
            )

        if event_log:
            from ncms.infrastructure.observability.event_log import DashboardEvent
            event_log.emit(DashboardEvent(
                type="pipeline.node",
                data=evt,
                agent_id=body.get("agent"),
            ))

        return JSONResponse({"stored": True})

    async def get_pipeline_events(request: Request) -> JSONResponse:
        project_id = request.path_params["project_id"]
        if doc_svc:
            events = await doc_svc.get_pipeline_events(project_id)
            return JSONResponse([
                {
                    "project_id": e.project_id,
                    "agent": e.agent,
                    "node": e.node,
                    "status": e.status,
                    "detail": e.detail,
                    "timestamp": (
                        e.timestamp.isoformat()
                        if hasattr(e.timestamp, "isoformat") else str(e.timestamp)
                    ),
                }
                for e in events
            ])
        return JSONResponse([])

    # -- Pipeline Interrupt ----------------------------------------------------

    _interrupts: dict[str, bool] = {}  # agent_id -> interrupted

    async def interrupt_agent(request: Request) -> JSONResponse:
        """POST: signal interrupt. GET: check interrupt status (and clear flag)."""
        agent_id = request.path_params["agent_id"]

        if request.method == "GET":
            interrupted = _interrupts.pop(agent_id, False)
            return JSONResponse({"interrupted": interrupted, "agent_id": agent_id})

        # POST: set interrupt flag
        _interrupts[agent_id] = True
        logger.info("Interrupt signal sent to agent %s", agent_id)

        if event_log:
            from ncms.infrastructure.observability.event_log import DashboardEvent
            event_log.emit(DashboardEvent(
                type="pipeline.interrupt",
                data={"agent_id": agent_id},
                agent_id=agent_id,
            ))

        # Mark the active project as interrupted and emit telemetry
        # so pipeline progress nodes stop showing as "started"
        if doc_svc:
            try:
                # Find the most recent active project for this agent
                projects = await doc_svc.list_projects(status="active")
                for proj in projects:
                    events = await doc_svc.get_pipeline_events(proj.id)
                    agent_events = [e for e in events if e.agent == agent_id]
                    if agent_events:
                        # Mark project as interrupted
                        await doc_svc.update_project_status(proj.id, "interrupted")
                        # Emit interrupted telemetry for any "started" nodes
                        # so the dashboard stops showing them as active
                        started_nodes = set()
                        for e in agent_events:
                            if e.status == "started":
                                started_nodes.add(e.node)
                            elif e.status in ("completed", "interrupted", "denied"):
                                started_nodes.discard(e.node)
                        for node in started_nodes:
                            await doc_svc.record_pipeline_event(
                                proj.id, agent_id, node, "interrupted",
                                detail="Interrupted by human",
                            )
                        logger.info(
                            "Project %s marked interrupted (%d nodes stopped)",
                            proj.id, len(started_nodes),
                        )
                        break  # Only interrupt the first matching project
            except Exception as e:
                logger.warning("Failed to mark project interrupted: %s", e)

        return JSONResponse({"interrupted": True, "agent_id": agent_id})

    # -- Prompt Store ----------------------------------------------------------

    _prompts: dict[str, dict[str, Any]] = {}

    async def store_prompt(request: Request) -> JSONResponse:
        body = await request.json()
        agent_id = body.get("agent_id", "")
        prompt_type = body.get("prompt_type", "")
        content = body.get("content", "")
        if not agent_id or not prompt_type or not content:
            return JSONResponse(
                {"error": "agent_id, prompt_type, and content are required"},
                status_code=400,
            )

        # Find highest existing version for this agent+type combo
        prefix = f"{agent_id}/{prompt_type}/"
        existing_versions = [
            v["version"] for k, v in _prompts.items() if k.startswith(prefix)
        ]
        version = max(existing_versions, default=0) + 1
        key = f"{agent_id}/{prompt_type}/{version}"

        meta = {
            "agent_id": agent_id,
            "prompt_type": prompt_type,
            "version": version,
            "content": content,
            "description": body.get("description", ""),
            "created_at": datetime.now(UTC).isoformat(),
        }
        _prompts[key] = meta
        return JSONResponse(meta, status_code=201)

    async def list_prompts(request: Request) -> JSONResponse:
        agent_filter = request.query_params.get("agent")
        type_filter = request.query_params.get("type")

        results = list(_prompts.values())
        if agent_filter:
            results = [p for p in results if p.get("agent_id") == agent_filter]
        if type_filter:
            results = [p for p in results if p.get("prompt_type") == type_filter]

        results.sort(key=lambda p: p.get("version", 0), reverse=True)
        return JSONResponse(results)

    async def get_latest_prompt(request: Request) -> JSONResponse:
        agent_id = request.path_params["agent_id"]
        prompt_type = request.path_params["prompt_type"]

        prefix = f"{agent_id}/{prompt_type}/"
        matching = [v for k, v in _prompts.items() if k.startswith(prefix)]
        if not matching:
            return JSONResponse({"error": "Prompt not found"}, status_code=404)

        latest = max(matching, key=lambda p: p.get("version", 0))
        return JSONResponse(latest)

    # -- Policy Store ----------------------------------------------------------

    _policies: dict[str, dict[str, Any]] = {}

    async def store_policy(request: Request) -> JSONResponse:
        body = await request.json()
        policy_type = body.get("policy_type", "")
        content = body.get("content", "")
        if not policy_type or not content:
            return JSONResponse(
                {"error": "policy_type and content are required"}, status_code=400,
            )

        existing = _policies.get(policy_type)
        version = (existing["version"] + 1) if existing else 1

        meta = {
            "policy_type": policy_type,
            "version": version,
            "content": content,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        _policies[policy_type] = meta
        return JSONResponse(meta, status_code=201)

    async def list_policies(request: Request) -> JSONResponse:
        return JSONResponse(list(_policies.values()))

    async def get_policy(request: Request) -> JSONResponse:
        policy_type = request.path_params["policy_type"]
        meta = _policies.get(policy_type)
        if not meta:
            return JSONResponse({"error": "Policy not found"}, status_code=404)
        return JSONResponse(meta)

    # -- Document Store (Phase 2.5 — persistent in SQLite) -------------------

    _documents_dir = Path(__file__).parent / "static" / "documents"
    _documents_dir.mkdir(parents=True, exist_ok=True)

    async def store_document(request: Request) -> JSONResponse:
        """Store a document with entity extraction and persist to DB."""
        import re as _re

        body = await request.json()
        title = body.get("title", "Untitled")
        content = body.get("content", "")
        from_agent = body.get("from_agent")

        if not content:
            return JSONResponse({"error": "content is required"}, status_code=400)

        # Extract project_id from content (embedded as HTML comment by agents)
        project_id = body.get("project_id")
        if not project_id:
            prj_match = _re.search(r"project_id:\s*(PRJ-[a-f0-9]{8})", content)
            if prj_match:
                project_id = prj_match.group(1)

        doc_type = body.get("doc_type")
        parent_doc_id = body.get("parent_doc_id")

        if doc_svc:
            # Phase 2.5: persistent document with entity extraction
            client_meta = body.get("metadata") or {}
            doc_metadata = {
                "plan_id": body.get("plan_id"),
                **client_meta,
            }
            logger.info(
                "[api] store_document: from=%s type=%s client_metadata_keys=%s",
                from_agent, doc_type, list(client_meta.keys()),
            )
            doc = await doc_svc.publish_document(
                title=title,
                content=content,
                from_agent=from_agent,
                project_id=project_id,
                doc_type=doc_type,
                parent_doc_id=parent_doc_id,
                metadata=doc_metadata,
            )

            # Also write to filesystem for static serving
            filename = f"{doc.id}.md"
            filepath = _documents_dir / filename
            filepath.write_text(content, encoding="utf-8")

            # Create NCMS memory for document (non-blocking)
            try:
                entity_names = [e["name"] for e in doc.entities[:5]]
                summary = f"Document '{title}' by {from_agent}: {content[:300]}"
                if entity_names:
                    summary += f"\nKey entities: {', '.join(entity_names)}"
                await memory_svc.store_memory(
                    content=summary,
                    memory_type="fact",
                    domains=["documents"],
                    tags=["document", doc.id] + [e["name"].lower() for e in doc.entities[:5]],
                    importance=6.0,
                    source_agent=from_agent,
                    entities=doc.entities,
                )
            except Exception as e:
                logger.warning("Failed to store document memory for %s: %s", doc.id, e)

            # Emit SSE event
            if event_log:
                from ncms.infrastructure.observability.event_log import DashboardEvent
                event_log.emit(DashboardEvent(
                    type="document.published",
                    data={
                        "document_id": doc.id,
                        "title": title,
                        "from_agent": from_agent,
                        "doc_type": doc_type,
                        "content": content[:200],
                    },
                    agent_id=from_agent,
                ))

            return JSONResponse({
                "document_id": doc.id,
                "title": doc.title,
                "from_agent": doc.from_agent,
                "project_id": doc.project_id,
                "doc_type": doc.doc_type,
                "version": doc.version,
                "content_hash": doc.content_hash,
                "format": doc.format,
                "url": f"/documents/{filename}",
                "created_at": doc.created_at.isoformat() if doc.created_at else None,
                "size_bytes": doc.size_bytes,
                "entities": doc.entities,
            }, status_code=201)

        # Fallback: no doc_svc (shouldn't happen in production)
        return JSONResponse({"error": "DocumentService not available"}, status_code=503)

    async def list_documents(request: Request) -> JSONResponse:
        """List all published documents."""
        if doc_svc:
            project_id = request.query_params.get("project_id")
            doc_type = request.query_params.get("doc_type")
            docs = await doc_svc.list_documents(project_id=project_id, doc_type=doc_type)
            return JSONResponse([
                {
                    "document_id": d.id,
                    "id": d.id,
                    "title": d.title,
                    "from_agent": d.from_agent,
                    "project_id": d.project_id,
                    "doc_type": d.doc_type,
                    "version": d.version,
                    "content_hash": d.content_hash,
                    "url": f"/documents/{d.id}.md",
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                    "size_bytes": d.size_bytes,
                    "entity_count": len(d.entities) if d.entities else 0,
                    "metadata": d.metadata if isinstance(d.metadata, dict) else {},
                    "entities": d.entities,
                }
                for d in docs
            ])
        return JSONResponse([])

    async def get_document(request: Request) -> JSONResponse:
        """Return a single document with its full content."""
        doc_id = request.path_params["doc_id"]
        if doc_svc:
            doc = await doc_svc.get_document(doc_id)
            if not doc:
                return JSONResponse({"error": "Document not found"}, status_code=404)
            return JSONResponse({
                "document_id": doc.id,
                "id": doc.id,
                "title": doc.title,
                "from_agent": doc.from_agent,
                "project_id": doc.project_id,
                "doc_type": doc.doc_type,
                "version": doc.version,
                "content_hash": doc.content_hash,
                "url": f"/documents/{doc.id}.md",
                "created_at": doc.created_at.isoformat() if doc.created_at else None,
                "size_bytes": doc.size_bytes,
                "entity_count": len(doc.entities) if doc.entities else 0,
                "entities": doc.entities,
                "metadata": doc.metadata if isinstance(doc.metadata, dict) else {},
                "content": doc.content,
            })
        return JSONResponse({"error": "Document not found"}, status_code=404)

    # -- Document Links & Reviews (Phase 2.5) --------------------------------

    async def create_document_link(request: Request) -> JSONResponse:
        body = await request.json()
        if not doc_svc:
            return JSONResponse({"error": "DocumentService not available"}, status_code=503)
        link = await doc_svc.create_link(
            source_doc_id=body["source_doc_id"],
            target_doc_id=body["target_doc_id"],
            link_type=body["link_type"],
            metadata=body.get("metadata"),
        )
        return JSONResponse({"link_id": link.id, "link_type": link.link_type}, status_code=201)

    async def save_review_score_endpoint(request: Request) -> JSONResponse:
        body = await request.json()
        if not doc_svc:
            return JSONResponse({"error": "DocumentService not available"}, status_code=503)
        review = await doc_svc.save_review_score(
            document_id=body["document_id"],
            project_id=body.get("project_id"),
            reviewer_agent=body["reviewer_agent"],
            review_round=body.get("review_round", 1),
            score=body.get("score"),
            severity=body.get("severity"),
            covered=body.get("covered"),
            missing=body.get("missing"),
            changes=body.get("changes"),
        )
        return JSONResponse({"review_id": review.id, "score": review.score}, status_code=201)

    async def get_document_chain(request: Request) -> JSONResponse:
        doc_id = request.path_params["doc_id"]
        if not doc_svc:
            return JSONResponse([], status_code=200)
        chain = await doc_svc.get_traceability_chain(doc_id)
        return JSONResponse([
            {
                "source_doc_id": lnk.source_doc_id,
                "target_doc_id": lnk.target_doc_id,
                "link_type": lnk.link_type,
                "metadata": lnk.metadata,
            }
            for lnk in chain
        ])

    async def get_document_versions(request: Request) -> JSONResponse:
        doc_id = request.path_params["doc_id"]
        if not doc_svc:
            return JSONResponse([], status_code=200)
        versions = await doc_svc.get_document_versions(doc_id)
        return JSONResponse([
            {
                "document_id": v.id,
                "version": v.version,
                "title": v.title,
                "size_bytes": v.size_bytes,
                "content_hash": v.content_hash,
                "created_at": v.created_at.isoformat() if v.created_at else None,
            }
            for v in versions
        ])

    async def get_document_reviews(request: Request) -> JSONResponse:
        doc_id = request.path_params["doc_id"]
        if not doc_svc:
            return JSONResponse([], status_code=200)
        scores = await doc_svc.get_review_scores(document_id=doc_id)
        return JSONResponse([s.model_dump(mode="json") for s in scores])

    async def search_documents_endpoint(request: Request) -> JSONResponse:
        if not doc_svc:
            return JSONResponse([], status_code=200)
        entity = request.query_params.get("entity")
        doc_type = request.query_params.get("doc_type")
        min_score = request.query_params.get("min_score")
        results = await doc_svc.search_documents(
            entity=entity, doc_type=doc_type,
            min_score=int(min_score) if min_score else None,
        )
        return JSONResponse([
            {
                "document_id": d.id,
                "title": d.title,
                "doc_type": d.doc_type,
                "from_agent": d.from_agent,
                "project_id": d.project_id,
                "entities": d.entities,
                "size_bytes": d.size_bytes,
            }
            for d in results
        ])

    # -- Guardrail Approval Gate endpoints ------------------------------------

    async def create_approval_request(request: Request) -> JSONResponse:
        """Agent creates a pending approval when guardrails flag issues."""
        if not doc_svc:
            return JSONResponse({"error": "doc service unavailable"}, status_code=503)
        body = await request.json()
        approval = await doc_svc.create_approval_request(
            project_id=body.get("project_id"),
            agent=body.get("agent", "unknown"),
            node=body.get("node", "unknown"),
            violations=body.get("violations", []),
            context=body.get("context"),
        )
        # Emit SSE event so dashboard knows immediately
        if event_log:
            event_log.append({
                "type": "approval_requested",
                "approval_id": approval.id,
                "project_id": approval.project_id,
                "agent": approval.agent,
                "node": approval.node,
                "violation_count": len(approval.violations),
            })
        return JSONResponse(approval.model_dump(mode="json"), status_code=201)

    async def list_approvals_endpoint(request: Request) -> JSONResponse:
        """Dashboard polls for pending approvals."""
        if not doc_svc:
            return JSONResponse([], status_code=200)
        status = request.query_params.get("status")
        project_id = request.query_params.get("project_id")
        approvals = await doc_svc.list_pending_approvals(
            status=status, project_id=project_id,
        )
        return JSONResponse([a.model_dump(mode="json") for a in approvals])

    async def get_approval_endpoint(request: Request) -> JSONResponse:
        """Agent polls for decision on a specific approval."""
        if not doc_svc:
            return JSONResponse({"error": "doc service unavailable"}, status_code=503)
        approval_id = request.path_params["approval_id"]
        approval = await doc_svc.get_approval_status(approval_id)
        if not approval:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(approval.model_dump(mode="json"))

    async def decide_approval_endpoint(request: Request) -> JSONResponse:
        """Human submits approve/deny decision from dashboard."""
        if not doc_svc:
            return JSONResponse({"error": "doc service unavailable"}, status_code=503)
        approval_id = request.path_params["approval_id"]
        body = await request.json()
        decision = body.get("decision")  # "approved" or "denied"
        # Identity from JWT token (server-side, not client-forged)
        user = getattr(request.state, "user", None)
        decided_by = user.get("sub", "human") if user else body.get("decided_by", "human")
        comment = body.get("comment")
        if decision not in ("approved", "denied"):
            return JSONResponse(
                {"error": "decision must be 'approved' or 'denied'"},
                status_code=400,
            )
        result = await doc_svc.decide_approval(
            approval_id, decision, decided_by, comment,
        )
        if not result:
            return JSONResponse(
                {"error": "approval not found or already decided"},
                status_code=404,
            )
        # Emit SSE event so dashboard + agents know
        if event_log:
            event_log.append({
                "type": "approval_decided",
                "approval_id": result.id,
                "project_id": result.project_id,
                "decision": decision,
                "decided_by": decided_by,
            })
        return JSONResponse(result.model_dump(mode="json"))

    # -- Audit Record endpoints ------------------------------------------------

    async def record_llm_call_endpoint(request: Request) -> JSONResponse:
        """Agent reports an LLM call for audit trail."""
        if not doc_svc:
            return JSONResponse({"ok": True})
        body = await request.json()
        await doc_svc.record_llm_call(
            project_id=body.get("project_id"),
            agent=body.get("agent", "unknown"),
            node=body.get("node", "unknown"),
            prompt_size=body.get("prompt_size"),
            response_size=body.get("response_size"),
            reasoning_size=body.get("reasoning_size", 0),
            model=body.get("model"),
            thinking_enabled=body.get("thinking_enabled", False),
            duration_ms=body.get("duration_ms"),
            trace_id=body.get("trace_id"),
            prompt_hash=body.get("prompt_hash"),
        )
        return JSONResponse({"ok": True}, status_code=201)

    async def record_config_snapshot_endpoint(request: Request) -> JSONResponse:
        """Agent reports its config at pipeline start."""
        if not doc_svc:
            return JSONResponse({"ok": True})
        body = await request.json()
        await doc_svc.record_config_snapshot(
            project_id=body.get("project_id"),
            agent=body.get("agent", "unknown"),
            config_hash=body.get("config_hash"),
            prompt_version=body.get("prompt_version"),
            model_name=body.get("model_name"),
            thinking_enabled=body.get("thinking_enabled", False),
            max_tokens=body.get("max_tokens"),
        )
        return JSONResponse({"ok": True}, status_code=201)

    async def record_grounding_endpoint(request: Request) -> JSONResponse:
        """Agent reports memory grounding for a review citation."""
        if not doc_svc:
            return JSONResponse({"ok": True})
        body = await request.json()
        await doc_svc.record_grounding(
            document_id=body.get("document_id", ""),
            memory_id=body.get("memory_id", ""),
            retrieval_score=body.get("retrieval_score"),
            entity_query=body.get("entity_query"),
            domain=body.get("domain"),
            review_score_id=body.get("review_score_id"),
        )
        return JSONResponse({"ok": True}, status_code=201)

    async def record_guardrail_violation_endpoint(request: Request) -> JSONResponse:
        """Agent reports a guardrail violation for the audit trail."""
        if not doc_svc:
            return JSONResponse({"ok": True})
        body = await request.json()
        await doc_svc.record_guardrail_violation(
            document_id=body.get("document_id"),
            project_id=body.get("project_id"),
            policy_type=body.get("policy_type", "unknown"),
            rule=body.get("rule", "unknown"),
            message=body.get("message", ""),
            escalation=body.get("escalation", "warn"),
        )
        return JSONResponse({"ok": True}, status_code=201)

    # -- Audit & Provenance endpoints -----------------------------------------

    async def audit_timeline_endpoint(request: Request) -> JSONResponse:
        """Unified chronological audit timeline for a project."""
        if not doc_svc:
            return JSONResponse([], status_code=200)
        project_id = request.path_params["project_id"]
        timeline = await doc_svc.get_audit_timeline(project_id)
        return JSONResponse(timeline)

    async def verify_integrity_endpoint(request: Request) -> JSONResponse:
        """Verify hash chain integrity for all audit tables in a project."""
        if not doc_svc:
            return JSONResponse({"error": "unavailable"}, status_code=503)
        project_id = request.path_params["project_id"]
        result = await doc_svc.verify_project_integrity(project_id)
        return JSONResponse(result)

    async def document_provenance_endpoint(request: Request) -> JSONResponse:
        """Complete provenance chain for a single document."""
        if not doc_svc:
            return JSONResponse({"error": "unavailable"}, status_code=503)
        doc_id = request.path_params["doc_id"]
        result = await doc_svc.get_document_provenance(doc_id)
        if "error" in result:
            return JSONResponse(result, status_code=404)
        return JSONResponse(result)

    async def verify_document_endpoint(request: Request) -> JSONResponse:
        """Verify content hash integrity for a single document."""
        if not doc_svc:
            return JSONResponse({"error": "unavailable"}, status_code=503)
        doc_id = request.path_params["doc_id"]
        result = await doc_svc.verify_document_integrity(doc_id)
        return JSONResponse(result)

    async def export_audit_report_endpoint(request: Request):
        """Export complete audit report as downloadable markdown."""
        if not doc_svc:
            return JSONResponse({"error": "unavailable"}, status_code=503)
        project_id = request.path_params["project_id"]
        report = await doc_svc.export_audit_report(project_id)
        from starlette.responses import Response
        return Response(
            content=report,
            media_type="text/markdown",
            headers={"Content-Disposition": f"attachment; filename=audit-report-{project_id}.md"},
        )

    async def compliance_score_endpoint(request: Request) -> JSONResponse:
        """Compute composite compliance score for a project."""
        if not doc_svc:
            return JSONResponse({"error": "unavailable"}, status_code=503)
        project_id = request.path_params["project_id"]
        result = await doc_svc.compute_compliance_score(project_id)
        return JSONResponse(result)

    # -- Maintenance ---------------------------------------------------------

    async def maintenance_status(request: Request) -> JSONResponse:
        """Return maintenance scheduler status."""
        if maintenance_scheduler is None:
            return JSONResponse(
                {"error": "maintenance scheduler not configured"},
                status_code=404,
            )
        try:
            return JSONResponse(maintenance_scheduler.status())
        except Exception:
            logger.error("Failed to get maintenance status", exc_info=True)
            return JSONResponse(
                {"error": "failed to retrieve status"}, status_code=500,
            )

    async def maintenance_run(request: Request) -> JSONResponse:
        """Trigger a maintenance task by name."""
        if maintenance_scheduler is None:
            return JSONResponse(
                {"error": "maintenance scheduler not configured"},
                status_code=503,
            )
        body = await request.json()
        task_name = body.get("task")
        if not task_name:
            return JSONResponse(
                {"error": "task is required"}, status_code=400,
            )
        try:
            await maintenance_scheduler.run_task(task_name)
            return JSONResponse({"status": "started", "task": task_name})
        except Exception as exc:
            logger.error(
                "Failed to trigger maintenance task %s", task_name,
                exc_info=True,
            )
            return JSONResponse(
                {"error": str(exc)}, status_code=500,
            )

    # -- Phase 6: Export & Feedback -------------------------------------------

    async def search_feedback_endpoint(request: Request) -> JSONResponse:
        """Record implicit feedback: which search result was actually used."""
        body = await request.json()
        query = body.get("query", "")
        selected_memory_id = body.get("selected_memory_id", "")
        if not query or not selected_memory_id:
            return JSONResponse(
                {"error": "query and selected_memory_id are required"},
                status_code=400,
            )
        await memory_svc.record_search_feedback(
            query=query,
            selected_memory_id=selected_memory_id,
            result_ids=body.get("result_ids"),
            agent_id=body.get("agent_id"),
        )
        result_ids = body.get("result_ids") or []
        position = (
            result_ids.index(selected_memory_id) + 1
            if selected_memory_id in result_ids
            else None
        )
        return JSONResponse({
            "recorded": True,
            "selected_memory_id": selected_memory_id,
            "position": position,
        })

    async def heartbeat_endpoint(request: Request) -> JSONResponse:
        """Record a heartbeat from an agent."""
        body = await request.json()
        agent_id = body.get("agent_id", "")
        if not agent_id:
            return JSONResponse({"error": "agent_id is required"}, status_code=400)
        await bus_svc.heartbeat(agent_id)
        online = bus_svc.is_agent_online(agent_id)
        return JSONResponse({
            "agent_id": agent_id,
            "status": "online" if online else "offline",
            "heartbeat_received": True,
        })

    async def scale_flags_endpoint(request: Request) -> JSONResponse:
        """Check scale-aware feature flags."""
        return JSONResponse(memory_svc.check_scale_flags())

    async def export_wiki_endpoint(request: Request) -> JSONResponse:
        """Export memory store as a linked markdown wiki to a temp directory."""
        import tempfile

        from ncms.interfaces.cli.export import export_wiki

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "wiki"
            output_path.mkdir()
            counts = await export_wiki(memory_svc._config, output_path)
            # Read all generated files into response
            pages: dict[str, str] = {}
            for md_file in sorted(output_path.rglob("*.md")):
                rel = str(md_file.relative_to(output_path))
                pages[rel] = md_file.read_text(encoding="utf-8")

        return JSONResponse({
            "counts": counts,
            "total_pages": sum(counts.values()),
            "pages": pages,
        })

    async def bulk_import_endpoint(request: Request) -> JSONResponse:
        """Bulk import knowledge from a directory path on the server.

        POST /api/v1/knowledge/bulk-import
        Body: {"path": "/path/to/knowledge", "domains": [...], "project": "..."}
        """
        from ncms.application.knowledge_loader import KnowledgeLoader

        body = await request.json()
        dir_path = body.get("path", "")
        domains = body.get("domains")
        project = body.get("project")

        if not dir_path:
            return JSONResponse({"error": "path is required"}, status_code=400)

        p = Path(dir_path)
        if not p.is_dir():
            return JSONResponse({"error": f"Not a directory: {dir_path}"}, status_code=400)

        loader = KnowledgeLoader(memory_svc)
        stats = await loader.bulk_load_directory(p, domains=domains, project=project)
        return JSONResponse({
            "files_processed": stats.files_processed,
            "memories_created": stats.memories_created,
            "chunks_total": stats.chunks_total,
            "errors": stats.errors,
        })

    # -- Routes --------------------------------------------------------------

    routes = [
        # Health
        Route("/api/v1/health", health, methods=["GET"]),

        # Auth
        Route("/api/v1/auth/login", login, methods=["POST"]),
        Route("/api/v1/auth/me", auth_me, methods=["GET"]),

        # Memory operations
        Route("/api/v1/memories", store_memory, methods=["POST"]),
        Route("/api/v1/memories/search", search_memory, methods=["GET", "POST"]),
        Route("/api/v1/memories/recall", recall_memory, methods=["GET", "POST"]),
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

        # Phase 5: Level-first retrieval & synthesis
        Route("/api/v1/memory/search-level", search_level_endpoint, methods=["POST"]),
        Route("/api/v1/memory/traverse", traverse_endpoint, methods=["POST"]),
        Route("/api/v1/memory/synthesize", synthesize_endpoint, methods=["POST"]),
        Route("/api/v1/topics", topic_map_endpoint, methods=["GET"]),

        # Phase 6: Export & Feedback
        Route("/api/v1/feedback", search_feedback_endpoint, methods=["POST"]),
        Route("/api/v1/heartbeat", heartbeat_endpoint, methods=["POST"]),
        Route("/api/v1/scale-flags", scale_flags_endpoint, methods=["GET"]),
        Route("/api/v1/export/wiki", export_wiki_endpoint, methods=["POST"]),
        Route("/api/v1/knowledge/bulk-import", bulk_import_endpoint, methods=["POST"]),

        # Consolidation
        Route("/api/v1/consolidation/run", run_consolidation_endpoint, methods=["POST"]),

        # Maintenance
        Route("/api/v1/maintenance/status", maintenance_status, methods=["GET"]),
        Route("/api/v1/maintenance/run", maintenance_run, methods=["POST"]),

        # Documents (Phase 2.5)
        Route("/api/v1/documents", store_document, methods=["POST"]),
        Route("/api/v1/documents", list_documents, methods=["GET"]),
        Route("/api/v1/documents/search", search_documents_endpoint, methods=["GET"]),
        Route("/api/v1/documents/links", create_document_link, methods=["POST"]),
        Route("/api/v1/documents/{doc_id}", get_document, methods=["GET"]),
        Route("/api/v1/documents/{doc_id}/chain", get_document_chain, methods=["GET"]),
        Route("/api/v1/documents/{doc_id}/versions", get_document_versions, methods=["GET"]),
        Route("/api/v1/documents/{doc_id}/reviews", get_document_reviews, methods=["GET"]),

        # Reviews (Phase 2.5)
        Route("/api/v1/reviews", save_review_score_endpoint, methods=["POST"]),

        # Projects
        Route("/api/v1/projects", create_project, methods=["POST"]),
        Route("/api/v1/projects", list_projects, methods=["GET"]),
        Route("/api/v1/projects/{project_id}", get_project, methods=["GET"]),
        Route("/api/v1/projects/{project_id}/archive", archive_project, methods=["POST"]),
        Route("/api/v1/projects/{project_id}/fail", fail_project, methods=["POST"]),
        Route("/api/v1/projects/{project_id}/complete", complete_project, methods=["POST"]),

        # Pipeline telemetry + control
        Route("/api/v1/pipeline/events", post_pipeline_event, methods=["POST"]),
        Route("/api/v1/pipeline/events/{project_id}", get_pipeline_events, methods=["GET"]),
        Route("/api/v1/pipeline/interrupt/{agent_id}", interrupt_agent, methods=["GET", "POST"]),

        # Prompts
        Route("/api/v1/prompts", store_prompt, methods=["POST"]),
        Route("/api/v1/prompts", list_prompts, methods=["GET"]),
        Route(
            "/api/v1/prompts/{agent_id}/{prompt_type}/latest",
            get_latest_prompt,
            methods=["GET"],
        ),

        # Policies
        Route("/api/v1/policies", store_policy, methods=["POST"]),
        Route("/api/v1/policies", list_policies, methods=["GET"]),
        Route("/api/v1/policies/{policy_type}", get_policy, methods=["GET"]),

        # Guardrail Approval Gates
        Route("/api/v1/approvals", create_approval_request, methods=["POST"]),
        Route("/api/v1/approvals", list_approvals_endpoint, methods=["GET"]),
        Route("/api/v1/approvals/{approval_id}", get_approval_endpoint, methods=["GET"]),
        Route("/api/v1/approvals/{approval_id}/decide", decide_approval_endpoint, methods=["POST"]),

        # Audit Records (agents report LLM calls, config, grounding, violations)
        Route("/api/v1/audit/llm-call", record_llm_call_endpoint, methods=["POST"]),
        Route("/api/v1/audit/config-snapshot", record_config_snapshot_endpoint, methods=["POST"]),
        Route("/api/v1/audit/grounding", record_grounding_endpoint, methods=["POST"]),
        Route(
            "/api/v1/audit/guardrail-violation",
            record_guardrail_violation_endpoint, methods=["POST"],
        ),

        # Audit & Provenance
        Route(
            "/api/v1/projects/{project_id}/audit-timeline",
            audit_timeline_endpoint, methods=["GET"],
        ),
        Route(
            "/api/v1/projects/{project_id}/verify-integrity",
            verify_integrity_endpoint, methods=["GET"],
        ),
        Route(
            "/api/v1/projects/{project_id}/compliance",
            compliance_score_endpoint, methods=["GET"],
        ),
        Route(
            "/api/v1/projects/{project_id}/export",
            export_audit_report_endpoint, methods=["GET"],
        ),
        Route(
            "/api/v1/documents/{doc_id}/provenance",
            document_provenance_endpoint, methods=["GET"],
        ),
        Route("/api/v1/documents/{doc_id}/verify", verify_document_endpoint, methods=["GET"]),
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
    memory_svc, bus_svc, snapshot_svc, consolidation_svc, _scheduler = (
        await create_ncms_services(config, event_log=event_log)
    )

    # Create DocumentService (Phase 2.5) using same DB connection
    from ncms.application.document_service import DocumentService
    from ncms.infrastructure.storage.document_store import SQLiteDocumentStore

    doc_store = SQLiteDocumentStore(memory_svc._store.db)
    doc_svc = DocumentService(store=doc_store, memory_svc=memory_svc)
    logger.info("DocumentService initialized (Phase 2.5)")

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
        doc_svc=doc_svc,
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
