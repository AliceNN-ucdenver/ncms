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
@click.option("--host", default="0.0.0.0", help="Bind address")
@click.option("--port", default=8420, type=int, help="Port number")
@click.option("--demo/--no-demo", "run_demo_flag", default=True, help="Run demo agents")
@click.option("--open/--no-open", "open_browser", default=True, help="Open browser automatically")
def dashboard(host: str, port: int, run_demo_flag: bool, open_browser: bool) -> None:
    """Start the NCMS observability dashboard (web UI).

    Opens a browser-based dashboard showing agents, bus activity,
    and Knowledge Bus visualization in real-time.

    Requires: pip install ncms[dashboard]
    """
    try:
        from ncms.interfaces.http.dashboard import run_dashboard
    except ImportError:
        click.echo(
            "Dashboard dependencies not installed. Run:\n"
            "  pip install ncms[dashboard]\n"
            "  # or: uv sync --extra dashboard",
            err=True,
        )
        raise SystemExit(1) from None

    url = f"http://localhost:{port}"
    click.echo(f"Starting NCMS Dashboard at {url}", err=True)
    if run_demo_flag:
        click.echo("Demo agents will start automatically.", err=True)

    if open_browser:
        import threading
        import webbrowser

        # Open browser after a short delay to let the server start
        # Daemon thread so it won't block process exit on Ctrl+C
        t = threading.Timer(1.5, webbrowser.open, args=[url])
        t.daemon = True
        t.start()

    try:
        asyncio.run(run_dashboard(host=host, port=port, run_demo=run_demo_flag))
    except KeyboardInterrupt:
        click.echo("\nDashboard stopped.", err=True)


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
def load(
    paths: tuple[str, ...], domains: tuple[str, ...],
    project: str | None, recursive: bool,
) -> None:
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
        from ncms.application.graph_service import GraphService
        from ncms.application.knowledge_loader import KnowledgeLoader
        from ncms.application.memory_service import MemoryService
        from ncms.config import NCMSConfig
        from ncms.infrastructure.graph.networkx_store import NetworkXGraph
        from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        config = NCMSConfig()
        store = SQLiteStore(db_path=config.db_path)
        await store.initialize()
        index = TantivyEngine(path=config.index_path)
        index.initialize()
        graph = NetworkXGraph()
        memory_svc = MemoryService(store=store, index=index, graph=graph, config=config)
        await GraphService(store=store, graph=graph).rebuild_from_store()
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
