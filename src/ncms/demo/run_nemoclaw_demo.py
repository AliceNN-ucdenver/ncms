"""NCMS NemoClaw Demo - Multi-agent cognitive memory with Knowledge Bus.

Demonstrates:
1. Three domain agents (Code, Ops, Security) sharing knowledge
2. Cross-agent collaboration via Knowledge Bus
3. Breaking change propagation and state reconciliation
4. Agent sleep/wake with surrogate responses
5. Structured recall with episode context
6. Dream cycle consolidation

Run: uv run ncms demo --nemoclaw
"""

from __future__ import annotations

import asyncio

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ncms.application.bus_service import BusService
from ncms.application.memory_service import MemoryService
from ncms.application.snapshot_service import SnapshotService
from ncms.config import NCMSConfig
from ncms.demo.agents.base_demo import DemoAgent
from ncms.domain.models import (
    ImpactAssessment,
    KnowledgeAnnounce,
    KnowledgeAsk,
    KnowledgePayload,
)
from ncms.infrastructure.bus.async_bus import AsyncKnowledgeBus
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore

console = Console()


# ── Output helpers (same pattern as run_demo.py) ──────────────────────────


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


# ── Specialized NemoClaw Agents ───────────────────────────────────────────


class CodeAgent(DemoAgent):
    """Agent responsible for backend code, API design, and architecture."""

    primary_domain = "backend"
    knowledge_type = "code-snippet"
    trust_level = "authoritative"
    max_confidence = 0.95
    snapshot_confidence = 0.9
    snapshot_volatility = "changing"
    include_structured_in_snapshot = True
    include_references_in_response = True

    def declare_expertise(self) -> list[str]:
        return ["backend", "api", "architecture"]

    def declare_subscriptions(self) -> list[str]:
        return ["ops", "security", "architecture"]


class OpsAgent(DemoAgent):
    """Agent responsible for deployments, monitoring, and incident response."""

    primary_domain = "ops"
    knowledge_type = "fact"
    trust_level = "authoritative"
    max_confidence = 0.95
    snapshot_confidence = 0.85
    snapshot_volatility = "changing"
    include_structured_in_snapshot = True
    include_references_in_response = True

    def declare_expertise(self) -> list[str]:
        return ["ops", "monitoring", "incidents"]

    def declare_subscriptions(self) -> list[str]:
        return ["backend", "api", "releases"]


class SecurityAgent(DemoAgent):
    """Agent responsible for auth policies, compliance, and security audits."""

    primary_domain = "security"
    knowledge_type = "constraint"
    trust_level = "authoritative"
    max_confidence = 0.95
    snapshot_confidence = 0.9
    snapshot_volatility = "stable"
    include_structured_in_snapshot = True
    include_references_in_response = True

    def declare_expertise(self) -> list[str]:
        return ["security", "auth", "compliance"]

    def declare_subscriptions(self) -> list[str]:
        return ["api", "ops", "architecture"]


def _surrogate_label(mode: str) -> str:
    return "discounted for surrogate" if mode == "warm" else "full confidence"


# ── Main Demo ─────────────────────────────────────────────────────────────


async def run_nemoclaw_demo() -> None:
    """Run the NemoClaw multi-agent demo end-to-end."""
    console.print(Panel(
        "[bold white]NeMo Cognitive Memory System x NemoClaw[/]\n"
        "[dim]Multi-Agent Knowledge Bus | Surrogate Responses | Structured Recall[/]\n\n"
        "This demo shows three domain agents (Code, Ops, Security) collaborating\n"
        "through the NCMS Knowledge Bus with sleep/wake lifecycle,\n"
        "surrogate responses, and structured recall with episode context.",
        title="[bold cyan]NCMS NemoClaw Demo[/]",
        border_style="cyan",
        padding=(1, 2),
    ))

    # ── System Initialization ─────────────────────────────────────────
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

    # Episode formation (Phase 3) - enable for richer demo
    config.temporal_enabled = True
    config.episode_create_min_entities = 1

    from ncms.application.episode_service import EpisodeService

    episode_svc = EpisodeService(
        store=store, index=index, config=config, splade=splade,
    )

    # Intent classifier (Phase 4) - enable for richer demo
    config.temporal_enabled = True

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

    # Create agents
    code_agent = CodeAgent("code-agent", bus_svc, memory_svc, snapshot_svc)
    ops_agent = OpsAgent("ops-agent", bus_svc, memory_svc, snapshot_svc)
    security_agent = SecurityAgent("security-agent", bus_svc, memory_svc, snapshot_svc)

    # ── Phase 1: Agent Registration & Wake ────────────────────────────
    header("Phase 1: Agent Registration & Wake")

    await code_agent.start()
    step("Code Agent registered -> domains: backend, api, architecture")

    await ops_agent.start()
    step("Ops Agent registered -> domains: ops, monitoring, incidents")

    await security_agent.start()
    step("Security Agent registered -> domains: security, auth, compliance")

    # Show agent status table
    agents = bus_svc.get_all_agents()
    table = Table(title="Registered Agents", box=box.ROUNDED)
    table.add_column("Agent", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Domains", style="yellow")
    for a in agents:
        table.add_row(a.agent_id, a.status, ", ".join(a.domains))
    console.print(table)

    # ── Phase 2: Knowledge Seeding ────────────────────────────────────
    header("Phase 2: Knowledge Seeding")

    # Code Agent stores knowledge
    code_knowledge = [
        (
            "The user-service API uses REST with JSON. Endpoints: GET /users, "
            "POST /users, PUT /users/{id}. Authentication via Bearer token.",
            ["api", "backend"],
        ),
        (
            "Architecture decision: Migrating from monolith to microservices. "
            "Phase 1 complete: user-service extracted. Phase 2: auth-service.",
            ["architecture", "backend"],
        ),
        (
            "Tech debt: The auth middleware has hardcoded token expiry of 24h. "
            "Should be configurable per environment.",
            ["backend"],
        ),
    ]
    for content, domains in code_knowledge:
        await memory_svc.store_memory(
            content=content, domains=domains,
            source_agent="code-agent", importance=7.0,
        )
        step(f"Code Agent stored [{domains[0]}]: {content[:60]}...")

    # Ops Agent stores knowledge
    ops_knowledge = [
        (
            "user-service deployed v2.1 to production on 2026-03-20. "
            "3 pods, 512MB memory each. Status: healthy.",
            ["ops"],
        ),
        (
            "Monitoring alert: user-service p99 latency increased from "
            "120ms to 340ms after v2.1 deploy.",
            ["monitoring", "ops"],
        ),
        (
            "Incident INC-2847: Database connection pool exhaustion caused "
            "by leaked connections in user-service v2.0. Fixed in v2.1.",
            ["incidents", "ops"],
        ),
    ]
    for content, domains in ops_knowledge:
        await memory_svc.store_memory(
            content=content, domains=domains,
            source_agent="ops-agent", importance=7.0,
        )
        step(f"Ops Agent stored [{domains[0]}]: {content[:60]}...")

    # Security Agent stores knowledge
    security_knowledge = [
        (
            "Auth policy: All API endpoints must use OAuth2 with JWT tokens. "
            "Token expiry: 1 hour for access tokens, 7 days for refresh tokens.",
            ["auth", "security"],
        ),
        (
            "SOC2 compliance requirement: All API calls must be logged with "
            "caller identity, timestamp, and response status.",
            ["compliance", "security"],
        ),
        (
            "Security audit finding: user-service v2.0 did not validate JWT "
            "audience claim. Fixed in v2.1 security patch.",
            ["security"],
        ),
    ]
    for content, domains in security_knowledge:
        await memory_svc.store_memory(
            content=content, domains=domains,
            source_agent="security-agent", importance=8.0,
        )
        step(f"Security Agent stored [{domains[0]}]: {content[:60]}...")

    mem_count = await memory_svc.memory_count()
    result(f"Knowledge seeded: {mem_count} memories stored across 3 agents")

    # Show auto-extracted entities
    all_entities = await memory_svc.list_entities()
    entity_table = Table(
        box=box.ROUNDED,
        title="Auto-Extracted Knowledge Graph Entities",
        show_header=True,
        header_style="bold",
    )
    entity_table.add_column("Entity", style="bold white")
    entity_table.add_column("Type", style="cyan")
    for entity in sorted(all_entities, key=lambda e: e.type)[:12]:
        entity_table.add_row(entity.name, entity.type)
    if len(all_entities) > 12:
        entity_table.add_row(f"... +{len(all_entities) - 12} more", "[dim]...[/]")
    console.print(entity_table)
    result(
        f"Knowledge graph: {memory_svc.entity_count()} entities, "
        f"{memory_svc.relationship_count()} relationships"
    )

    # ── Phase 3: Cross-Agent Collaboration ────────────────────────────
    header("Phase 3: Cross-Agent Collaboration")

    # Code Agent asks Security Agent a question via Knowledge Bus
    step("Code Agent asks: 'What authentication method should new endpoints use?'")
    ask1 = KnowledgeAsk(
        question="What authentication method should new API endpoints use?",
        domains=["auth", "security"],
        from_agent="code-agent",
    )
    response1 = await bus_svc.ask_sync(ask1, timeout_ms=5000)
    if response1:
        console.print(Panel(
            f"[bold]From:[/] {response1.from_agent}\n"
            f"[bold]Mode:[/] [green]{response1.source_mode.upper()}[/]\n"
            f"[bold]Confidence:[/] {response1.confidence:.2f}\n"
            f"[bold]Content:[/] {response1.knowledge.content[:200]}",
            title="[green]Security Agent Response[/]",
            border_style="green",
        ))
    else:
        warn("No response received (Security Agent may not have matching knowledge)")

    # Ops Agent announces successful deployment
    step("Ops Agent announces: auth-service v2.3 deployed successfully")
    announcement = KnowledgeAnnounce(
        from_agent="ops-agent",
        event="updated",
        domains=["ops", "backend"],
        knowledge=KnowledgePayload(
            type="fact",
            content=(
                "auth-service v2.3 deployed to production. JWT audience "
                "validation enabled. All health checks passing."
            ),
        ),
        impact=ImpactAssessment(
            affected_domains=["ops", "backend"],
            severity="info",
            description="Routine deployment of auth-service v2.3.",
        ),
    )
    await bus_svc.announce(announcement)
    result("Announcement broadcast to subscribers of [ops, backend]")

    # Let the bus deliver
    await asyncio.sleep(0.1)

    # ── Phase 4: Breaking Change Propagation ──────────────────────────
    header("Phase 4: Breaking Change Propagation")

    step("Code Agent announces: BREAKING -- User API response format changed v1 -> v2")
    breaking = KnowledgeAnnounce(
        from_agent="code-agent",
        event="breaking-change",
        domains=["api", "backend", "architecture"],
        knowledge=KnowledgePayload(
            type="interface-spec",
            content=(
                "BREAKING CHANGE: User API response format changed from v1 to v2. "
                "'user_name' field renamed to 'username'. "
                "'created' field changed from Unix timestamp to ISO 8601."
            ),
        ),
        impact=ImpactAssessment(
            breaking_change=True,
            affected_domains=["api", "backend"],
            severity="critical",
            description="User API response format v1 -> v2 migration.",
        ),
    )
    await bus_svc.announce(breaking)

    # Store the breaking change as a memory too
    await memory_svc.store_memory(
        content=(
            "BREAKING CHANGE: User API response format changed from v1 to v2. "
            "'user_name' field renamed to 'username'. "
            "'created' field changed from Unix timestamp to ISO 8601."
        ),
        domains=["api", "backend"],
        source_agent="code-agent",
        importance=9.0,
        structured={"entity": "user-api", "key": "response_format", "value": "v2"},
    )
    result(
        "Breaking change announced and stored with entity state: "
        "user-api.response_format = v2"
    )

    # Ops agent receives and stores it
    await memory_svc.store_memory(
        content=(
            "Ops note: User API response format changed to v2. Need to update "
            "monitoring dashboards and alerting rules to handle new field names."
        ),
        domains=["ops", "monitoring"],
        source_agent="ops-agent",
        importance=8.0,
    )
    step("Ops Agent received breaking change, stored monitoring update note")

    # Let the bus deliver
    await asyncio.sleep(0.1)

    # Show who received the breaking change
    ops_ann = await bus_svc.drain_announcements("ops-agent")
    sec_ann = await bus_svc.drain_announcements("security-agent")

    ann_table = Table(box=box.ROUNDED, title="Breaking Change Delivery")
    ann_table.add_column("Agent", style="bold")
    ann_table.add_column("Received", justify="center")
    ann_table.add_column("Status")
    ann_table.add_row(
        "ops-agent",
        f"[green]{len(ops_ann)} announcement(s)[/]",
        "Will update dashboards",
    )
    ann_table.add_row(
        "security-agent",
        f"[green]{len(sec_ann)} announcement(s)[/]",
        "Will review compliance",
    )
    ann_table.add_row(
        "code-agent",
        "[dim]sender (self)[/]",
        "Published the breaking change",
    )
    console.print(ann_table)

    # ── Phase 5: Agent Sleep & Surrogate ──────────────────────────────
    header("Phase 5: Agent Sleep & Surrogate Response")

    step("Security Agent going to sleep...")
    snapshot = await security_agent.sleep()
    result(
        f"Security Agent sleeping -- snapshot published with "
        f"{len(snapshot.entries)} entries, domains: {snapshot.domains}"
    )

    # Show updated agent status
    agents = bus_svc.get_all_agents()
    status_table = Table(title="Agent Status After Sleep", box=box.ROUNDED)
    status_table.add_column("Agent", style="cyan")
    status_table.add_column("Status")
    status_table.add_column("Domains", style="yellow")
    for a in agents:
        status_style = (
            "green" if a.status == "online"
            else "yellow" if a.status == "sleeping"
            else "red"
        )
        status_table.add_row(
            a.agent_id,
            f"[{status_style}]{a.status}[/]",
            ", ".join(a.domains),
        )
    console.print(status_table)

    # Code Agent asks Security Agent (should get surrogate response)
    step("Code Agent asks sleeping Security Agent: 'Is the new endpoint SOC2 compliant?'")
    ask2 = KnowledgeAsk(
        question="Is the new user API endpoint compliant with SOC2 requirements?",
        domains=["compliance", "security"],
        from_agent="code-agent",
    )
    response2 = await bus_svc.ask_sync(ask2, timeout_ms=2000)
    if response2:
        mode_color = "yellow" if response2.source_mode == "warm" else "green"
        console.print(Panel(
            f"[bold]From:[/] {response2.from_agent}\n"
            f"[bold]Mode:[/] [{mode_color}]{response2.source_mode.upper()}[/] "
            f"({'surrogate from snapshot' if response2.source_mode == 'warm' else 'live'})\n"
            f"[bold]Confidence:[/] {response2.confidence:.2f} "
            f"[dim]({_surrogate_label(response2.source_mode)})[/]\n"
            f"[bold]Staleness:[/] {response2.staleness_warning or 'Fresh'}\n"
            f"[bold]Content:[/] {response2.knowledge.content[:200]}",
            title=(
                "[yellow]Surrogate Response (Warm Mode)[/]"
                if response2.source_mode == "warm"
                else "[green]Live Response[/]"
            ),
            border_style="yellow" if response2.source_mode == "warm" else "green",
        ))
    else:
        warn("No response -- no surrogate match found in Security Agent's snapshot")

    # ── Phase 6: Consolidation ────────────────────────────────────────
    header("Phase 6: Consolidation Pass")

    step("Running consolidation (decay + episode closure)...")
    try:
        from ncms.application.consolidation_service import ConsolidationService

        consolidation_svc = ConsolidationService(
            store=store, index=index, graph=graph, config=config, splade=splade,
        )
        consolidation_results = await consolidation_svc.run_consolidation_pass()

        consolidation_table = Table(title="Consolidation Results", box=box.ROUNDED)
        consolidation_table.add_column("Task", style="cyan")
        consolidation_table.add_column("Count", justify="right", style="bold")
        for task_name, count in consolidation_results.items():
            consolidation_table.add_row(task_name, str(count))
        console.print(consolidation_table)

        result("Consolidation pass complete")
    except Exception as e:
        warn(f"Consolidation skipped: {e}")

    # ── Phase 7: Structured Recall ────────────────────────────────────
    header("Phase 7: Structured Recall")

    step("Ops Agent recalls: 'What happened with the user service API changes?'")
    recall_results = await memory_svc.recall(
        query="What happened with the user service API changes?",
        domain=None,
        limit=5,
    )

    if recall_results:
        recall_table = Table(
            title="Recall Results", box=box.ROUNDED, show_lines=True,
        )
        recall_table.add_column("Content", style="white", max_width=55)
        recall_table.add_column("Score", style="cyan", justify="right")
        recall_table.add_column("Path", style="yellow")
        recall_table.add_column("Episode", style="magenta")

        for r in recall_results[:5]:
            episode_info = ""
            if r.context.episode:
                ep = r.context.episode
                if ep.episode_title:
                    episode_info = ep.episode_title[:30]
                else:
                    episode_info = f"ep:{ep.episode_id[:8]}"

            content_preview = r.memory.memory.content[:55]
            if len(r.memory.memory.content) > 55:
                content_preview += "..."

            recall_table.add_row(
                content_preview,
                f"{r.memory.total_activation:.3f}",
                r.retrieval_path,
                episode_info or "--",
            )
        console.print(recall_table)

        # Show entity states from first result
        first = recall_results[0]
        if first.context.entity_states:
            step("Entity states found in recalled memories:")
            for s in first.context.entity_states:
                result(
                    f"  {s.entity_name}.{s.state_key} = {s.state_value} "
                    f"(current={s.is_current})"
                )

        # Show causal chains
        if first.context.causal_chain.supersedes or first.context.causal_chain.superseded_by:
            step("Causal chain:")
            for sup in first.context.causal_chain.supersedes:
                result(f"  supersedes: {sup[:40]}")
            for sup in first.context.causal_chain.superseded_by:
                result(f"  superseded_by: {sup[:40]}")

        # Show episode siblings if any
        for r in recall_results[:3]:
            if r.context.episode and r.context.episode.sibling_ids:
                step(
                    f"Episode '"
                    f"{r.context.episode.episode_title or r.context.episode.episode_id[:8]}"
                    f"' has {len(r.context.episode.sibling_ids)} sibling fragment(s)"
                )
                break
    else:
        warn("No recall results found")

    # ── Phase 8: Security Agent Wakes ─────────────────────────────────
    header("Phase 8: Security Agent Wakes")

    step("Security Agent waking up...")
    await security_agent.wake()
    result("Security Agent restored from snapshot, marked as live")

    # Check inbox for announcements received while sleeping
    inbox = await bus_svc.drain_announcements("security-agent")
    if inbox:
        step(f"Security Agent processing {len(inbox)} announcement(s) from while sleeping:")
        for ann in inbox:
            result(f"  [{ann.event}] from {ann.from_agent}: {ann.knowledge.content[:80]}...")
    else:
        step("No pending announcements (already drained or none received while sleeping)")

    # Final live exchange to confirm Security Agent is back
    step("Code Agent asks now-awake Security Agent: 'What is the JWT token policy?'")
    ask3 = KnowledgeAsk(
        question="What is the JWT token expiry policy for access and refresh tokens?",
        domains=["auth", "security"],
        from_agent="code-agent",
    )
    response3 = await bus_svc.ask_sync(ask3, timeout_ms=5000)
    if response3:
        console.print(Panel(
            f"[bold]From:[/] {response3.from_agent}\n"
            f"[bold]Mode:[/] [green]{response3.source_mode.upper()}[/] (direct response)\n"
            f"[bold]Confidence:[/] {response3.confidence:.2f}\n"
            f"[bold]Content:[/] {response3.knowledge.content[:200]}",
            title="[green]Live Response (Security Agent Back Online)[/]",
            border_style="green",
        ))
    else:
        warn("No response from Security Agent")

    # Final status
    agents = bus_svc.get_all_agents()
    final_table = Table(title="Final Agent Status", box=box.ROUNDED)
    final_table.add_column("Agent", style="cyan")
    final_table.add_column("Status", style="green")
    final_table.add_column("Domains", style="yellow")
    for a in agents:
        final_table.add_row(a.agent_id, a.status, ", ".join(a.domains))
    console.print(final_table)

    # ── Summary ───────────────────────────────────────────────────────
    header("Demo Summary")

    mem_count = await memory_svc.memory_count()
    entity_count = memory_svc.entity_count()
    rel_count = memory_svc.relationship_count()
    domain_count = len(bus_svc.list_domains())

    summary_table = Table(box=box.ROUNDED)
    summary_table.add_column("Metric", style="bold")
    summary_table.add_column("Value", style="cyan")
    summary_table.add_row("Total Memories", str(mem_count))
    summary_table.add_row("Knowledge Graph Entities", str(entity_count))
    summary_table.add_row("Graph Relationships", str(rel_count))
    summary_table.add_row("Registered Agents", str(len(agents)))
    summary_table.add_row("Knowledge Domains", str(domain_count))
    summary_table.add_row("Cross-Agent Asks", "3")
    summary_table.add_row("Surrogate Responses", "1")
    summary_table.add_row("Breaking Changes", "1")
    console.print(summary_table)

    console.print()
    console.print(Panel(
        "[bold]What you just saw:[/]\n\n"
        "1. Three domain agents (Code, Ops, Security) registered with expertise\n"
        "2. Each agent seeded domain knowledge (entities auto-extracted to graph)\n"
        "3. Code Agent asked Security Agent about auth via Knowledge Bus\n"
        "4. Breaking API change propagated to Ops and Security agents\n"
        "5. Security Agent slept; Code Agent got a [yellow]surrogate response[/] from snapshot\n"
        "6. Consolidation pass ran (decay + episode closure)\n"
        "7. Structured [cyan]recall[/] returned memories with episode context + entity states\n"
        "8. Security Agent woke up, drained inbox, and answered [green]live[/]\n\n"
        "[dim]All running in-process with zero external dependencies.[/]",
        title="[bold cyan]NCMS x NemoClaw - Multi-Agent Cognitive Memory[/]",
        border_style="cyan",
    ))

    # Cleanup
    await code_agent.shutdown()
    await ops_agent.shutdown()
    await security_agent.shutdown()
    await store.close()


if __name__ == "__main__":
    asyncio.run(run_nemoclaw_demo())
