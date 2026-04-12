"""ncms-context-loader - Load relevant context at session start.

Queries NCMS for recent knowledge, pending work, breaking changes,
and relevant architecture decisions. Outputs to stdout for agent injection.

Usage:
    ncms-context-loader --project /path/to/project
"""

from __future__ import annotations

import asyncio

import click


@click.command()
@click.option("--project", default=".", help="Project directory path.")
@click.option("--limit", default=20, help="Max context items to load.")
def main(project: str, limit: int) -> None:
    """Load relevant NCMS context for a new coding session."""
    asyncio.run(_load_context(project, limit))


async def _load_context(project: str, limit: int) -> None:
    from ncms.application.graph_service import GraphService
    from ncms.application.memory_service import MemoryService
    from ncms.config import NCMSConfig
    from ncms.infrastructure.graph.networkx_store import NetworkXGraph
    from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
    from ncms.infrastructure.storage.sqlite_store import SQLiteStore

    config = NCMSConfig()
    store = SQLiteStore(db_path=config.db_path)

    try:
        await store.initialize()
        index = TantivyEngine(path=config.index_path)
        index.initialize()
        graph = NetworkXGraph()
        memory_svc = MemoryService(store=store, index=index, graph=graph, config=config)
        await GraphService(store=store, graph=graph).rebuild_from_store()

        # Load recent memories for this project
        memories = await memory_svc.list_memories(limit=limit)

        if not memories:
            click.echo("# NCMS Context\nNo previous session knowledge found.")
            return

        lines = ["# NCMS Context - Previous Session Knowledge\n"]

        # Group by type
        pending = [m for m in memories if m.type == "pending-work"]
        breaking = [m for m in memories if "breaking" in " ".join(m.tags).lower()]
        recent = [m for m in memories if m not in pending and m not in breaking]

        if pending:
            lines.append("## Pending Work")
            for m in pending:
                lines.append(f"- {m.content}")
            lines.append("")

        if breaking:
            lines.append("## Breaking Changes")
            for m in breaking:
                lines.append(f"- [{m.source_agent or 'unknown'}] {m.content}")
            lines.append("")

        if recent:
            lines.append("## Recent Knowledge")
            for m in recent[:limit]:
                domains_str = ", ".join(m.domains) if m.domains else "general"
                lines.append(f"- [{domains_str}] {m.content}")
            lines.append("")

        click.echo("\n".join(lines))

    except Exception:
        # Context loading should never block the agent from starting
        click.echo("# NCMS Context\nFailed to load context (database may not exist yet).")
    finally:
        await store.close()


if __name__ == "__main__":
    main()
