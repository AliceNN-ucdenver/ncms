# Phase B Claims — Subject Index + Lookup Helpers

> **🏁 RETIRED — Phase B complete.**
>
> Phase B shipped: ``idx_mnodes_subject`` partial index on the
> subject-id JSON path, the new ``get_subject_states`` helper
> with composable filters (added to ``MemoryStore`` protocol +
> ``SQLiteStore`` delegate), the walker ``_load_subject_zones``
> cut over to direct calls of the new helper, 4 of 6 legacy
> helpers rewritten as wrappers (single source of truth for the
> subject-state SQL), and a real pytest perf test that asserts
> p95 < 5ms / p99 < 20ms on 10K rows × 1K subjects.
>
> Schema bumped 14 → 15.  No changes to L2 schema, no new edge
> types, no ingest-path changes (read-side optimization only).
>
> The claim doc was locked at the start of execution under the
> user's Option-B sign-off (B.4 + B.6 + B.7 rewrite + Open
> Questions → Resolved Decisions); no further edits during the
> work.  All 18 callers of legacy helpers continue to work
> unchanged because public signatures are preserved.
>
> 1685 tests pass; ruff / mypy (Phase B surface) / vulture clean.
>
> This document is preserved for audit history; do not edit further.
> Phase C claim doc remains in ``docs/research/phases/`` until
> Phase C lands.

**Phase:** B
**Status:** ✅ retired — implementation complete and merged
**Owner:** NCMS core
**Reviewer:** codex 5.5 (format spec at `claims-format.md`)
**Companion:** `../research/subject-centered-graph-design.md` §6 Phase B
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

## Pre-conditions (verify on the commit immediately after Phase A merges)

> **Reviewer note:** these pre-conditions are NOT verifiable on
> `main` today. They become verifiable after Phase A's PR merges.
> A pre-flight audit before Phase A code lands should report these
> as "deferred — Phase A not yet merged" rather than ❌ failures.

### PC-B.1 — Phase A merged
**[BEHAVIOR]**
**Verify:**
- `git log --oneline | grep -iE "Phase A.*subject|subject.*payload"`
- Expected: at least one matching commit.
- `uv run python -c "from ncms.domain.models import Subject; print(Subject)"`
- Expected: imports without error and prints the Subject class.

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

### PC-B.7 — Architecture fitness baseline (no NEW D+ vs Phase A merge commit)
**[NEGATIVE]**
The CTLG in-flight work introduced 3 D-grade methods that fail the
fitness gate today:
- `application/adapters/ctlg/generator.py::_clean_generated_cue_noise`
- `application/entity_extraction_mode.py::structured_slm_entities`
- `domain/tlg/semantic_parser.py::_slm_grounding`

These are tracked as accepted in-flight debt. Phase B's gate is
"no NEW regressions vs the post-Phase-A merge commit," not "228
pass." The 3 known fails stay; the count must not grow.

**Verify:**
- `uv run pytest tests/architecture/ -q 2>&1 | tail -3`
- Expected: failure list is exactly the 3 above; pass count ≥ Phase A merge baseline.
- `uv run radon cc src/ncms/ -a -nc --min D 2>&1 | grep -v "demo/"`
- Expected: exactly 3 D+ methods (the 3 above), no others. (Demo orchestrators are accepted out-of-scope.)
**Failure mode:** Phase B introduces a 4th D+ regression beyond the in-flight CTLG set.

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
    is_current: bool | None = None,
    limit: int | None = None,
) -> list[MemoryNode]:
```
- `scope` filters by `metadata.state_key`.
- `is_current` filters by the column.
- `limit` caps result size.
- All filters are optional and compose.
- Result ordering: `created_at DESC` (most-recent first).
- The query uses `idx_mnodes_subject` for the subject_id lookup
  (verified by B.3's `EXPLAIN QUERY PLAN` test).

**Scope note:** `as_of` was removed from the original draft after
code audit.  Bitemporal range queries (`valid_from <= ? AND
valid_to > ?` with fallback) are fundamentally different from a
single-subject filter — they live in `get_state_at_time`, which
is one of the two helpers NOT folded into the new shape (see B.6).

`get_subject_states` is also added to the `MemoryStore` protocol
(`src/ncms/domain/protocols.py`) and exposed as a thin delegate
on `SQLiteStore` so application-layer callers reach it through
the protocol — same pattern as `find_memory_by_doc_id` in Phase A.

**Verify:**
- `grep -A 10 "^async def get_subject_states" src/ncms/infrastructure/storage/sqlite_memory_nodes.py`
- Expected: matches the signature above.
- `grep -n "get_subject_states" src/ncms/domain/protocols.py src/ncms/infrastructure/storage/sqlite_store.py`
- Expected: protocol declaration + thin SQLiteStore delegate.
- Tests: `tests/unit/infrastructure/storage/test_subject_index.py` covers each filter combination + filter composition.

### B.5 — `_load_subject_zones` routes through the new helper
**[COVERAGE]**
**Pre:** `_load_subject_zones` calls `store.get_entity_states_by_entity(subject)` (`tlg/dispatch.py:139`).
**Post:** Updated to call `store.get_subject_states(subject_id=subject)` with no other behavior change.
**Verify:**
- `grep "get_subject_states\|get_entity_states_by_entity" src/ncms/application/tlg/dispatch.py`
- Expected: `get_subject_states` present; legacy call removed (or kept only in a deprecation comment).
- Test: existing TLG dispatcher tests in `tests/integration/test_tlg_dispatch.py` continue to pass with no test changes.
**Failure mode:** Walkers diverge between paths or behavior changes.

### B.6 — 4 of 6 legacy helpers become wrappers; 2 stay literal
**[NEGATIVE]**
**Pre:** 6 helpers exist with current signatures and 18 callers
across application + interfaces (verified by audit).
**Post:** All 6 still exist with unchanged public signatures and
behavior.  Internal implementations split into two groups:

| Helper | Disposition |
|---|---|
| `get_entity_states_by_entity` | wrapper → `get_subject_states(entity_id)` |
| `get_current_entity_states` | wrapper → `get_subject_states(..., is_current=True)` |
| `get_current_state` | wrapper → `get_subject_states(..., limit=1)` returning first or None |
| `get_state_history` | wrapper → `get_subject_states(...)` then `reversed(...)` (preserves ASC ordering) |
| `get_state_at_time` | **stays literal** — bitemporal range query (`valid_from`/`valid_to` + fallback) doesn't fit a single-subject filter |
| `get_state_changes_since` | **stays literal** — global query, no `metadata.entity_id` filter, the new index does not apply |

The 4 wrappers are thin enough that every existing caller's
behavior is byte-equivalent.  The 2 literal helpers are unchanged.
18 caller sites (in `reconciliation_service`, `retrieval/pipeline`,
`traversal/pipeline`, `enrichment/pipeline`, `consolidation_service`,
`diagnostics/search_diag`, MCP tools, MCP resources, CLI export,
CLI main, HTTP dashboard, HTTP api) continue to work without
changes because protocol + SQLiteStore signatures are preserved.
**Verify:**
- `git diff main -- src/ncms/infrastructure/storage/sqlite_memory_nodes.py | grep -E "^[-+]async def get_"`
- Expected: only additions of `get_subject_states`; no removals or signature changes to the 6 existing helpers.
- Test: every test in `tests/unit/infrastructure/test_sqlite_entity_state_store.py` (6 test classes covering all 6 helpers) continues to pass without test changes — that's the parity gate.
**Failure mode:** Phase B regresses any subject-state caller.

### B.7 — Performance test: 10K L2 nodes, ≤ 5 ms p95
**[PERF]**
**Pre:** No subject-lookup performance test exists.
**Post:** A checked-in pytest test seeds 10K L2 nodes across 1K
distinct subjects, runs 100 random
`get_subject_states(subject_id, is_current=True)` queries, and
asserts:
- p95 ≤ 5 ms.
- p99 ≤ 20 ms.

10K not 100K so the test runs in under ~10 seconds in CI; the
SHAPE of the verify (does the index actually help) is what
matters, not the absolute count.  Marked with a slow-test
marker for callers who want to skip it.
**Verify:**
- `uv run pytest tests/integration/test_subject_index_perf.py -q`
- Expected: passes.  Test source asserts p95 < 5ms and p99 < 20ms.
- Reviewer reads the test source to confirm it's an honest stress
  test (no caching shortcuts; subject_ids drawn uniformly random).
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

### NEG-B.2 — No changes to `EdgeType` enum (count unchanged at 15)
**[NEGATIVE]**
**Verify:**
- `uv run python -c "from ncms.domain.models import EdgeType; assert len(list(EdgeType)) == 15"`
- Expected: assertion holds.

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

## Resolved decisions (locked before execution)

The original draft had 3 open questions.  Each is answered below
so the doc is a binding contract, not a discussion document.

1. **Legacy helpers** — 4 of 6 become wrappers around
   `get_subject_states`; 2 stay literal because their query shapes
   are fundamentally different (see B.6 disposition table).
   Public signatures preserved; 18 caller sites unaffected.

2. **`as_of` semantics** — dropped from the new helper's API.
   Bitemporal range queries (`valid_from`/`valid_to` + fallback)
   stay in `get_state_at_time`; the new helper handles the
   common subject-filter case only.  Cleaner separation of
   responsibilities.

3. **Composite `(entity_id, state_key)` index** — not added.
   Single-column index is enough; the perf test (B.7) validates
   p95 < 5ms threshold.  If the test fails on the simple case,
   add the composite then — but we lock no-composite as the
   default and refuse to "defer to Phase E."
