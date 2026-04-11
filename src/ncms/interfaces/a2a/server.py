"""A2A Protocol server for NCMS.

Bridges the NCMS Knowledge Bus to the Agent-to-Agent Protocol
(A2A, HTTP JSON-RPC 2.0), enabling NCMS agents to communicate
with any A2A-compatible agent (LangChain, CrewAI, Google ADK).

Spec: https://a2a-protocol.org/latest/specification/

Mount on the HTTP API:
    from ncms.interfaces.a2a.server import create_a2a_routes
    routes = create_a2a_routes(memory_svc, bus_svc, snapshot_svc)
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, cast

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ncms.application.bus_service import BusService
from ncms.application.memory_service import MemoryService
from ncms.application.snapshot_service import SnapshotService
from ncms.domain.models import (
    KnowledgeAnnounce,
    KnowledgeAsk,
    KnowledgePayload,
)

logger = logging.getLogger(__name__)

# A2A Agent Card
AGENT_CARD = {
    "name": "ncms-memory-hub",
    "version": "1.0.0",
    "description": (
        "NeMo Cognitive Memory System — persistent cognitive memory for AI agents "
        "with hybrid retrieval, knowledge graph, and structured recall"
    ),
    "skills": [
        {
            "name": "memory_store",
            "description": "Store knowledge with entity extraction and episode linking",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Content to store"},
                    "type": {"type": "string", "default": "fact"},
                    "domains": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["content"],
            },
        },
        {
            "name": "memory_recall",
            "description": (
                "Search memory with structured context enrichment "
                "(episodes, entity states, causal chains)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "default": 10},
                    "domain": {"type": "string"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "memory_search",
            "description": "Flat hybrid search (BM25 + SPLADE + Graph)",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                    "domain": {"type": "string"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "knowledge_ask",
            "description": "Route questions to domain experts or their surrogates",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "domains": {"type": "array", "items": {"type": "string"}},
                    "timeout_ms": {"type": "integer", "default": 5000},
                },
                "required": ["question"],
            },
        },
        {
            "name": "knowledge_announce",
            "description": "Broadcast observations to subscribed agents",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "domains": {"type": "array", "items": {"type": "string"}},
                    "event": {"type": "string", "default": "updated"},
                },
                "required": ["content", "domains"],
            },
        },
    ],
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
    },
}


def _jsonrpc_error(id: Any, code: int, message: str) -> dict:
    """Create a JSON-RPC 2.0 error response."""
    return {
        "jsonrpc": "2.0",
        "id": id,
        "error": {"code": code, "message": message},
    }


def _jsonrpc_result(id: Any, result: Any) -> dict:
    """Create a JSON-RPC 2.0 success response."""
    return {
        "jsonrpc": "2.0",
        "id": id,
        "result": result,
    }


def create_a2a_routes(
    memory_svc: MemoryService,
    bus_svc: BusService,
    snapshot_svc: SnapshotService,
) -> list[Route]:
    """Create Starlette routes for the A2A protocol endpoint."""

    # In-memory task store (completed tasks with results)
    _tasks: dict[str, dict] = {}

    async def _execute_skill(skill_name: str, params: dict, from_agent: str) -> dict:
        """Execute an NCMS skill and return the result."""
        if skill_name == "memory_store":
            memory = await memory_svc.store_memory(
                content=params["content"],
                memory_type=params.get("type", "fact"),
                domains=params.get("domains"),
                tags=params.get("tags"),
                importance=params.get("importance", 5.0),
                source_agent=from_agent,
            )
            return {
                "memory_id": memory.id,
                "content": memory.content[:200],
                "domains": memory.domains,
            }

        elif skill_name == "memory_recall":
            results = await memory_svc.recall(
                query=params["query"],
                domain=params.get("domain"),
                limit=params.get("limit", 10),
            )
            return {
                "results": [
                    {
                        "memory_id": r.memory.memory.id,
                        "content": r.memory.memory.content[:500],
                        "score": r.memory.total_activation,
                        "retrieval_path": r.retrieval_path,
                    }
                    for r in results
                ],
                "count": len(results),
            }

        elif skill_name == "memory_search":
            search_results = await memory_svc.search(
                query=params["query"],
                domain=params.get("domain"),
                limit=params.get("limit", 10),
            )
            return {
                "results": [
                    {
                        "memory_id": r.memory.id,
                        "content": r.memory.content[:500],
                        "score": r.total_activation,
                    }
                    for r in search_results
                ],
                "count": len(search_results),
            }

        elif skill_name == "knowledge_ask":
            ask_obj = KnowledgeAsk(
                question=params["question"],
                domains=params.get("domains", []),
                from_agent=from_agent,
            )
            response = await bus_svc.ask_sync(
                ask_obj, timeout_ms=params.get("timeout_ms", 5000),
            )
            if response is None:
                return {"answered": False}
            return {
                "answered": True,
                "content": response.knowledge.content,
                "from_agent": response.from_agent,
                "source_mode": response.source_mode,
                "confidence": response.confidence,
            }

        elif skill_name == "knowledge_announce":
            announcement = KnowledgeAnnounce(
                knowledge=KnowledgePayload(content=params["content"]),
                domains=params["domains"],
                from_agent=from_agent,
                event=params.get("event", "updated"),
            )
            await bus_svc.announce(announcement)
            return {"announced": True, "domains": params["domains"]}

        else:
            raise ValueError(f"Unknown skill: {skill_name}")

    async def a2a_endpoint(request: Request) -> JSONResponse:
        """Handle A2A JSON-RPC 2.0 requests."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                _jsonrpc_error(None, -32700, "Parse error"),
            )

        jsonrpc = body.get("jsonrpc")
        if jsonrpc != "2.0":
            return JSONResponse(
                _jsonrpc_error(body.get("id"), -32600, "Invalid JSON-RPC version"),
            )

        method = body.get("method", "")
        params = body.get("params", {})
        req_id = body.get("id")
        from_agent = request.headers.get("X-Agent-ID", "a2a-client")

        # Agent Card discovery
        if method == "agent/card":
            return JSONResponse(_jsonrpc_result(req_id, AGENT_CARD))

        # Task execution
        elif method == "tasks/send":
            skill_name = params.get("skill", "")
            skill_params = params.get("parameters", {})
            task_id = params.get("id", str(uuid.uuid4()))

            valid_skills = {s["name"] for s in cast(list[dict[str, Any]], AGENT_CARD["skills"])}
            if skill_name not in valid_skills:
                return JSONResponse(
                    _jsonrpc_error(req_id, -32602, f"Unknown skill: {skill_name}"),
                )

            try:
                result = await _execute_skill(skill_name, skill_params, from_agent)
                task = {
                    "id": task_id,
                    "status": "completed",
                    "skill": skill_name,
                    "result": result,
                }
                _tasks[task_id] = task
                return JSONResponse(_jsonrpc_result(req_id, task))
            except Exception as e:
                logger.exception("A2A skill execution failed: %s", skill_name)
                return JSONResponse(
                    _jsonrpc_error(req_id, -32000, f"Skill execution failed: {e}"),
                )

        # Task status
        elif method == "tasks/get":
            task_id = params.get("id", "")
            existing_task: dict[str, Any] | None = _tasks.get(task_id)
            if existing_task is None:
                return JSONResponse(
                    _jsonrpc_error(req_id, -32602, f"Task not found: {task_id}"),
                )
            return JSONResponse(_jsonrpc_result(req_id, existing_task))

        else:
            return JSONResponse(
                _jsonrpc_error(req_id, -32601, f"Method not found: {method}"),
            )

    async def agent_card_get(request: Request) -> JSONResponse:
        """GET endpoint for agent card discovery (convenience)."""
        return JSONResponse(AGENT_CARD)

    return [
        Route("/a2a", a2a_endpoint, methods=["POST"]),
        Route("/a2a/agent-card", agent_card_get, methods=["GET"]),
        Route("/.well-known/a2a/agent-card", agent_card_get, methods=["GET"]),
    ]
