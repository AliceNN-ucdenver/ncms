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
@click.option(
    "--transport", default="stdio", type=click.Choice(["stdio", "http"]),
    help="Transport protocol",
)
@click.option("--host", default="0.0.0.0", help="HTTP bind address (http transport only)")
@click.option("--port", default=8080, type=int, help="HTTP port (http transport only)")
@click.option("--auth-token", default=None, help="Bearer token for HTTP auth")
@click.option("--dashboard-port", default=None, type=int, help="Also start dashboard on this port")
def serve(
    db: str | None,
    index: str | None,
    transport: str,
    host: str,
    port: int,
    auth_token: str | None,
    dashboard_port: int | None,
) -> None:
    """Start the NCMS server (MCP stdio or HTTP REST)."""
    import logging
    import os

    log_level = os.environ.get("NCMS_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    from ncms.config import NCMSConfig

    config = NCMSConfig()
    if db:
        config.db_path = db
    if index:
        config.index_path = index

    if transport == "http":
        from ncms.interfaces.http.api import run_http_server

        click.echo(f"Starting NCMS HTTP API server on {host}:{port}...", err=True)
        if dashboard_port:
            click.echo(f"Dashboard on {host}:{dashboard_port} (shared EventLog)", err=True)
        asyncio.run(run_http_server(
            config=config, host=host, port=port,
            auth_token=auth_token, dashboard_port=dashboard_port,
        ))
    else:
        from ncms.interfaces.mcp.server import run_server

        click.echo("Starting NCMS MCP server...", err=True)
        asyncio.run(run_server(config))


@cli.command("bus-agent")
@click.option("--hub", required=True, help="NCMS Hub URL (e.g. http://ncms-hub:8080)")
@click.option("--agent-id", required=True, help="Agent identifier")
@click.option("--domains", required=True, help="Comma-separated domain list")
@click.option("--subscribe-to", default=None, help="Domains to subscribe to (comma-sep)")
@click.option("--llm-model", default=None, envvar="NCMS_LLM_MODEL", help="LLM model for synthesis")
@click.option("--llm-api-base", default=None, envvar="NCMS_LLM_API_BASE", help="LLM API base URL")
@click.option("--system-prompt", default=None, help="System prompt for LLM synthesis")
def bus_agent(
    hub: str,
    agent_id: str,
    domains: str,
    subscribe_to: str | None,
    llm_model: str | None,
    llm_api_base: str | None,
    system_prompt: str | None,
) -> None:
    """Run the NCMS Bus Agent sidecar (for NemoClaw sandboxes).

    Maintains an SSE connection to the NCMS Hub. Handles incoming
    questions (search + LLM synthesis) and stores announcements.
    """
    from ncms.interfaces.cli.bus_agent import run_bus_agent

    run_bus_agent(
        hub_url=hub,
        agent_id=agent_id,
        domains=domains,
        subscribe_to=subscribe_to,
        llm_model=llm_model,
        llm_api_base=llm_api_base,
        system_prompt=system_prompt,
    )


@cli.command()
@click.option("--nemoclaw", is_flag=True, help="Run NemoClaw multi-agent demo")
@click.option(
    "--nemoclaw-nd", "nemoclaw_nd", is_flag=True,
    help="Run NemoClaw Non-Deterministic demo (LLM-powered agents)",
)
def demo(nemoclaw: bool, nemoclaw_nd: bool) -> None:
    """Run the interactive NCMS demo with collaborative agents."""
    if nemoclaw_nd:
        from ncms.demo.nemoclaw_nd.run import run_nemoclaw_nd_demo

        asyncio.run(run_nemoclaw_nd_demo())
    elif nemoclaw:
        from ncms.demo.run_nemoclaw_demo import run_nemoclaw_demo

        asyncio.run(run_nemoclaw_demo())
    else:
        from ncms.demo.run_demo import run_demo

        asyncio.run(run_demo())


@cli.command()
@click.option("--host", default="0.0.0.0", help="Bind address")
@click.option("--port", default=8420, type=int, help="Port number")
@click.option("--demo/--no-demo", "run_demo_flag", default=True, help="Run demo agents")
@click.option("--nd", "nd_demo", is_flag=True, help="Run ND agents (Architect/Security/Builder)")
@click.option("--open/--no-open", "open_browser", default=True, help="Open browser automatically")
@click.option("--debug/--no-debug", "debug_flag", default=False, help="Emit candidate details")
def dashboard(
    host: str, port: int, run_demo_flag: bool, nd_demo: bool,
    open_browser: bool, debug_flag: bool,
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

    demo_mode = "nd" if nd_demo else ("classic" if run_demo_flag else None)

    try:
        asyncio.run(run_dashboard(
            host=host, port=port, run_demo=run_demo_flag,
            pipeline_debug=effective_debug,
            demo_mode=demo_mode,
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
@click.option("--bulk/--no-bulk", default=True, help="Bulk import with async indexing (default).")
def load(
    paths: tuple[str, ...], domains: tuple[str, ...],
    project: str | None, recursive: bool, bulk: bool,
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
        from ncms.application.knowledge_loader import KnowledgeLoader, LoadStats
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

        # Start index pool for async indexing during load
        await memory_svc.start_index_pool(
            queue_size=config.bulk_import_queue_size if bulk else None,
        )

        for p in paths:
            path = Path(p)
            if path.is_dir() and bulk:
                # Bulk mode: load + index in parallel, flush at end
                def _progress(fp: Path, fs: LoadStats) -> None:
                    console.print(f"  [dim]{fp.name}[/] {fs.memories_created} memories")

                stats = await loader.bulk_load_directory(
                    path, domains=domain_list, project=project,
                    recursive=recursive, progress_callback=_progress,
                )
            elif path.is_dir():
                stats = await loader.load_directory(
                    path, domains=domain_list, project=project, recursive=recursive,
                )
            else:
                stats = await loader.load_file(
                    path, domains=domain_list, project=project,
                )

            total_files += stats.files_processed
            total_memories += stats.memories_created

            if stats.errors:
                for err in stats.errors:
                    console.print(f"  [red]Error:[/] {err}")

        # Ensure all indexing completes before exit
        await memory_svc.flush_indexing()
        await memory_svc.stop_index_pool()

        console.print(
            f"[green]Loaded {total_files} file(s) -> {total_memories} memories[/]"
        )
        await store.close()

    asyncio.run(_load())


@cli.command()
@click.option("--db", default=None, help="Database path")
@click.option("--index-path", default=None, help="Index path")
@click.option("--bm25/--no-bm25", default=True, help="Rebuild BM25 index (default: on)")
@click.option("--splade/--no-splade", default=True, help="Rebuild SPLADE index (default: on)")
@click.option(
    "--entities/--no-entities", default=False,
    help="Re-extract entities via GLiNER (default: off)",
)
@click.option(
    "--graph/--no-graph", default=False,
    help="Rebuild knowledge graph from SQLite (default: off)",
)
def reindex(
    db: str | None,
    index_path: str | None,
    bm25: bool,
    splade: bool,
    entities: bool,
    graph: bool,
) -> None:
    """Rebuild search indexes from persisted memories.

    By default rebuilds BM25 and SPLADE indexes. Use flags to control
    which indexes are rebuilt. Entity re-extraction and graph rebuild
    are off by default since they are more expensive.

    Examples:
        ncms reindex
        ncms reindex --no-splade
        ncms reindex --entities --graph
        ncms reindex --no-bm25 --splade
    """
    from rich.console import Console
    from rich.progress import Progress

    console = Console()

    if not any([bm25, splade, entities, graph]):
        console.print("[yellow]Nothing to do.[/] Enable at least one rebuild target.")
        return

    async def _reindex() -> None:
        from ncms.application.reindex_service import ReindexService
        from ncms.config import NCMSConfig
        from ncms.infrastructure.graph.networkx_store import NetworkXGraph
        from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        config = NCMSConfig()
        if db:
            config.db_path = db

        # Initialize store
        store = SQLiteStore(db_path=config.db_path)
        await store.initialize()

        # Initialize BM25 index
        tantivy = TantivyEngine(path=index_path or config.index_path)
        tantivy.initialize()

        # Initialize SPLADE if enabled and requested
        splade_engine = None
        if splade and config.splade_enabled:
            from ncms.infrastructure.indexing.splade_engine import SpladeEngine

            console.print("Loading SPLADE model...")
            splade_engine = SpladeEngine(
                model_name=config.splade_model,
                cache_dir=config.model_cache_dir,
            )
            console.print(f"[green]SPLADE loaded:[/] {config.splade_model}")
        elif splade and not config.splade_enabled:
            console.print(
                "[yellow]SPLADE is not enabled.[/] Set NCMS_SPLADE_ENABLED=true "
                "to enable SPLADE retrieval. Skipping SPLADE rebuild."
            )

        # Initialize graph
        graph_engine = NetworkXGraph()

        # Create reindex service
        svc = ReindexService(
            store=store,
            tantivy=tantivy,
            splade=splade_engine,
            graph=graph_engine,
            config=config,
        )

        # Load all memories once
        memories = await store.list_memories(limit=1_000_000)
        total = len(memories)
        console.print(f"Found {total} memories in database.")

        if total == 0:
            console.print("[yellow]No memories to re-index.[/]")
            await store.close()
            return

        # Build target list for display
        targets = []
        if bm25:
            targets.append("BM25")
        if splade and splade_engine is not None:
            targets.append("SPLADE")
        if entities:
            targets.append("Entities")
        if graph:
            targets.append("Graph")
        console.print(f"Rebuilding: {', '.join(targets)}")

        # Run each rebuild phase with its own progress bar
        with Progress(console=console) as progress:
            if bm25:
                task_id = progress.add_task("[cyan]BM25 indexing...", total=total)
                try:
                    await svc.rebuild_bm25(
                        memories=memories,
                        progress_callback=lambda cur, _tot: progress.update(
                            task_id, completed=cur,
                        ),
                    )
                    progress.update(task_id, completed=total)
                except Exception as e:
                    console.print(f"[red]BM25 rebuild failed:[/] {e}")

            if splade and splade_engine is not None:
                task_id = progress.add_task(
                    "[cyan]SPLADE indexing...", total=total,
                )
                try:
                    await svc.rebuild_splade(
                        memories=memories,
                        progress_callback=lambda cur, _tot: progress.update(
                            task_id, completed=cur,
                        ),
                    )
                    progress.update(task_id, completed=total)
                except Exception as e:
                    console.print(f"[red]SPLADE rebuild failed:[/] {e}")

            if entities:
                task_id = progress.add_task(
                    "[cyan]Entity extraction...", total=total,
                )
                try:
                    await svc.rebuild_entities(
                        memories=memories,
                        progress_callback=lambda cur, _tot: progress.update(
                            task_id, completed=cur,
                        ),
                    )
                    progress.update(task_id, completed=total)
                except Exception as e:
                    console.print(f"[red]Entity rebuild failed:[/] {e}")

            if graph:
                task_id = progress.add_task("[cyan]Graph rebuild...", total=1)
                try:
                    from ncms.application.graph_service import GraphService

                    graph_svc = GraphService(store=store, graph=graph_engine)
                    await graph_svc.rebuild_from_store()
                    progress.update(task_id, completed=1)
                except Exception as e:
                    console.print(f"[red]Graph rebuild failed:[/] {e}")

        console.print("\n[green]Re-index complete.[/]")
        await store.close()

    asyncio.run(_reindex())


@cli.command()
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("--domain", "-d", default=None, help="Override domain for all files.")
@click.option("--recursive/--no-recursive", default=True, help="Watch subdirectories.")
@click.option("--debounce", default=2.0, type=float, help="Debounce window in seconds.")
@click.option("--exclude", default=None, help="Comma-separated exclude patterns.")
@click.option("--importance", default=6.0, type=float, help="Default importance.")
@click.option("--scan-only", is_flag=True, help="Scan once and exit (no live watching).")
def watch(
    paths: tuple[str, ...],
    domain: str | None,
    recursive: bool,
    debounce: float,
    exclude: str | None,
    importance: float,
    scan_only: bool,
) -> None:
    """Watch directories for file changes and auto-ingest into memory.

    Monitors files for changes, classifies domains automatically, and
    ingests new/modified files into NCMS memory via KnowledgeLoader.

    Examples:
        ncms watch docs/ -d documentation
        ncms watch src/ docs/ --recursive
        ncms watch . --scan-only
        ncms watch docs/ --exclude "*.log,*.tmp"
    """
    from rich.console import Console

    console = Console()

    async def _watch() -> None:
        from pathlib import Path

        from ncms.application.graph_service import GraphService
        from ncms.application.memory_service import MemoryService
        from ncms.application.watch_service import WatchService
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

        # Optional SPLADE
        splade = None
        if config.splade_enabled:
            from ncms.infrastructure.indexing.splade_engine import SpladeEngine

            splade = SpladeEngine(
                model_name=config.splade_model,
                cache_dir=config.model_cache_dir,
            )

        # Optional admission
        admission = None
        if config.admission_enabled:
            from ncms.application.admission_service import AdmissionService

            admission = AdmissionService(
                store=store, index=index, graph=graph, config=config,
            )

        # Optional reconciliation
        reconciliation = None
        if config.reconciliation_enabled:
            from ncms.application.reconciliation_service import ReconciliationService

            reconciliation = ReconciliationService(store=store, config=config)

        # Optional episodes
        episode = None
        if config.episodes_enabled:
            from ncms.application.episode_service import EpisodeService

            episode = EpisodeService(
                store=store, index=index, config=config, splade=splade,
            )

        # Optional reranker
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

        exclude_patterns = None
        if exclude:
            exclude_patterns = [p.strip() for p in exclude.split(",") if p.strip()]

        watch_svc = WatchService(
            memory_svc,
            default_domain=domain,
            default_importance=importance,
        )

        if scan_only:
            console.print("[bold]Scanning directories...[/]")
            for p in paths:
                path = Path(p)
                if path.is_dir():
                    await watch_svc.scan_directory(path, recursive=recursive)
                else:
                    console.print(f"[yellow]Skipping non-directory: {p}[/]")
            stats = watch_svc.stats
            console.print(
                f"\n[green]Scan complete:[/] "
                f"{stats.files_ingested} ingested, "
                f"{stats.files_skipped_hash} unchanged, "
                f"{stats.files_skipped_unsupported} unsupported, "
                f"{stats.files_errored} errors, "
                f"{stats.total_memories_created} memories created"
            )
            if stats.domains_detected:
                console.print(
                    f"[bold]Domains:[/] {', '.join(sorted(stats.domains_detected))}"
                )
            await store.close()
            return

        # Live watching mode
        try:
            from ncms.infrastructure.watch.filesystem_watcher import FilesystemWatcher
        except ImportError:
            from rich.console import Console as _ErrConsole
            _err_console = _ErrConsole(stderr=True)
            _err_console.print(
                "[red]watchdog not installed. Run:[/]\n"
                "  pip install ncms[watch]\n"
                "  # or: pip install watchdog",
            )
            await store.close()
            raise SystemExit(1) from None

        watcher = FilesystemWatcher(
            watch_svc,
            debounce_seconds=debounce,
            exclude_patterns=exclude_patterns,
        )

        watch_paths = [(p, recursive) for p in paths]
        console.print(f"[bold]Watching {len(paths)} path(s)...[/] (Ctrl+C to stop)")
        for p in paths:
            console.print(f"  [cyan]{p}[/] (recursive={recursive})")

        await watcher.start(watch_paths)

        try:
            # Run until interrupted
            while watcher.running:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        finally:
            await watcher.stop()
            stats = watcher.get_stats()
            console.print(
                f"\n[green]Watch stopped:[/] "
                f"{stats.files_ingested} ingested, "
                f"{stats.files_skipped_hash} unchanged, "
                f"{stats.total_memories_created} memories created"
            )
            await store.close()

    try:
        asyncio.run(_watch())
    except KeyboardInterrupt:
        console.print("\n[dim]Watcher stopped.[/]")


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
@click.option("--keep-universal", is_flag=True, default=False,
              help="Keep universal labels (additive). Default: domain labels replace universal.")
def topics_set(domain: str, labels: tuple[str, ...], db: str | None, keep_universal: bool) -> None:
    """Set entity labels for a domain.

    By default, domain labels REPLACE universal labels for faster GLiNER
    extraction (~10 labels instead of ~20). Use --keep-universal to merge
    domain labels on top of universal labels (slower but broader coverage).

    Examples:
        ncms topics set software framework database protocol standard
        ncms topics set --keep-universal software framework database protocol
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
            # Store the keep_universal preference
            await store.set_consolidation_value(
                "_keep_universal", json.dumps(keep_universal)
            )
            mode = "additive (universal + domain)" if keep_universal else "replace (domain only)"
            console.print(
                f"[green]Set {len(label_list)} labels for domain '{domain}' ({mode}):[/] "
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
            val = node.metadata.get("state_value", "")
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
                val = n.metadata.get("state_value", "")
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


@cli.group()
def maintenance() -> None:
    """Maintenance scheduler: status and manual task execution.

    View scheduler status or manually trigger maintenance tasks
    (consolidation, dream cycles, episode closure, decay).
    """
    pass


@maintenance.command("status")
def maintenance_status() -> None:
    """Show maintenance scheduler status.

    Displays which tasks are registered, last/next run times,
    run counts, and any errors.

    Examples:
        ncms maintenance status
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()

    async def _status() -> None:
        from ncms.config import NCMSConfig

        config = NCMSConfig()

        if not config.maintenance_enabled:
            console.print(
                "[yellow]Maintenance scheduler is disabled.[/]\n"
                "Set NCMS_MAINTENANCE_ENABLED=true to enable."
            )
            return

        # Show configured intervals and feature flags
        table = Table(title="Maintenance Configuration")
        table.add_column("Task", style="cyan")
        table.add_column("Interval")
        table.add_column("Feature Flag")
        table.add_column("Enabled", justify="center")

        tasks_info = [
            (
                "consolidation",
                f"{config.maintenance_consolidation_interval_minutes}m",
                "consolidation_knowledge_enabled",
                config.consolidation_knowledge_enabled,
            ),
            (
                "dream",
                f"{config.maintenance_dream_interval_minutes}m",
                "dream_cycle_enabled",
                config.dream_cycle_enabled,
            ),
            (
                "episode_close",
                f"{config.maintenance_episode_close_interval_minutes}m",
                "episodes_enabled",
                config.episodes_enabled,
            ),
            (
                "decay",
                f"{config.maintenance_decay_interval_minutes}m",
                "(always)",
                True,
            ),
        ]

        for name, interval, flag, enabled in tasks_info:
            status = "[green]yes[/]" if enabled else "[red]no[/]"
            table.add_row(name, interval, flag, status)

        console.print(table)
        console.print(
            "\n[dim]Note: Tasks only run when both maintenance_enabled=true "
            "and the task's feature flag is enabled.[/]"
        )

    asyncio.run(_status())


@maintenance.command("run")
@click.argument(
    "task",
    type=click.Choice(
        ["consolidation", "dream", "episode-close", "decay", "all"],
    ),
)
def maintenance_run(task: str) -> None:
    """Manually run a maintenance task.

    Executes the task immediately and displays results.

    Examples:
        ncms maintenance run consolidation
        ncms maintenance run decay
        ncms maintenance run all
    """
    from rich.console import Console

    console = Console()

    async def _run() -> None:
        from ncms.application.consolidation_service import ConsolidationService
        from ncms.application.maintenance_scheduler import MaintenanceScheduler
        from ncms.config import NCMSConfig
        from ncms.infrastructure.graph.networkx_store import NetworkXGraph
        from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        config = NCMSConfig()

        # Stand up minimal services for the task
        store = SQLiteStore(db_path=config.db_path)
        await store.initialize()
        index = TantivyEngine(path=config.index_path)
        index.initialize()
        graph = NetworkXGraph()

        # SPLADE (optional)
        splade = None
        if config.splade_enabled:
            from ncms.infrastructure.indexing.splade_engine import SpladeEngine

            splade = SpladeEngine(
                model_name=config.splade_model,
                cache_dir=config.model_cache_dir,
            )

        consolidation_svc = ConsolidationService(
            store=store, index=index, graph=graph, config=config,
            splade=splade,
        )

        # Episode service (optional)
        episode_svc = None
        if config.episodes_enabled:
            from ncms.application.episode_service import EpisodeService

            episode_svc = EpisodeService(
                store=store, index=index, config=config, splade=splade,
            )

        # Rebuild graph for consolidation
        from ncms.application.graph_service import GraphService

        await GraphService(store=store, graph=graph).rebuild_from_store()

        scheduler = MaintenanceScheduler(
            consolidation_svc=consolidation_svc,
            episode_svc=episode_svc,
            config=config,
        )

        # Normalize task name (CLI uses hyphen, internal uses underscore)
        task_name = task.replace("-", "_")

        console.print(f"Running maintenance task: [bold]{task}[/]...")

        try:
            result = await scheduler.run_now(task_name)

            if "error" in result:
                console.print(f"[red]Error:[/] {result['error']}")
            else:
                for name, res in result.items():
                    console.print(f"  [green]{name}:[/] {res}")
        finally:
            await store.close()

    asyncio.run(_run())


@cli.command()
@click.option("--db", default=None, help="Database path")
def lint(db: str | None) -> None:
    """Run read-only diagnostics on the memory store.

    Detects orphan nodes, duplicates, junk entities, dangling
    references, and stale episodes. Returns exit code 1 if any
    errors are found.

    Examples:
        ncms lint
        ncms lint --db /path/to/ncms.db
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()

    async def _lint() -> None:
        from ncms.application.graph_service import GraphService
        from ncms.application.lint_service import LintService
        from ncms.config import NCMSConfig
        from ncms.infrastructure.graph.networkx_store import NetworkXGraph
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        config = NCMSConfig()
        if db:
            config.db_path = db

        store = SQLiteStore(db_path=config.db_path)
        await store.initialize()
        graph = NetworkXGraph()

        try:
            # Rebuild graph from store so entity-memory links are available
            await GraphService(store=store, graph=graph).rebuild_from_store()

            svc = LintService(store=store, graph=graph)
            report = await svc.run_full_lint()

            # Summary line
            n_errors = sum(1 for i in report.issues if i.severity == "error")
            n_warnings = sum(1 for i in report.issues if i.severity == "warning")
            n_info = sum(1 for i in report.issues if i.severity == "info")

            if not report.issues:
                console.print(
                    f"[green]No issues found.[/] "
                    f"({report.duration_ms:.0f}ms)"
                )
                return

            # Issues table
            table = Table(title="Lint Results")
            table.add_column("Severity", style="bold", width=8)
            table.add_column("Category", style="cyan", width=16)
            table.add_column("Message")

            severity_style = {
                "error": "red",
                "warning": "yellow",
                "info": "dim",
            }

            for issue in report.issues:
                style = severity_style.get(issue.severity, "")
                table.add_row(
                    f"[{style}]{issue.severity}[/]",
                    issue.category,
                    issue.message,
                )

            console.print(table)

            # Summary
            parts = []
            if n_errors:
                parts.append(f"[red]{n_errors} error(s)[/]")
            if n_warnings:
                parts.append(f"[yellow]{n_warnings} warning(s)[/]")
            if n_info:
                parts.append(f"[dim]{n_info} info[/]")
            console.print(
                f"\n{', '.join(parts)} "
                f"| {report.duration_ms:.0f}ms"
            )

            if n_errors > 0:
                raise SystemExit(1)
        finally:
            await store.close()

    asyncio.run(_lint())


@cli.command("topic-map")
@click.option("--db", default=None, help="Database path")
def topic_map(db: str | None) -> None:
    """Show emergent topic map from abstract memory clustering.

    Clusters L4 abstracts by shared entities to reveal knowledge themes.
    Requires at least one consolidation pass to have generated abstracts.

    Examples:
        ncms topic-map
        ncms topic-map --db /path/to/ncms.db
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()

    async def _topics() -> None:
        from ncms.config import NCMSConfig
        from ncms.infrastructure.graph.networkx_store import NetworkXGraph
        from ncms.infrastructure.storage.sqlite_store import SQLiteStore

        config = NCMSConfig(topic_map_enabled=True)
        if db:
            config.db_path = db

        store = SQLiteStore(db_path=config.db_path)
        await store.initialize()
        graph = NetworkXGraph()

        from ncms.application.memory_service import MemoryService
        from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine

        index = TantivyEngine(path=config.index_path)
        index.initialize()
        svc = MemoryService(store=store, index=index, graph=graph, config=config)

        try:
            clusters = await svc.get_topic_map()
            if not clusters:
                console.print("[yellow]No topic clusters found.[/]")
                console.print("Run consolidation first to generate abstracts.")
                return

            table = Table(title="Emergent Topic Map")
            table.add_column("Topic", style="bold")
            table.add_column("Entities", style="cyan")
            table.add_column("Abstracts", justify="right")
            table.add_column("Episodes", justify="right")
            table.add_column("Confidence", justify="right")

            for c in clusters:
                table.add_row(
                    c.label,
                    ", ".join(c.entity_keys[:3]),
                    str(len(c.abstract_ids)),
                    str(len(c.episode_ids)),
                    f"{c.confidence:.1%}",
                )

            console.print(table)
        finally:
            await store.close()

    asyncio.run(_topics())


@cli.command()
@click.option("--output", "-o", required=True, type=click.Path(), help="Output directory for wiki.")
def export(output: str) -> None:
    """Export memory store as a linked markdown wiki."""
    import asyncio
    from pathlib import Path

    from rich.console import Console

    from ncms.config import NCMSConfig
    from ncms.interfaces.cli.export import export_wiki

    console = Console()
    output_path = Path(output)
    config = NCMSConfig()

    async def _export() -> None:
        console.print(f"[bold]Exporting wiki to {output_path}...[/]")
        counts = await export_wiki(config, output_path)
        total = sum(counts.values())
        console.print(f"[green]✓ Exported {total} pages:[/]")
        for category, count in counts.items():
            console.print(f"  {category}: {count}")
        console.print(f"\nOpen {output_path / 'index.md'} to browse.")

    asyncio.run(_export())


if __name__ == "__main__":
    cli()
