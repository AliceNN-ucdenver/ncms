"""NCMS Interactive Demo - End-to-end collaborative agent scenario.

Demonstrates:
1. Three agents collaborating via Knowledge Bus
2. Agent sleep/wake with surrogate response
3. Breaking change announcement flow
4. Memory search with ACT-R activation scoring

Run: uv run ncms demo
"""

from __future__ import annotations

import asyncio

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ncms.application.bus_service import BusService
from ncms.application.memory_service import MemoryService
from ncms.application.snapshot_service import SnapshotService
from ncms.config import NCMSConfig
from ncms.demo.agents.api_agent import ApiAgent
from ncms.demo.agents.database_agent import DatabaseAgent
from ncms.demo.agents.frontend_agent import FrontendAgent
from ncms.infrastructure.bus.async_bus import AsyncKnowledgeBus
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

console = Console()


def header(title: str) -> None:
    console.print()
    console.rule(f"[bold cyan]{title}[/]", style="cyan")
    console.print()


def step(msg: str) -> None:
    console.print(f"  [dim]>[/] {msg}")


def result(msg: str) -> None:
    console.print(f"  [green]{msg}[/]")


def warn(msg: str) -> None:
    console.print(f"  [yellow]{msg}[/]")


async def run_demo() -> None:
    console.print(
        Panel(
            "[bold white]NeMo Cognitive Memory System[/]\n"
            "[dim]Vector-Free Retrieval | Embedded Knowledge Bus | Agent Snapshots[/]\n\n"
            "This demo shows three collaborative agents sharing knowledge\n"
            "through the NCMS Knowledge Bus, including sleep/wake cycles\n"
            "with surrogate responses and breaking change propagation.",
            title="[bold cyan]NCMS Demo[/]",
            border_style="cyan",
            padding=(1, 2),
        )
    )

    # ── Initialize System ────────────────────────────────────────────
    header("System Initialization")

    config = NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,  # Deterministic for demo
    )

    store = SQLiteStore(db_path=":memory:")
    await store.initialize()

    index = TantivyEngine()
    index.initialize()

    graph = NetworkXGraph()
    bus = AsyncKnowledgeBus(ask_timeout_ms=2000)

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

        admission = AdmissionService(store=store, index=index, graph=graph, config=config)

    # Reconciliation service (Phase 2, disabled by default)
    reconciliation = None
    if config.temporal_enabled:
        from ncms.application.reconciliation_service import ReconciliationService

        reconciliation = ReconciliationService(store=store, config=config)

    # Episode formation (Phase 3, disabled by default)
    episode = None
    if config.temporal_enabled:
        from ncms.application.episode_service import EpisodeService

        episode = EpisodeService(
            store=store, index=index, config=config, splade=splade,
        )

    # Intent classifier (Phase 4, disabled by default)
    intent_classifier = None
    if config.temporal_enabled:
        from ncms.infrastructure.indexing.exemplar_intent_index import (
            ExemplarIntentIndex,
        )

        intent_classifier = ExemplarIntentIndex()

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
        intent_classifier=intent_classifier,
        reranker=reranker,
    )
    snapshot_svc = SnapshotService(store=store, max_entries=50, ttl_hours=168)
    bus_svc = BusService(bus=bus, snapshot_service=snapshot_svc)

    # Start background indexing if enabled (default: True)
    await memory_svc.start_index_pool()

    step("SQLite (in-memory) initialized")
    step("Tantivy BM25 index created")
    step("NetworkX knowledge graph ready")
    step("AsyncIO Knowledge Bus started")
    if memory_svc._index_pool is not None:
        step("Background indexing pool started")
    if splade:
        step("SPLADE sparse neural retrieval enabled")
    if admission:
        step("Admission scoring enabled")
    if reconciliation:
        step("Reconciliation service enabled")
    if episode:
        step("Episode formation enabled")
    if intent_classifier:
        step("Intent classification enabled")
    result("All services online")

    # ── Create Agents ────────────────────────────────────────────────
    header("Agent Registration")

    api_agent = ApiAgent("api-agent", bus_svc, memory_svc, snapshot_svc)
    frontend_agent = FrontendAgent("frontend-agent", bus_svc, memory_svc, snapshot_svc)
    db_agent = DatabaseAgent("db-agent", bus_svc, memory_svc, snapshot_svc)

    await api_agent.start()
    await frontend_agent.start()
    await db_agent.start()

    agents_table = Table(box=box.ROUNDED, show_header=True, header_style="bold")
    agents_table.add_column("Agent", style="bold white")
    agents_table.add_column("Status", style="green")
    agents_table.add_column("Expertise Domains")
    agents_table.add_column("Subscriptions")

    agents_table.add_row(
        "api-agent", "ONLINE",
        "api, api:user-service, api:auth-service",
        "db, db:user-schema, config",
    )
    agents_table.add_row(
        "frontend-agent", "ONLINE",
        "frontend, ui:components, ui:pages",
        "api, api:user-service, api:auth-service",
    )
    agents_table.add_row(
        "db-agent", "ONLINE",
        "db, db:user-schema, db:auth-schema, db:migrations",
        "api, config",
    )
    console.print(agents_table)

    # ── Phase 0: Matrix Knowledge Download ──────────────────────────
    header("Phase 0: Matrix Knowledge Download")

    from ncms.application.knowledge_loader import KnowledgeLoader

    loader = KnowledgeLoader(memory_svc, chunk_max_chars=1500)

    # Seed architecture knowledge from inline text (like downloading to Neo)
    architecture_doc = """\
# ACME Platform Architecture

## API Gateway
The API gateway runs on Express.js behind an NGINX reverse proxy.
All endpoints are versioned under /api/v2/. Authentication uses JWT tokens
with 1-hour expiry. Rate limiting is applied at the gateway level:
100 requests/minute for authenticated users, 20 for anonymous.

## Database Layer
PostgreSQL 15 with read replicas. Connection pooling via PgBouncer.
The users table is the core entity, referenced by auth_tokens, profiles,
and user_preferences. All timestamps use UTC with timezone.

## Frontend Stack
React 18 with TypeScript. State management via React Query for server state
and Zustand for client state. Component library: Shadcn/UI.
Deployment: Vercel with edge functions for SSR.
"""

    stats = await loader.load_text(
        architecture_doc,
        domains=["architecture", "platform"],
        source_agent="knowledge-loader",
        project="acme-platform",
    )
    step(f"Downloaded architecture knowledge: {stats.memories_created} memories loaded")
    result("\"I know kung fu.\" -- Neo")

    # ── Phase 1: Store Initial Knowledge ─────────────────────────────
    header("Phase 1: Agents Store Domain Knowledge")

    await api_agent.store_knowledge(
        "GET /api/v2/users returns paginated user list. Response: "
        '{"users": [{"id": int, "name": str, "email": str}], '
        '"cursor": str, "has_more": bool}. Supports cursor-based pagination.',
        domains=["api", "api:user-service"],
        memory_type="interface-spec",
        structured={
            "method": "GET",
            "path": "/api/v2/users",
            "response": {"users": "User[]", "cursor": "string", "has_more": "boolean"},
        },
    )
    step("[api-agent] Stored: GET /api/v2/users endpoint spec")

    await api_agent.store_knowledge(
        "POST /api/v2/auth/login accepts {email, password} and returns "
        '{"token": str, "expires_in": int}. Rate limited to 5 attempts per minute.',
        domains=["api", "api:auth-service"],
        memory_type="interface-spec",
    )
    step("[api-agent] Stored: POST /api/v2/auth/login endpoint spec")

    await frontend_agent.store_knowledge(
        "UserList component fetches from GET /api/v2/users with infinite scroll. "
        "Uses React Query for caching with 30s stale time. "
        "Renders UserCard sub-components for each user.",
        domains=["frontend", "ui:components"],
        memory_type="code-pattern",
    )
    step("[frontend-agent] Stored: UserList component pattern")

    await db_agent.store_knowledge(
        "users table schema: id SERIAL PRIMARY KEY, name VARCHAR(255) NOT NULL, "
        "email VARCHAR(255) UNIQUE NOT NULL, created_at TIMESTAMP DEFAULT NOW(). "
        "Indexed on email for login lookups.",
        domains=["db", "db:user-schema"],
        memory_type="interface-spec",
        structured={
            "table": "users",
            "columns": {
                "id": "SERIAL PRIMARY KEY",
                "name": "VARCHAR(255) NOT NULL",
                "email": "VARCHAR(255) UNIQUE NOT NULL",
                "created_at": "TIMESTAMP DEFAULT NOW()",
            },
            "indexes": ["email"],
        },
    )
    step("[db-agent] Stored: users table schema")

    await db_agent.store_knowledge(
        "auth_tokens table: id SERIAL PRIMARY KEY, user_id INT REFERENCES users(id), "
        "token VARCHAR(512) NOT NULL, expires_at TIMESTAMP NOT NULL. "
        "Tokens are JWT with 1-hour expiry.",
        domains=["db", "db:auth-schema"],
        memory_type="interface-spec",
    )
    step("[db-agent] Stored: auth_tokens table schema")

    result(f"Total memories stored: {await memory_svc.memory_count()}")

    # Show auto-extracted entities
    entity_table = Table(
        box=box.ROUNDED,
        title="Auto-Extracted Knowledge Graph Entities",
        show_header=True,
        header_style="bold",
    )
    entity_table.add_column("Entity", style="bold white")
    entity_table.add_column("Type", style="cyan")

    all_entities = await memory_svc.list_entities()
    # Show up to 15 entities, sorted by type
    for entity in sorted(all_entities, key=lambda e: e.type)[:15]:
        entity_table.add_row(entity.name, entity.type)
    if len(all_entities) > 15:
        entity_table.add_row(f"... +{len(all_entities) - 15} more", "[dim]...[/]")

    console.print(entity_table)
    result(
        f"Knowledge graph: {memory_svc.entity_count()} entities, "
        f"{memory_svc.relationship_count()} relationships"
    )

    # ── Phase 2: Live Knowledge Exchange ─────────────────────────────
    header("Phase 2: Live Knowledge Exchange")

    step("[frontend-agent] Asking: What format does the user list endpoint return?")

    response = await frontend_agent.ask_knowledge(
        question="What format does the user list endpoint return? What fields are in the response?",
        domains=["api", "api:user-service"],
    )

    if response:
        console.print(Panel(
            f"[bold]From:[/] {response.from_agent}\n"
            f"[bold]Confidence:[/] {response.confidence:.2f}\n"
            f"[bold]Mode:[/] {response.source_mode}\n"
            f"[bold]Content:[/] {response.knowledge.content[:200]}",
            title="[green]Knowledge Response[/]",
            border_style="green",
        ))
    else:
        warn("No response received")

    # ── Phase 3: Agent Sleep & Surrogate Response ────────────────────
    header("Phase 3: Agent Sleep & Surrogate Response")

    step("[api-agent] Going to sleep... publishing snapshot")
    snapshot = await api_agent.sleep()
    result(f"Snapshot published: {len(snapshot.entries)} entries, domains: {snapshot.domains}")

    # Show agent status
    status_table = Table(box=box.ROUNDED, title="Agent Status After Sleep")
    status_table.add_column("Agent", style="bold")
    status_table.add_column("Status")
    for agent_info in bus_svc.get_all_agents():
        color = "green" if agent_info.status == "online" else "yellow"
        status_table.add_row(agent_info.agent_id, f"[{color}]{agent_info.status}[/]")
    console.print(status_table)

    step("[frontend-agent] Asking sleeping api-agent: What is the current API version?")

    response2 = await frontend_agent.ask_knowledge(
        question="What is the current API version and what does the users endpoint return?",
        domains=["api", "api:user-service"],
    )

    if response2:
        mode_color = "yellow" if response2.source_mode == "warm" else "green"
        console.print(Panel(
            f"[bold]From:[/] {response2.from_agent}\n"
            f"[bold]Mode:[/] [{mode_color}]{response2.source_mode.upper()}[/] "
            f"(surrogate from snapshot)\n"
            f"[bold]Confidence:[/] {response2.confidence:.2f} "
            f"[dim](discounted for surrogate)[/]\n"
            f"[bold]Staleness:[/] {response2.staleness_warning or 'Fresh'}\n"
            f"[bold]Content:[/] {response2.knowledge.content[:200]}",
            title="[yellow]Surrogate Response (Warm Mode)[/]",
            border_style="yellow",
        ))
    else:
        warn("No surrogate response available")

    # Wake up the agent
    step("[api-agent] Waking up...")
    await api_agent.wake()
    result("api-agent is back online with restored context")

    # Ask again - should get live response
    step("[frontend-agent] Asking same question to now-awake api-agent")
    response3 = await frontend_agent.ask_knowledge(
        question="What is the current API version and what does the users endpoint return?",
        domains=["api", "api:user-service"],
    )

    if response3:
        console.print(Panel(
            f"[bold]From:[/] {response3.from_agent}\n"
            f"[bold]Mode:[/] [green]{response3.source_mode.upper()}[/] (direct response)\n"
            f"[bold]Confidence:[/] {response3.confidence:.2f}\n"
            f"[bold]Content:[/] {response3.knowledge.content[:200]}",
            title="[green]Live Response[/]",
            border_style="green",
        ))

    # ── Phase 4: Breaking Change Announcement ────────────────────────
    header("Phase 4: Breaking Change Announcement")

    step("[db-agent] Announcing: Adding required 'role' column to users table")

    await db_agent.announce_knowledge(
        event="breaking-change",
        domains=["db", "db:user-schema"],
        content="users table: Adding required 'role' column (VARCHAR(50) NOT NULL "
        "DEFAULT 'user'). All INSERT queries must include role field. "
        "Existing rows will be migrated with DEFAULT 'user'. "
        "API responses should include the new role field.",
        breaking=True,
        severity="critical",
    )
    result("Breaking change announced to Knowledge Bus")

    # Process announcements
    await asyncio.sleep(0.1)  # Let the bus deliver

    api_announcements = await bus_svc.drain_announcements("api-agent")
    frontend_announcements = await bus_svc.drain_announcements("frontend-agent")

    ann_table = Table(box=box.ROUNDED, title="Announcement Delivery")
    ann_table.add_column("Agent", style="bold")
    ann_table.add_column("Received", justify="center")
    ann_table.add_column("Action")

    ann_table.add_row(
        "api-agent",
        f"[green]{len(api_announcements)} announcement(s)[/]",
        "Will update /users response to include role field",
    )
    ann_table.add_row(
        "frontend-agent",
        f"[green]{len(frontend_announcements)} announcement(s)[/]",
        "Will add role field to UserCard component",
    )
    ann_table.add_row(
        "db-agent",
        "[dim]sender (self)[/]",
        "Published the breaking change",
    )
    console.print(ann_table)

    # ── Phase 5: Memory Search with ACT-R Scoring ────────────────────
    header("Phase 5: Memory Search with ACT-R Activation Scoring")

    step('Searching all memories for "users"...')

    search_results = await memory_svc.search(query="users endpoint schema", limit=8)

    results_table = Table(
        box=box.ROUNDED,
        title="Search Results: 'users endpoint schema'  (3-Tier Pipeline)",
        show_header=True,
        header_style="bold",
    )
    results_table.add_column("#", justify="right", style="dim")
    results_table.add_column("Content", max_width=45)
    results_table.add_column("Agent", style="cyan")
    results_table.add_column("BM25", justify="right")
    results_table.add_column("Base", justify="right")
    results_table.add_column("Spread", justify="right", style="magenta")
    results_table.add_column("RetProb", justify="right")
    results_table.add_column("Total", justify="right", style="bold green")

    for i, sr in enumerate(search_results, 1):
        content_preview = (
            sr.memory.content[:45] + "..."
            if len(sr.memory.content) > 45
            else sr.memory.content
        )
        spread_style = "bold magenta" if sr.spreading > 0 else "dim"
        results_table.add_row(
            str(i),
            content_preview,
            sr.memory.source_agent or "?",
            f"{sr.bm25_score:.2f}",
            f"{sr.base_level:.2f}",
            Text(f"{sr.spreading:.2f}", style=spread_style),
            f"{sr.retrieval_prob:.2f}",
            f"{sr.total_activation:.2f}",
        )

    console.print(results_table)

    # ── Phase 6: Intent-Aware Search (if enabled) ─────────────────────
    if config.temporal_enabled:
        header("Phase 6: Intent-Aware Search")

        step('Searching with intent override: current_state_lookup')
        intent_results = await memory_svc.search(
            "What is the current auth token format?",
            intent_override="current_state_lookup",
            limit=3,
        )
        for ir in intent_results:
            result(
                f"  [{ir.intent}] {ir.memory.content[:60]}... "
                f"(hierarchy_bonus={ir.hierarchy_bonus:.2f})"
            )

        step('Searching with intent override: event_reconstruction')
        intent_results2 = await memory_svc.search(
            "What happened with the users table schema change?",
            intent_override="event_reconstruction",
            limit=3,
        )
        for ir in intent_results2:
            result(
                f"  [{ir.intent}] {ir.memory.content[:60]}... "
                f"(hierarchy_bonus={ir.hierarchy_bonus:.2f})"
            )
    else:
        header("Phase 6: Intent-Aware Search [dim](skipped — not enabled)[/]")
        step("Set NCMS_TEMPORAL_ENABLED=true to enable")

    # ── Summary ──────────────────────────────────────────────────────
    header("Demo Summary")

    summary_table = Table(box=box.ROUNDED)
    summary_table.add_column("Metric", style="bold")
    summary_table.add_column("Value", style="cyan")

    mem_count = await memory_svc.memory_count()
    summary_table.add_row("Total Memories", str(mem_count))
    summary_table.add_row("Knowledge Graph Entities", str(memory_svc.entity_count()))
    summary_table.add_row("Graph Relationships", str(memory_svc.relationship_count()))
    summary_table.add_row("Registered Agents", str(len(bus_svc.get_all_agents())))
    summary_table.add_row("Knowledge Domains", str(len(bus_svc.list_domains())))
    summary_table.add_row("Live Asks Completed", "2")
    summary_table.add_row("Surrogate Responses", "1")
    summary_table.add_row("Breaking Changes", "1")

    console.print(summary_table)

    console.print()
    console.print(Panel(
        "[bold]What you just saw:[/]\n\n"
        "1. Knowledge downloaded Matrix-style and auto-indexed with entity extraction\n"
        "2. Three agents stored domain knowledge (entities auto-extracted to graph)\n"
        "3. Frontend agent asked API agent via the Knowledge Bus\n"
        "4. API agent slept; frontend got a [yellow]surrogate response[/] from snapshot\n"
        "5. API agent woke up and answered [green]live[/] on the same question\n"
        "6. Database agent announced a [red]breaking change[/] to subscribers\n"
        "7. 3-tier search: [cyan]BM25 + Spreading Activation + ACT-R scoring[/]\n\n"
        "[dim]All running in-process with zero external dependencies.[/]",
        title="[bold cyan]NCMS - It Just Works[/]",
        border_style="cyan",
    ))

    # Cleanup
    await memory_svc.stop_index_pool()
    await api_agent.shutdown()
    await frontend_agent.shutdown()
    await db_agent.shutdown()
    await store.close()


if __name__ == "__main__":
    asyncio.run(run_demo())
