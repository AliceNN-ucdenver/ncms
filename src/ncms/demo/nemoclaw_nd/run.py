"""NemoClaw Non-Deterministic Demo - LLM-powered multi-agent design session.

Three LLM agents (Architect, Security, Builder) reason autonomously over
real governance-mesh knowledge files. The Builder drives a work loop,
consulting Architecture and Security agents via the Knowledge Bus.

Run: uv run ncms demo --nemoclaw-nd
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ncms.application.bus_service import BusService
from ncms.application.knowledge_loader import KnowledgeLoader
from ncms.application.memory_service import MemoryService
from ncms.application.snapshot_service import SnapshotService
from ncms.config import NCMSConfig
from ncms.demo.nemoclaw_nd.architect_agent import ArchitectAgent
from ncms.demo.nemoclaw_nd.builder_agent import BuilderAgent
from ncms.demo.nemoclaw_nd.security_agent import SecurityAgent
from ncms.infrastructure.bus.async_bus import AsyncKnowledgeBus
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)
console = Console()

# ── Governance-mesh knowledge files ─────────────────────────────────────

_GOV_BASE = Path.home() / "Documents" / "governance-mesh"
_APP_BASE = (
    _GOV_BASE / "platforms" / "imdb-lite" / "bars" / "imdb-lite-application"
)

KNOWLEDGE_FILES: list[tuple[str, list[str]]] = [
    # Architecture files
    (
        str(_APP_BASE / "architecture" / "bar.arch.json"),
        ["architecture", "calm-model"],
    ),
    (
        str(_APP_BASE / "architecture" / "ADRs" / "001-initial-architecture.md"),
        ["architecture", "decisions"],
    ),
    (
        str(_APP_BASE / "architecture" / "ADRs" / "002-mongodb-document-store.md"),
        ["architecture", "decisions"],
    ),
    (
        str(_APP_BASE / "architecture" / "ADRs" / "003-jwt-rbac-authentication.md"),
        ["architecture", "decisions", "security"],
    ),
    (
        str(_APP_BASE / "architecture" / "ADRs" / "004-mongodb-memory-server-testing.md"),
        ["architecture", "decisions"],
    ),
    (
        str(_APP_BASE / "architecture" / "quality-attributes.yaml"),
        ["architecture", "quality"],
    ),
    (
        str(_APP_BASE / "architecture" / "fitness-functions.yaml"),
        ["architecture", "quality"],
    ),
    (
        str(_GOV_BASE / ".caterpillar" / "prompts" / "architecture.md"),
        ["architecture", "calm-model"],
    ),
    # Security files
    (
        str(_APP_BASE / "security" / "threat-model.yaml"),
        ["security", "threats"],
    ),
    (
        str(_APP_BASE / "security" / "security-controls.yaml"),
        ["security", "controls"],
    ),
    (
        str(_APP_BASE / "security" / "compliance-checklist.yaml"),
        ["security", "compliance"],
    ),
    (
        str(_GOV_BASE / ".caterpillar" / "prompts" / "application-security.md"),
        ["security", "threats", "controls"],
    ),
    # App metadata
    (
        str(_APP_BASE / "app.yaml"),
        ["architecture", "identity-service"],
    ),
]


# ── Output helpers ──────────────────────────────────────────────────────


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


# ── Main demo ───────────────────────────────────────────────────────────


async def run_nemoclaw_nd_demo() -> None:
    """Run the NemoClaw Non-Deterministic multi-agent demo."""
    t0 = time.monotonic()

    console.print(Panel(
        "[bold white]NeMo Cognitive Memory System x NemoClaw ND[/]\n"
        "[dim]Non-Deterministic Multi-Agent Design | LLM-Powered Reasoning[/]\n\n"
        "Three LLM agents (Architect, Security, Builder) reason autonomously\n"
        "over real governance-mesh knowledge files. The Builder drives a work\n"
        "loop, consulting the other agents via the NCMS Knowledge Bus.\n\n"
        "[dim]LLM Backend: DGX Spark (Nemotron) or configured endpoint[/]",
        title="[bold cyan]NemoClaw Non-Deterministic Demo[/]",
        border_style="cyan",
        padding=(1, 2),
    ))

    # ── Phase 0: Infrastructure Setup ──────────────────────────────────
    header("Phase 0: Infrastructure Setup")

    config = NCMSConfig(
        db_path=":memory:",
        actr_noise=0.0,
    )

    store = SQLiteStore(db_path=":memory:")
    await store.initialize()

    index = TantivyEngine()
    index.initialize()

    graph = NetworkXGraph()
    bus = AsyncKnowledgeBus(ask_timeout_ms=config.bus_ask_timeout_ms)

    # SPLADE (disabled by default)
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

    # Reconciliation (Phase 2, disabled by default)
    reconciliation = None
    if config.reconciliation_enabled:
        from ncms.application.reconciliation_service import ReconciliationService

        reconciliation = ReconciliationService(store=store, config=config)

    # Episodes (Phase 3)
    config.episodes_enabled = True
    config.episode_create_min_entities = 1

    from ncms.application.episode_service import EpisodeService

    episode_svc = EpisodeService(
        store=store, index=index, config=config, splade=splade,
    )

    # Intent classifier (Phase 4)
    config.intent_classification_enabled = True
    intent_classifier = None
    try:
        from ncms.infrastructure.indexing.exemplar_intent_index import ExemplarIntentIndex

        intent_classifier = ExemplarIntentIndex()
    except Exception:
        pass

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
        reconciliation=reconciliation, episode=episode_svc,
        intent_classifier=intent_classifier,
        reranker=reranker,
    )
    snapshot_svc = SnapshotService(store=store, max_entries=50, ttl_hours=168)
    bus_svc = BusService(bus=bus, snapshot_service=snapshot_svc)

    step("SQLite (in-memory) initialized")
    step("Tantivy BM25 index created")
    step("NetworkX knowledge graph ready")
    step("AsyncIO Knowledge Bus started")
    step("Episode formation enabled")
    step("Intent classification enabled")
    if splade:
        step("SPLADE sparse neural retrieval enabled")
    result("All services online")

    # Resolve LLM configuration
    llm_model = config.llm_model
    llm_api_base = config.llm_api_base
    step(f"LLM model: {llm_model}")
    step(f"LLM API base: {llm_api_base or '(default)'}")

    # ── Phase 1: Knowledge Loading ─────────────────────────────────────
    header("Phase 1: Knowledge Loading")

    loader = KnowledgeLoader(memory_svc)
    total_memories = 0
    total_files = 0
    total_errors: list[str] = []

    for file_path, domains in KNOWLEDGE_FILES:
        p = Path(file_path)
        if not p.exists():
            warn(f"File not found, skipping: {p.name}")
            continue

        stats = await loader.load_file(
            file_path, domains=domains, source_agent="knowledge-loader", importance=7.0,
        )
        total_files += stats.files_processed
        total_memories += stats.memories_created
        total_errors.extend(stats.errors)

        if stats.memories_created > 0:
            step(f"Loaded {p.name} -> {stats.memories_created} memories [{', '.join(domains)}]")
        for err in stats.errors:
            warn(f"  Error: {err}")

    result(f"Knowledge loaded: {total_files} files -> {total_memories} memories")
    if total_errors:
        warn(f"  {len(total_errors)} error(s) during loading")

    # Show knowledge graph stats
    entity_count = memory_svc.entity_count()
    rel_count = memory_svc.relationship_count()
    result(f"Knowledge graph: {entity_count} entities, {rel_count} relationships")

    # ── Phase 2: Agent Registration ────────────────────────────────────
    header("Phase 2: Agent Registration")

    # Create agents with LLM config
    architect = ArchitectAgent("architect-agent", bus_svc, memory_svc, snapshot_svc)
    architect.llm_model = llm_model
    architect.llm_api_base = llm_api_base

    security = SecurityAgent("security-agent", bus_svc, memory_svc, snapshot_svc)
    security.llm_model = llm_model
    security.llm_api_base = llm_api_base

    builder = BuilderAgent("builder-agent", bus_svc, memory_svc, snapshot_svc)
    builder.llm_model = llm_model
    builder.llm_api_base = llm_api_base

    await architect.start()
    step("Architect Agent registered -> domains: architecture, calm-model, quality, decisions")

    await security.start()
    step("Security Agent registered -> domains: security, threats, compliance, controls")

    await builder.start()
    step("Builder Agent registered -> domains: identity-service, implementation")

    # Show agent table
    agents = bus_svc.get_all_agents()
    table = Table(title="Registered Agents", box=box.ROUNDED)
    table.add_column("Agent", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Domains", style="yellow")
    for a in agents:
        table.add_row(a.agent_id, a.status, ", ".join(a.domains))
    console.print(table)

    # ── Phase 3: Builder Work Loop ─────────────────────────────────────
    header("Phase 3: Builder Work Loop (Non-Deterministic)")

    step("Builder Agent begins autonomous design of imdb-identity-service...")
    step("(LLM decides what to ask/decide each turn)")
    console.print()

    async def on_turn(turn_num: int, turn_record: dict[str, str]) -> None:
        """Live output callback for each turn."""
        action = turn_record.get("action", "unknown")
        if action.startswith("ask_"):
            target = action[4:]
            console.print(Panel(
                f"[bold]Question:[/] {turn_record.get('question', '')[:200]}\n\n"
                f"[bold]Answer:[/] {turn_record.get('answer', '')[:300]}\n"
                f"[dim]Confidence: {turn_record.get('confidence', '?')}[/]",
                title=f"[cyan]Turn {turn_num}: Ask {target.title()} Agent[/]",
                border_style="cyan",
            ))
        elif action == "decide":
            console.print(Panel(
                turn_record.get("detail", "")[:400],
                title=f"[green]Turn {turn_num}: Design Decision[/]",
                border_style="green",
            ))
        elif action == "announce":
            console.print(Panel(
                turn_record.get("detail", "")[:400],
                title=f"[yellow]Turn {turn_num}: Announcement[/]",
                border_style="yellow",
            ))
        elif action == "done":
            console.print(Panel(
                turn_record.get("detail", "")[:400],
                title=f"[bold green]Turn {turn_num}: Design Complete[/]",
                border_style="green",
            ))
        elif action == "error":
            console.print(Panel(
                turn_record.get("detail", "LLM call failed"),
                title=f"[red]Turn {turn_num}: Error[/]",
                border_style="red",
            ))

    try:
        turns = await builder.work_loop(max_turns=8, on_turn=on_turn)
        result(f"Builder completed {len(turns)} turn(s)")
    except Exception as e:
        warn(f"Builder work loop failed: {e}")
        logger.exception("Builder work loop error")
        turns = []

    # ── Phase 4: Announcement Round ────────────────────────────────────
    header("Phase 4: Final Announcements")

    # Builder announces final design
    step("Builder Agent announces final design to all agents...")
    try:
        summary = builder._build_summary()  # noqa: SLF001
        await builder.announce_knowledge(
            event="created",
            domains=["identity-service", "implementation", "architecture"],
            content=summary[:1000],
        )
        result("Final design announced to Knowledge Bus")
    except Exception as e:
        warn(f"Announcement failed: {e}")

    # Let bus deliver
    await asyncio.sleep(0.2)

    # Process announcements at architect and security
    for agent, name in [(architect, "Architect"), (security, "Security")]:
        ann_list = await bus_svc.drain_announcements(agent.agent_id)
        if ann_list:
            step(f"{name} Agent received {len(ann_list)} announcement(s)")
        else:
            step(f"{name} Agent: no announcements in queue")

    # ── Phase 5: Cleanup ───────────────────────────────────────────────
    header("Phase 5: Cleanup")

    await builder.shutdown()
    step("Builder Agent shut down")
    await architect.shutdown()
    step("Architect Agent shut down")
    await security.shutdown()
    step("Security Agent shut down")
    await store.close()
    step("Store closed")

    # ── Summary ────────────────────────────────────────────────────────
    header("Demo Summary")

    elapsed = time.monotonic() - t0

    summary_table = Table(box=box.ROUNDED)
    summary_table.add_column("Metric", style="bold")
    summary_table.add_column("Value", style="cyan")
    summary_table.add_row("Knowledge Files Loaded", str(total_files))
    summary_table.add_row("Memories Created", str(total_memories))
    summary_table.add_row("Knowledge Graph Entities", str(entity_count))
    summary_table.add_row("Graph Relationships", str(rel_count))
    summary_table.add_row("Builder Turns", str(len(turns)))
    summary_table.add_row(
        "Actions",
        ", ".join(
            f"{t.get('action', '?')}" for t in turns
        ) if turns else "(none)",
    )
    summary_table.add_row("LLM Model", llm_model)
    summary_table.add_row("Elapsed Time", f"{elapsed:.1f}s")
    console.print(summary_table)

    console.print()
    console.print(Panel(
        "[bold]What you just saw:[/]\n\n"
        "1. Real governance-mesh files loaded into NCMS cognitive memory\n"
        "2. Three LLM agents registered with domain expertise\n"
        "3. Builder Agent autonomously designed imdb-identity-service\n"
        "4. Each turn: LLM chose to ask Architecture, ask Security, or decide\n"
        "5. Agents answered via memory search + LLM synthesis (non-deterministic)\n"
        "6. Final design announced through Knowledge Bus\n\n"
        f"[dim]Total time: {elapsed:.1f}s | LLM: {llm_model}[/]",
        title="[bold cyan]NemoClaw ND - Non-Deterministic Multi-Agent Design[/]",
        border_style="cyan",
    ))


if __name__ == "__main__":
    asyncio.run(run_nemoclaw_nd_demo())
