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
- `grep -B 1 -A 8 "Caller-asserted subject" src/ncms/application/memory_service.py | grep -E '"type":\s*"subject"'`
- Expected: matches.

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

### PC-A.7 — 14 `EdgeType` values
**[SCHEMA]**
**Verify:**
- `python -c "from ncms.domain.models import EdgeType; print(len(list(EdgeType)))"`
- Expected: `14`.

### PC-A.8 — `IndexTask.subject` is singular
**[SCHEMA]**
**Verify:**
- `grep -E "subject:\s*str" src/ncms/application/index_worker.py`
- Expected: matches at IndexTask dataclass; never `list[str]`.

### PC-A.9 — Every ingest path that writes Memory exists
**[COVERAGE]** (audit existence; PR will update them)
**Verify:** the following files all contain a call into `MemoryService.store_memory` or `IngestionPipeline.create_memory_nodes`:
- `src/ncms/application/memory_service.py` — inline path
- `src/ncms/application/index_worker.py` — async pool path
- `src/ncms/application/document_service.py` — document publish
- `src/ncms/application/reindex_service.py` — reindex path
- `src/ncms/application/knowledge_loader.py` — bulk import
- `src/ncms/application/section_service.py` — section ingest
- `benchmarks/mseb/backends/ncms_backend.py` — MSEB harness

**Verify:**
- `grep -ln "store_memory\|create_memory_nodes" <each file>`
- Expected: each file has at least one match.
**Failure mode:** PR ships subject canonicalization in some paths but not others — the SLM/GLiNER divergence problem.

### PC-A.10 — `link_memory_entity` is the canonical memory→entity attach point
**[COVERAGE]**
**Verify:**
- `grep -rn "link_memory_entity" src/ncms 2>/dev/null | grep -v __pycache__`
- Expected: at least one call site in ingestion or memory_service.
**Failure mode:** PR adds new entity-linking code that bypasses this hook, splitting the graph.

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
- `python -c "from ncms.domain.models import Subject; s = Subject(id='application:xyz', type='application', primary=True, aliases=(), source='caller', confidence=1.0); print(s)"`
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

### NEG-A.2 — `EdgeType` enum has 14 values (unchanged)
**[NEGATIVE]**
**Verify:**
- `python -c "from ncms.domain.models import EdgeType; assert len(list(EdgeType)) == 14"`
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
