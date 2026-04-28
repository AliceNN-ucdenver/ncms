"""Phase B claim B.7 — performance test for ``get_subject_states``.

Seeds 10K ENTITY_STATE rows across 1K distinct subjects, runs
100 random ``get_subject_states(subject_id, is_current=True)``
queries, and asserts:

- p95 ≤ 5 ms
- p99 ≤ 20 ms

10K not 100K so the test runs in under ~10s in CI; the SHAPE of
the verify (does the index actually help) is what matters, not
the absolute count.  The benchmark is honest:

* Subjects drawn uniformly random from the seeded pool, so we
  exercise both hot and cold cache paths.
* No memoization, no caching shortcuts.
* Per-query timing is wall-clock around the awaited call.

Marked with the ``slow`` pytest marker so it can be excluded
from default CI runs if a tighter loop is needed:
``pytest -m "not slow"``.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from datetime import UTC, datetime

import aiosqlite
import pytest

from ncms.domain.models import NodeType
from ncms.infrastructure.storage.migrations import create_schema
from ncms.infrastructure.storage.sqlite_memory_nodes import get_subject_states

_NUM_SUBJECTS = 1_000
_STATES_PER_SUBJECT = 10  # 10K rows total
_NUM_QUERIES = 100
_P95_BUDGET_MS = 5.0
_P99_BUDGET_MS = 20.0
_SCOPES = ("status", "version", "owner", "deployed_at", "deprecated_at")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _seed_states(db: aiosqlite.Connection, num_subjects: int) -> list[str]:
    """Seed ``num_subjects`` × ``_STATES_PER_SUBJECT`` rows.

    Returns the list of subject_ids so the query loop can pick
    from them uniformly.
    """
    rng = random.Random(0)
    subject_ids = [f"service:s{i:05d}" for i in range(num_subjects)]
    now = _now_iso()
    # Bulk-insert via executemany for speed.
    memory_rows: list[tuple] = []
    node_rows: list[tuple] = []
    for subject_id in subject_ids:
        for _ in range(_STATES_PER_SUBJECT):
            scope = rng.choice(_SCOPES)
            mid = str(uuid.uuid4())
            nid = str(uuid.uuid4())
            metadata = {
                "entity_id": subject_id,
                "state_key": scope,
                "state_value": rng.choice(("v1", "v2", "v3")),
            }
            memory_rows.append(
                (mid, f"stub for {subject_id}/{scope}", "fact", now, now),
            )
            node_rows.append(
                (
                    nid,
                    mid,
                    NodeType.ENTITY_STATE.value,
                    json.dumps(metadata),
                    rng.choice((0, 1)),  # is_current mix
                    5.0,
                    now,
                    now,
                ),
            )
    await db.executemany(
        "INSERT INTO memories (id, content, type, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        memory_rows,
    )
    await db.executemany(
        "INSERT INTO memory_nodes "
        "(id, memory_id, node_type, metadata, is_current, "
        " importance, ingested_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        node_rows,
    )
    await db.commit()
    return subject_ids


@pytest.mark.slow
@pytest.mark.asyncio
async def test_get_subject_states_p95_under_5ms() -> None:
    """B.7 — index actually helps; p95 < 5ms, p99 < 20ms."""
    rng = random.Random(42)
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await create_schema(db)
        subject_ids = await _seed_states(db, _NUM_SUBJECTS)
        # Drive the query loop.  Use a hot connection so we
        # measure the index lookup, not connection setup.
        latencies_ms: list[float] = []
        for _ in range(_NUM_QUERIES):
            sid = rng.choice(subject_ids)
            t0 = time.perf_counter()
            await get_subject_states(db, sid, is_current=True)
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)

    latencies_ms.sort()
    p50 = latencies_ms[len(latencies_ms) // 2]
    p95 = latencies_ms[int(len(latencies_ms) * 0.95)]
    p99 = latencies_ms[int(len(latencies_ms) * 0.99)]
    assert p95 < _P95_BUDGET_MS, (
        f"get_subject_states p95={p95:.2f}ms exceeds {_P95_BUDGET_MS}ms budget; "
        f"p50={p50:.2f}ms p99={p99:.2f}ms.  Either the index is not being "
        "used, the seeded dataset is pathological, or the perf budget needs "
        "to be revisited.  EXPLAIN QUERY PLAN test in test_subject_index.py "
        "verifies the optimizer picks the index — start there if this fails."
    )
    assert p99 < _P99_BUDGET_MS, (
        f"get_subject_states p99={p99:.2f}ms exceeds {_P99_BUDGET_MS}ms budget; "
        f"p50={p50:.2f}ms p95={p95:.2f}ms."
    )


@pytest.mark.slow
@pytest.mark.asyncio
async def test_get_subject_states_scope_filter_p95_under_5ms() -> None:
    """B.7 — scope-filtered case also clears the budget.

    Same dataset, same query loop, but each query also filters by
    ``scope`` (state_key).  Validates that the partial index plus
    the scope predicate combine well; if this fails but the
    subject-only test passes, we may need the composite
    ``(entity_id, state_key)`` index that we explicitly chose to
    defer (see the locked-decisions section of the claim doc).
    """
    rng = random.Random(7)
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await create_schema(db)
        subject_ids = await _seed_states(db, _NUM_SUBJECTS)
        latencies_ms: list[float] = []
        for _ in range(_NUM_QUERIES):
            sid = rng.choice(subject_ids)
            scope = rng.choice(_SCOPES)
            t0 = time.perf_counter()
            await get_subject_states(db, sid, scope=scope, is_current=True)
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)

    latencies_ms.sort()
    p95 = latencies_ms[int(len(latencies_ms) * 0.95)]
    p99 = latencies_ms[int(len(latencies_ms) * 0.99)]
    assert p95 < _P95_BUDGET_MS, (
        f"scope-filtered p95={p95:.2f}ms exceeds {_P95_BUDGET_MS}ms; "
        "if this is the only failure, consider adding a composite "
        "(entity_id, state_key) index.  See claim doc's resolved "
        "decisions section."
    )
    assert p99 < _P99_BUDGET_MS, (
        f"scope-filtered p99={p99:.2f}ms exceeds {_P99_BUDGET_MS}ms"
    )
