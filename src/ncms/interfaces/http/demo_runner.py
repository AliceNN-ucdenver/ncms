"""Demo runner for the dashboard - replays agent scenarios with pauses.

Runs the same 6-phase demo as `ncms demo` but with delays between steps
so the dashboard can visualize events in real-time.
"""

from __future__ import annotations

import asyncio
import logging

from ncms.application.bus_service import BusService
from ncms.application.memory_service import MemoryService
from ncms.application.snapshot_service import SnapshotService
from ncms.demo.agents.api_agent import ApiAgent
from ncms.demo.agents.database_agent import DatabaseAgent
from ncms.demo.agents.frontend_agent import FrontendAgent
from ncms.infrastructure.observability.event_log import EventLog

logger = logging.getLogger(__name__)

STEP_DELAY = 2.0  # seconds between demo steps


async def run_demo_loop(
    memory_svc: MemoryService,
    bus_svc: BusService,
    snapshot_svc: SnapshotService,
    event_log: EventLog,
) -> None:
    """Run a demo scenario that generates observable events for the dashboard."""
    await asyncio.sleep(2.0)  # Let the server start up

    logger.info("Dashboard demo starting...")

    # ── Phase 1: Agent Registration ───────────────────────────────────────

    api_agent = ApiAgent("api-agent", bus_svc, memory_svc, snapshot_svc)
    frontend_agent = FrontendAgent("frontend-agent", bus_svc, memory_svc, snapshot_svc)
    db_agent = DatabaseAgent("db-agent", bus_svc, memory_svc, snapshot_svc)

    await api_agent.start()
    await asyncio.sleep(STEP_DELAY)
    await frontend_agent.start()
    await asyncio.sleep(STEP_DELAY)
    await db_agent.start()
    await asyncio.sleep(STEP_DELAY)

    # ── Phase 2: Store Domain Knowledge ───────────────────────────────────

    # API agent stores knowledge
    api_knowledge = [
        "GET /api/users returns list of User objects with id, name, email, role fields",
        "POST /api/auth/login accepts {email, password} and returns JWT token",
        "UserService handles authentication via JWT with RS256 signing",
        "Rate limiting is set to 100 requests per minute per API key",
        "The /api/users endpoint supports pagination via ?page=N&limit=M query params",
    ]
    for content in api_knowledge:
        await api_agent.store_knowledge(content, domains=["api"])
        await asyncio.sleep(0.5)

    await asyncio.sleep(STEP_DELAY)

    # Frontend agent stores knowledge
    frontend_knowledge = [
        "React dashboard uses TanStack Query for API data fetching",
        "The UserProfile component renders at /app/users/:id route",
        "Authentication state is managed via React Context with JWT refresh logic",
        "The design system uses Tailwind CSS with a custom color palette",
    ]
    for content in frontend_knowledge:
        await frontend_agent.store_knowledge(content, domains=["frontend"])
        await asyncio.sleep(0.5)

    await asyncio.sleep(STEP_DELAY)

    # Database agent stores knowledge
    db_knowledge = [
        "The users table has columns: id (UUID PK), name, email (unique), role, created_at",
        "PostgreSQL is the primary database with pgbouncer connection pooling",
        "The auth_tokens table stores refresh tokens with TTL of 7 days",
        "Database migrations use Alembic with auto-generate from SQLAlchemy models",
    ]
    for content in db_knowledge:
        await db_agent.store_knowledge(content, domains=["db"])
        await asyncio.sleep(0.5)

    await asyncio.sleep(STEP_DELAY)

    # ── Phase 3: Live Knowledge Exchange ──────────────────────────────────

    # Frontend asks API agent about user endpoints
    await frontend_agent.ask_knowledge(
        "What fields does the /api/users endpoint return?",
        domains=["api"],
    )
    await asyncio.sleep(STEP_DELAY * 1.5)

    # Database asks API agent about auth
    await db_agent.ask_knowledge(
        "How does the authentication system work?",
        domains=["api"],
    )
    await asyncio.sleep(STEP_DELAY * 1.5)

    # ── Phase 4: API Agent Sleeps ─────────────────────────────────────────

    await api_agent.sleep()
    await asyncio.sleep(STEP_DELAY)

    # Frontend asks again — gets surrogate response from snapshot
    await frontend_agent.ask_knowledge(
        "What is the rate limit for the API?",
        domains=["api"],
    )
    await asyncio.sleep(STEP_DELAY * 1.5)

    # ── Phase 5: Breaking Change Announcement ─────────────────────────────

    await db_agent.announce_knowledge(
        event="breaking-change",
        domains=["db", "db:user-schema"],
        content="ALTER TABLE users ADD COLUMN role VARCHAR(50) DEFAULT 'viewer'",
        breaking=True,
    )
    await asyncio.sleep(STEP_DELAY)

    # ── Phase 6: Memory Search ────────────────────────────────────────────

    # Search for JWT-related memories
    await memory_svc.search("JWT authentication token")
    await asyncio.sleep(STEP_DELAY)

    # Search for user endpoint memories
    await memory_svc.search("user profile API endpoint")
    await asyncio.sleep(STEP_DELAY)

    # Search for database schema
    await memory_svc.search("PostgreSQL users table schema")
    await asyncio.sleep(STEP_DELAY)

    # ── Phase 7: Wake API Agent ───────────────────────────────────────────

    await api_agent.wake()
    await asyncio.sleep(STEP_DELAY)

    # One more live exchange
    await frontend_agent.ask_knowledge(
        "How do I paginate the users list?",
        domains=["api"],
    )
    await asyncio.sleep(STEP_DELAY * 2)

    # ── Cleanup ───────────────────────────────────────────────────────────

    await api_agent.shutdown()
    await asyncio.sleep(1)
    await frontend_agent.shutdown()
    await asyncio.sleep(1)
    await db_agent.shutdown()

    logger.info("Dashboard demo completed. Events remain in the log for viewing.")

    # Keep running so events stay available
    while True:
        await asyncio.sleep(60)
