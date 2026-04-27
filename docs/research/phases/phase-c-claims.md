# Phase C Claims — `SubjectBindingContext` at Query Time

**Phase:** C
**Status:** not-yet-started — claim doc only
**Owner:** NCMS core
**Reviewer:** codex 5.5 (format spec at `claims-format.md`)
**Companion:** `subject-centered-graph-design.md` §6 Phase C
**Depends on:** Phases A and B merged

## What Phase C delivers

A `SubjectBindingContext` dataclass plumbed through `search()`,
`recall()`, and `retrieve_lg()`, with deterministic precedence over
synthesizer-derived subject candidates and abstain-on-ambiguous
defaults. MCP tools surface the new fields. CTLG remains
shadow-only at the end of Phase C; the only behavioral change is
that an explicitly-pinned subject is honored.

**PR scope budget:** 1 PR, ≤ 600 lines including tests.
**Estimated total:** 1 week.

---

## Pre-conditions (verify on `main` after Phases A and B merge)

### PC-C.1 — Phases A and B merged
**[BEHAVIOR]**
**Verify:**
- Commit log mentions both phases.
- `python -c "from ncms.domain.models import Subject; from ncms.infrastructure.storage.sqlite_memory_nodes import get_subject_states; print('OK')"`
- Expected: imports without error.

### PC-C.2 — `search()` and `recall()` do not accept context kwargs
**[API]**
**Verify:**
- `grep -E "subject_context|subject_hint|active_document_id" src/ncms/application/memory_service.py`
- Expected: no match.

### PC-C.3 — `retrieve_lg()` synthesizes subject from cue tagger / parser only
**[BEHAVIOR]**
**Verify:**
- `grep -B 2 -A 5 "trace.intent.subject" src/ncms/application/tlg/walkers.py | head -20`
- Expected: walkers read `trace.intent.subject` set by synthesizer / parser.
- `grep "subject_hint\|subject_context" src/ncms/application/tlg/dispatch.py`
- Expected: no match.

### PC-C.4 — TLGQuery has a `subject` field
**[SCHEMA]**
**Verify:**
- `python -c "from ncms.domain.tlg.semantic_parser import TLGQuery; t = TLGQuery(axis='state', relation='current', subject='application:xyz'); print(t.subject)"`
- Expected: `application:xyz`.

### PC-C.5 — CTLG is shadow-only end-to-end
**[BEHAVIOR]**
**Verify:**
- Inspect `benchmarks/mseb/backends/ncms_backend.py::ctlg_shadow_query`.
- Expected: returns diagnostics only; baseline `ranked_mids` not mutated.
- `grep -i "ctlg.*on\|ctlg.*live" docs/research/ctlg-implementation-plan.md`
- Expected: phase 5 lists `ctlg_on` as `[ ]` (not done).
**Failure mode:** Phase C accidentally enables CTLG mutation; not in scope.

### PC-C.6 — Subject-resolver code (deterministic) does not yet exist in `src/`
**[SCHEMA]**
**Verify:**
- `grep -rn "subject_resolver\|SubjectResolver\|resolve_subject_candidates" src/ncms 2>/dev/null | grep -v __pycache__`
- Expected: no match. (The benchmark has shadow diagnostics; src/ is clean.)

### PC-C.7 — MCP tools do not surface subject-context
**[API]**
**Verify:**
- `grep -E "subject_hint|active_document_id|subject_context" src/ncms/interfaces/mcp/tools.py`
- Expected: no match.

---

## Delivered claims

### C.1 — `SubjectBindingContext` dataclass exists
**[SCHEMA]**
**Pre:** No such dataclass.
**Post:** Frozen dataclass in `src/ncms/domain/models.py`:
```python
@dataclass(frozen=True)
class SubjectBindingContext:
    subject_hint: str | None = None
    active_document_id: str | None = None
    active_episode_id: str | None = None
    active_memory_id: str | None = None
    active_project: str | None = None
    active_repo: str | None = None
    active_session_id: str | None = None
```
All fields optional; passing none is equivalent to today's behavior.
**Verify:**
- `python -c "from ncms.domain.models import SubjectBindingContext; ctx = SubjectBindingContext(); print(ctx)"`
- Expected: prints without error.
- `python -c "from ncms.domain.models import SubjectBindingContext; ctx = SubjectBindingContext(subject_hint='application:xyz'); print(ctx.subject_hint)"`
- Expected: `application:xyz`.
**Citation:** `src/ncms/domain/models.py` (NEW field block).

### C.2 — `search()` accepts optional `subject_context`
**[API]**
**Pre:** No such kwarg.
**Post:** `MemoryService.search(..., subject_context: SubjectBindingContext | None = None, ...)` is back-compatible: callers passing nothing get unchanged behavior.
**Verify:**
- `grep "subject_context: SubjectBindingContext" src/ncms/application/memory_service.py`
- Expected: matches in search signature.
- Test: `tests/integration/test_subject_binding_context.py::test_search_without_context_unchanged`.
- Test: `test_search_with_context_threaded_to_dispatch`.

### C.3 — `recall()` accepts optional `subject_context`
**[API]**
**Pre:** No such kwarg.
**Post:** Same shape as C.2.
**Verify:**
- Test: `tests/integration/test_subject_binding_context.py::test_recall_with_context`.

### C.4 — `retrieve_lg()` accepts optional `subject_context`
**[API]**
**Verify:**
- `grep "subject_context: SubjectBindingContext" src/ncms/application/memory_service.py | grep retrieve_lg`
- Expected: matches.
- Test: `tests/integration/test_tlg_subject_binding.py::test_retrieve_lg_pins_subject_hint`.

### C.5 — Subject precedence in dispatcher
**[BEHAVIOR]**
**Pre:** Dispatcher uses synthesizer-derived subject only.
**Post:** Dispatcher resolves the working subject from candidate
sources in this order, returning the first non-empty:
1. `subject_context.subject_hint` — explicit caller pin.
2. Active context hints — when `active_document_id` /
   `active_episode_id` resolves to a document/episode whose primary
   subject is known, use that.
3. CTLG cue tagger's `B-SUBJECT` span (already populated on
   `TLGQuery.subject` by the synthesizer).
4. Parser fallback (`analyze_query` heuristic).
5. Abstain — set `Confidence.NONE` and proof "no subject resolvable."

**Verify:**
- `grep -A 30 "def _resolve_query_subject" src/ncms/application/tlg/dispatch.py`
- Expected: function exists; precedence implemented.
- Test: `tests/integration/test_tlg_subject_binding.py::test_subject_hint_overrides_cue` — passing `subject_hint="application:xyz"` while query says "the app" → walker uses `application:xyz`.
- Test: `test_active_document_overrides_cue` — passing `active_document_id` whose primary subject is `application:xyz` while query says "this application" → walker uses `application:xyz`.
- Test: `test_cue_used_when_no_context` — query "what database for app:xyz?" with no context → walker uses cue's subject.
- Test: `test_abstain_when_all_sources_empty` — deictic query "the framework" with no context, no cue, no parser hit → trace.confidence = NONE.

### C.6 — Active-document lookup resolves to primary subject
**[BEHAVIOR]**
**Pre:** No active-document-to-subject lookup.
**Post:** When `active_document_id` is provided, the dispatcher
loads the document and reads its primary subject from
`memory.structured["subjects"]`. If the document has no subjects,
falls through to the next precedence tier.
**Verify:**
- Test: `tests/integration/test_subject_binding_context.py::test_active_document_with_subject`.
- Test: `test_active_document_without_subject_falls_through`.

### C.7 — Active-episode lookup resolves to primary subject
**[BEHAVIOR]**
**Pre:** No active-episode-to-subject lookup.
**Post:** Same shape as C.6 but for episodes. The episode's primary
subject is the most-frequent primary subject across its member
fragments (count-based, ties broken by recency).
**Verify:**
- Test: `tests/integration/test_subject_binding_context.py::test_active_episode_with_subject`.
- Test: `test_active_episode_majority_primary_subject`.

### C.8 — MCP tools surface subject-context fields
**[API]**
**Pre:** MCP tools don't expose subject context.
**Post:** `search_memory`, `recall_memory`, and `retrieve_lg_query`
MCP tools accept the same optional fields as the Python API.
Schemas updated in `src/ncms/interfaces/mcp/tools.py`.
**Verify:**
- `grep -A 5 "subject_hint\|active_document_id\|active_episode_id" src/ncms/interfaces/mcp/tools.py`
- Expected: matches.
- Test: `tests/integration/test_mcp_subject_context.py::test_mcp_search_with_subject_hint`.

### C.9 — Oracle subject reproduces stress-mini ceiling
**[BEHAVIOR]**
**Pre:** Stress mini reports oracle 9 improved / 3 worse / 22 composed.
**Post:** Running the stress mini with `subject_context.subject_hint = gold_subject` reproduces the oracle numbers within ±1 (stochastic noise floor).
**Verify:**
- `bash benchmarks/mseb/run_main_12.sh --stress --feature subject_context_oracle`
- Expected: `improved >= 8 AND improved <= 10`, `worse <= 4`, `composed >= 21`.
- Reviewer reads the harness change to confirm subject_hint is genuinely consumed by retrieve_lg, not just logged.
**Failure mode:** Phase C plumbing exists but isn't actually consumed by the dispatcher.

### C.10 — Dispatcher diagnostic logs subject-binding source
**[BEHAVIOR]**
**Pre:** Dispatcher logs grammar trace but not subject-binding source.
**Post:** Every CTLG dispatch emits a `query_diagnostic` event
with `subject_binding: {source, subject_id, confidence,
abstain_reason}` populated. Source ∈ `{hint, active_document,
active_episode, cue, parser, abstained}`.
**Verify:**
- `grep "subject_binding" src/ncms/application/diagnostics/search_diag.py`
- Expected: matches.
- Test: `tests/integration/test_subject_binding_diagnostics.py::test_subject_binding_emitted_per_query`.

### C.11 — Conservative abstention preserved on ambiguous queries
**[BEHAVIOR]**
**Pre:** Today's behavior: ambiguous deictic queries fall through; no live regression.
**Post:** Same behavior preserved when `subject_context` is None or empty.
The new active-context resolution adds capability (when context exists)
without removing the abstain default.
**Verify:**
- Test: stress mini run with `subject_context=None` produces 0 improved / 0 worse vs main (matches `conservative subject resolver: 0 / 0` baseline from the design doc §0).
- Reviewer reads the test fixture to confirm: query like "the framework" with no context should NOT pick a subject from BM25 candidates.
**Failure mode:** Abstention regresses; CTLG starts mutating ranking on ambiguous queries.

---

## Negative claims (regression scope)

### NEG-C.1 — CTLG remains shadow-only at end of Phase C
**[NEGATIVE]**
**Verify:**
- `grep -i "ctlg.*on\|ctlg_on" docs/research/ctlg-implementation-plan.md`
- Expected: phase 5 lists `ctlg_on` still as not-done.
- `git diff main -- src/ncms/application/tlg/composition.py`
- Expected: no behavioral change to grammar composition gates.
**Failure mode:** Phase C accidentally turns on live CTLG ranking; that's a separate gated decision.

### NEG-C.2 — No new subject resolver in src/
**[NEGATIVE]**
**Verify:**
- `git diff main -- src/ncms/application/ | grep "subject_resolver\|SubjectResolver\|resolve_subject_candidates"`
- Expected: empty. (Phase D delivers the resolver.)

### NEG-C.3 — No SPLADE / scoring weight changes
**[NEGATIVE]**
**Verify:**
- `git diff main -- src/ncms/application/scoring/ src/ncms/application/retrieval/ src/ncms/infrastructure/indexing/splade_engine.py`
- Expected: empty.
**Failure mode:** Phase C reaches into Phase G's SPLADE-gating scope.

### NEG-C.4 — No edge type or L2 schema changes
**[NEGATIVE]**
**Verify:**
- `git diff main -- src/ncms/domain/models.py | grep -E "^[+-]\s+(state_key|state_value|entity_id)"`
- Expected: empty.
- `python -c "from ncms.domain.models import EdgeType; assert len(list(EdgeType)) == 14"`

### NEG-C.5 — `search()` / `recall()` callers without context: zero behavior change
**[NEGATIVE]**
**Verify:**
- Test: every existing search / recall integration test passes with no test changes.
- `uv run pytest tests/integration -q -k "search or recall or retrieve_lg"`
- Expected: all pass.

### NEG-C.6 — Lint / complexity / mypy / vulture clean
**[NEGATIVE]**
**Verify:**
- `uv run ruff check src/ benchmarks/ tests/` — clean
- `uv run radon cc src/ncms/ -a -nc --min D` — only the two demo orchestrators
- `uv run radon mi src/ncms/ -nc --min B` — empty
- `uv run --with vulture vulture src/ncms/ --min-confidence 80` — clean
- `uv run mypy src/ncms/domain/models.py src/ncms/application/tlg/dispatch.py src/ncms/application/memory_service.py` — clean

### NEG-C.7 — No live ranking changes when context is None
**[PARITY]**
**Verify:**
- Test: `tests/integration/test_subject_binding_context.py::test_no_context_byte_equivalent_to_main`.
- Run with same query against pre-Phase-C and post-Phase-C builds; assert `ranked_mids` are byte-equal.

### NEG-C.8 — Architecture fitness baseline holds
**[NEGATIVE]**
**Verify:**
- `uv run pytest tests/architecture/ -q`
- Expected: 228+ pass (existing 228 plus any new fitness tests).

---

## Behavioral parity tests (mandatory)

| Test | What it proves |
|---|---|
| `tests/integration/test_subject_binding_context.py::test_search_without_context_unchanged` | C.2 — back-compat |
| `test_no_context_byte_equivalent_to_main` | NEG-C.7 — true no-op when off |
| `tests/integration/test_tlg_subject_binding.py::test_subject_hint_overrides_cue` | C.5 — precedence #1 |
| `test_active_document_overrides_cue` | C.5 — precedence #2 |
| `test_cue_used_when_no_context` | C.5 — precedence #3 |
| `test_abstain_when_all_sources_empty` | C.5 — abstention |
| `tests/integration/test_subject_binding_diagnostics.py::test_subject_binding_emitted_per_query` | C.10 — observability |
| `benchmarks/mseb/run_main_12.sh --stress --feature subject_context_oracle` (manual) | C.9 — ceiling reproduction |

---

## Verification script

```bash
bash docs/research/phases/verify_phase_c.sh
```

---

## Decision gate (mandatory after C ships)

After A/B/C are all merged, run the §7 ablation cells (from
`subject-centered-graph-design.md`):

| Cell | Cells to compare |
|---|---|
| Index win | 4 vs 2 |
| Active-context win | 6 vs 5 |
| (Resolver quality) | (skipped — Phase D not built yet) |

Record findings in `docs/research/phases/phase-c-results.md` with
right-recall@1/5/10, MRR, abstention precision, and per-intent
breakdown.

The decision after C:

| Outcome | Next phase |
|---|---|
| Active-context win > 0 on CTLG slice, no regression elsewhere | Proceed to Phase D (deterministic resolver) |
| Active-context wins only when context exists; benefit modest | Proceed to D anyway — resolver will help when context is absent |
| No win even with oracle context | Stop. A/B/C are still wins (multi-subject, faster index, cleaner API). CTLG remains shadow-only. The doc's §9 "Decision" is updated with this finding. |

This is the gate. Do not start Phase D before this decision is
written down.

---

## Open questions for codex / reviewer

1. **`active_memory_id` semantics.** When the user is "looking at"
   memory M, should that bind to M's primary subject, or should it
   only bind if the query is conversational ("for this one, what
   came after?")? Doc says "yes" by default; reviewer to push back
   if that's too aggressive.

2. **`active_session_id` for conversational backends.** The
   conversational SLM doesn't currently emit session subjects.
   Should Phase C wire session→primary-subject lookup through the
   episode service, or punt to Phase D?

3. **Resolver vs context priority.** When Phase D ships its
   resolver, where does it sit in C.5's precedence list?
   Recommended: between cue and parser. Reviewer to confirm before
   D lands.
