"""Phase A — alias-collision audit event integration tests.

Covers claim A.16: when ``SubjectRegistry.canonicalize()`` resolves
a surface via fuzzy (normalized) match — confidence < 1.0 — it
emits a ``subject.alias_collision`` dashboard event with payload
``{surface, picked_canonical, confidence, alternatives}``.

End-to-end: the event must be emitted into the EventLog, drained
by the persistence task, and queryable from the
``dashboard_events`` SQLite table.  No UI is required for Phase A
— SQL queries against ``dashboard_events`` are the validation
surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import aiosqlite
import pytest

from ncms.application.subject_registry import SubjectRegistry
from ncms.infrastructure.observability.event_log import EventLog
from ncms.infrastructure.storage.migrations import create_schema


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        await create_schema(conn)
        yield conn


@pytest.fixture
async def event_log(db: aiosqlite.Connection):
    log = EventLog(db=db)
    task = asyncio.create_task(log.start_persistence())
    yield log
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.fixture
def registry(
    db: aiosqlite.Connection,
    event_log: EventLog,
) -> SubjectRegistry:
    return SubjectRegistry(db, event_log=event_log)


async def _drain_persistence(event_log: EventLog) -> None:
    """Yield enough times for the persistence task to drain the queue."""
    for _ in range(20):
        if event_log._write_queue.empty():
            # One more yield so the inflight insert commits.
            await asyncio.sleep(0.01)
            return
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# A.16 verifies
# ---------------------------------------------------------------------------


async def test_event_emitted_on_fuzzy_match(
    registry: SubjectRegistry,
    event_log: EventLog,
) -> None:
    """Tier-2 (normalized) match emits ``subject.alias_collision``."""
    await registry.canonicalize("auth service", type_hint="service")
    # Fuzzy match: same normalized form, different surface.
    await registry.canonicalize("AUTH SERVICE", type_hint="service")

    events = [
        e for e in event_log.get_all_events()
        if e.type == "subject.alias_collision"
    ]
    assert len(events) == 1
    payload = events[0].data
    assert payload["surface"] == "AUTH SERVICE"
    assert payload["picked_canonical"] == "service:auth-service"
    assert payload["confidence"] == 0.85
    assert "alternatives" in payload
    assert "service:auth-service" in payload["alternatives"]


async def test_no_event_on_exact_match(
    registry: SubjectRegistry,
    event_log: EventLog,
) -> None:
    """Tier-1 (exact) re-canonicalization does NOT emit a collision."""
    await registry.canonicalize("auth service", type_hint="service")
    await registry.canonicalize("auth service", type_hint="service")
    events = [
        e for e in event_log.get_all_events()
        if e.type == "subject.alias_collision"
    ]
    assert events == []


async def test_no_event_on_mint(
    registry: SubjectRegistry,
    event_log: EventLog,
) -> None:
    """Tier-3 (mint) does NOT emit a collision (no alternatives existed)."""
    await registry.canonicalize("auth service", type_hint="service")
    events = [
        e for e in event_log.get_all_events()
        if e.type == "subject.alias_collision"
    ]
    assert events == []


async def test_event_queryable_from_dashboard_events(
    registry: SubjectRegistry,
    event_log: EventLog,
    db: aiosqlite.Connection,
) -> None:
    """A.16 validation surface: the event lands in dashboard_events.

    Confirms a reviewer can run::

        SELECT * FROM dashboard_events
        WHERE type = 'subject.alias_collision'

    and recover the canonicalization audit trail without any UI.
    """
    await registry.canonicalize("auth service", type_hint="service")
    await registry.canonicalize("AUTH SERVICE", type_hint="service")

    await _drain_persistence(event_log)

    cur = await db.execute(
        "SELECT type, data FROM dashboard_events "
        "WHERE type = 'subject.alias_collision' ORDER BY timestamp",
    )
    rows = await cur.fetchall()
    assert len(rows) == 1
    # The ``data`` column stores the event's data dict directly
    # (see EventLog._persist_batch).
    payload = json.loads(rows[0][1])
    assert payload["surface"] == "AUTH SERVICE"
    assert payload["picked_canonical"] == "service:auth-service"
    assert payload["confidence"] == 0.85


async def test_no_event_log_does_not_raise(
    db: aiosqlite.Connection,
) -> None:
    """Registry without an event log silently drops collisions."""
    reg = SubjectRegistry(db)  # event_log default = None
    await reg.canonicalize("auth service", type_hint="service")
    # Should not raise — emit is a no-op.
    s = await reg.canonicalize("AUTH SERVICE", type_hint="service")
    assert s.confidence == 0.85
    assert s.id == "service:auth-service"
