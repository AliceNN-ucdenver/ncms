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
    consolidation_svc: object | None = None,
) -> None:
    """Register all NCMS MCP tools on the given server."""

    @mcp.tool()
    async def search_memory(
        query: str,
        domain: str | None = None,
        limit: int = 10,
        intent: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search cognitive memory with BM25 + ACT-R scoring pipeline.

        Args:
            query: Natural language search query.
            domain: Optional domain filter (e.g., "api", "frontend").
            limit: Maximum results to return.
            intent: Optional intent override (fact_lookup, current_state_lookup,
                historical_lookup, event_reconstruction, change_detection,
                pattern_lookup, strategic_reflection). Bypasses auto-classification.

        Returns:
            List of scored memories with activation components.
        """
        results = await memory_svc.search(
            query, domain=domain, limit=limit, intent_override=intent,
        )
        return [
            {
                "memory_id": r.memory.id,
                "content": r.memory.content,
                "type": r.memory.type,
                "domains": r.memory.domains,
                "tags": r.memory.tags,
                "bm25_score": round(r.bm25_score, 4),
                "splade_score": round(r.splade_score, 4),
                "base_level_activation": round(r.base_level, 4),
                "spreading_activation": round(r.spreading, 4),
                "total_activation": round(r.total_activation, 4),
                "is_superseded": r.is_superseded,
                "has_conflicts": r.has_conflicts,
                "superseded_by": r.superseded_by,
                "node_types": r.node_types,
                "intent": r.intent,
                "hierarchy_bonus": round(r.hierarchy_bonus, 4),
                "source_agent": r.memory.source_agent,
                "created_at": r.memory.created_at.isoformat(),
            }
            for r in results
        ]

    @mcp.tool()
    async def recall_memory(
        query: str,
        domain: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Recall memory with full context enrichment.

        Unlike search_memory which returns flat ranked results, recall_memory
        routes queries to specialized retrieval paths based on intent and
        enriches each result with episode context, entity states, and causal
        chain edges. One call returns the complete picture.

        Context includes:
        - Episode membership (which episode, summary, sibling memories)
        - Entity states (current state of all mentioned entities)
        - Causal chain (supersedes, superseded_by, derived_from, conflicts)
        - Retrieval path used (fact_lookup, state_lookup, episode_expansion, etc.)

        Args:
            query: Natural language query.
            domain: Optional domain filter.
            limit: Maximum results.

        Returns:
            List of memories with full context graph.
        """
        results = await memory_svc.recall(
            query, domain=domain, limit=limit,
        )
        out = []
        for r in results:
            entry: dict[str, Any] = {
                "memory_id": r.memory.memory.id,
                "content": r.memory.memory.content,
                "type": r.memory.memory.type,
                "domains": r.memory.memory.domains,
                "retrieval_path": r.retrieval_path,
                "bm25_score": round(r.memory.bm25_score, 4),
                "is_superseded": r.memory.is_superseded,
                "created_at": r.memory.memory.created_at.isoformat(),
            }
            # Episode context
            if r.context.episode:
                ep = r.context.episode
                entry["episode"] = {
                    "id": ep.episode_id,
                    "title": ep.episode_title,
                    "status": ep.status,
                    "member_count": ep.member_count,
                    "topic_entities": ep.topic_entities[:10],
                    "sibling_count": len(ep.sibling_ids),
                    "summary": ep.summary[:300] if ep.summary else None,
                }
            # Entity states
            if r.context.entity_states:
                entry["entity_states"] = [
                    {
                        "entity": s.entity_name,
                        "key": s.state_key,
                        "value": s.state_value,
                        "is_current": s.is_current,
                    }
                    for s in r.context.entity_states[:10]
                ]
            # Causal chain (only include non-empty)
            cc = r.context.causal_chain
            chain: dict[str, list[str]] = {}
            if cc.supersedes:
                chain["supersedes"] = cc.supersedes
            if cc.superseded_by:
                chain["superseded_by"] = cc.superseded_by
            if cc.derived_from:
                chain["derived_from"] = cc.derived_from
            if cc.conflicts_with:
                chain["conflicts_with"] = cc.conflicts_with
            if chain:
                entry["causal_chain"] = chain
            out.append(entry)
        return out

    @mcp.tool()
    async def store_memory(
        content: str,
        type: str = "fact",
        domains: list[str] | None = None,
        tags: list[str] | None = None,
        project: str | None = None,
        structured: dict[str, Any] | None = None,
        importance: float = 5.0,
        show_admission: bool = False,
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
            show_admission: If True, include admission scoring details in response.

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
        result: dict[str, Any] = {
            "memory_id": memory.id,
            "content": memory.content,
            "domains": memory.domains,
            "created_at": memory.created_at.isoformat(),
        }
        if show_admission:
            result["admission"] = (memory.structured or {}).get("admission")
        return result

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

    # ── Phase 6: HTMG exposure tools ─────────────────────────────────────

    @mcp.tool()
    async def get_current_state(
        entity_id: str,
        state_key: str = "state",
    ) -> dict[str, Any]:
        """Look up the current state of an entity.

        Args:
            entity_id: The entity whose state to look up.
            state_key: The state facet key (default: "state").

        Returns:
            The current state node, or an error if not found or feature disabled.
        """
        if not memory_svc._config.reconciliation_enabled:
            return {
                "error": "State reconciliation not enabled "
                "(set NCMS_RECONCILIATION_ENABLED=true)",
            }
        node = await memory_svc.store.get_current_state(entity_id, state_key)
        if node is None:
            return {"found": False, "entity_id": entity_id, "state_key": state_key}
        return {"found": True, "node": node.model_dump(mode="json")}

    @mcp.tool()
    async def get_state_history(
        entity_id: str,
        state_key: str = "state",
    ) -> dict[str, Any]:
        """Retrieve the temporal chain of state transitions for an entity.

        Args:
            entity_id: The entity whose state history to retrieve.
            state_key: The state facet key (default: "state").

        Returns:
            Ordered list of state nodes from oldest to newest.
        """
        if not memory_svc._config.reconciliation_enabled:
            return {
                "error": "State reconciliation not enabled "
                "(set NCMS_RECONCILIATION_ENABLED=true)",
            }
        nodes = await memory_svc.store.get_state_history(entity_id, state_key)
        return {
            "entity_id": entity_id,
            "state_key": state_key,
            "count": len(nodes),
            "states": [n.model_dump(mode="json") for n in nodes],
        }

    @mcp.tool()
    async def list_episodes(
        include_closed: bool = False,
    ) -> dict[str, Any]:
        """List open (and optionally closed) episodes.

        Args:
            include_closed: If True, include closed episodes as well.

        Returns:
            List of episodes with metadata and member counts.
        """
        if not memory_svc._config.episodes_enabled:
            return {
                "error": "Episode formation not enabled "
                "(set NCMS_EPISODES_ENABLED=true)",
            }
        from ncms.domain.models import NodeType

        episodes = list(await memory_svc.store.get_open_episodes())
        if include_closed:
            all_episode_nodes = await memory_svc.store.get_memory_nodes_by_type(
                NodeType.EPISODE.value,
            )
            seen = {ep.id for ep in episodes}
            for node in all_episode_nodes:
                if node.id not in seen:
                    episodes.append(node)

        result = []
        for ep in episodes:
            members = await memory_svc.store.get_episode_members(ep.id)
            result.append({
                "episode_id": ep.id,
                "memory_id": ep.memory_id,
                "status": ep.metadata.get("status", "unknown"),
                "title": ep.metadata.get("episode_title", ""),
                "member_count": len(members),
                "created_at": ep.created_at.isoformat() if ep.created_at else None,
                "closed_at": ep.metadata.get("closed_at"),
            })
        return {"count": len(result), "episodes": result}

    @mcp.tool()
    async def get_episode(episode_id: str) -> dict[str, Any]:
        """Retrieve an episode with all its member fragments.

        Args:
            episode_id: The episode node ID.

        Returns:
            Episode metadata and member fragment contents.
        """
        if not memory_svc._config.episodes_enabled:
            return {
                "error": "Episode formation not enabled "
                "(set NCMS_EPISODES_ENABLED=true)",
            }
        episode_node = await memory_svc.store.get_memory_node(episode_id)
        if episode_node is None:
            return {"error": f"Episode not found: {episode_id}"}

        members = await memory_svc.store.get_episode_members(episode_id)
        member_details = []
        for m in members:
            mem = await memory_svc.get_memory(m.memory_id)
            member_details.append({
                "node_id": m.id,
                "memory_id": m.memory_id,
                "node_type": m.node_type,
                "content": mem.content[:500] if mem else None,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            })

        return {
            "episode_id": episode_node.id,
            "status": episode_node.metadata.get("status", "unknown"),
            "title": episode_node.metadata.get("episode_title", ""),
            "member_count": len(member_details),
            "members": member_details,
            "metadata": episode_node.metadata,
        }

    if consolidation_svc is not None:

        @mcp.tool()
        async def run_consolidation() -> dict[str, Any]:
            """Run a full consolidation pass.

            Executes all consolidation subtasks in sequence:
            decay scoring, knowledge synthesis, episode summaries,
            state trajectories, and pattern detection.

            Returns:
                Counts per subtask (decay, knowledge, episodes,
                trajectories, patterns, refresh).
            """
            return await consolidation_svc.run_consolidation_pass()  # type: ignore[union-attr]
