"""ND demo runner for the dashboard — runs Architect/Security/Builder agents.

Uses the dashboard's shared services (memory, bus, event_log) so all events
appear in the dashboard in real-time. Loads governance-mesh knowledge files
and runs the Builder's autonomous work loop.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ncms.application.bus_service import BusService
from ncms.application.knowledge_loader import KnowledgeLoader
from ncms.application.memory_service import MemoryService
from ncms.application.snapshot_service import SnapshotService
from ncms.config import NCMSConfig
from ncms.infrastructure.observability.event_log import EventLog

logger = logging.getLogger(__name__)

STEP_DELAY = 2.0  # seconds between phases for dashboard visibility

# ── Governance-mesh knowledge files ──────────────────────────────────────
# Resolve paths: container (/app/knowledge/) or host (~Documents/governance-mesh/)

_CONTAINER_KNOWLEDGE = Path("/app/knowledge")
_GOV_BASE = Path.home() / "Documents" / "governance-mesh"
_APP_BASE = _GOV_BASE / "platforms" / "imdb-lite" / "bars" / "imdb-lite-application"


def _resolve(container_path: str, host_path: str) -> str:
    """Return container path if it exists, else host path."""
    cp = _CONTAINER_KNOWLEDGE / container_path
    if cp.exists():
        return str(cp)
    return host_path


def _build_knowledge_files() -> list[tuple[str, list[str]]]:
    """Build knowledge file list with container/host path resolution."""
    return [
        (
            _resolve(
                "architecture/bar.arch.json", str(_APP_BASE / "architecture" / "bar.arch.json")
            ),
            ["architecture", "calm-model"],
        ),
        (
            _resolve(
                "architecture/ADRs/001-initial-architecture.md",
                str(_APP_BASE / "architecture" / "ADRs" / "001-initial-architecture.md"),
            ),
            ["architecture", "decisions"],
        ),
        (
            _resolve(
                "architecture/ADRs/002-mongodb-document-store.md",
                str(_APP_BASE / "architecture" / "ADRs" / "002-mongodb-document-store.md"),
            ),
            ["architecture", "decisions"],
        ),
        (
            _resolve(
                "architecture/ADRs/003-jwt-rbac-authentication.md",
                str(_APP_BASE / "architecture" / "ADRs" / "003-jwt-rbac-authentication.md"),
            ),
            ["architecture", "decisions", "security"],
        ),
        (
            _resolve(
                "architecture/ADRs/004-mongodb-memory-server-testing.md",
                str(_APP_BASE / "architecture" / "ADRs" / "004-mongodb-memory-server-testing.md"),
            ),
            ["architecture", "decisions"],
        ),
        (
            _resolve(
                "architecture/quality-attributes.yaml",
                str(_APP_BASE / "architecture" / "quality-attributes.yaml"),
            ),
            ["architecture", "quality"],
        ),
        (
            _resolve(
                "architecture/fitness-functions.yaml",
                str(_APP_BASE / "architecture" / "fitness-functions.yaml"),
            ),
            ["architecture", "quality"],
        ),
        (
            _resolve(
                "prompts/architecture.md",
                str(_GOV_BASE / ".caterpillar" / "prompts" / "architecture.md"),
            ),
            ["architecture", "calm-model"],
        ),
        (
            _resolve(
                "security/threat-model.yaml", str(_APP_BASE / "security" / "threat-model.yaml")
            ),
            ["security", "threats"],
        ),
        (
            _resolve(
                "security/security-controls.yaml",
                str(_APP_BASE / "security" / "security-controls.yaml"),
            ),
            ["security", "controls"],
        ),
        (
            _resolve(
                "security/compliance-checklist.yaml",
                str(_APP_BASE / "security" / "compliance-checklist.yaml"),
            ),
            ["security", "compliance"],
        ),
        (
            _resolve(
                "prompts/application-security.md",
                str(_GOV_BASE / ".caterpillar" / "prompts" / "application-security.md"),
            ),
            ["security", "threats", "controls"],
        ),
        (
            _resolve("app.yaml", str(_APP_BASE / "app.yaml")),
            ["architecture", "identity-service"],
        ),
    ]


async def run_nd_demo_loop(
    memory_svc: MemoryService,
    bus_svc: BusService,
    snapshot_svc: SnapshotService,
    event_log: EventLog,
) -> None:
    """Run the ND demo using the dashboard's shared services."""
    await asyncio.sleep(3.0)  # Let the dashboard start up

    config = NCMSConfig()
    llm_model = config.llm_model
    llm_api_base = config.llm_api_base

    logger.info("ND demo starting — LLM: %s @ %s", llm_model, llm_api_base or "(default)")

    # ── Phase 1: Knowledge Loading ───────────────────────────────────────

    loader = KnowledgeLoader(memory_svc)
    total_memories = 0
    files_loaded = 0
    knowledge_files = _build_knowledge_files()

    for file_path, domains in knowledge_files:
        p = Path(file_path)
        if not p.exists():
            logger.warning("Knowledge file not found, skipping: %s", p.name)
            continue

        stats = await loader.load_file(
            file_path,
            domains=domains,
            source_agent="knowledge-loader",
            importance=7.0,
        )
        files_loaded += stats.files_processed
        total_memories += stats.memories_created

        if stats.memories_created > 0:
            logger.info("Loaded %s -> %d memories", p.name, stats.memories_created)

        await asyncio.sleep(0.3)  # Space out events for dashboard

    logger.info("Knowledge loaded: %d files -> %d memories", files_loaded, total_memories)
    await asyncio.sleep(STEP_DELAY)

    # ── Phase 2: Agent Registration ──────────────────────────────────────

    from ncms.demo.nemoclaw_nd.architect_agent import ArchitectAgent
    from ncms.demo.nemoclaw_nd.builder_agent import BuilderAgent
    from ncms.demo.nemoclaw_nd.security_agent import SecurityAgent

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
    await asyncio.sleep(STEP_DELAY)
    await security.start()
    await asyncio.sleep(STEP_DELAY)
    await builder.start()
    await asyncio.sleep(STEP_DELAY)

    logger.info("All 3 ND agents registered: Architect, Security, Builder")

    # ── Phase 3: Builder Work Loop ───────────────────────────────────────

    logger.info("Builder Agent starting autonomous work loop...")

    async def on_turn(turn_num: int, turn_record: dict[str, str]) -> None:
        """Log each turn for dashboard visibility."""
        action = turn_record.get("action", "unknown")
        logger.info(
            "Builder turn %d: %s — %s",
            turn_num,
            action,
            turn_record.get("detail", turn_record.get("question", ""))[:200],
        )

    try:
        turns = await builder.work_loop(max_turns=8, on_turn=on_turn)
        logger.info("Builder completed %d turns", len(turns))
    except Exception:
        logger.exception("Builder work loop error")
        turns = []

    await asyncio.sleep(STEP_DELAY)

    # ── Phase 4: Announcement Round ──────────────────────────────────────

    try:
        summary = builder._build_summary()  # noqa: SLF001
        await builder.announce_knowledge(
            event="created",
            domains=["identity-service", "implementation", "architecture"],
            content=summary[:1000],
        )
        logger.info("Builder announced final design to Knowledge Bus")
    except Exception:
        logger.exception("Builder announcement failed")

    await asyncio.sleep(STEP_DELAY)

    # Process announcements
    for agent, name in [(architect, "Architect"), (security, "Security")]:
        ann_list = await bus_svc.drain_announcements(agent.agent_id)
        if ann_list:
            logger.info("%s Agent received %d announcement(s)", name, len(ann_list))

    await asyncio.sleep(STEP_DELAY)

    # ── Phase 5: Searches ────────────────────────────────────────────────

    for query in [
        "JWT authentication token handling",
        "identity service API design",
        "STRIDE threats for auth microservice",
    ]:
        await memory_svc.search(query)
        await asyncio.sleep(STEP_DELAY)

    # ── Cleanup ──────────────────────────────────────────────────────────

    await builder.shutdown()
    await asyncio.sleep(1)
    await architect.shutdown()
    await asyncio.sleep(1)
    await security.shutdown()

    logger.info(
        "ND demo completed: %d files, %d memories, %d builder turns",
        files_loaded,
        total_memories,
        len(turns),
    )

    # Keep running so events stay in the log
    while True:
        await asyncio.sleep(60)
