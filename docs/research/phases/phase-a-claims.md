# Phase A Claims — Subject Payload + Canonicalization

**Phase:** A
**Status:** not-yet-started — claim doc only
**Owner:** NCMS core
**Reviewer:** codex 5.5 (or any reviewer; format spec at `claims-format.md`)
**Companion:** `subject-centered-graph-design.md` §6 Phase A

## What Phase A delivers

A first-class, multi-subject, alias-canonicalized `Subject` payload
on every persisted Memory, written consistently by every ingest path.
Zero new edge types. Zero changes to the L2 ENTITY\_STATE schema.

**PR scope budget:** ≤ 6 sub-PRs, each independently reviewable.
**Estimated total:** 1.5 weeks.

---

## Pre-conditions (verify on `main` before PR work starts)

If any pre-condition fails, stop. Either the audit was wrong or
`main` has drifted; the doc needs revision before code lands.

### PC-A.1 — `store_memory` accepts `subject: str | None`
**[API]**
**Verify:**
- `grep -n "subject: str | None = None" src/ncms/application/memory_service.py`
- Expected: matches at line ~331 inside `store_memory` signature.

### PC-A.2 — Subject creates an Entity with `type="subject"`
**[BEHAVIOR]**
**Verify:**
- `awk '/Caller-asserted subject/,/^\s*\)/' src/ncms/application/memory_service.py | grep -E '"type":\s*"subject"'`
- Expected: one match. (The previous `-A 8` window was too narrow for the surrounding dict literal; awk reads the whole block until the closing paren.)

### PC-A.3 — `Memory` has no `subject` column today
**[SCHEMA]**
**Verify:**
- `awk '/^class Memory\b/,/^class [A-Z]/' src/ncms/domain/models.py | grep -E "^\s+subject"`
- Expected: no match.

### PC-A.4 — L2 metadata uses singular `entity_id`
**[SCHEMA]**
**Verify:**
- `grep -E "metadata\[.entity_id.\]|entity_id.*subject" src/ncms/application/ingestion/l2_detection.py | head`
- Expected: every reference is singular `entity_id`, never `entity_ids`.

### PC-A.5 — `store_memory` has no `subjects=` kwarg
**[API]**
**Verify:**
- `grep -E "subjects:\s*list" src/ncms/application/memory_service.py`
- Expected: no match.

### PC-A.6 — No `SubjectRegistry` in `src/`
**[SCHEMA]**
**Verify:**
- `grep -rln "SubjectRegistry\|subject_aliases" src/ncms 2>/dev/null | grep -v __pycache__`
- Expected: no output.

### PC-A.7 — 15 `EdgeType` values
**[SCHEMA]**
**Verify:**
- `uv run python -c "from ncms.domain.models import EdgeType; print(len(list(EdgeType)))"`
- Expected: `15`.
- (Original audit said 14; codex pre-flight caught the miscount. The 15 are: belongs\_to\_episode, abstracts, derived\_from, summarizes, mentions\_entity, related\_to, supports, refines, supersedes, superseded\_by, conflicts\_with, current\_state\_of, precedes, caused\_by, enables.)

### PC-A.8 — `IndexTask.subject` is singular
**[SCHEMA]**
**Verify:**
- `grep -E "subject:\s*str" src/ncms/application/index_worker.py`
- Expected: matches at IndexTask dataclass; never `list[str]`.

### PC-A.9 — Memory-creating entry points (categorized)
**[COVERAGE]** (audit existence; PR will update the user-facing ones)

Memories enter the system by THREE distinct mechanisms today.
Phase A must address each separately:

**Category 1 — User-facing `store_memory` API (Phase A target):**
Files that call `MemoryService.store_memory`. These accept caller
input and must canonicalize subjects.

`src/ncms/` callers (production code paths) — only files that
actually invoke `.store_memory(` (verified by the grep below;
`memory_service.py` defines but does not call externally;
`index_worker.py` only mentions in docstrings):
- `src/ncms/application/knowledge_loader.py`
- `src/ncms/application/section_service.py`
- `src/ncms/integrations/nat_memory.py`
- `src/ncms/interfaces/mcp/tools.py`
- `src/ncms/interfaces/http/api.py`
- `src/ncms/interfaces/a2a/server.py`
- `src/ncms/interfaces/agent/base.py`
- `src/ncms/experimental/commit_hook.py` *(experimental, may not require subject support)*
- `src/ncms/demo/run_nemoclaw_demo.py` *(demo, lower priority)*

`benchmarks/` callers (test infrastructure):
- `benchmarks/mseb/backends/ncms_backend.py` — *required to canonicalize*
- `benchmarks/{beir,dream,hub_replay,locomo,longmemeval,memoryagentbench,swebench}/harness.py`
- `benchmarks/mseb/forensic.py`
- `benchmarks/profile_{pipeline,store}.py`
- `benchmarks/tuning/{eval_quality,run_smoke_test,tune_episodes,tune_reconciliation}.py`

**Verify:**
- `grep -rn --include="*.py" "\.store_memory(" src/ncms/ benchmarks/ 2>/dev/null | grep -v __pycache__ | grep -v "/results/" | cut -d: -f1 | sort -u`
- Expected: at minimum the files listed above. Codex must compare
  the actual output against this list — if NEW `.py` files appear
  that aren't enumerated, they may need Phase A treatment too. If
  LISTED files are missing, the audit was wrong.
- The pattern `\.store_memory(` matches method-call sites only —
  it skips `def store_memory` lines (the round-2 leak that put
  `memory_service.py` in the list) and module docstrings (the
  round-3 leak that put `index_worker.py` in the list). The
  `cut -d: -f1 | sort -u` collapses line-number output to unique
  filenames. Round-3 verified output matches the enumerated list
  exactly with `memory_service.py` and `index_worker.py` correctly
  absent (both define/document but do not call `store_memory`
  externally).
- `--include="*.py"` and the `/results/` exclusion together suppress
  log-file false positives codex round-2 flagged.

**Phase A coverage scope decision:**
Of the ~26 callers above, the design must explicitly state which
get the new `subjects=` kwarg threaded through:
- **MUST:** mseb backend, MCP tools, HTTP API, A2A server, agent base — these surface to users.
- **SHOULD:** knowledge_loader, section_service, integrations/nat_memory — used in production flows.
- **MAY/DEFER:** benchmark harnesses other than mseb (test code; can be updated incrementally), demo files, experimental/commit_hook.

Phase A's PR description must enumerate which category each caller
falls into. Reviewer flags as 🚧 Partial if scope is left ambiguous.

**Category 2 — Direct `_store.save_memory` writers (out of scope but
must not regress):**
Internal-system-synthesized memories that bypass `store_memory`.
These do NOT take user-provided subjects; their subject treatment
is design-deferred (see Open Questions §10).
- `src/ncms/application/memory_service.py:457` — the canonical
  `store_memory` path's terminal write
- `src/ncms/application/consolidation_service.py:171` — insight
  memories from clustering
- `src/ncms/application/consolidation_service.py:659` — abstract
  memories from synthesis
- `src/ncms/application/episode_service.py:707` — episode summary
  memories

**Verify:**
- `grep -rn "_store\.save_memory(\|store\.save_memory(" src/ncms/ 2>/dev/null | grep -v __pycache__ | grep -v "def save_memory" | sort`
- Expected: exactly these four sites.
- Failure mode if list grows: a new direct-save path appeared that
  bypasses `store_memory`; Phase A must decide whether to backport
  subjects there or document the exception.

**Category 3 — Documents (separate storage primitive):**
`document_service.publish_document` writes to the `documents` table,
not the `memories` table. It does NOT create a `Memory` row directly;
it produces a Document, and a separate code path may produce a
document profile Memory via `store_memory`. Phase A's contract: the
profile Memory carries the subject; the Document row does not.

**Verify:**
- `grep "save_memory\|store_memory" src/ncms/application/document_service.py`
- Expected: no calls to `save_memory` or `store_memory` in this file.
  (The doc profile is created by a separate caller, e.g. `section_service`.)
- `grep -n "save_document\|self._store\.save_document" src/ncms/application/document_service.py`
- Expected: matches present (the document persistence path).

**Failure mode (any category):** PR ships subject canonicalization in
some paths but not others — the SLM/GLiNER divergence problem.

### PC-A.10 — `link_memory_entity` is the canonical memory→entity attach point
**[COVERAGE]**
**Verify:**
- `grep -rn "link_memory_entity" src/ncms 2>/dev/null | grep -v __pycache__`
- Expected: at least one call site in ingestion or memory_service.
**Failure mode:** PR adds new entity-linking code that bypasses this hook, splitting the graph.

### PC-A.11 — `Memory.structured` survives SQLite round-trip
**[BEHAVIOR]**
Phase A's payload (`memory.structured["subjects"]`) only works if
`Memory.structured` is persisted as JSON and re-hydrated correctly.
Verify the existing path before depending on it.
**Verify:**
- `grep -A 3 "structured" src/ncms/infrastructure/storage/row_mappers.py | head -10`
- Expected: `row["structured"]` is parsed via `json.loads(...)`.
- `uv run pytest tests/integration -k "structured" -q 2>&1 | tail`
- Expected: at least one test asserting `Memory.structured` round-trip works.
- If no such test exists, this PC is ⚠️ Unverifiable until one is added — flag for codex.
**Failure mode:** Phase A writes `subjects` into structured but it doesn't survive read-back; payload is silently lost.

### PC-A.12 — `MemoryService.recall` exists and is the secondary user-facing API
**[API]**
Phase A doesn't modify recall, but Phase C does. Codex should
confirm recall is a real first-class method so Phase C's
pre-conditions are auditable later.
**Verify:**
- `grep -n "async def recall" src/ncms/application/memory_service.py`
- Expected: one match (typically around line 1159).
- `grep -n "memory_svc\.recall\|svc\.recall" src/ncms/interfaces/mcp/tools.py`
- Expected: at least one call site in MCP tools.

### PC-A.13 — MCP store-side tools that need `subjects=` threading
**[API]**
Phase A must thread the new `subjects=` kwarg through every MCP
tool that accepts user content and persists it via
`memory_svc.store_memory(...)`. Two such tools exist today
(both call `store_memory` directly):

1. `store_memory` (line 172) — the canonical write API.
2. `commit_knowledge` (line 322) — session-checkpoint store path;
   round-3 audit caught that I missed this one.

`load_knowledge` (line 444) is also a store-side MCP tool but it
delegates through `KnowledgeLoader.load_file` /
`bulk_load_directory`, so its subject coverage is inherited
through Cat-1 coverage of `KnowledgeLoader` (PC-A.13b makes this
explicit).

`recall_memory` (line 89) is a query-side tool — it does NOT
write memories — so it's NOT in Phase A scope. It belongs to
Phase C (subject-binding context at query time).

`publish_document` does NOT exist as an MCP tool (codex round-2
audit caught my earlier false claim).

**Verify:**
- `grep -nE "^\s+async def (store_memory|commit_knowledge)\b" src/ncms/interfaces/mcp/tools.py`
- Expected: two matches — one for each.
- `grep -n "publish_document" src/ncms/interfaces/mcp/tools.py`
- Expected: NO match.
- `grep -B 30 "memory_svc\.store_memory\|svc\.store_memory" src/ncms/interfaces/mcp/tools.py | grep -E "^\s+async def \w+\(" | sort -u`
- Expected: exactly two distinct functions — `store_memory` and `commit_knowledge`. If a NEW MCP tool calls `store_memory` directly, it must be added to Phase A scope.
- Optional broader audit: `grep -nE "^\s+async def \w+\(" src/ncms/interfaces/mcp/tools.py` enumerates every MCP-decorated function. Reviewer cross-checks that no other tool accepts user-content-to-be-stored beyond the three above (`store_memory`, `commit_knowledge`, `load_knowledge`).

**Failure mode if mismatched:** Phase A targets the wrong MCP
surface (e.g. plumbs subjects into recall but misses
commit_knowledge — repeating the SLM/GLiNER divergence pattern
where one path got updated and others silently didn't).

### PC-A.13b — MCP `load_knowledge` is a separate ingest entry
**[API]**
The MCP `load_knowledge` tool delegates to `KnowledgeLoader`, which
calls `MemoryService.store_memory`. Subject canonicalization there
is inherited via Cat-1 coverage of `KnowledgeLoader` in PC-A.9. PC
exists to make the relationship explicit so reviewer doesn't miss
it as a "new" path.
**Verify:**
- `grep -nE "^\s+async def load_knowledge\b" src/ncms/interfaces/mcp/tools.py`
- Expected: one match.
- `grep -A 30 "async def load_knowledge" src/ncms/interfaces/mcp/tools.py | grep -E "KnowledgeLoader|loader\.load"`
- Expected: at least one match — confirms it delegates to KnowledgeLoader (the import is ~23 lines into the function body, so `-A 30` is the minimum window).

### PC-A.14 — `IngestionPipeline.create_memory_nodes` is the L1/L2 emission point
**[API]**
Phase A modifies L2 emission per-subject. Verify the entry point
shape before depending on it.
**Verify:**
- `grep -n "async def create_memory_nodes\|def create_memory_nodes" src/ncms/application/ingestion/pipeline.py`
- Expected: one match.
- `grep -n "detect_and_create_l2_node" src/ncms/application/ingestion/l2_detection.py`
- Expected: one match (the canonical helper after Phase G.1 DRY).

---

## Delivered claims

### A.1 — `Subject` dataclass exists in `domain.models`
**[SCHEMA]**
**Pre:** No `Subject` class in `src/ncms/domain/models.py`.
**Post:** A frozen Pydantic / dataclass with exactly these fields:
- `id: str` (canonical id, e.g. `"application:xyz"`)
- `type: str` (domain-plugin-defined, e.g. `"application"`)
- `primary: bool` (defaults `True` when only one)
- `aliases: tuple[str, ...]` (always tuple for hashability)
- `source: Literal["caller", "document", "episode", "slm_role", "ctlg_cue", "resolver"]`
- `confidence: float` (range [0.0, 1.0])
**Verify:**
- `uv run python -c "from ncms.domain.models import Subject; s = Subject(id='application:xyz', type='application', primary=True, aliases=(), source='caller', confidence=1.0); print(s)"`
- Expected: prints without error.
- `grep -n "^class Subject" src/ncms/domain/models.py`
- Expected: one match.
**Citation:** `src/ncms/domain/models.py` (NEW).

### A.2 — `memory.structured["subjects"]` is a list-of-dicts
**[SCHEMA]**
**Pre:** `memory.structured` has no `subjects` key after ingest today.
**Post:** Every persisted memory's `structured["subjects"]` is `list[dict]`
where each dict is `Subject.model_dump()`.
**Verify:**
- Existing `bake_intent_slot_payload` writes `intent_slot`; new equivalent must populate `subjects`.
- `grep -A 5 "structured\[\"subjects\"\]" src/ncms/application/ingestion/store_helpers.py`
- Expected: shows the bake call.
- Integration: `tests/integration/test_subject_payload.py::test_subjects_persisted_after_ingest`.
**Failure mode:** Some ingest paths write the payload, others don't — silent divergence.

### A.3 — `store_memory` accepts both `subject=str` and `subjects=list[Subject]`
**[API]**
**Pre:** Only `subject: str | None` exists today.
**Post:** Signature is `store_memory(..., subject: str | None = None, subjects: list[Subject] | None = None, ...)`. When both are provided, `subjects` wins; when only `subject` is provided, it's promoted into a one-element list with `source="caller"`, `primary=True`. Passing both with conflicting primary values raises `ValueError`.
**Verify:**
- `grep -E "subject: str \| None|subjects: list\[Subject\] \| None" src/ncms/application/memory_service.py`
- Expected: both lines present.
- Test: `tests/integration/test_subject_payload.py::test_legacy_subject_string_promoted_to_list`.
- Test: `tests/integration/test_subject_payload.py::test_subjects_list_takes_precedence_over_subject_string`.
- Test: `tests/integration/test_subject_payload.py::test_conflicting_primaries_raises`.

### A.4 — `SubjectRegistry` table + alias canonicalization
**[SCHEMA]**
**Pre:** No `subject_aliases` table.
**Post:** New table `subject_aliases (canonical_id TEXT, type TEXT, alias TEXT, created_at TEXT, PRIMARY KEY (canonical_id, alias))` plus `subjects (canonical_id TEXT PRIMARY KEY, type TEXT, created_at TEXT)`. Migration applies on existing DBs without breaking.
**Verify:**
- `grep "CREATE TABLE.*subject_aliases\|CREATE TABLE.*subjects\b" src/ncms/infrastructure/storage/migrations.py`
- Expected: both tables defined.
- Test: `tests/unit/infrastructure/storage/test_subject_registry.py::test_migration_applies_on_existing_db`.

### A.5 — `SubjectRegistry.canonicalize` returns deterministic ids
**[BEHAVIOR]**
**Pre:** No canonicalization exists.
**Post:** `SubjectRegistry.canonicalize(surface: str, type_hint: str | None, domain: str | None) -> Subject` returns a `Subject` with:
- Existing canonical id when alias matches (exact or normalized lowercase).
- Newly minted canonical id otherwise; alias persisted before return.
- `confidence` reflects which lookup tier hit (exact: 1.0; fuzzy: 0.85; minted: 0.6).
**Verify:**
- Test: `tests/unit/application/test_subject_registry.py::test_alias_variants_resolve_to_one_canonical` — ingest "application xyz", "the xyz service", "app xyz" → all return same canonical id.
- Test: `test_minted_id_persists_aliases`.
- Test: `test_exact_match_higher_confidence_than_fuzzy`.

### A.6 — Multi-subject ingest emits one L2 per affected timeline
**[BEHAVIOR]**
**Pre:** Today a memory has at most one L2.
**Post:** When `subjects=[A, B]` and the SLM declares a state change, ingest emits one L2 per subject whose state changed. `entity_id` of each L2 is the corresponding canonical id. SUPERSEDES/SUPERSEDED_BY edges flow through reconciliation per timeline independently.
**Verify:**
- Test: `tests/integration/test_multi_subject_ingest.py::test_two_subjects_two_l2_nodes` — ingest "ADR-004 supersedes ADR-002 for auth-service" with `subjects=[adr:004, adr:002, service:auth-api]`. Assert two L2 nodes (one for each ADR), reconciliation flips ADR-002 to `is_current=False`, no L2 for the service if no service-state event.
- Test: `test_co_subjects_get_mentions_entity_role_metadata` (see A.7).

### A.7 — Co-subjects get `MENTIONS_ENTITY` edges with role metadata
**[SCHEMA]** **[BEHAVIOR]**
**Pre:** `MENTIONS_ENTITY` edges exist but `metadata.role` is unset.
**Post:** For each subject in `memory.structured["subjects"]`, the ingest creates a `MENTIONS_ENTITY` edge from the memory's L1 atomic node to the subject Entity, with `metadata.role` ∈ `{"primary_subject", "co_subject"}`. Other entity mentions (GLiNER / SLM slot extractions that aren't subjects) have no `role` key.
**Verify:**
- `grep "metadata.*role" src/ncms/application/ingestion/pipeline.py | grep "subject"`
- Expected: matches.
- Test: `tests/integration/test_multi_subject_ingest.py::test_primary_and_co_subject_edges_have_role`.
- Test: `test_non_subject_entity_mention_has_no_role`.
**Failure mode:** Subject and non-subject mentions become indistinguishable; resolver can't filter.

### A.8 — Inline ingest path writes canonical subjects
**[COVERAGE]**
**Verify:**
- Test: `tests/integration/test_subject_payload_parity.py::test_inline_path_canonicalizes`.
**Citation:** `src/ncms/application/memory_service.py`, `src/ncms/application/ingestion/pipeline.py`, `src/ncms/application/ingestion/store_helpers.py`.

### A.9 — Async indexing path writes canonical subjects
**[COVERAGE]**
**Verify:**
- Test: `tests/integration/test_subject_payload_parity.py::test_async_path_canonicalizes`.
- Inspect `IndexTask` schema includes `subjects: list[Subject]`.
**Citation:** `src/ncms/application/index_worker.py`.
**Failure mode:** *Same as the SLM/GLiNER divergence problem we already paid for.*

### A.10 — Document publish writes canonical subjects
**[COVERAGE]**
**Verify:**
- Test: `tests/integration/test_subject_payload_parity.py::test_document_publish_canonicalizes`.
- Documents with a `parent_doc_id` inherit the parent's primary subject by default.
**Citation:** `src/ncms/application/document_service.py`.

### A.11 — Reindex preserves canonical subjects
**[COVERAGE]**
**Verify:**
- Test: `tests/integration/test_reindex_subjects.py::test_reindex_preserves_subject_payload`.
- Reindex over existing memories writes `structured["subjects"]` if missing (back-fill).
**Citation:** `src/ncms/application/reindex_service.py`.

### A.12 — Knowledge loader writes canonical subjects
**[COVERAGE]**
**Verify:**
- Test: `tests/integration/test_knowledge_loader_subjects.py::test_loader_canonicalizes_subjects`.
**Citation:** `src/ncms/application/knowledge_loader.py`.

### A.13 — MSEB backend writes canonical subjects
**[COVERAGE]**
**Verify:**
- Inspect `benchmarks/mseb/backends/ncms_backend.py::ingest`. The current code passes `subject=m.subject`; updated code passes `subjects=[Subject(id=m.subject, ...)]` after canonicalization.
- `subject_map` derived from canonical ids has zero alias splits on the MSEB stress mini.
- Test: `tests/integration/test_mseb_subject_canonicalization.py`.
**Citation:** `benchmarks/mseb/backends/ncms_backend.py`.

### A.14 — Inline + async paths produce byte-equivalent subject payload
**[PARITY]**
**Pre:** No parity test exists between paths today (cf. SLM/GLiNER divergence).
**Post:** A test ingests the same content through both paths and asserts equality on:
- `memory.structured["subjects"]` (canonical ids, types, ordering)
- L2 nodes per subject (count, entity_id, state_key, state_value)
- `MENTIONS_ENTITY` edges with role metadata (primary_subject / co_subject sets equal)
**Verify:**
- Test: `tests/integration/test_subject_payload_parity.py::test_inline_async_byte_equivalent`.
- Reviewer reads the test source to confirm both paths are exercised on identical input.
**Failure mode:** Recurrence of the SLM/GLiNER divergence pattern.

### A.15 — Subject types come from domain.yaml plugins, not a global enum
**[SCHEMA]**
**Pre:** No subject type registry exists.
**Post:** `domain.yaml` for software_dev / clinical / conversational lists `subject_types: [...]`. `SubjectRegistry` validates `type` against the loaded domain spec; unknown types are accepted (string-typed, low confidence) but logged for review.
**Verify:**
- `grep "subject_types" adapters/domains/software_dev/domain.yaml`
- Expected: matches.
- Test: `tests/unit/application/adapters/test_domain_loader.py::test_subject_types_loaded`.
- Test: `test_unknown_subject_type_accepted_with_warning`.
**Failure mode:** Subject types drift into a brittle classifier — the `shape_intent` pattern.

### A.16 — Alias collision audit event
**[BEHAVIOR]**
**Pre:** No alias collision tracking.
**Post:** When canonicalize() picks an existing canonical id via fuzzy match (confidence < 1.0), it emits a `subject.alias_collision` dashboard event with `{surface, picked_canonical, confidence, alternatives}`. Reviewer can grep these events to spot wrong canonicalization.
**Verify:**
- `grep "alias_collision" src/ncms/application/subject_registry.py`
- Expected: matches.
- Test: `tests/integration/test_subject_alias_collision.py::test_event_emitted_on_fuzzy_match`.

---

## Negative claims (regression scope)

### NEG-A.1 — L2 ENTITY_STATE node schema unchanged
**[NEGATIVE]**
**Verify:**
- `git diff main -- src/ncms/domain/models.py | grep -E "^[+-]\s+(state_key|state_value|state_previous|state_alternative|valid_from|valid_to|observed_at|ingested_at|is_current)"`
- Expected: no output.
**Failure mode:** Phase A inadvertently changed L2 fields; reconciliation regression risk.

### NEG-A.2 — `EdgeType` enum has 15 values (unchanged from Phase A start)
**[NEGATIVE]**
**Verify:**
- `uv run python -c "from ncms.domain.models import EdgeType; assert len(list(EdgeType)) == 15"`
- Expected: assertion holds.
**Failure mode:** A new edge type slipped in despite the design saying "no new edge types in Phase A."

### NEG-A.3 — Reconciliation logic unchanged
**[NEGATIVE]**
**Verify:**
- `git diff main -- src/ncms/application/reconciliation_service.py`
- Expected: no functional changes (comment / docstring updates allowed).
**Failure mode:** Reconciliation silently changed behavior; supersession/conflicts regress.

### NEG-A.4 — Bitemporal field semantics unchanged
**[NEGATIVE]**
**Verify:**
- Test: `tests/integration/test_bitemporal_wiring.py` continues to pass with no changes.
- Run: `uv run pytest tests/integration/test_bitemporal_wiring.py -q`.
- Expected: all pass; no test was modified.

### NEG-A.5 — Causal-edge ingest path unchanged
**[NEGATIVE]**
**Verify:**
- `git diff main -- src/ncms/application/ingestion/causal_edges.py`
- Expected: no change.

### NEG-A.6 — TLG dispatch / walkers unchanged
**[NEGATIVE]**
**Verify:**
- `git diff main -- src/ncms/application/tlg/ src/ncms/domain/tlg/`
- Expected: no change.
**Failure mode:** Phase A reached into CTLG; that's Phase C's scope.

### NEG-A.7 — Existing 6 sqlite_memory_nodes helpers unchanged
**[NEGATIVE]**
**Verify:**
- `git diff main -- src/ncms/infrastructure/storage/sqlite_memory_nodes.py`
- Expected: no change to existing function bodies (new helpers are Phase B).

### NEG-A.8 — `search()` and `recall()` API unchanged
**[NEGATIVE]**
**Verify:**
- `git diff main -- src/ncms/application/memory_service.py | grep -E "async def (search|recall)"`
- Expected: signature lines unchanged.
**Failure mode:** Phase A reached into the query API; that's Phase C.

### NEG-A.9 — No new SQLite indexes on memory_nodes
**[NEGATIVE]**
**Verify:**
- `git diff main -- src/ncms/infrastructure/storage/migrations.py | grep -E "^\+CREATE INDEX.*memory_nodes"`
- Expected: no new index. (`idx_mnodes_subject` is Phase B.)
**Failure mode:** Phase A pulled in Phase B work; bigger PR than scoped.

### NEG-A.10 — Lint / complexity / vulture / mypy clean
**[NEGATIVE]**
**Verify:**
- `uv run ruff check src/ benchmarks/ tests/` — clean
- `uv run radon cc src/ncms/ -a -nc --min D` — only the two demo orchestrators (already accepted)
- `uv run radon mi src/ncms/ -nc --min B` — empty
- `uv run --with vulture vulture src/ncms/ --min-confidence 80` — clean
- `uv run mypy src/ncms/domain/models.py src/ncms/application/subject_registry.py src/ncms/application/memory_service.py` — clean

---

## Behavioral parity tests (mandatory)

The following tests are required, not optional. The PR cannot merge
without them green.

| Test | What it proves |
|---|---|
| `tests/integration/test_subject_payload_parity.py::test_inline_async_byte_equivalent` | A.14 — inline + async paths agree |
| `tests/integration/test_subject_payload_parity.py::test_inline_async_with_co_subjects` | Multi-subject parity |
| `tests/integration/test_multi_subject_ingest.py::test_two_subjects_two_l2_nodes` | A.6 — N timelines, N L2s |
| `tests/integration/test_multi_subject_ingest.py::test_primary_and_co_subject_edges_have_role` | A.7 — role metadata correct |
| `tests/unit/application/test_subject_registry.py::test_alias_variants_resolve_to_one_canonical` | A.5 — canonicalization |

---

## Verification script

A reviewer (or codex) can run a single command after the PR is up:

```bash
bash docs/research/phases/verify_phase_a.sh
```

The script runs through every Pre-condition, Delivered claim, and
Negative claim, reporting pass/fail per ID. Output format:

```
PC-A.1 ✅ store_memory accepts subject:str|None
PC-A.2 ✅ Subject creates Entity type='subject'
...
A.6 ❌ Multi-subject ingest emits one L2 per affected timeline
       expected: 2 L2 nodes; got: 1
       see tests/integration/test_multi_subject_ingest.py::test_two_subjects_two_l2_nodes
...
NEG-A.2 ✅ EdgeType has 14 values
```

If the script doesn't exist yet, this doc is enough — codex can
verify each claim manually using the `Verify:` lines.

---

## Open questions for codex / reviewer

1. **Should Phase A include the alias-collision review queue UI**, or
   is logging an event enough for v1? Doc says "review queue" in
   §8 risks; this claim doc says "events." Reviewer to flag.

2. **Does the SLM `primary` role span auto-suggest a subject when
   the caller doesn't provide one?** §3.5 of the design doc says
   yes (priority 3 in the subject set); this claim doc doesn't
   require it. Should A.16 be expanded to cover this?

3. **Backwards-compat for existing memories** — should reindex
   back-fill `structured["subjects"]` for memories that predate this
   PR? Claim A.11 says yes; reviewer to confirm scope.

These should be resolved before code lands, not during.
