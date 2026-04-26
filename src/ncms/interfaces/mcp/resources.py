"""MCP resource implementations for NCMS.

Resources provide read-only data access via ncms:// URIs.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ncms.application.bus_service import BusService
from ncms.application.memory_service import MemoryService
from ncms.application.snapshot_service import SnapshotService


def register_resources(
    mcp: FastMCP,
    memory_svc: MemoryService,
    bus_svc: BusService,
    snapshot_svc: SnapshotService,
) -> None:
    """Register all NCMS MCP resources on the given server."""

    @mcp.resource("ncms://domains")
    async def list_domains() -> str:
        """List all knowledge domains with their providers."""
        domain_map = bus_svc.list_domains()
        lines = ["# Knowledge Domains\n"]
        for domain, agents in sorted(domain_map.items()):
            status_list = ", ".join(agents)
            lines.append(f"- **{domain}**: {status_list}")
        if not domain_map:
            lines.append("No domains registered.")
        return "\n".join(lines)

    @mcp.resource("ncms://agents")
    async def list_agents() -> str:
        """List all registered agents with status."""
        agents = bus_svc.get_all_agents()
        lines = ["# Registered Agents\n"]
        for agent in agents:
            lines.append(
                f"- **{agent.agent_id}** [{agent.status}] domains: {', '.join(agent.domains)}"
            )
        if not agents:
            lines.append("No agents registered.")
        return "\n".join(lines)

    @mcp.resource("ncms://graph/entities")
    async def list_entities() -> str:
        """Browse knowledge graph entities."""
        entities = await memory_svc.list_entities()
        lines = ["# Knowledge Graph Entities\n"]
        for entity in entities[:100]:
            lines.append(f"- **{entity.name}** ({entity.type}) [id: {entity.id}]")
        total = memory_svc.entity_count()
        lines.append(f"\nTotal: {total} entities, {memory_svc.relationship_count()} relationships")
        return "\n".join(lines)

    @mcp.resource("ncms://entities/{entity_id}/state")
    async def entity_state(entity_id: str) -> str:
        """Current state and recent history for an entity."""
        entity = await memory_svc.store.get_entity(entity_id)
        name = entity.name if entity else entity_id

        states = await memory_svc.store.get_entity_states_by_entity(entity_id)
        if not states:
            return f"# Entity State: {name}\n\nNo state nodes found for entity `{entity_id}`."

        current = [s for s in states if s.is_current]
        historical = [s for s in states if not s.is_current]

        lines = [f"# Entity State: {name}\n"]

        if current:
            lines.append("## Current State\n")
            for s in current:
                key = s.metadata.get("state_key", "state")
                val = s.metadata.get("state_value", "")
                lines.append(f"- **{key}**: {val}")
        else:
            lines.append("*No current state.*\n")

        if historical:
            lines.append("\n## History\n")
            for s in historical:
                key = s.metadata.get("state_key", "state")
                val = s.metadata.get("state_value", "")
                ts = s.created_at.isoformat() if s.created_at else "?"
                lines.append(f"- [{ts}] **{key}**: {val} *(superseded)*")

        return "\n".join(lines)

    @mcp.resource("ncms://status")
    async def system_status() -> str:
        """NCMS system status overview."""
        mem_count = await memory_svc.memory_count()
        entity_count = memory_svc.entity_count()
        rel_count = memory_svc.relationship_count()
        agents = bus_svc.get_all_agents()
        domains = bus_svc.list_domains()

        online = sum(1 for a in agents if a.status == "online")
        sleeping = sum(1 for a in agents if a.status == "sleeping")

        lines = [
            "# NCMS System Status\n",
            f"- **Memories**: {mem_count}",
            f"- **Entities**: {entity_count}",
            f"- **Relationships**: {rel_count}",
            f"- **Agents**: {len(agents)} ({online} online, {sleeping} sleeping)",
            f"- **Domains**: {len(domains)}",
        ]

        # Background indexing stats
        idx_stats = memory_svc.index_pool_stats()
        if idx_stats is not None:
            lines.append(
                f"- **Index Queue**: {idx_stats['queue_depth']}"
                f"/{idx_stats['queue_capacity']}"
                f" ({idx_stats['workers_busy']}/{idx_stats['workers']} workers busy)"
            )
            lines.append(
                f"- **Indexed**: {idx_stats['processed_total']} processed"
                f", {idx_stats['failed_total']} failed"
                f", avg {idx_stats['avg_process_ms']:.0f}ms"
            )

        return "\n".join(lines)

    @mcp.resource("ncms://indexing/status")
    async def indexing_status() -> str:
        """Background indexing pipeline health."""
        stats = memory_svc.index_pool_stats()
        if stats is None:
            return (
                "# Indexing Status\n\n"
                "Background indexing is **disabled**.\n"
                "Set `NCMS_ASYNC_INDEXING_ENABLED=true` to enable."
            )

        return "\n".join(
            [
                "# Indexing Status\n",
                f"- **Queue depth**: {stats['queue_depth']}/{stats['queue_capacity']}",
                f"- **Workers**: {stats['workers_busy']}/{stats['workers']} busy",
                f"- **Processed**: {stats['processed_total']}",
                f"- **Failed**: {stats['failed_total']}",
                f"- **Retried**: {stats['retried_total']}",
                f"- **Avg processing time**: {stats['avg_process_ms']:.0f}ms",
                f"- **Est. oldest pending age**: {stats['oldest_pending_age_ms']:.0f}ms",
            ]
        )
