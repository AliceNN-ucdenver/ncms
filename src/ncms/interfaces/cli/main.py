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
@click.option("--debug/--no-debug", "debug_flag", default=False, help="Emit candidate details")
def dashboard(
    host: str, port: int, run_demo_flag: bool, open_browser: bool, debug_flag: bool,
) -> None:
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

    # Demo mode implies pipeline debug for richer observability
    effective_debug = debug_flag or run_demo_flag
    if effective_debug:
        click.echo("Pipeline debug enabled (candidate details in events).", err=True)

    if open_browser:
        import threading
        import webbrowser

        # Open browser after a short delay to let the server start
        # Daemon thread so it won't block process exit on Ctrl+C
        t = threading.Timer(1.5, webbrowser.open, args=[url])
        t.daemon = True
        t.start()

    try:
        asyncio.run(run_dashboard(
            host=host, port=port, run_demo=run_demo_flag,
            pipeline_debug=effective_debug,
        ))
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

        # SPLADE sparse neural retrieval (disabled by default)
        splade = None
        if config.splade_enabled:
            from ncms.infrastructure.indexing.splade_engine import SpladeEngine

            splade = SpladeEngine(
                model_name=config.splade_model,
                cache_dir=config.model_cache_dir,
            )

        # Admission scoring (Phase 1, disabled by default)
        admission = None
        if config.admission_enabled:
            from ncms.application.admission_service import AdmissionService

            admission = AdmissionService(
                store=store, index=index, graph=graph, config=config,
            )

        # Reconciliation service (Phase 2, disabled by default)
        reconciliation = None
        if config.reconciliation_enabled:
            from ncms.application.reconciliation_service import ReconciliationService

            reconciliation = ReconciliationService(store=store, config=config)

        # Episode formation (Phase 3, disabled by default)
        episode = None
        if config.episodes_enabled:
            from ncms.application.episode_service import EpisodeService

            episode = EpisodeService(
                store=store, index=index, config=config, splade=splade,
            )

        # Cross-encoder reranker (Phase 10, disabled by default)
        reranker = None
        if config.reranker_enabled:
            from ncms.infrastructure.reranking.cross_encoder_reranker import (
                CrossEncoderReranker,
            )

            reranker = CrossEncoderReranker(
                model_name=config.reranker_model,
                cache_dir=config.model_cache_dir,
            )

        memory_svc = MemoryService(
            store=store, index=index, graph=graph, config=config,
            splade=splade, admission=admission,
            reconciliation=reconciliation, episode=episode,
            reranker=reranker,
        )
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


@cli.group()
def topics() -> None:
    """Manage domain-specific entity extraction labels.

    Labels control which entity types GLiNER extracts from content.
    Without cached labels, NCMS uses universal defaults.
    """
    pass


@topics.command("set")
@click.argument("domain")
@click.argument("labels", nargs=-1, required=True)
@click.option("--db", default=None, help="Database path")
def topics_set(domain: str, labels: tuple[str, ...], db: str | None) -> None:
    """Set entity labels for a domain.

    Examples:
        ncms topics set api endpoint service protocol authentication
        ncms topics set finance stock bond portfolio risk
    """
    import json

    from rich.console import Console

    console = Console()

    async def _set() -> None:
        from ncms.config import NCMSConfig
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        config = NCMSConfig()
        if db:
            config.db_path = db
        store = SQLiteStore(db_path=config.db_path)
        try:
            await store.initialize()
            label_list = list(labels)
            await store.set_consolidation_value(
                f"entity_labels:{domain}", json.dumps(label_list)
            )
            console.print(
                f"[green]Set {len(label_list)} labels for domain '{domain}':[/] "
                + ", ".join(label_list)
            )
        finally:
            await store.close()

    asyncio.run(_set())


@topics.command("list")
@click.argument("domain", required=False)
@click.option("--db", default=None, help="Database path")
def topics_list(domain: str | None, db: str | None) -> None:
    """List cached entity labels for one or all domains.

    Examples:
        ncms topics list          # Show all domains
        ncms topics list api      # Show labels for 'api' domain
    """
    import json

    from rich.console import Console
    from rich.table import Table

    console = Console()

    async def _list() -> None:
        from ncms.config import NCMSConfig
        from ncms.domain.entity_extraction import UNIVERSAL_LABELS
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        config = NCMSConfig()
        if db:
            config.db_path = db
        store = SQLiteStore(db_path=config.db_path)
        try:
            await store.initialize()

            if domain:
                raw = await store.get_consolidation_value(f"entity_labels:{domain}")
                if raw:
                    labels = json.loads(raw)
                    console.print(f"[bold]Domain '{domain}':[/] {', '.join(labels)}")
                else:
                    console.print(
                        f"[yellow]No cached labels for '{domain}'.[/] "
                        f"Using universal fallback: {', '.join(UNIVERSAL_LABELS)}"
                    )
            else:
                # Query all entity_labels:* keys from consolidation_state
                cursor = await store.db.execute(
                    "SELECT key, value FROM consolidation_state WHERE key LIKE 'entity_labels:%'"
                )
                rows = await cursor.fetchall()

                if not rows:
                    console.print(
                        "[yellow]No domain labels cached.[/]\n"
                        f"Universal fallback: {', '.join(UNIVERSAL_LABELS)}"
                    )
                    return

                table = Table(title="Cached Domain Labels")
                table.add_column("Domain", style="cyan")
                table.add_column("Labels")
                for row in rows:
                    d = row[0].replace("entity_labels:", "")
                    labels = json.loads(row[1])
                    table.add_row(d, ", ".join(labels))
                console.print(table)
                console.print(
                    f"\n[dim]Universal fallback (when no cache): "
                    f"{', '.join(UNIVERSAL_LABELS)}[/]"
                )
        finally:
            await store.close()

    asyncio.run(_list())


@topics.command("detect")
@click.argument("domain")
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("--model", default=None, help="LLM model for detection (default: config value)")
@click.option("--api-base", default=None, help="LLM API base URL")
@click.option("--dry-run", is_flag=True, help="Show detected labels without saving")
@click.option("--db", default=None, help="Database path")
def topics_detect(
    domain: str, paths: tuple[str, ...],
    model: str | None, api_base: str | None,
    dry_run: bool, db: str | None,
) -> None:
    """Auto-detect entity labels for a domain from sample files.

    Reads sample content from files and uses an LLM to propose optimal
    entity type labels for GLiNER extraction.

    Examples:
        ncms topics detect api docs/api-spec.md
        ncms topics detect finance reports/ --dry-run
        ncms topics detect biomedical papers/ --model ollama_chat/qwen3.5:35b-a3b
    """
    import json

    from rich.console import Console

    console = Console()

    async def _detect() -> None:
        from pathlib import Path

        from ncms.config import NCMSConfig
        from ncms.infrastructure.extraction.label_detector import detect_labels
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        config = NCMSConfig()
        if db:
            config.db_path = db

        # Collect sample texts from files
        import contextlib

        text_suffixes = {
            ".md", ".txt", ".json", ".yaml", ".yml",
            ".rst", ".html", ".csv",
        }
        sample_texts: list[str] = []
        for p in paths:
            path = Path(p)
            if path.is_dir():
                for f in sorted(path.rglob("*"))[:20]:
                    if f.is_file() and f.suffix in text_suffixes:
                        with contextlib.suppress(Exception):
                            sample_texts.append(f.read_text(errors="ignore")[:2000])
            elif path.is_file():
                try:
                    sample_texts.append(path.read_text(errors="ignore")[:2000])
                except Exception:
                    console.print(f"[red]Could not read:[/] {path}")

        if not sample_texts:
            console.print("[red]No readable text found in the provided paths.[/]")
            return

        console.print(
            f"Analyzing {len(sample_texts)} sample(s) for domain '{domain}'..."
        )

        llm_model = model or config.label_detection_model
        llm_api_base = api_base or config.label_detection_api_base
        labels = await detect_labels(
            domain=domain,
            sample_texts=sample_texts,
            model=llm_model,
            api_base=llm_api_base,
        )

        if not labels:
            console.print("[red]Label detection returned no results.[/]")
            return

        console.print(f"[bold]Detected {len(labels)} labels:[/] {', '.join(labels)}")

        if dry_run:
            console.print("[yellow]Dry run — labels not saved.[/]")
            return

        store = SQLiteStore(db_path=config.db_path)
        try:
            await store.initialize()
            await store.set_consolidation_value(
                f"entity_labels:{domain}", json.dumps(labels)
            )
            console.print(f"[green]Saved labels for domain '{domain}'.[/]")
        finally:
            await store.close()

    asyncio.run(_detect())


@topics.command("clear")
@click.argument("domain")
@click.option("--db", default=None, help="Database path")
def topics_clear(domain: str, db: str | None) -> None:
    """Clear cached entity labels for a domain.

    After clearing, NCMS will use universal fallback labels for this domain.

    Examples:
        ncms topics clear api
    """
    from rich.console import Console

    console = Console()

    async def _clear() -> None:
        from ncms.config import NCMSConfig
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        config = NCMSConfig()
        if db:
            config.db_path = db
        store = SQLiteStore(db_path=config.db_path)
        try:
            await store.initialize()
            await store.delete_consolidation_value(f"entity_labels:{domain}")
            console.print(
                f"[green]Cleared labels for domain '{domain}'.[/] "
                "Will use universal fallback."
            )
        finally:
            await store.close()

    asyncio.run(_clear())


@cli.group()
def state() -> None:
    """Query entity states and history (Phase 2 reconciliation).

    View current entity states and their temporal transition history.
    Requires NCMS_RECONCILIATION_ENABLED=true for data to exist.
    """
    pass


@state.command("get")
@click.argument("entity_id")
@click.option("--key", default="state", help="State facet key (default: state)")
@click.option("--db", default=None, help="Database path")
def state_get(entity_id: str, key: str, db: str | None) -> None:
    """Show current state of an entity.

    Examples:
        ncms state get auth-service
        ncms state get user-table --key schema_version
    """
    from rich.console import Console

    console = Console()

    async def _get() -> None:
        from ncms.config import NCMSConfig
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        config = NCMSConfig()
        if db:
            config.db_path = db
        store = SQLiteStore(db_path=config.db_path)
        try:
            await store.initialize()
            node = await store.get_current_state(entity_id, key)
            if node is None:
                console.print(
                    f"[yellow]No current state for entity '{entity_id}' "
                    f"key '{key}'.[/]"
                )
                return
            val = node.metadata.get("state_value", node.content or "")
            label = node.metadata.get("state_label", "")
            console.print(f"[bold]Entity:[/] {entity_id}")
            console.print(f"[bold]Key:[/]    {key}")
            console.print(f"[bold]Value:[/]  {val}")
            if label:
                console.print(f"[bold]Label:[/]  {label}")
            ts = node.created_at.isoformat() if node.created_at else "?"
            console.print(f"[bold]Since:[/]  {ts}")
        finally:
            await store.close()

    asyncio.run(_get())


@state.command("history")
@click.argument("entity_id")
@click.option("--key", default="state", help="State facet key (default: state)")
@click.option("--db", default=None, help="Database path")
def state_history(entity_id: str, key: str, db: str | None) -> None:
    """Show state transition history for an entity.

    Examples:
        ncms state history auth-service
        ncms state history user-table --key schema_version
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()

    async def _history() -> None:
        from ncms.config import NCMSConfig
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        config = NCMSConfig()
        if db:
            config.db_path = db
        store = SQLiteStore(db_path=config.db_path)
        try:
            await store.initialize()
            nodes = await store.get_state_history(entity_id, key)
            if not nodes:
                console.print(
                    f"[yellow]No state history for entity '{entity_id}' "
                    f"key '{key}'.[/]"
                )
                return

            table = Table(title=f"State History: {entity_id} / {key}")
            table.add_column("Created", style="dim")
            table.add_column("Value")
            table.add_column("Current", justify="center")
            for n in nodes:
                val = n.metadata.get("state_value", n.content or "")
                ts = n.created_at.isoformat() if n.created_at else "?"
                cur = "[green]yes[/]" if n.is_current else "[dim]no[/]"
                table.add_row(ts, str(val)[:100], cur)
            console.print(table)
        finally:
            await store.close()

    asyncio.run(_history())


@state.command("list")
@click.option("--db", default=None, help="Database path")
def state_list(db: str | None) -> None:
    """List entities that have state nodes.

    Examples:
        ncms state list
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()

    async def _list() -> None:
        from ncms.config import NCMSConfig
        from ncms.domain.models import NodeType
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        config = NCMSConfig()
        if db:
            config.db_path = db
        store = SQLiteStore(db_path=config.db_path)
        try:
            await store.initialize()
            nodes = await store.get_memory_nodes_by_type(
                NodeType.ENTITY_STATE.value,
            )
            if not nodes:
                console.print("[yellow]No entity state nodes found.[/]")
                return

            # Group by entity_id
            entities: dict[str, int] = {}
            for n in nodes:
                eid = n.metadata.get("entity_id", "unknown")
                entities[eid] = entities.get(eid, 0) + 1

            table = Table(title="Entities with State Nodes")
            table.add_column("Entity ID", style="cyan")
            table.add_column("State Count", justify="right")
            for eid, count in sorted(entities.items(), key=lambda x: -x[1]):
                table.add_row(eid, str(count))
            console.print(table)
        finally:
            await store.close()

    asyncio.run(_list())


@cli.group()
def episodes() -> None:
    """Query episode formation data (Phase 3).

    View open/closed episodes and their member fragments.
    Requires NCMS_EPISODES_ENABLED=true for data to exist.
    """
    pass


@episodes.command("list")
@click.option("--closed", is_flag=True, help="Include closed episodes")
@click.option("--db", default=None, help="Database path")
def episodes_list(closed: bool, db: str | None) -> None:
    """List episodes.

    Examples:
        ncms episodes list
        ncms episodes list --closed
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()

    async def _list() -> None:
        from ncms.config import NCMSConfig
        from ncms.domain.models import NodeType
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        config = NCMSConfig()
        if db:
            config.db_path = db
        store = SQLiteStore(db_path=config.db_path)
        try:
            await store.initialize()
            if closed:
                all_episodes = await store.get_memory_nodes_by_type(
                    NodeType.EPISODE.value,
                )
            else:
                all_episodes = await store.get_open_episodes()

            if not all_episodes:
                console.print("[yellow]No episodes found.[/]")
                return

            table = Table(title="Episodes")
            table.add_column("Episode ID", style="cyan", max_width=24)
            table.add_column("Title")
            table.add_column("Status")
            table.add_column("Members", justify="right")
            table.add_column("Created")

            for ep in all_episodes:
                members = await store.get_episode_members(ep.id)
                status = ep.metadata.get("status", "unknown")
                title = ep.metadata.get("episode_title", "")[:40]
                ts = ep.created_at.isoformat() if ep.created_at else "?"
                style = "green" if status == "open" else "dim"
                table.add_row(
                    ep.id[:24], title, f"[{style}]{status}[/]",
                    str(len(members)), ts,
                )
            console.print(table)
        finally:
            await store.close()

    asyncio.run(_list())


@episodes.command("show")
@click.argument("episode_id")
@click.option("--db", default=None, help="Database path")
def episodes_show(episode_id: str, db: str | None) -> None:
    """Show an episode with its member fragments.

    Examples:
        ncms episodes show ep-abc123
    """
    from rich.console import Console

    console = Console()

    async def _show() -> None:
        from ncms.config import NCMSConfig
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        config = NCMSConfig()
        if db:
            config.db_path = db
        store = SQLiteStore(db_path=config.db_path)
        try:
            await store.initialize()
            node = await store.get_memory_node(episode_id)
            if not node:
                console.print(f"[red]Episode not found: {episode_id}[/]")
                return

            status = node.metadata.get("status", "unknown")
            title = node.metadata.get("episode_title", "")
            console.print(f"[bold]Episode:[/] {node.id}")
            console.print(f"[bold]Title:[/]   {title}")
            console.print(f"[bold]Status:[/]  {status}")

            members = await store.get_episode_members(episode_id)
            console.print(f"[bold]Members:[/] {len(members)}\n")
            for m in members:
                mem = await store.get_memory(m.memory_id)
                content = (mem.content[:200] if mem else "(no content)").replace("\n", " ")
                ts = m.created_at.isoformat() if m.created_at else "?"
                console.print(f"  [{ts}] {content}")
        finally:
            await store.close()

    asyncio.run(_show())


if __name__ == "__main__":
    cli()
