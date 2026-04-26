"""MCP tool implementations for NCMS.

Each tool is a function that gets registered with the FastMCP server.
Tools provide the primary interface for external agents (Claude, Copilot, etc.)
to interact with the cognitive memory system.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

from ncms.application.bus_service import BusService
from ncms.application.consolidation_service import ConsolidationService
from ncms.application.memory_service import MemoryService
from ncms.application.snapshot_service import SnapshotService
from ncms.domain.models import (
    ImpactAssessment,
    KnowledgeAnnounce,
    KnowledgeAsk,
    KnowledgePayload,
)

if TYPE_CHECKING:
    from ncms.infrastructure.watch.filesystem_watcher import FilesystemWatcher


def register_tools(
    mcp: FastMCP,
    memory_svc: MemoryService,
    bus_svc: BusService,
    snapshot_svc: SnapshotService,
    consolidation_svc: ConsolidationService | None = None,
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
            query,
            domain=domain,
            limit=limit,
            intent_override=intent,
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
            query,
            domain=domain,
            limit=limit,
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
                {"agent_id": aid, "status": agent_status.get(aid, "unknown")} for aid in agent_ids
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
        """Load knowledge from a file or directory into NCMS memory.

        Automatically chunks content by headings (markdown), paragraphs (text),
        or entries (JSON/CSV) for precise retrieval.  Directories use bulk
        import mode (async indexing, larger queue) for throughput.

        Supports: .md, .txt, .json, .yaml, .csv, .html, .rst

        Args:
            file_path: Path to the file or directory to import.
            domains: Knowledge domains for the imported content.
            project: Project context.

        Returns:
            Import statistics: files processed, memories created.
        """
        from pathlib import Path

        from ncms.application.knowledge_loader import KnowledgeLoader

        loader = KnowledgeLoader(memory_svc)
        p = Path(file_path)
        if p.is_dir():
            stats = await loader.bulk_load_directory(
                p,
                domains=domains,
                project=project,
            )
        else:
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
        if not memory_svc._config.temporal_enabled:
            return {
                "error": "State reconciliation not enabled (set NCMS_TEMPORAL_ENABLED=true)",
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
        if not memory_svc._config.temporal_enabled:
            return {
                "error": "State reconciliation not enabled (set NCMS_TEMPORAL_ENABLED=true)",
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
        if not memory_svc._config.temporal_enabled:
            return {
                "error": "Episode formation not enabled (set NCMS_TEMPORAL_ENABLED=true)",
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
            result.append(
                {
                    "episode_id": ep.id,
                    "memory_id": ep.memory_id,
                    "status": ep.metadata.get("status", "unknown"),
                    "title": ep.metadata.get("episode_title", ""),
                    "member_count": len(members),
                    "created_at": ep.created_at.isoformat() if ep.created_at else None,
                    "closed_at": ep.metadata.get("closed_at"),
                }
            )
        return {"count": len(result), "episodes": result}

    @mcp.tool()
    async def get_episode(episode_id: str) -> dict[str, Any]:
        """Retrieve an episode with all its member fragments.

        Args:
            episode_id: The episode node ID.

        Returns:
            Episode metadata and member fragment contents.
        """
        if not memory_svc._config.temporal_enabled:
            return {
                "error": "Episode formation not enabled (set NCMS_TEMPORAL_ENABLED=true)",
            }
        episode_node = await memory_svc.store.get_memory_node(episode_id)
        if episode_node is None:
            return {"error": f"Episode not found: {episode_id}"}

        members = await memory_svc.store.get_episode_members(episode_id)
        member_details = []
        for m in members:
            mem = await memory_svc.get_memory(m.memory_id)
            member_details.append(
                {
                    "node_id": m.id,
                    "memory_id": m.memory_id,
                    "node_type": m.node_type,
                    "content": mem.content[:500] if mem else None,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
            )

        return {
            "episode_id": episode_node.id,
            "status": episode_node.metadata.get("status", "unknown"),
            "title": episode_node.metadata.get("episode_title", ""),
            "member_count": len(member_details),
            "members": member_details,
            "metadata": episode_node.metadata,
        }

    @mcp.tool()
    async def delete_memory(memory_id: str) -> dict[str, Any]:
        """Delete a memory from the store and all indexes.

        Args:
            memory_id: The ID of the memory to delete.

        Returns:
            deleted: Whether the memory was found and deleted.
        """
        deleted = await memory_svc.delete(memory_id)
        return {"deleted": deleted, "memory_id": memory_id}

    # ── Filesystem watcher tools ────────────────────────────────────────

    # Active watchers registry (shared across tool invocations)
    _active_watchers: dict[str, FilesystemWatcher] = {}

    @mcp.tool()
    async def watch_directory(
        path: str,
        domain: str | None = None,
        recursive: bool = True,
        scan_only: bool = False,
    ) -> dict[str, Any]:
        """Watch a directory for file changes and auto-ingest into memory.

        Monitors files for creation/modification, classifies domains
        automatically based on directory names, file extensions, and path
        patterns, then ingests into NCMS memory.

        Args:
            path: Directory path to watch.
            domain: Override domain for all files (auto-classified if not set).
            recursive: Watch subdirectories.
            scan_only: If True, scan once and return without live watching.

        Returns:
            watch_id and initial scan statistics.
        """
        import uuid
        from pathlib import Path as PathLib

        from ncms.application.watch_service import WatchService

        dir_path = PathLib(path)
        if not dir_path.is_dir():
            return {"error": f"Not a directory: {path}"}

        watch_svc = WatchService(
            memory_svc,
            default_domain=domain,
        )

        if scan_only:
            stats = await watch_svc.scan_directory(dir_path, recursive=recursive)
            return {
                "mode": "scan_only",
                "path": path,
                **stats.to_dict(),
            }

        # Live watching
        try:
            from ncms.infrastructure.watch.filesystem_watcher import FilesystemWatcher
        except ImportError:
            return {
                "error": "watchdog not installed. Install with: pip install ncms[watch]",
            }

        watch_id = uuid.uuid4().hex[:8]
        watcher = FilesystemWatcher(watch_svc, debounce_seconds=2.0)
        await watcher.start([(path, recursive)])
        _active_watchers[watch_id] = watcher

        stats = watcher.get_stats()
        return {
            "watch_id": watch_id,
            "mode": "live",
            "path": path,
            "recursive": recursive,
            "domain": domain or "auto-classified",
            "initial_scan": stats.to_dict(),
        }

    @mcp.tool()
    async def stop_watch(
        watch_id: str = "",
    ) -> dict[str, Any]:
        """Stop a filesystem watcher.

        Args:
            watch_id: The watch ID returned by watch_directory.
                If empty, stops all active watchers.

        Returns:
            Final statistics from the stopped watcher(s).
        """
        if watch_id and watch_id in _active_watchers:
            watcher = _active_watchers.pop(watch_id)
            await watcher.stop()
            stats = watcher.get_stats()
            return {
                "stopped": watch_id,
                **stats.to_dict(),
            }
        elif not watch_id:
            # Stop all
            results = {}
            for wid, watcher in list(_active_watchers.items()):
                await watcher.stop()
                stats = watcher.get_stats()
                results[wid] = stats.to_dict()
            _active_watchers.clear()
            return {"stopped_all": True, "watchers": results}
        else:
            return {
                "error": f"Watch not found: {watch_id}",
                "active_watches": list(_active_watchers.keys()),
            }

    # ── Phase 5: Level-First Retrieval & Synthesis ─────────────────

    @mcp.tool()
    async def search_level(
        query: str,
        node_types: list[str] | None = None,
        domain: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search memories filtered to specific HTMG hierarchy levels.

        Scopes retrieval to particular node types (atomic, entity_state,
        episode, abstract) — useful for finding only episodes, only
        abstracts/insights, or only entity states.

        Args:
            query: Search query.
            node_types: Filter to these types (e.g. ["abstract", "episode"]).
            domain: Optional domain scope.
            limit: Max results.

        Returns:
            Scored memories filtered to requested level(s).
        """
        results = await memory_svc.search_level(
            query,
            node_types=node_types,
            domain=domain,
            limit=limit,
        )
        return [
            {
                "memory_id": sm.memory.id,
                "content": sm.memory.content[:500],
                "score": round(sm.total_activation, 4),
                "node_types": sm.node_types,
                "memory_type": sm.memory.type,
            }
            for sm in results
        ]

    @mcp.tool()
    async def traverse_memory(
        seed_memory_id: str,
        mode: str = "bottom_up",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Traverse the HTMG hierarchy from a seed memory.

        Navigate the memory hierarchy using different strategies:
        - top_down: From abstract → episodes → atomic fragments
        - bottom_up: From atomic → episode → abstracts
        - temporal: Entity state timeline
        - lateral: Episode siblings and related episodes

        Args:
            seed_memory_id: Starting memory ID.
            mode: Traversal strategy (top_down, bottom_up, temporal, lateral).
            limit: Max results to collect.

        Returns:
            Traversal results with path and hierarchy levels.
        """
        result = await memory_svc.traverse(
            seed_memory_id,
            mode=mode,
            limit=limit,
        )
        return {
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
                    "memory_type": rr.memory.memory.type,
                }
                for rr in result.results
            ],
        }

    @mcp.tool()
    async def get_topic_map() -> list[dict[str, Any]]:
        """Get emergent topic map from abstract memory clustering.

        Clusters L4 abstract nodes by shared entities to reveal
        emergent knowledge themes. Requires at least one prior
        consolidation pass to have generated abstracts.

        Returns:
            Topic clusters with labels, entities, and member counts.
        """
        clusters = await memory_svc.get_topic_map()
        return [
            {
                "topic_id": c.topic_id,
                "label": c.label,
                "entity_keys": c.entity_keys,
                "abstract_count": len(c.abstract_ids),
                "episode_count": len(c.episode_ids),
                "confidence": round(c.confidence, 3),
            }
            for c in clusters
        ]

    @mcp.tool()
    async def synthesize_memory(
        query: str,
        mode: str = "summary",
        domain: str | None = None,
        limit: int = 10,
        token_budget: int | None = None,
        traversal: str | None = None,
        seed_memory_id: str | None = None,
    ) -> dict[str, Any]:
        """Synthesize a structured response from memory retrieval.

        Combines search/recall with LLM synthesis to produce
        token-budgeted responses with source provenance.

        Modes:
        - summary: Concise key points
        - detail: Comprehensive with evidence
        - timeline: Chronological narrative
        - comparison: Before/after or multi-perspective
        - evidence: Fact-backed claims with citations

        Args:
            query: Question to answer from memory.
            mode: Synthesis mode.
            domain: Optional domain scope.
            limit: Max source memories.
            token_budget: Max output tokens (default from config).
            traversal: Optional traversal strategy instead of search.
            seed_memory_id: Required with traversal.

        Returns:
            Synthesized response with content, sources, and token stats.
        """
        result = await memory_svc.synthesize(
            query,
            mode=mode,
            domain=domain,
            limit=limit,
            token_budget=token_budget,
            traversal=traversal,
            seed_memory_id=seed_memory_id,
        )
        return {
            "query": result.query,
            "mode": result.mode,
            "content": result.content,
            "sources": result.sources,
            "source_count": result.source_count,
            "token_budget": result.token_budget,
            "tokens_used": result.tokens_used,
            "traversal": result.traversal,
            "intent": result.intent,
        }

    # ── Phase 6: Export & Feedback ───────────────────────────────────

    @mcp.tool()
    async def record_search_feedback(
        query: str,
        selected_memory_id: str,
        result_ids: list[str] | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Record which search result was actually used (implicit feedback).

        Call after a search_memory result is selected by the user/agent.
        Boosts ACT-R base-level activation for the selected memory and
        tracks position for retrieval quality metrics.

        Args:
            query: The original search query.
            selected_memory_id: Memory ID that was selected/used.
            result_ids: Full result ID list (for position tracking).
            agent_id: Agent making the selection.

        Returns:
            Confirmation with position info.
        """
        await memory_svc.record_search_feedback(
            query=query,
            selected_memory_id=selected_memory_id,
            result_ids=result_ids,
            agent_id=agent_id,
        )
        position = (
            result_ids.index(selected_memory_id) + 1
            if result_ids and selected_memory_id in result_ids
            else None
        )
        return {
            "recorded": True,
            "selected_memory_id": selected_memory_id,
            "position": position,
        }

    @mcp.tool()
    async def heartbeat(agent_id: str) -> dict[str, Any]:
        """Send a heartbeat from an agent to indicate it is still alive.

        Agents should call this periodically (default: every 30s).
        If no heartbeat is received within the timeout window (default: 90s),
        the agent is marked offline and surrogate mode activates.

        Args:
            agent_id: The agent sending the heartbeat.

        Returns:
            Acknowledgement with current status.
        """
        await bus_svc.heartbeat(agent_id)
        online = bus_svc.is_agent_online(agent_id)
        return {
            "agent_id": agent_id,
            "status": "online" if online else "offline",
            "heartbeat_received": True,
        }

    @mcp.tool()
    def check_scale_flags() -> dict[str, Any]:
        """Check which features are auto-disabled based on corpus size.

        When scale_aware_flags is enabled, expensive features like
        cross-encoder reranking and intent classification are automatically
        disabled when the corpus exceeds configured thresholds.

        Returns:
            Feature flags with effective enabled/disabled status.
        """
        return memory_svc.check_scale_flags()

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
            return await consolidation_svc.run_consolidation_pass()
