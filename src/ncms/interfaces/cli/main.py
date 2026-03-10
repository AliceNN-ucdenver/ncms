"""NCMS CLI entry point.

Usage:
    ncms serve     - Start the MCP server
    ncms demo      - Run the interactive demo
    ncms info      - Show system status
"""

from __future__ import annotations

import asyncio

import click


@click.group()
@click.version_option(package_name="ncms")
def cli() -> None:
    """NeMo Cognitive Memory System - Vector-free persistent memory for AI agents."""
    pass


@cli.command()
@click.option("--db", default=None, help="Database path (default: ~/.ncms/ncms.db)")
@click.option("--index", default=None, help="Index path (default: ~/.ncms/index)")
def serve(db: str | None, index: str | None) -> None:
    """Start the NCMS MCP server (stdio transport)."""
    from ncms.config import NCMSConfig
    from ncms.interfaces.mcp.server import run_server

    config = NCMSConfig()
    if db:
        config.db_path = db
    if index:
        config.index_path = index

    click.echo("Starting NCMS MCP server...", err=True)
    asyncio.run(run_server(config))


@cli.command()
def demo() -> None:
    """Run the interactive NCMS demo with 3 collaborative agents."""
    from ncms.demo.run_demo import run_demo

    asyncio.run(run_demo())


@cli.command()
@click.option("--db", default=None, help="Database path")
def info(db: str | None) -> None:
    """Show NCMS system status."""
    from ncms.config import NCMSConfig

    async def _show_info() -> None:
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        config = NCMSConfig()
        if db:
            config.db_path = db

        store = SQLiteStore(db_path=config.db_path)
        try:
            await store.initialize()
            memories = await store.list_memories(limit=100000)
            entities = await store.list_entities()

            click.echo(f"Database: {config.db_path}")
            click.echo(f"Memories: {len(memories)}")
            click.echo(f"Entities: {len(entities)}")

            if memories:
                domains: set[str] = set()
                agents: set[str] = set()
                for m in memories:
                    domains.update(m.domains)
                    if m.source_agent:
                        agents.add(m.source_agent)
                click.echo(f"Domains: {', '.join(sorted(domains)) or 'none'}")
                click.echo(f"Agents: {', '.join(sorted(agents)) or 'none'}")
        finally:
            await store.close()

    asyncio.run(_show_info())


@cli.command()
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("--domains", "-d", multiple=True, help="Knowledge domains for imported content.")
@click.option("--project", "-p", default=None, help="Project context.")
@click.option("--recursive/--no-recursive", default=True, help="Recurse into directories.")
def load(paths: tuple[str, ...], domains: tuple[str, ...], project: str | None, recursive: bool) -> None:
    """Load knowledge from files into NCMS memory (The Matrix download).

    Supports: .md, .txt, .json, .yaml, .csv, .html, .rst

    Examples:
        ncms load docs/architecture.md -d arch
        ncms load docs/ -d project-docs --recursive
        ncms load api-spec.json -d api -p my-project
    """
    from rich.console import Console

    console = Console()

    async def _load() -> None:
        from ncms.config import NCMSConfig
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore
        from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
        from ncms.infrastructure.graph.networkx_store import NetworkXGraph
        from ncms.application.memory_service import MemoryService
        from ncms.application.knowledge_loader import KnowledgeLoader

        config = NCMSConfig()
        store = SQLiteStore(db_path=config.db_path)
        await store.initialize()
        index = TantivyEngine(path=config.index_path)
        index.initialize()
        graph = NetworkXGraph()
        memory_svc = MemoryService(store=store, index=index, graph=graph, config=config)
        loader = KnowledgeLoader(memory_svc)

        domain_list = list(domains) if domains else None
        total_files = 0
        total_memories = 0

        from pathlib import Path

        for p in paths:
            path = Path(p)
            if path.is_dir():
                stats = await loader.load_directory(
                    path, domains=domain_list, project=project, recursive=recursive
                )
            else:
                stats = await loader.load_file(path, domains=domain_list, project=project)

            total_files += stats.files_processed
            total_memories += stats.memories_created

            if stats.errors:
                for err in stats.errors:
                    console.print(f"  [red]Error:[/] {err}")

        console.print(
            f"[green]Loaded {total_files} file(s) -> {total_memories} memories[/]"
        )
        await store.close()

    asyncio.run(_load())


if __name__ == "__main__":
    cli()
