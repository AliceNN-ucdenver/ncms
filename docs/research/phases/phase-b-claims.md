# Phase B Claims — Subject Index + Lookup Helpers

**Phase:** B
**Status:** not-yet-started — claim doc only
**Owner:** NCMS core
**Reviewer:** codex 5.5 (format spec at `claims-format.md`)
**Companion:** `subject-centered-graph-design.md` §6 Phase B
**Depends on:** Phase A merged

## What Phase B delivers

1. One SQLite migration: an index on `memory_nodes.metadata->entity_id`
   filtered by `node_type='entity_state'`.
2. One new helper: `get_subject_states(subject_id, scope=None,
   as_of=None, is_current=None)`.
3. Walker code routes through the new helper instead of
   `get_entity_states_by_entity` + Python-side filter loops.
4. A 100K-L2-node performance benchmark.

**PR scope budget:** 1 PR, ≤ 400 lines including tests.
**Estimated total:** 3 days.

---

## Pre-conditions (verify on `main` after Phase A merges)

### PC-B.1 — Phase A merged
**[BEHAVIOR]**
**Verify:**
- `git log --oneline | grep -i "Phase A: subject payload"`
- Expected: at least one matching commit.
- `python -c "from ncms.domain.models import Subject; print(Subject)"`
- Expected: imports without error.

### PC-B.2 — `memory_nodes.metadata->entity_id` is the canonical subject lookup
**[SCHEMA]**
**Verify:**
- `grep -n "json_extract(metadata, '\\\$.entity_id')" src/ncms/infrastructure/storage/sqlite_memory_nodes.py`
- Expected: matches in at least 4 helpers (current_entity_states, by_entity, current_state, state_at_time, history).

### PC-B.3 — No `idx_mnodes_subject` exists today
**[SCHEMA]**
**Verify:**
- `grep "idx_mnodes_subject" src/ncms/infrastructure/storage/migrations.py`
- Expected: no match.

### PC-B.4 — 6 existing subject→state helpers
**[SCHEMA]**
**Verify:**
- `grep -E "^async def (get_current_entity_states|get_entity_states_by_entity|get_current_state|get_state_at_time|get_state_changes_since|get_state_history)" src/ncms/infrastructure/storage/sqlite_memory_nodes.py | wc -l`
- Expected: `6`.

### PC-B.5 — Walker uses `get_entity_states_by_entity` today
**[BEHAVIOR]**
**Verify:**
- `grep -n "get_entity_states_by_entity" src/ncms/application/tlg/dispatch.py`
- Expected: at least one call site (typically `_load_subject_zones`).

### PC-B.6 — No `get_subject_states` helper exists
**[SCHEMA]**
**Verify:**
- `grep -rn "get_subject_states\|getSubjectStates" src/ncms 2>/dev/null | grep -v __pycache__`
- Expected: no match.

### PC-B.7 — Architecture fitness baseline holds
**[NEGATIVE]**
**Verify:**
- `uv run pytest tests/architecture/ -q`
- Expected: 228 pass.

---

## Delivered claims

### B.1 — Migration `idx_mnodes_subject` adds the index
**[SCHEMA]**
**Pre:** No matching index in `migrations.py`.
**Post:** New index defined as:
```sql
CREATE INDEX IF NOT EXISTS idx_mnodes_subject
  ON memory_nodes (json_extract(metadata, '$.entity_id'))
  WHERE node_type = 'entity_state';
```
**Verify:**
- `grep -A 2 "idx_mnodes_subject" src/ncms/infrastructure/storage/migrations.py`
- Expected: shows the CREATE INDEX statement.
- Test: `tests/unit/infrastructure/storage/test_subject_index.py::test_migration_creates_index`.
**Failure mode:** Index not present; subject-state queries remain full scans.

### B.2 — Migration applies cleanly to existing DB
**[BEHAVIOR]**
**Pre:** Existing production DBs may have ≥ 100K memory_nodes.
**Post:** Running `run_migrations` on a non-fresh DB:
- Adds the index without locking longer than ~5 s for 100K rows.
- Does not require any application-side data migration.
- Is idempotent (running twice does not error).
**Verify:**
- Test: `tests/unit/infrastructure/storage/test_subject_index.py::test_migration_idempotent`.
- Test: `test_migration_on_populated_db_succeeds` (fixture seeds 10K rows; not 100K to keep test fast, but the *shape* is the same).
- Manual: developer runs migration on a copy of a populated dev DB before merge; no errors.

### B.3 — `EXPLAIN QUERY PLAN` shows index use
**[BEHAVIOR]** **[PERF]**
**Verify:**
- Test: `tests/unit/infrastructure/storage/test_subject_index.py::test_query_plan_uses_index`.
- The test runs `EXPLAIN QUERY PLAN SELECT * FROM memory_nodes WHERE node_type='entity_state' AND json_extract(metadata, '$.entity_id') = ?`.
- Expected: plan output contains `USING INDEX idx_mnodes_subject` (or sqlite-equivalent).
**Failure mode:** Index exists but query optimizer doesn't use it (e.g. wrong WHERE clause shape).

### B.4 — `get_subject_states` helper exists with composable filters
**[API]**
**Pre:** No such helper.
**Post:** New function in `sqlite_memory_nodes.py`:
```python
async def get_subject_states(
    db: aiosqlite.Connection,
    subject_id: str,
    *,
    scope: str | None = None,
    as_of: datetime | None = None,
    is_current: bool | None = None,
    limit: int | None = None,
) -> list[MemoryNode]:
```
- `scope` filters by `metadata.state_key`.
- `as_of` returns states valid at that point in time (uses `valid_from`/`valid_to`).
- `is_current` filters by the column.
- `limit` caps result size.
- All filters are optional and compose.
**Verify:**
- `grep -A 12 "^async def get_subject_states" src/ncms/infrastructure/storage/sqlite_memory_nodes.py`
- Expected: matches the signature above.
- Tests: `tests/unit/infrastructure/storage/test_get_subject_states.py` covers each filter combination.

### B.5 — `_load_subject_zones` routes through the new helper
**[COVERAGE]**
**Pre:** `_load_subject_zones` calls `store.get_entity_states_by_entity(subject)` (`tlg/dispatch.py:139`).
**Post:** Updated to call `store.get_subject_states(subject_id=subject)` with no other behavior change.
**Verify:**
- `grep "get_subject_states\|get_entity_states_by_entity" src/ncms/application/tlg/dispatch.py`
- Expected: `get_subject_states` present; legacy call removed (or kept only in a deprecation comment).
- Test: existing TLG dispatcher tests in `tests/integration/test_tlg_dispatch.py` continue to pass with no test changes.
**Failure mode:** Walkers diverge between paths or behavior changes.

### B.6 — Existing 6 helpers preserve public signatures (back-compat)
**[NEGATIVE]**
**Pre:** 6 helpers exist with current signatures.
**Post:** All 6 still exist; signatures unchanged. Internal implementations *may* be refactored to delegate to `get_subject_states`, but every existing caller's behavior is byte-equivalent.
**Verify:**
- `git diff main -- src/ncms/infrastructure/storage/sqlite_memory_nodes.py | grep -E "^[-+]async def get_"`
- Expected: only additions of `get_subject_states`; no removals or signature changes to the 6 existing helpers.
- Test: every existing caller of `get_entity_states_by_entity`, `get_current_state`, `get_state_at_time`, `get_state_history`, `get_state_changes_since`, `get_current_entity_states` continues to pass without test changes.
**Failure mode:** Phase B regresses any subject-state caller.

### B.7 — Performance benchmark: 100K L2 nodes, ≤ 5 ms p95
**[PERF]**
**Pre:** Today's full-table scan on 100K nodes is ~50–200 ms p95 (estimate; may not have a benchmark today).
**Post:** A reproducible benchmark seeds 100K L2 nodes across 10K distinct subjects, runs 1000 random `get_subject_states(subject_id, scope, is_current=True)` queries, and reports p50/p95/p99.
- p95 ≤ 5 ms.
- p99 ≤ 20 ms.
**Verify:**
- `uv run python benchmarks/profile_subject_lookup.py --n-nodes 100000 --n-queries 1000`
- Expected: prints `p95=<5ms` and `p99=<20ms`.
- Reviewer reads the benchmark source to confirm it's an honest stress test (no caching shortcuts).
**Failure mode:** Index doesn't actually help; query is somehow worse than the scan.

### B.8 — `_load_causal_graph` does not regress
**[NEGATIVE]**
**Pre:** Causal-graph load uses a different pattern (`list_graph_edges_by_type`).
**Post:** Phase B does not touch causal-graph-load code. Existing CTLG causal-walker tests continue to pass.
**Verify:**
- `git diff main -- src/ncms/application/tlg/dispatch.py | grep "_load_causal_graph"`
- Expected: no functional change.
- `uv run pytest tests/unit/application/test_ctlg_causal_walker.py -q`
- Expected: 11 tests pass.

---

## Negative claims (regression scope)

### NEG-B.1 — No changes to L2 schema
**[NEGATIVE]**
**Verify:**
- `git diff main -- src/ncms/domain/models.py | grep -E "^[+-]\s+(state_key|state_value|entity_id|valid_from|valid_to|observed_at|ingested_at|is_current)"`
- Expected: empty.

### NEG-B.2 — No changes to `EdgeType` enum
**[NEGATIVE]**
**Verify:**
- `python -c "from ncms.domain.models import EdgeType; assert len(list(EdgeType)) == 14"`

### NEG-B.3 — No new ingest paths
**[NEGATIVE]**
**Verify:**
- `git diff main -- src/ncms/application/ingestion/`
- Expected: empty (Phase B is a read-side change only).

### NEG-B.4 — Reconciliation, episode, abstract paths untouched
**[NEGATIVE]**
**Verify:**
- `git diff main -- src/ncms/application/reconciliation_service.py src/ncms/application/episode_service.py src/ncms/application/consolidation/`
- Expected: empty.

### NEG-B.5 — Lint / complexity / mypy clean
**[NEGATIVE]**
**Verify:**
- `uv run ruff check src/ benchmarks/ tests/` — clean
- `uv run radon cc src/ncms/infrastructure/storage/sqlite_memory_nodes.py -nc --min D` — empty
- `uv run radon mi src/ncms/infrastructure/storage/sqlite_memory_nodes.py -s` — A grade preserved
- `uv run mypy src/ncms/infrastructure/storage/sqlite_memory_nodes.py` — clean

### NEG-B.6 — Existing 1134 unit + 520 integration tests pass
**[NEGATIVE]**
**Verify:**
- `uv run pytest tests/unit -q` — 1134+ pass (the +N is from new B-tests, but no existing test fails)
- `uv run pytest tests/integration -q` — 520+ pass

---

## Behavioral parity tests (mandatory)

| Test | What it proves |
|---|---|
| `tests/unit/infrastructure/storage/test_subject_index.py::test_migration_creates_index` | B.1 — index defined |
| `test_migration_idempotent` | B.2 — re-runnable |
| `test_query_plan_uses_index` | B.3 — optimizer picks it |
| `tests/unit/infrastructure/storage/test_get_subject_states.py::test_filters_compose` | B.4 — helper API |
| `test_subject_lookup_matches_legacy` | B.6 — behavior parity vs `get_entity_states_by_entity` for the simple case |
| `benchmarks/profile_subject_lookup.py` (manual) | B.7 — perf threshold |

---

## Verification script

```bash
bash docs/research/phases/verify_phase_b.sh
```

Output format same as Phase A.

---

## Open questions for codex / reviewer

1. **Should we drop `get_entity_states_by_entity` and the other 5
   legacy helpers** in favor of `get_subject_states` everywhere? The
   claim doc preserves them for back-compat. Aggressive option:
   re-implement the 5 as one-liners that call `get_subject_states`.
   That's cleaner but expands the change surface.

2. **`as_of` semantics under the index.** The index is on
   `entity_id`; `valid_from <= ? AND (valid_to IS NULL OR valid_to >
   ?)` is a separate filter. Does the optimizer combine them
   efficiently? B.3 verifies the simple case; the as-of test should
   verify the complex one.

3. **Should the index also cover `metadata.state_key`?** A composite
   index on `(entity_id, state_key)` would speed up subject+scope
   lookups further. Trade-off: larger index, more disk. Defer to
   Phase E if scope-filtered queries dominate.
