"""MCP tool implementations for NCMS.

Each tool is a function that gets registered with the FastMCP server.
Tools provide the primary interface for external agents (Claude, Copilot, etc.)
to interact with the cognitive memory system.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ncms.application.bus_service import BusService
from ncms.application.memory_service import MemoryService
from ncms.application.snapshot_service import SnapshotService
from ncms.domain.models import (
    ImpactAssessment,
    KnowledgeAnnounce,
    KnowledgeAsk,
    KnowledgePayload,
)


def register_tools(
    mcp: FastMCP,
    memory_svc: MemoryService,
    bus_svc: BusService,
    snapshot_svc: SnapshotService,
) -> None:
    """Register all NCMS MCP tools on the given server."""

    @mcp.tool()
    async def search_memory(
        query: str,
        domain: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search cognitive memory with BM25 + ACT-R scoring pipeline.

        Args:
            query: Natural language search query.
            domain: Optional domain filter (e.g., "api", "frontend").
            limit: Maximum results to return.

        Returns:
            List of scored memories with activation components.
        """
        results = await memory_svc.search(query, domain=domain, limit=limit)
        return [
            {
                "memory_id": r.memory.id,
                "content": r.memory.content,
                "type": r.memory.type,
                "domains": r.memory.domains,
                "tags": r.memory.tags,
                "bm25_score": round(r.bm25_score, 4),
                "base_level_activation": round(r.base_level, 4),
                "spreading_activation": round(r.spreading, 4),
                "total_activation": round(r.total_activation, 4),
                "source_agent": r.memory.source_agent,
                "created_at": r.memory.created_at.isoformat(),
            }
            for r in results
        ]

    @mcp.tool()
    async def store_memory(
        content: str,
        type: str = "fact",
        domains: list[str] | None = None,
        tags: list[str] | None = None,
        project: str | None = None,
        structured: dict[str, Any] | None = None,
        importance: float = 5.0,
    ) -> dict[str, Any]:
        """Store a new memory with automatic entity extraction and indexing.

        Args:
            content: The knowledge content to store.
            type: Memory type (fact, interface-spec, code-pattern, etc.).
            domains: Knowledge domains this memory belongs to.
            tags: Free-form tags for categorization.
            project: Project/repo context.
            structured: Optional structured data (OpenAPI spec, JSON schema).
            importance: Importance score (1-10).

        Returns:
            The stored memory with its generated ID.
        """
        memory = await memory_svc.store_memory(
            content=content,
            memory_type=type,
            domains=domains,
            tags=tags,
            project=project,
            structured=structured,
            importance=importance,
        )
        return {
            "memory_id": memory.id,
            "content": memory.content,
            "domains": memory.domains,
            "created_at": memory.created_at.isoformat(),
        }

    @mcp.tool()
    async def ask_knowledge(
        question: str,
        domains: list[str] | None = None,
        from_agent: str = "mcp-client",
    ) -> dict[str, Any]:
        """Non-blocking ask routed to live agents or surrogate snapshots.

        Args:
            question: Natural language question.
            domains: Domain routing hints.
            from_agent: Identifier for the asking agent.

        Returns:
            The ask_id for tracking the response.
        """
        ask = KnowledgeAsk(
            from_agent=from_agent,
            question=question,
            domains=domains or [],
        )
        ask_id = await bus_svc.ask(ask)
        return {"ask_id": ask_id, "status": "routed"}

    @mcp.tool()
    async def ask_knowledge_sync(
        question: str,
        domains: list[str] | None = None,
        from_agent: str = "mcp-client",
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        """Blocking ask that waits for a response (with timeout).

        Tries live agents first, falls back to surrogate snapshot responses.

        Args:
            question: Natural language question.
            domains: Domain routing hints.
            from_agent: Identifier for the asking agent.
            timeout_ms: Maximum wait time in milliseconds.

        Returns:
            The knowledge response or null if no answer available.
        """
        ask = KnowledgeAsk(
            from_agent=from_agent,
            question=question,
            domains=domains or [],
            ttl_ms=timeout_ms,
        )
        response = await bus_svc.ask_sync(ask, timeout_ms=timeout_ms)

        if response:
            return {
                "answered": True,
                "content": response.knowledge.content,
                "structured": response.knowledge.structured,
                "confidence": response.confidence,
                "source_mode": response.source_mode,
                "from_agent": response.from_agent,
                "staleness_warning": response.staleness_warning,
                "provenance": {
                    "source": response.provenance.source,
                    "trust_level": response.provenance.trust_level,
                },
            }
        return {"answered": False, "reason": "No live agents or snapshots for these domains"}

    @mcp.tool()
    async def announce_knowledge(
        content: str,
        domains: list[str],
        event: str = "updated",
        from_agent: str = "mcp-client",
        breaking: bool = False,
        severity: str = "info",
    ) -> dict[str, str]:
        """Broadcast a knowledge update to all subscribed agents.

        Args:
            content: Description of the knowledge update.
            domains: Affected knowledge domains.
            event: Event type (created, updated, deprecated, breaking-change).
            from_agent: Identifier for the announcing agent.
            breaking: Whether this is a breaking change.
            severity: Impact severity (info, warning, critical).

        Returns:
            The announcement ID.
        """
        announcement = KnowledgeAnnounce(
            from_agent=from_agent,
            event=event,  # type: ignore[arg-type]
            domains=domains,
            knowledge=KnowledgePayload(type="fact", content=content),
            impact=ImpactAssessment(
                breaking_change=breaking,
                affected_domains=domains,
                severity=severity,  # type: ignore[arg-type]
                description=content,
            ),
        )
        await bus_svc.announce(announcement)
        return {"announce_id": announcement.announce_id, "status": "broadcast"}

    @mcp.tool()
    async def commit_knowledge(
        content: str,
        domains: list[str] | None = None,
        type: str = "fact",
        structured: dict[str, Any] | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Store knowledge learned during a coding session.

        Call this when you've completed a task, made an architecture decision,
        discovered a pattern, changed an interface, or learned something useful.

        Args:
            content: What was learned or changed.
            domains: Knowledge domains.
            type: Knowledge type.
            structured: Optional structured data (OpenAPI spec, JSON schema).
            project: Project/repo context.
            tags: Free-form tags.
            session_id: Links to a specific coding session.

        Returns:
            The stored memory with entity count.
        """
        memory = await memory_svc.store_memory(
            content=content,
            memory_type=type,
            domains=domains,
            tags=(tags or []) + ([f"session:{session_id}"] if session_id else []),
            project=project,
            structured=structured,
            importance=6.0,
        )
        return {
            "memory_id": memory.id,
            "domains_detected": memory.domains,
            "stored": True,
        }

    @mcp.tool()
    async def get_provenance(memory_id: str) -> dict[str, Any]:
        """Trace the origin and modification history of a memory.

        Args:
            memory_id: The memory ID to trace.

        Returns:
            Provenance chain including source agent, timestamps, and access history.
        """
        memory = await memory_svc.get_memory(memory_id)
        if not memory:
            return {"error": f"Memory not found: {memory_id}"}

        access_ages = await memory_svc.store.get_access_times(memory_id)
        entities = await memory_svc.store.get_memory_entities(memory_id)

        return {
            "memory_id": memory.id,
            "content": memory.content,
            "source_agent": memory.source_agent,
            "created_at": memory.created_at.isoformat(),
            "updated_at": memory.updated_at.isoformat(),
            "access_count": len(access_ages),
            "linked_entities": entities,
            "domains": memory.domains,
            "importance": memory.importance,
        }

    @mcp.tool()
    async def list_domains() -> dict[str, Any]:
        """List all registered knowledge domains with their providers.

        Returns:
            Domains with provider agent IDs and online/offline status.
        """
        domain_map = bus_svc.list_domains()
        agents = bus_svc.get_all_agents()
        agent_status = {a.agent_id: a.status for a in agents}

        result: dict[str, list[dict[str, str]]] = {}
        for domain, agent_ids in domain_map.items():
            result[domain] = [
                {"agent_id": aid, "status": agent_status.get(aid, "unknown")}
                for aid in agent_ids
            ]
        return {"domains": result, "total": len(domain_map)}

    @mcp.tool()
    async def get_snapshot(agent_id: str) -> dict[str, Any]:
        """Retrieve the latest Knowledge Snapshot for an agent.

        Args:
            agent_id: The agent whose snapshot to retrieve.

        Returns:
            The snapshot entries and metadata, or null if none exists.
        """
        snapshot = await snapshot_svc.get_snapshot(agent_id)
        if not snapshot:
            return {"exists": False, "agent_id": agent_id}

        return {
            "exists": True,
            "snapshot_id": snapshot.snapshot_id,
            "agent_id": snapshot.agent_id,
            "timestamp": snapshot.timestamp.isoformat(),
            "domains": snapshot.domains,
            "entry_count": len(snapshot.entries),
            "entries": [
                {
                    "domain": e.domain,
                    "content": e.knowledge.content,
                    "confidence": e.confidence,
                    "volatility": e.volatility,
                }
                for e in snapshot.entries
            ],
            "ttl_hours": snapshot.ttl_hours,
        }

    @mcp.tool()
    async def load_knowledge(
        file_path: str,
        domains: list[str] | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Load knowledge from a file into NCMS memory (Matrix-style download).

        Automatically chunks content by headings (markdown), paragraphs (text),
        or entries (JSON/CSV) for precise retrieval.

        Supports: .md, .txt, .json, .yaml, .csv, .html, .rst

        Args:
            file_path: Path to the file to import.
            domains: Knowledge domains for the imported content.
            project: Project context.

        Returns:
            Import statistics: files processed, memories created.
        """
        from ncms.application.knowledge_loader import KnowledgeLoader

        loader = KnowledgeLoader(memory_svc)
        stats = await loader.load_file(
            file_path,
            domains=domains,
            project=project,
        )
        return {
            "files_processed": stats.files_processed,
            "memories_created": stats.memories_created,
            "chunks_total": stats.chunks_total,
            "errors": stats.errors,
        }
