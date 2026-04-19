# P1 Integration Plan — Temporal Linguistic Geometry → NCMS

*Planning document · 2026-04-18 · Revised post-TLG validation.*

**This is a PLAN, not an implementation.**  No code changes yet.
Every section describes what will happen, in what order, under
what feature flag.  Reviewers and future agents should treat this
as the authoritative spec for the TLG→NCMS integration work.

### Terminology

Two parallel numbering schemes appear in the broader project:

| Term | Where it lives | What it numbers |
|---|---|---|
| **Phase n** (P0, P1, ..., P7) | This document (`docs/p1-plan.md`) | NCMS code-shipping phases of the TLG integration.  Each phase is one or more PRs. |
| **Milestone Mn** (M1 – M8) | TLG pre-paper (`docs/temporal-linguistic-geometry.md` §7.6) | Research work-items that feed the paper roadmap.  Some milestones (notably M2 = NCMS production integration) correspond to phase-level work in this plan. |

They are not the same.  When this document says "Phase 3" it
means NCMS integration code being shipped.  When the TLG paper
says "M2" it means the research work of getting the NCMS
integration running and measured.  M2 and this plan's Phase 0 –
Phase 7 jointly produce the evidence for the paper's Stage 2
submission.

**Legacy terms.**  The repository still carries `p1-*.md` docs in
`docs/retired/` referring to an earlier P1 effort (numeric
temporal boosting — P1a, P1b-v1, P1b-v2).  That lineage is
terminated; TLG integration is what "P1" means going forward.
Appendix A covers the retirement move.

---

## 0. Summary

TLG (Temporal Linguistic Geometry, documented in
`docs/temporal-linguistic-geometry.md`) is ready for integration
into NCMS.  Pre-paper validation: 32/32 structured, 15/15
adversarial, 15/15 taxonomy, **0/500 confidently-wrong on
LongMemEval**, scale-regressed to 50 k memories.  The
integration replaces a sequence of failed P1/P1a/P1b attempts
with a single principled framework.

This plan:

1. **Retires** the old P1 documentation and deprecates superseded
   code/flags from the failed approaches.
2. **Integrates** TLG as a new grammar layer inside NCMS's
   retrieval pipeline, feature-flag-gated.
3. **Extends** `ReconciliationService` to produce the typed-edge
   annotations TLG needs (`retires_entities`).
4. **Updates** the design spec (`ncms-design-spec.md`) to reflect
   TLG as the temporal-retrieval story.
5. **Revises** `p2-plan.md` in light of TLG's lessons — proposes
   whether P2 needs its own TLG-style exploratory experiment.

No production code changes happen until this plan is reviewed
and approved.

---

## 1. Target architecture

TLG sits in the retrieval pipeline as a **composable grammar
stage** that runs in parallel with (not instead of) BM25 +
SPLADE + graph spreading activation.

```
┌─────────────────────────────────────────────────────────────────┐
│  query                                                          │
└─────────────┬───────────────────────────────┬───────────────────┘
              │                               │
              ▼                               ▼
    ┌──────────────────┐              ┌──────────────────┐
    │  TLG grammar     │              │  Hybrid retriever│
    │  (new)           │              │  (existing)      │
    │                  │              │                  │
    │  productions     │              │  BM25 + SPLADE   │
    │  zone compute    │              │  + graph         │
    │  confidence      │              │  + ACT-R         │
    └────────┬─────────┘              └────────┬─────────┘
             │                                 │
             ▼                                 ▼
      grammar_answer                    bm25_ranking
      + confidence                      (always produced)
             │                                 │
             └────────────┬────────────────────┘
                          ▼
            ┌─────────────────────────────┐
            │  has_confident_answer() ?   │
            └────┬───────────────────┬────┘
               YES                  NO
                │                    │
                ▼                    ▼
      grammar prepended       BM25 ordering
      to rank-1 + zone        preserved unchanged
      context + BM25 tail     (Proposition 1)
```

**The composition invariant (Proposition 1 of the pre-paper):**
when the grammar abstains, BM25's ordering is returned
unchanged.  NCMS integration preserves this; that's the
integration-safety property.

---

## 2. What retires

### 2.1 Documents

Already retired in `docs/retired/`:

| Doc | Reason |
|---|---|
| `p1-measurement.md` | P1a zero-impact measurement; TLG supersedes |
| `p1b-redesign.md` | Pool-wide + subject-scoped ordinal rerank; both variants measured and rejected |
| `p1-temporal-findings.md` | Early hypothesis scaffolding, superseded |
| `p1-temporal-usecases.md` | Early hypothesis scaffolding, superseded |

**To retire in this plan:**

| Doc | Action | Rationale |
|---|---|---|
| `docs/p1-temporal-experiment.md` | Move to `docs/retired/` with a "superseded by TLG" banner | 754-line post-pivot plan (metadata-anchored + intent-router) is replaced by TLG's grammar approach; useful as historical record |

**Keep as-is:**

| Doc | Rationale |
|---|---|
| `docs/research-longmemeval-temporal.md` | Direct paper analysis; still relevant context for TLG evaluation |
| `docs/p1-experiment-diary.md` | Build-log including TLG entries; historical record |
| `docs/temporal-linguistic-geometry.md` | The TLG pre-paper |
| `docs/tlg-scale-validation.md` | Scale regression report |

**Keep but update:**

| Doc | Update |
|---|---|
| `docs/ncms-design-spec.md` | §2.1, §2.2, §4.x, §5B need rewrites (see §8 below) |
| `docs/design-query-performance.md` | Replace "P1/P1a/P1b attempts" narrative with "TLG is the temporal story" summary |
| `docs/p2-plan.md` | Revise given TLG's lessons (see Appendix C) |

### 2.2 Code (dead / superseded)

A comprehensive audit (2026-04-18) surfaced the full inventory of
temporal-boosting code.  See Appendix F for the complete line-by-
line list.  Summary here.

**Dead (already removed, verify):**

* `apply_ordinal_rerank` (pool-wide ordinal reranker) — removed
  2026-04-18 per diary.
* `apply_subject_scoped_ordinal_rerank` — removed 2026-04-18.

**Action:** `grep -r "apply_ordinal_rerank\|apply_subject_scoped_ordinal_rerank" src/ tests/ benchmarks/`
must return zero hits; any stragglers delete in Phase 0.

**Retire (the pre-TLG Phase B retrieval-side temporal stack):**

TLG's grammar layer subsumes the ordinal / range-filter / intent-
classification logic built during the failed P1/P1a/P1b experiments.
These are all LIVE but feature-flagged off; integration removes
them (behind the same flag TLG takes over).

| Location | Lines | Role | TLG replacement |
|---|---:|---|---|
| `src/ncms/domain/temporal_intent.py` | ~240 | `TemporalIntent` enum (NONE/ORDINAL_SINGLE/ORDINAL_COMPARE/ORDINAL_ORDER/RANGE/RELATIVE_ANCHOR/ARITHMETIC) + `classify_temporal_intent()` | TLG's 13-intent production grammar |
| `application/retrieval/pipeline.py::apply_ordinal_ordering` | 315–399 | Reorder top-K by `observed_at` for single-subject / cross-subject ordinal | TLG `origin` / `sequence` / `predecessor` / `before_named` intent handlers |
| `application/retrieval/pipeline.py::apply_range_filter` | 449–511 | Hard-filter candidates by `[a,b) ∩ [c,d)` overlap with memory content ranges | TLG `range` intent handler (with `missing_range_policy`) |
| `application/retrieval/pipeline.py::split_entity_and_temporal_spans` | 288–313 | Partition GLiNER output into entity-only + temporal spans | Helper — **keep** (TLG range-intent consumer) |
| `application/retrieval/pipeline.py::resolve_temporal_range` | 435–447 | Merge normalized spans → query-range interval | Helper — **keep** (TLG range-intent consumer) |
| `application/memory_service.py::_extract_query_range` | 521–561 | Query-side span partition wrapping `retrieval.split_entity_and_temporal_spans` | Remove; TLG consumes directly |
| `application/memory_service.py::_apply_ordinal_if_eligible` | 563–631 | Intent-gated ordinal dispatch | Remove; TLG dispatcher replaces |
| `application/memory_service.py::_apply_range_filter_if_eligible` | 632–727 | Intent-gated range-filter dispatch | Remove; TLG dispatcher replaces |
| `application/scoring/pipeline.py::compute_temporal_proximity` call chain | 41, 374–381, 481–490, 552–553 | Scalar temporal-proximity signal added to combined score (w_temporal, default 0.2) | Remove; TLG's grammar gating replaces scalar |
| `application/scoring/pipeline.py::_resolve_event_time` | 405–425 | Extract `observed_at` from Memory/MemoryNode for temporal signal | Keep — TLG zone handlers reuse |

**Keep (TLG extends, does not replace):**

| Module | Role under TLG |
|---|---|
| `src/ncms/domain/temporal_parser.py` | Range-reference parsing for TLG's range-intent handler.  TLG grammar already calls `parse_temporal_reference()` in the experiment. |
| `src/ncms/domain/temporal_normalizer.py` | GLiNER temporal-span post-processing → `NormalizedInterval`.  Feeds `memory_content_ranges` which TLG's range-intent consumes. |
| `src/ncms/domain/entity_extraction.py::TEMPORAL_LABELS` + `add_temporal_labels()` | GLiNER temporal label registry.  Unchanged; drives the ingest-side label pass. |
| `src/ncms/infrastructure/extraction/gliner_extractor.py` (temporal-label split at lines 222–260) | Dual-GLiNER-call budget splitting stays; labels unchanged. |
| `src/ncms/infrastructure/indexing/exemplar_intent_index.py` | BM25 exemplar intent classifier.  Runs BEFORE TLG as coarse router — TLG handles state-evolution branch; other intents (fact_lookup, strategic_reflection, etc.) stay on existing path. |
| `src/ncms/infrastructure/llm/intent_classifier_llm.py` | LLM fallback for non-TLG intent classes.  Unchanged. |
| `src/ncms/application/reconciliation_service.py` | **Extended** (see §3).  Already emits typed edges; will additionally populate `retires_entities`. |
| `src/ncms/application/memory_service.py::compute_temporal_arithmetic` (lines 1159–1445) | Orthogonal arithmetic resolver ("how many days between X and Y").  No TLG interaction — TLG abstains on aggregation queries and BM25 handles.  Keep behind its own flag. |
| `src/ncms/application/ingestion/pipeline.py::_persist_content_range` (lines 645–708) | Writes `memory_content_ranges` at ingest.  TLG range-intent depends on this. |
| `src/ncms/infrastructure/storage/sqlite_store.py::save/get_content_range(s)` (339–381) | Storage API for content ranges.  Used by TLG range-intent handler. |

### 2.3 Config flags

Full inventory from the audit:

**Deprecate (preserve one release for backward-compat; warn on use):**

| Flag | Default | Used by | Replacement |
|---|---|---|---|
| `NCMS_TEMPORAL_ENABLED` | `False` | Scoring pipeline (lines 158-159) | `NCMS_TLG_ENABLED` — semantics differ; not a direct rename |
| `NCMS_SCORING_WEIGHT_TEMPORAL` | `0.2` | `scoring/pipeline.py:487-490` | N/A — TLG uses grammar gating, not scalar temporal weight |
| `NCMS_SCORING_WEIGHT_RECENCY` | `0.0` | `scoring/pipeline.py:231, 371-372` | N/A — recency is a subset of TLG's `current`/`origin` intents |
| `NCMS_RECENCY_HALF_LIFE_DAYS` | `30.0` | `scoring/pipeline.py:371-372` | N/A — no scalar recency under TLG |

**Repurpose (same name, TLG semantics under `NCMS_TLG_ENABLED=True`):**

| Flag | Old semantics | New semantics under TLG |
|---|---|---|
| `NCMS_TEMPORAL_RANGE_FILTER_ENABLED` | Hard-filter retrieval by GLiNER-extracted query range | Enable TLG range-intent handler (still consumes `memory_content_ranges`) |
| `NCMS_TEMPORAL_MISSING_RANGE_POLICY` | Recall/precision tradeoff for memories with no range | Same — feeds TLG range-intent's candidate set |

**Keep unchanged (orthogonal — episode/dream loops):**

| Flag | Default | Why keep |
|---|---|---|
| `NCMS_EPISODE_WEIGHT_TEMPORAL` | 0.10 | Episode-formation proximity (Phase 3), not retrieval-side temporal boost |
| `NCMS_DREAM_REHEARSAL_WEIGHT_RECENCY` | 0.05 | Dream-cycle rehearsal ranking, orthogonal to retrieval |

**New flags:**

| Flag | Default | Purpose |
|---|---|---|
| `NCMS_TLG_ENABLED` | `False` | Master flag for TLG grammar layer |
| `NCMS_TLG_CONFIDENCE_MIN` | `"medium"` | Minimum confidence for grammar to override BM25 (`high` / `medium`) |
| `NCMS_TLG_SHAPE_CACHE_PERSIST` | `True` | Persist query-shape cache to SQLite across restarts |
| `NCMS_TLG_CACHE_MAX_SKELETONS` | `10000` | Cap on shape-cache size (LRU eviction beyond) |

**Keep — hard dependencies of TLG:**

* `NCMS_INTENT_CLASSIFICATION_ENABLED` — coarse router; TLG runs for state-evolution intents only.
* `NCMS_RECONCILIATION_ENABLED` — must be on for TLG to work.  TLG needs typed edges (SUPERSEDES) with `retires_entities`.

---

### 2.4 Orthogonal temporal features — **TLG does NOT touch these**

This is critical for future integrators.  NCMS has many
time-related behaviors that are **not** part of "query-time
temporal boosting" and **must not** be modified by TLG
integration.  Future agents who see the broader sweep of
timestamp-touching code may mistakenly pull these in; the rule
is: TLG is about query-time RETRIEVAL grammar.  Everything
below is a different concern with its own lifecycle.

**All of these stay exactly as they are:**

| Area | Temporal role | Why orthogonal to TLG |
|---|---|---|
| **ACT-R activation** (`domain/scoring.py::base_level_activation`) | Computes B_i from `access_log.accessed_at` via `ln(Σt^-d)` with `actr_decay`  | TLG operates on typed edges in the memory graph; ACT-R operates on retrieval-access patterns.  Different signal, different input. |
| **Recency decay** (`domain/scoring.py::recency_score`) | `exp(-λ * age_days)` with half-life 30 days using `created_at` | Continues under `scoring_weight_recency` (0.0 default).  TLG doesn't ever write this.  Consider deprecating in a future release but NOT in this P1. |
| **Episode formation temporal signal** (`episode_service.py:291-297`) | 1 of 7 episode-linker weights: linear decay within `episode_window_minutes` (1440) using `last_member_time` | Episode formation is Phase 3 (HTMG L3), not retrieval.  Episodes are *formed* temporally but not *queried* temporally via TLG. |
| **Episode auto-closure** (`episode_service.py:857-881`) | `close_stale_episodes()` uses `episode_close_minutes` (1440) vs. `created_at` | Background maintenance, not retrieval. |
| **Dream-cycle rehearsal** (`consolidation_service.py:560-730`) | 5 signals: centrality / staleness / importance / access_count / recency all computed from `access_log` | Offline consolidation, not retrieval. Weights `dream_rehearsal_weight_*`. |
| **Dream importance drift** (`consolidation_service.py:899-960`) | 14-day window comparing recent vs. older access rates | Offline consolidation. |
| **Active forgetting** (`consolidation_service.py:1035-1121`) | Decay rate + 90-day access prune + 14-day conflict-age | Offline consolidation. |
| **Maintenance scheduler** (`maintenance_scheduler.py:200-219`) | 4 background loops: consolidation (360m) / dream (1440m) / episode_close (60m) / decay (720m) | Scheduler, not retrieval. |
| **Admission `temporal_salience`** (`admission_service.py:133-145`) | 1 of 4 admission features: date patterns + temporal verbs in content | Ingest-time quality gate, not retrieval.  Feeds persist/ephemeral routing. |
| **Ephemeral cache TTL** (`admission_ephemeral_ttl_seconds`, 3600) | `ephemeral_cache.expires_at` cleanup | Ingest-side tier. |
| **Abstract staleness** (`consolidation_service.py:1268-1277`) | `abstract_refresh_days` (7) vs. `refresh_due_at` | Consolidation re-synthesis trigger. |
| **Bus heartbeats** (`async_bus.py:126-137`) | `bus_heartbeat_interval_seconds` (30) + `bus_heartbeat_timeout_seconds` (90); offline detection | Agent liveness, not retrieval. |
| **Snapshot TTL** (`snapshot_service.py:77-93`) | `snapshot_ttl_hours` (168) for surrogate response freshness | Agent surrogate, not retrieval. |
| **Watch-service debounce** (`watch_service.py`) | 2.0s debounce for file-modification events | Ingest side. |
| **Co-occurrence PMI** (`consolidation_service.py::learn_association_strengths`) | Timeless (uses all search logs ever) | Offline consolidation. |
| **Contradiction "temporal" category** (`contradiction_detector.py`) | Label only (factual / temporal / configuration); no actual time check | Semantic label, not a feature. |
| **Bitemporal state reconciliation** (`reconciliation_service.py:188-223`) | Sets `valid_from`, `valid_to`, `is_current` on state supersession | **TLG extends this** (adds `retires_entities`) but keeps existing semantics unchanged. |
| **Bitemporal query methods** (`sqlite_store.py`) | `get_state_at_time`, `get_state_changes_since`, `get_state_history`, `get_current_entity_states` | Keep.  TLG zone computation reads `is_current` via these methods. |
| **PRECEDES / CAUSED_BY edge types** (`models.py`) | Temporal edge types for causal ordering | Keep.  Not currently emitted by any production path; reserved for future consolidation work. |
| **Relationships `valid_at` / `invalid_at`** (`relationships` table) | Bitemporal validity on entity-graph edges | Keep.  Orthogonal to memory-node validity; used by entity relationship queries. |
| **Audit timestamp columns on 18 operational tables** | `created_at` / `updated_at` / `timestamp` on projects, llm_calls, pipeline_events, dashboard_events, guardrail_violations, approval_decisions, bus_conversations, users, etc. | Audit trail.  None read by TLG.  None modified. |

### 2.5 Summary: what TLG changes vs. what TLG doesn't

```
CHANGES (this integration):
├─ Adds:
│  ├─ src/ncms/domain/grammar/ package (new)
│  ├─ grammar_pipeline.py (retrieval stage)
│  ├─ entity_memory_index.py (O(1) lookup)
│  ├─ graph_edges.retires_entities (column)
│  ├─ grammar_shape_cache (table)
│  └─ NCMS_TLG_* config flags
│
├─ Extends:
│  └─ ReconciliationService → emits retires_entities on SUPERSEDES
│
└─ Retires (query-time retrieval-side temporal boosting only):
   ├─ temporal_intent.py module
   ├─ apply_ordinal_ordering (retrieval/pipeline.py)
   ├─ apply_range_filter (retrieval/pipeline.py)
   ├─ _apply_ordinal_if_eligible (memory_service.py)
   ├─ _apply_range_filter_if_eligible (memory_service.py)
   ├─ _extract_query_range (memory_service.py wrapper)
   ├─ compute_temporal_proximity signal (scoring/pipeline.py)
   ├─ NCMS_TEMPORAL_ENABLED config flag
   ├─ NCMS_SCORING_WEIGHT_TEMPORAL config flag
   ├─ NCMS_SCORING_WEIGHT_RECENCY config flag
   ├─ NCMS_RECENCY_HALF_LIFE_DAYS config flag
   ├─ 6 test files (P1a/P1b specific)
   └─ benchmarks/longmemeval/temporal_diagnostic.py

DOES NOT TOUCH (orthogonal, stays as-is):
├─ ACT-R activation + access_log mechanics
├─ Episode formation temporal signal + auto-closure
├─ Dream cycle (rehearsal / drift / forgetting)
├─ Maintenance scheduler (4 background loops)
├─ Admission temporal_salience feature
├─ Ephemeral cache TTL
├─ Abstract staleness / refresh_due_at
├─ Bus heartbeats + snapshot TTL
├─ Watch-service debounce
├─ Contradiction detection
├─ Bitemporal query methods (reads)
├─ PRECEDES / CAUSED_BY edges (reserved)
├─ Relationships valid_at/invalid_at
└─ 34 audit timestamp columns across 18 operational tables
```

## 3. What changes

### 3.1 ReconciliationService extension

**Change.**  The existing `ReconciliationService.classify_and_apply()`
emits typed edges (`SUPPORTS`, `REFINES`, `SUPERSEDES`,
`CONFLICTS_WITH`) but does NOT populate `retires_entities`.  TLG
requires this annotation.

**Work.**

1. Port the structural retirement extractor from
   `experiments/temporal_trajectory/retirement_extractor.py` into
   `src/ncms/application/reconciliation/retirement_extractor.py`.
2. When emitting a `SUPERSEDES` edge, call
   `extract_retired(dst_content, src_entities, dst_entities, subject)`
   and store the result on the edge.
3. Add migration to persist `retires_entities` in `graph_edges`
   (schema v13, §5).
4. Backfill migration: for existing SUPERSEDES edges, re-run the
   extractor at migration time (bounded cost: O(|supersedes edges|)).

**Tests.**

* Add unit tests mirroring
  `experiments/temporal_trajectory/retirement_extractor.py` tests
  — active / passive / directional patterns.
* Integration test: ingest a corpus with known supersedes edges
  and verify `retires_entities` matches expectations.
* Parity test: run the same corpus through mock_reconciliation
  (experiment) and the NCMS reconciler — they should emit
  identical `retires_entities` sets.

### 3.2 Retrieval pipeline gains a grammar stage

**Change.**  `RetrievalPipeline.retrieve_candidates()` currently
runs BM25 + SPLADE + graph-expansion + intent-supplementation.
TLG adds a new stage BEFORE these that runs the grammar and
produces a confidence-gated rank-1 override.

**Work.**

1. New module `src/ncms/application/retrieval/grammar_pipeline.py`
   that wraps TLG's `retrieve_lg()` logic.
2. Hook inserted at the top of `RetrievalPipeline.retrieve_candidates()`,
   gated on `NCMS_TLG_ENABLED`.
3. Interface: grammar stage returns `(rank1_override, trace)` where
   `rank1_override` is either `None` (abstain) or a specific memory id;
   `trace` is an `LGTrace` for observability.
4. When `rank1_override` is not None, the rest of the pipeline runs
   as normal but the grammar's memory is guaranteed rank-1 before
   final scoring.
5. Dashboard integration: emit `grammar.confident_answer` event
   when TLG fires; emit `grammar.abstain` otherwise.

**Tests.**

* Integration test: query a reconciled corpus via
  `MemoryService.search` with TLG enabled, verify rank-1
  matches the grammar's answer on trajectory queries.
* Integration test: TLG disabled — results unchanged from the
  current baseline.
* Adversarial integration test: reuse the 15 adversarial queries
  from the experiment; verify zero confident-wrong.

### 3.3 Intent taxonomy reconciliation

**Current state.**

* NCMS's `domain/intent.py` has 7 intents:
  `FACT_LOOKUP`, `CURRENT_STATE_LOOKUP`, `HISTORICAL_LOOKUP`,
  `EVENT_RECONSTRUCTION`, `CHANGE_DETECTION`, `PATTERN_LOOKUP`,
  `STRATEGIC_REFLECTION`.
* TLG has 13 temporal-specific intents.

**Proposed resolution.**

Treat TLG's taxonomy as a **sub-taxonomy** of NCMS's
`CURRENT_STATE_LOOKUP`, `HISTORICAL_LOOKUP`, `EVENT_RECONSTRUCTION`,
and `CHANGE_DETECTION` branches.  Mapping:

| NCMS intent | TLG sub-intents |
|---|---|
| `FACT_LOOKUP` | (out of TLG scope — BM25 handles) |
| `CURRENT_STATE_LOOKUP` | `current`, `still` |
| `HISTORICAL_LOOKUP` | `origin`, `ordinal_first`, `ordinal_last` (legacy shape labels) |
| `EVENT_RECONSTRUCTION` | `sequence`, `predecessor`, `interval`, `transitive_cause` |
| `CHANGE_DETECTION` | `retirement`, `cause_of` |
| `PATTERN_LOOKUP` | (out of TLG scope) |
| `STRATEGIC_REFLECTION` | (out of TLG scope) |
| (new) `TEMPORAL_RANGE` | `range` |
| (new) `CROSS_SUBJECT` | `before_named`, `concurrent` |

**Implementation.**

* Add `TLGSubIntent` enum to `domain/intent.py`.
* Update `classify_intent()` to return both `(Intent, TLGSubIntent)`.
* Intent classifier unchanged; TLG sub-intent classifier is the
  query-parser's production list.

**This keeps NCMS's 7-intent public API stable** while giving TLG
a place to classify finer-grained temporal intents internally.

---

### 3.4 Ingest-time edge dynamics and TLG hook points

TLG's zero-confidently-wrong guarantee depends on typed edges
(`SUPERSEDES` with correct `retires_entities`) being correctly
emitted at ingest time.  Before integration the team needs to
understand the edge-creation flow is **dynamic and
conditionally-gated** with several trap-doors that can silently
prevent edge emission.  This subsection documents the flow,
every condition that can skip edge emission, and where TLG
integration hooks in.

#### 3.4.1 Current edge-creation flow

Per `MemoryService.store_memory()` (`memory_service.py:282-449`),
edges reach `graph_edges` via two mutually-exclusive paths:

```
                  store_memory(content, ...)
                            │
                            ▼
                   dedup / content classify
                            │
                            ▼
                   admission scoring
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
          ATOMIC                      NAVIGABLE
              │                           │
              ▼                           ▼
       persist to SQLite          SectionService
              │                   (doc + sections,
              ▼                    skips TLG path!)
      try enqueue to
      background worker?
              │
      ┌───────┴────────┐
      ▼                ▼
   YES (default)   NO (queue full)
      │                │
      │                ▼
      │     ┌──────────────────────┐
      │     │  INLINE INDEXING     │
      │     │  - BM25, SPLADE      │
      │     │  - GLiNER entities   │
      │     │  - L1 ATOMIC node    │
      │     │  - L2 ENTITY_STATE   │
      │     │    (iff state-change │
      │     │    signal ≥ 0.35 OR  │
      │     │    regex match)      │
      │     │  - DERIVED_FROM edge │
      │     │  - CO_OCCURS edges   │
      │     │  - reconciliation    │
      │     │    (iff enabled)     │
      │     │  - episode formation │
      │     │    (iff enabled)     │
      │     └──────────────────────┘
      │                │
      ▼                │
 ┌──────────────────┐  │
 │  Return Memory   │◄─┘
 │  (edges NOT yet  │
 │  visible!)       │
 └──────────────────┘
      │
      ▼ async (IndexWorkerPool)
 ┌─────────────────────────────┐
 │  BACKGROUND INDEXING         │
 │  (same steps as inline,      │
 │  in worker process)          │
 │                              │
 │  On failure: retry N times,  │
 │  then dead-letter → partial  │
 │  state in graph!             │
 └─────────────────────────────┘
```

**Two critical properties:**

1. **Default ingest is ASYNC.** There is no "async indexing" flag;
   async processing is always on when an `IndexWorkerPool` is
   configured (default).  `store_memory()` returns in ~2 ms but
   edges are not visible until the background worker finishes.
   Fallback to inline happens on queue-full backpressure
   (`memory_service.py` line ~494) and on pool not being
   initialized, NOT via a config toggle.
2. **L2 node creation is heuristic-gated.**  `state_change_signal ≥ 0.35`
   OR structured declaration (`Entity: key = value` regex) must
   fire, or NO L2 node is created, which means NO reconciliation,
   which means NO typed state edges (`SUPERSEDES` / `REFINES`
   / etc.).

#### 3.4.2 Edge emission trigger matrix

| Edge type | Condition to emit | Inline? | Async? | Required flag |
|---|---|---|---|---|
| `CO_OCCURS` | ≥2 entities linked to memory | ✓ | ✓ | — |
| `DERIVED_FROM` (L2→L1) | L2 created AND entity_id in metadata | ✓ | ✓ | — |
| `SUPPORTS` | Reconciliation classifies same state | ✓ | ✓ | `reconciliation_enabled` |
| `REFINES` | Reconciliation classifies narrower scope | ✓ | ✓ | `reconciliation_enabled` |
| `SUPERSEDES` + `SUPERSEDED_BY` | Reconciliation classifies state change | ✓ | ✓ | `reconciliation_enabled` |
| `CONFLICTS_WITH` (bidirectional) | Reconciliation classifies different scope | ✓ | ✓ | `reconciliation_enabled` |
| `BELONGS_TO_EPISODE` | Episode formation assigns L1 to episode | ✓ | ✓ | `episodes_enabled` |
| `MENTIONS_ENTITY` | Entity linked to memory | via Relationship | via Relationship | — |

**Not currently emitted by any production path:**
- `PRECEDES` — reserved for future causal reasoning
- `CAUSED_BY` — reserved

#### 3.4.3 What can silently SKIP edge emission

These are the trap-doors TLG must be aware of.  Each represents
a class of memories where TLG will NOT find the typed edges it
expects.

| Skip condition | What happens | TLG impact |
|---|---|---|
| **`admission.persist=False`** (ephemeral cache) | Memory never reaches SQLite | TLG never sees this memory (correct behavior — transient) |
| **Document path** via `publish_document()` | `SectionService._ingest_with_doc_store()` creates ONE document-profile memory in the memory store (goes through reconciliation if enabled) PLUS N section memories in document store only (no reconciliation) | **Document profile flows through TLG** (§3.5.2); sections do not.  Risk: profile summary may elide state-change content. |
| **`state_change_signal < 0.35` AND no regex match** | No L2 node created | **No reconciliation, no SUPERSEDES edges.**  Memory is an L1 atomic but has no position in the state-evolution graph. |
| **`NCMS_RECONCILIATION_ENABLED=False`** (current default) | L2 nodes created, DERIVED_FROM edges created, but NO typed state edges | **TLG cannot run.**  Hard dependency — flipped to True by TLG integration. |
| **`NCMS_EPISODES_ENABLED=False`** | No `BELONGS_TO_EPISODE` edges | TLG doesn't depend on episode edges; acceptable |
| **GLiNER extraction failure** | Empty entity list → no CO_OCCURS, no L2 (no entity metadata) → no reconciliation | Same as state-change miss |
| **Background task dead-lettered** | L1 may exist but L2 / reconciliation / episodes may not | **Partial graph state.**  TLG zones may be incomplete. |
| **Back-to-back stores (M1 → M2)** | M2's ingest may run before M1's async processing completes | **TLG may see M2 but not know about M1's state.** |

#### 3.4.4 Rehydration gap — process-restart risk

`GraphService.rebuild_from_store()` (`graph_service.py:20-60`)
rehydrates entities, relationships, and memory-entity links from
SQLite on startup but **does NOT reload `graph_edges` table rows
into the NetworkX in-memory graph**.

**Current consequence:** any code reading typed edges via the
NetworkX layer after a restart will miss them.

**Durability is preserved in SQLite** — edges are persistent.
But if any TLG code paths query NetworkX for typed edges instead
of SQLite, they'll silently return empty sets post-restart.

**Integration action** (Phase 1): TLG's grammar layer reads
typed edges from SQLite directly (via
`store.get_outgoing_graph_edges()` or equivalent), NOT via
NetworkX.  Explicit rehydration of typed edges into NetworkX can
be added in a later phase if other consumers need it; TLG itself
bypasses the issue by reading SQLite.

#### 3.4.5 TLG integration hook points

TLG grammar needs typed edges at retrieval time.  Four
candidate hook points exist in the ingest flow:

| Hook | Where | What's available | TLG action |
|---|---|---|---|
| **A. After L1 node creation** | `ingestion/pipeline.py:816-827`, `index_worker.py` L1 at line 557 | L1 node, memory entities | Too early — no edges yet |
| **B. After L2 node creation** | `ingestion/pipeline.py` DERIVED_FROM edge at 914-921, `index_worker.py` DERIVED_FROM at 650-655 | L1, L2, `DERIVED_FROM` edge | Early state-inspection hook |
| **C. After reconciliation (INTEGRATION POINT)** | `ingestion/pipeline.py` reconcile at 842, `index_worker.py` reconcile.reconcile() at 667 | L1, L2, all reconciliation edges with `retires_entities` | **TLG hook here** — this is where `retires_entities` gets populated |
| **D. After episode formation** | `ingestion/pipeline.py:848`, `index_worker.py:691` | Everything + `BELONGS_TO_EPISODE` | Acceptable — TLG doesn't need episode info |

**Phase 1 integration** extends `ReconciliationService.classify_and_apply()`
at hook C so that whenever it emits a `SUPERSEDES` edge, it also
calls the structural extractor to populate `retires_entities`.
This happens once per L2 node creation, in whatever path
(inline or async) the memory is processed.

#### 3.4.6 Dynamic-behavior risks for TLG and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| State-change heuristic misses a real state change | Medium | TLG misses a zone transition; wrong `current` intent answer | §7 adversarial test; monitor false-negative rate via LongMemEval |
| Async visibility delay on back-to-back writes | Medium | TLG queries during the window return stale state | `MemoryService.flush_indexing()` option for test/critical paths; document best practice |
| Dead-lettered task leaves partial graph | Low | Broken zone in a specific subject | Property-invariant check (`properties.py` ported from experiment) as CI gate; detect inconsistency on every run |
| Typed-edge rehydration gap on restart | Medium (deployment-time) | TLG reads empty NetworkX edges | TLG reads SQLite directly for typed edges (Phase 1) |
| Reconciliation disabled → no TLG signal | High before flag flip | TLG produces zero confident answers | Phase 5 hard-requires `NCMS_RECONCILIATION_ENABLED=True` when `NCMS_TLG_ENABLED=True` — config-validator enforces |
| Document profile summary elides state-change fact | Medium | State transition in an ADR-style doc not captured in graph | Phase 6 benchmark on ADR-style test corpus; if weak, add explicit "Supersedes <ID>" extractor in `section_service` (P2 candidate) |
| `retires_entities` incorrectly populated | Low | Wrong retirement lookup | Phase 1 parity test: mock-reconciliation (experiment) vs. production reconciler |

#### 3.4.7 Concrete integration dynamics

**When a user stores a memory "OAuth supersedes session cookies":**

1. (inline) Memory persisted to SQLite (~2 ms).
2. `store_memory()` returns.
3. (async, < 100 ms later) Background worker:
   - Runs GLiNER → extracts `OAuth`, `session cookies`.
   - Creates L1 ATOMIC node.
   - State-change signal likely fires (verb "supersedes" detected) → creates L2 ENTITY_STATE node for `authentication`.
   - `DERIVED_FROM` edge: L2 → L1.
   - Reconciliation runs: finds existing `authentication=session_cookies` state node (L2) → classifies as SUPERSEDES → creates:
     - `SUPERSEDES` edge: new_L2 → old_L2
     - `SUPERSEDED_BY` edge: old_L2 → new_L2
     - **`retires_entities = {"session cookies"}`** on new edge (TLG extension — Phase 1 adds this)
     - Updates old_L2: `is_current=False`, `valid_to=now`
     - Updates new_L2: `valid_from=now`
   - Episode formation (if enabled).

4. **TLG query after this point:**
   - `retrieval_lg("What authentication does the system use?")` → grammar identifies `current` intent, subject = `authentication`, looks up current zone → returns new_L2 (the OAuth memory).
   - `retrieval_lg("Do we still use session cookies?")` → grammar identifies `still` intent, entity = `session cookies`, calls `retirement_memory()` → finds SUPERSEDES edge with `retires_entities ∋ "session cookies"` → returns new_L2 (the memory that retired session cookies).

**Critical invariant:** Both queries must return the correct
memory in step 4 — which requires the SUPERSEDES edge + correct
`retires_entities` to have been emitted in step 3.  If the
state-change heuristic missed step 3, neither query works, and
TLG silently abstains.  That's safe (zero-confidently-wrong) but
represents a **coverage gap** we need to measure (Phase 6
benchmarks).

#### 3.4.8 What stays dynamic vs. what becomes deterministic

**Stays dynamic** (properties TLG inherits and must handle):
- Edge creation timing (async, queue-dependent)
- State-change detection heuristic (probabilistic, not exhaustive)
- Entity extraction quality (GLiNER confidence-thresholded)
- Reconciliation classification (heuristic: same/different value+scope)

**Becomes deterministic** (TLG adds these guarantees):
- Once typed edges exist, zone computation is deterministic
- Given a zone, current-state / origin / retirement lookup is deterministic
- `has_confident_answer()` → grammar decision is reproducible
- Query skeleton caching is deterministic

**The integration contract:** TLG trusts that whatever edges
reconciliation emits are correct; it does not re-interpret or
override them.  If reconciliation says "A supersedes B retires
session cookies," TLG answers "current auth is A, session cookies
was retired at A."  TLG's correctness is bounded by
reconciliation's correctness.

### 3.5 Architectural questions — honest answers

Five questions surfaced during review of §3.4.  Each has
implications for integration scope; some changed my earlier
statements.

#### 3.5.1 Rehydration gap — can we mitigate, or does this need larger changes?

**User context** (institutional memory): at some point the
NetworkX rehydration of typed edges was investigated and found
to cause graph memory explosion — bidirectional edges
(`SUPERSEDES` + `SUPERSEDED_BY`, `CONFLICTS_WITH` in both
directions) doubled the edge count, and hydrating all typed
edges for a large corpus blew up memory.  The chosen solution was
to not rehydrate.

**Verdict.**  The current behavior — typed edges persist in
SQLite but are NOT rehydrated into NetworkX on startup — is the
correct choice, not an oversight.  TLG integration respects it.

**TLG approach.**  Read typed edges from SQLite directly via
`store.get_graph_edges(source_id=..., edge_type=...)` and
equivalents.  No attempt to maintain a second copy in NetworkX.
The entity-memory index (Phase 4, §4.3) keeps these reads O(1)
per lookup via subject/entity → memory_id indexing.

**Larger change?  No.**  This is cleaner than trying to fix
NetworkX rehydration.  TLG has a natural locality (per-subject
zone computation needs only ~10-20 edges), so SQLite reads
are cheap if the index is right.  The right code model is
"NetworkX for non-typed relationships (entity co-occurrence,
spreading activation); SQLite for typed state edges."

#### 3.5.2 NAVIGABLE documents — do we miss them?

**My earlier claim was partially wrong.**  I said document
ingest "bypasses reconciliation entirely."  The code shows a
more nuanced flow:

* `publish_document()` → `SectionService._ingest_with_doc_store()` creates:
  * **ONE document profile memory** (`section_service.py:204-217`) of type `document_profile`, stored in the memory store via `_store_bypassing_classification()`.  This bypasses content classification but **goes through reconciliation** if `reconciliation_enabled=True`.
  * **N child section memories** (`section_service.py:167-177`) of type `section`, stored in the **document store only** (not the memory store).  These do NOT drive reconciliation.

**Practical consequence:**

* Document PROFILE memories DO flow through TLG's reconciliation hook.
* Document SECTION memories do NOT — they live in the document store and are retrieved via `DocumentService.get_sections()`, not via grammar.

**Risk.**  Profile memories are 500–800 character summaries
compiled from the document.  If a document's profile elides a
state-change fact (e.g., "Supersedes ADR-007") in summarization,
reconciliation won't fire for that state change and TLG misses
the edge.

**Mitigation (Phase 6 validation):**

1. Build a test corpus of 10–20 ADR-style documents with explicit supersedes statements.
2. Ingest via `publish_document()`.
3. Query "What is our current X?" through TLG; verify the current-zone memory is the latest ADR's profile.
4. If coverage is weak (profile elides the superseding fact), add an explicit ADR-style extractor that scans section content for "Supersedes <ID>" and emits `SUPERSEDES` edges directly, independent of the state-change heuristic.

**Larger change needed?  Potentially yes, as a Phase 2 / P2-style
enhancement.**  Document-ADR-extractor is worth building if
benchmark shows TLG covers ATOMIC memories well but misses
document-driven state transitions.  Not blocking for P1.

#### 3.5.3 Async insertion — benchmark discipline

**Confirmed.**  NCMS ingest is async by default.  For benchmarks
measuring TLG correctness, the test harness MUST drain the
indexing queue before querying.

**Action (Phase 6):**

* Document `MemoryService.flush_indexing()` as the required entry
  point for benchmark and integration test runners.
* Add a decorator / helper `@ensure_drained` for test utilities.
* Update benchmark base class (`benchmarks/run_ablation.py` and
  LongMemEval runner) to call it before the query loop.

**Not a design change** — just disciplined use of existing API.

#### 3.5.4 Does TLG replace the cross-encoder reranker?

**No.**  The reranker and TLG operate on **disjoint intent sets**:

| Intent | Current behavior | Under TLG |
|---|---|---|
| `FACT_LOOKUP` | Reranker ON | Reranker ON (unchanged) |
| `PATTERN_LOOKUP` | Reranker ON | Reranker ON (unchanged) |
| `STRATEGIC_REFLECTION` | Reranker ON | Reranker ON (unchanged) |
| `CURRENT_STATE_LOOKUP` | Reranker OFF | TLG handles (grammar gate) |
| `HISTORICAL_LOOKUP` | Reranker OFF | TLG handles |
| `CHANGE_DETECTION` | Reranker OFF | TLG handles |
| `EVENT_RECONSTRUCTION` | Reranker OFF | TLG handles |

**Evidence** (`application/retrieval/pipeline.py:558-569`):

```python
ce_intents = {
    QueryIntent.FACT_LOOKUP,
    QueryIntent.PATTERN_LOOKUP,
    QueryIntent.STRATEGIC_REFLECTION,
}
_use_ce = (
    self._reranker is not None
    and self._config.reranker_enabled
    and (intent_result is None or intent_result.intent in ce_intents)
)
```

NCMS already decided (Phase 10 design) that the reranker damages
state/historical/temporal queries — hence disabled for those
intents.  TLG steps into the space the reranker wasn't serving.
**No interaction, no replacement.**

**Future (M-series work, not P1):**  a non-temporal LG extension
could in principle extend grammatical routing to `FACT_LOOKUP` or
other reranker-served intents.  That's the TLG paper's milestone
M7 (production induction) research direction.  If successful,
reranker might eventually narrow further.  But for P1: reranker
stays exactly as-is.

#### 3.5.5 What simplifies, what doesn't, what stays

Honest tally, so no one over-claims simplification:

**Simplifies (net removal):**

| Layer | What goes away |
|---|---|
| Query-time retrieval | scalar `temporal_proximity` signal |
| Query-time retrieval | `apply_ordinal_ordering` (Phase B.2) |
| Query-time retrieval | `apply_range_filter` (Phase B.4) |
| Query-time retrieval | 3 `memory_service.py` wrappers |
| Query-time retrieval | `compute_temporal_proximity` call chain |
| Domain | `temporal_intent.py` module (6-intent taxonomy) |
| Config | 4 deprecated flags (temporal / recency scalar) |
| Tests | 6 Phase B test files |
| Benchmark | `temporal_diagnostic.py` harness |
| **~350 LOC removed, 1 module, 4 flags, 6 tests.** | |

**Does NOT simplify (preserved, TLG uses):**

| Layer | Why it stays |
|---|---|
| `temporal_parser.py` | TLG range-intent delegates calendar parsing |
| `temporal_normalizer.py` | GLiNER span → interval conversion |
| `entity_extraction.py::TEMPORAL_LABELS` + `add_temporal_labels()` | GLiNER temporal-label registry |
| GLiNER dual-call split for temporal labels | Ingest-side extraction |
| `memory_content_ranges` table + indexes | TLG range-intent consumer |
| `memory_nodes` bitemporal fields (`valid_from`, `valid_to`, `is_current`) | TLG zone computation |
| Reconciliation service | TLG extends (`retires_entities`) |

**Extended (TLG adds behavior, doesn't replace):**

| Layer | What changes |
|---|---|
| `reconciliation_service.py` | Emits `retires_entities` on SUPERSEDES (new column) |
| `graph_edges` schema | Adds `retires_entities` column (v13) |

**Orthogonal (completely unchanged):**

| Layer | Why unchanged |
|---|---|
| Cross-encoder reranker | Operates on disjoint intents (§3.5.4) |
| ACT-R activation / base-level / recency | Different signal (access-time, not query-time) |
| Episode formation | Phase 3 (HTMG L3), not retrieval |
| Dream cycle (rehearsal / drift / forgetting) | Offline consolidation |
| Maintenance scheduler | Background loops |
| Admission `temporal_salience` | Ingest-side quality gate |
| Ephemeral cache TTL | Ingest-side tier |
| Bus heartbeats, snapshot TTL | Agent liveness, not retrieval |
| Bitemporal query methods (`get_state_at_time` etc.) | TLG reads them; they don't change |

#### 3.5.6 Do we still need GLiNER temporal extraction?

**Yes.**  TLG does NOT remove the need for GLiNER temporal labels.

Reasoning:

* `observed_at` is WHEN the memory event happened (bitemporal).
* GLiNER temporal extraction finds dates MENTIONED in content
  (different semantic).  E.g., "We ran the quarterly review in
  Q2 2025" — `observed_at` might be the meeting date; content
  mentions Q2 2025 as a range.
* TLG's `range` intent filters memories by `memory_content_ranges`
  (content-mentioned ranges), not just `observed_at`.
* Without GLiNER temporal labels, `memory_content_ranges` is
  never populated → TLG range-intent always abstains.

**Conclusion.**  Keep GLiNER temporal extraction.  TLG range-
intent depends on it.  Simplification happens on the query side,
not the ingest side.

#### 3.5.7 Net simplification summary

Honest answer: **integration is a net +3,700 LOC.**

* Retire ~350 LOC of Phase B retrieval-side temporal boosting.
* Port ~4,000 LOC of grammar package from `experiments/`.
* Extend reconciliation by ~50 LOC.

The *conceptual* simplification is larger than the LOC delta
suggests: 13 intents with a single dispatcher beats 6 intents
with scattered handlers.  But it's not a reduction in code size.
It's a cleaner architecture built on top of the existing one.

**No larger architectural changes needed for P1.**  Rehydration
stays as-is, documents flow through profile reconciliation,
reranker stays disjoint, GLiNER temporal stays.  The integration
is additive with a small retirement footprint.

## 4. What's new

### 4.1 `src/ncms/domain/grammar/` package

New domain package with pure (no-infra) grammar modules ported
from the experiment:

| Module | Ported from | Role |
|---|---|---|
| `grammar/productions.py` | `experiments/.../query_parser.py` | 12 production matchers |
| `grammar/zones.py` | `experiments/.../grammar.py` | `compute_zones`, `current_zone`, `origin_memory` |
| `grammar/aliases.py` | `experiments/.../aliases.py` | Initials-based alias induction (bucketed) |
| `grammar/confidence.py` | `experiments/.../lg_retriever.py::LGTrace` | 4-level confidence + `has_confident_answer()` predicate |
| `grammar/retirement_extractor.py` | `experiments/.../retirement_extractor.py` | Structural `retires_entities` extraction (used by reconciler) |
| `grammar/edge_markers.py` | `experiments/.../edge_markers.py` | Layer 2 induced transition markers |
| `grammar/shape_cache.py` | `experiments/.../shape_cache.py` | Query-shape cache with persistence hooks |
| `grammar/vocab_induction.py` | `experiments/.../vocab_induction.py` | Layer 1 vocabulary (subject/entity tokens) |

### 4.2 `src/ncms/application/retrieval/grammar_pipeline.py`

Integration layer.  Consumes NCMS's entity graph + memory store
(rather than the experiment's in-process corpus) and emits a
confidence-gated ranking.

### 4.3 `src/ncms/infrastructure/indexing/entity_memory_index.py`

**Critical integration-bottleneck fix.**  The experiment's
`_find_memory()` iterates the full corpus; TLG scale regression
measured 8 seconds per query at 50 k memories.  NCMS's entity
graph already has the primitives for O(1) entity → memory
lookup; this module wires them into the TLG integration layer.

Target post-integration query cost: <50 ms regardless of corpus
size.

### 4.4 Schema migrations (v12 → v13)

```sql
-- Schema v13: TLG integration

-- Add retires_entities to typed edges.
ALTER TABLE graph_edges ADD COLUMN retires_entities TEXT;  -- JSON array
-- Backfill '[]' for existing rows.
UPDATE graph_edges SET retires_entities = '[]'
  WHERE retires_entities IS NULL;

-- Persisted query-shape cache.
CREATE TABLE IF NOT EXISTS grammar_shape_cache (
    skeleton TEXT PRIMARY KEY,
    intent TEXT NOT NULL,
    slot_names TEXT,               -- JSON array
    hit_count INTEGER NOT NULL DEFAULT 0,
    last_used TEXT
);

-- Record schema version.
INSERT INTO schema_version (version, applied_at) VALUES (13, datetime('now'));
```

Re-run migration backfill step for existing SUPERSEDES edges
using the structural retirement extractor (separate step, not
part of the migration SQL — runs as a one-time job).

### 4.5 `src/ncms/config.py` additions

New settings under the `NCMS_TLG_*` prefix (see §2.3 for
details).

---

## 5. Integration phases

Each phase is independently committable and testable.

### Phase 0 — Pre-flight (1–2 days)

- Verify dead code removed (`grep apply_ordinal_rerank`,
  `apply_subject_scoped_ordinal_rerank`).
- Move `docs/p1-temporal-experiment.md` → `docs/retired/` with a
  top-banner note linking to the TLG pre-paper.
- Update `docs/design-query-performance.md` "History" section to
  reflect TLG supersedence.
- Update `CLAUDE.md` (project guide) to reference TLG as the
  temporal story.
- PR: `docs-only: retire P1 docs, prep for TLG integration`.

### Phase 1 — Reconciliation extension (3–5 days)

- Port `retirement_extractor.py` to
  `src/ncms/application/reconciliation/retirement_extractor.py`.
- Extend `ReconciliationService.classify_and_apply()` to emit
  `retires_entities` on SUPERSEDES edges.
- Schema v13 migration (add `graph_edges.retires_entities`).
- Backfill job for existing supersedes edges.
- Tests: unit (extractor) + integration (reconcile → retires set)
  + parity (mock-reconciliation vs production reconciler).
- PR: `reconciliation: emit retires_entities via structural extractor`.

### Phase 2 — Port grammar core (5–7 days)

- Port 8 modules from `experiments/temporal_trajectory/` to
  `src/ncms/domain/grammar/`.
- Adapt imports to use `ncms.domain.models.Memory` instead of
  experiment's `corpus.Memory`.
- Tests: unit tests preserved; add `tests/unit/domain/grammar/`
  directory mirroring experiment's test structure.
- PR: `grammar: port TLG domain package`.

### Phase 3 — Retrieval integration (3–5 days)

- Create `src/ncms/application/retrieval/grammar_pipeline.py`.
- Wire `GrammarPipeline.run(query, bm25_ranking)` → `LGTrace` into
  `RetrievalPipeline.retrieve_candidates()`.
- Gate on `NCMS_TLG_ENABLED`.
- Dashboard event emission (`grammar.*` events).
- Tests: integration tests with TLG on/off.
- PR: `retrieval: integrate TLG grammar stage`.

### Phase 4 — Entity-memory index (2–3 days)

- Port `experiments/.../lg_retriever.py::_find_memory` to
  `infrastructure/indexing/entity_memory_index.py`.
- Replace O(n) scan with NCMS entity-graph O(1) lookup.
- Benchmark: query cost curve re-measured at 100, 1 k, 10 k,
  100 k memories.  Target: <50 ms at all sizes.
- PR: `indexing: O(1) entity→memory lookup for TLG`.

### Phase 5 — Feature-flag rollout (1 day)

- `NCMS_TLG_ENABLED` added to `config.py` (default False).
- Add to demo flag set for observability.
- Add `--tlg` to benchmark CLI.
- PR: `config: NCMS_TLG_ENABLED feature flag`.

### Phase 6 — Validation (3–5 days)

- Run benchmarks with TLG on/off:
  - SciFact / NFCorpus / ArguAna (existing ablation)
  - LongMemEval --all (full 500 questions)
  - MemoryAgentBench (existing harness)
- Document delta per benchmark (accuracy, latency,
  confidently-wrong rate).
- Paper revision: check off TLG milestones M1, M2, M3 in
  `docs/temporal-linguistic-geometry.md` §7.6.
- PR: `validation: TLG benchmark results`.

### Phase 7 — Deprecation (1–2 days)

- Mark superseded modules as deprecated (inline comments +
  `DeprecationWarning`).
- Update design-spec with final TLG architecture.
- Retire `p1-temporal-experiment.md`.
- PR: `cleanup: deprecate superseded temporal code`.

**Total budget: ~3 weeks** end-to-end at one engineer, assuming
no unexpected schema conflicts.  Phases 1–4 can parallelize
with careful coordination.

---

## 6. Schema changes

### 6.1 Existing temporal-relevant schema (inventory)

Audit result — every schema element that supports temporal /
reconciliation retrieval today:

| Table / column | Schema version | Purpose | TLG action |
|---|---|---|---|
| `memories.observed_at` (TEXT) | v10 | When the source event occurred (bitemporal) | **Keep** — TLG zone handlers use this |
| `idx_memories_observed_at` | v10 | Index supporting temporal sort | **Keep** |
| `memory_nodes.observed_at` (TEXT) | v10 | Node-specific event time | **Keep** — TLG operates at L1/L2 node level |
| `memory_nodes.ingested_at` (TEXT) | v10+ | NCMS ingest timestamp (audit) | Keep — unused by TLG |
| `memory_nodes.valid_from` (TEXT) | v6+ | State validity start | **Keep** — TLG zone computation uses this |
| `memory_nodes.valid_to` (TEXT) | v6+ | State validity end (closure on supersedes) | **Keep** — needed for TLG current-zone detection |
| `memory_nodes.is_current` (INT) | v6+ | Current-state flag | **Keep** — TLG `current` intent depends on this |
| `memory_nodes.metadata` (JSON) | v6+ | Carries `supersedes` / `conflicts_with` IDs | **Keep** — reconciliation context |
| `memory_content_ranges` (table) | v11 | Per-memory `[range_start, range_end)` from GLiNER or metadata | **Keep** — TLG range-intent consumes |
| `idx_mcr_range` (range_start, range_end) | v11 | Supports range-overlap queries | **Keep** |
| `graph_edges.edge_type` (TEXT) | v6+ | SUPPORTS / REFINES / SUPERSEDES / SUPERSEDED_BY / CONFLICTS_WITH | **Keep** — TLG reads these |
| `schema_version` (table) | v1 | Tracks applied migrations | **Keep** |

### 6.2 New in v13 (this integration)

```sql
-- Schema v13: TLG integration

-- Add retires_entities to typed edges (populated by reconciler's
-- structural extractor on SUPERSEDES edges).
ALTER TABLE graph_edges ADD COLUMN retires_entities TEXT;  -- JSON array

-- Backfill '[]' for existing rows; subsequent backfill job
-- re-runs the structural extractor over historical supersedes.
UPDATE graph_edges SET retires_entities = '[]'
  WHERE retires_entities IS NULL;

-- Persisted query-shape cache.
CREATE TABLE IF NOT EXISTS grammar_shape_cache (
    skeleton TEXT PRIMARY KEY,
    intent TEXT NOT NULL,
    slot_names TEXT,                                -- JSON array
    hit_count INTEGER NOT NULL DEFAULT 0,
    last_used TEXT
);
CREATE INDEX IF NOT EXISTS idx_gsc_hit_count
    ON grammar_shape_cache(hit_count DESC);

-- Optional: Layer 2 marker inventory persistence.
-- (Alternative to recomputing from edges at every import.)
CREATE TABLE IF NOT EXISTS grammar_transition_markers (
    transition_type TEXT NOT NULL,                  -- supersedes | refines | ...
    marker_head TEXT NOT NULL,                      -- verb head
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (transition_type, marker_head)
);

-- Record schema version.
INSERT INTO schema_version (version, applied_at)
VALUES (13, datetime('now'));
```

Key properties:

* **Forward-compatible:** old code still works; `retires_entities = '[]'` is the no-op default for any consumer that doesn't know about the column.
* **Backward-migratable:** drop the new column and tables to revert to v12.
* **Idempotent migration:** `ALTER TABLE` is guarded; `INSERT` of schema-version uses natural key.
* **No change** to `memories` / `memory_nodes` / `memory_content_ranges` tables.  All existing temporal foundation stays.

### 6.3 Backfill plan for `retires_entities`

One-time job, run post-migration:

1. Enumerate all `graph_edges` where `edge_type = 'SUPERSEDES'`
   and `retires_entities = '[]'`.
2. For each, fetch src memory content + entities and dst memory
   content + entities.
3. Run the structural extractor (`ncms.application.reconciliation.retirement_extractor::extract_retired`).
4. Update the row's `retires_entities`.

Cost: O(|supersedes edges|) — single-pass, bounded.  In production
the number of supersedes edges is small (state changes are rarer
than observations).

---

## 7. Test strategy

### 7.1 Unit (per module)

* Port experiment's `tests/` to `tests/unit/domain/grammar/`.
* ~50 unit tests expected (one per algorithm + edge cases).

### 7.2 Integration

* `tests/integration/test_tlg_pipeline.py` — full retrieval
  pipeline with TLG on/off on small structured corpora.
* `tests/integration/test_tlg_reconciliation.py` — ingest →
  reconcile → retires_entities populated → grammar retrieves.
* `tests/integration/test_tlg_scale.py` — corpus at 1 k memories,
  verify query latency < 100 ms (scale-bound).

### 7.3 Adversarial (critical integration-safety test)

* Port the 15 adversarial queries to
  `tests/integration/test_tlg_adversarial.py`.
* CI gate: any confidently-wrong answer fails the build.

### 7.4 Parity

* `tests/integration/test_tlg_parity.py` — run the same query set
  through the experiment and through NCMS integration; results
  must match.  This is the bridge test that catches integration
  bugs.

### 7.5 Architecture fitness

* Extend `tests/architecture/` with:
  - import-boundary checks (grammar package pure, no infra deps)
  - zero-confidently-wrong invariant check
  - ablation runner as a CI job (runs weekly, not per-PR)

---

## 8. Design-spec updates (`ncms-design-spec.md`)

Sections that need rewrites because TLG changes the story:

### §2.1–§2.2 Entity nodes & relationship edges

**Current:** describes typed edges with bi-temporal validity
(`valid_from`, `valid_to`) and the five edge types.

**Update:** add a subsection about `retires_entities` as the
reconciler's structural output.  Reference TLG's §4.2
retirement-extractor algorithm.

### §4 Tier-3 intent classification

**Current:** describes BM25 exemplar index + LLM fallback for 7
intents.  Notes reranker is disabled for temporal/state queries.

**Update:** describe the intent hierarchy:

* **Coarse intent** via BM25 exemplar → 7 NCMS intents.
* **Fine-grained temporal sub-intent** via TLG productions → 13
  sub-intents when coarse intent is in
  `{CURRENT_STATE_LOOKUP, HISTORICAL_LOOKUP, EVENT_RECONSTRUCTION,
  CHANGE_DETECTION}` + new `TEMPORAL_RANGE` / `CROSS_SUBJECT`.

### §4.2 Scoring pipeline

**Current:** describes ACT-R + spreading activation +
reconciliation penalties.  Lists recency / temporal as scalar
signals.

**Update:** note that under TLG (`NCMS_TLG_ENABLED=True`),
temporal signal is handled by grammar gating rather than scalar
reranking.  Scalar temporal weight remains configurable for
compatibility but is 0.0 by default when TLG is on.

### §5B State trajectories

**Current:** describes `NCMS_TRAJECTORY_CONSOLIDATION_ENABLED`
that generates temporal progression narratives via LLM synthesis.

**Update:** clarify the relationship to TLG.  State trajectories
at the CONSOLIDATION layer (Phase 5B) consume the *same* typed
edges that TLG retrieval uses at query time.  Shared edge graph;
different consumers.  No conflict; TLG doesn't replace
consolidation.

### §8.4 Schema

**Current:** documents schema version 11 (from P1 Phase A) with
`memory_content_ranges` table.

**Update:** bump to v13 with `graph_edges.retires_entities` and
`grammar_shape_cache`.  Include the migration SQL.

### §5 New subsection: "Grammar layer (TLG)"

**Add:** a new subsection describing:
* What TLG is (one paragraph summary).
* Where it sits in the retrieval pipeline.
* The `has_confident_answer()` integration primitive.
* Reference to `docs/temporal-linguistic-geometry.md` for the
  full framework.

---

## 9. Success criteria

Integration is done when ALL of:

1. ✅ `NCMS_TLG_ENABLED=True` can be flipped without breaking any
   existing test.
2. ✅ Full LongMemEval run shows 0 confidently-wrong AT SCALE
   (same invariant as experiment).
3. ✅ Query dispatch latency < 50 ms at 10 k+ memories (entity-
   index fix).
4. ✅ Benchmark deltas documented: SciFact, NFCorpus, LongMemEval,
   MemoryAgentBench.
5. ✅ Parity test passes (experiment vs. integration identical
   on structured corpora).
6. ✅ Architecture fitness tests pass (import boundaries, zero-
   confidently-wrong gate).
7. ✅ Dashboard shows grammar confidence per query.
8. ✅ Design-spec updated to reflect TLG.
9. ✅ `p1-temporal-experiment.md` retired.
10. ✅ Deprecated flags generate `DeprecationWarning` on use.

---

## 10. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Reconciler + TLG emit different `retires_entities` | Medium | Parity-test failures, integration stalls | Parity test in §7.4; mock-reconciliation as source of truth |
| Entity-graph index missing primitives for TLG | Low | Slow query dispatch | Measure in Phase 4; if index lacks support, add targeted primitives |
| LongMemEval scale regresses at production | Low | Integration rollback needed | Benchmark Phase 6 before rollout; keep flag gated |
| TLG breaks existing benchmarks | Medium | Regression risk | All existing tests with TLG off as baseline; progressive enablement |
| Schema migration fails on large existing DBs | Low | Migration timeout | Backfill in batches; use `INSERT OR IGNORE` for idempotency |
| Shape-cache grows unbounded | Medium | Memory bloat | `NCMS_TLG_CACHE_MAX_SKELETONS`; LRU eviction on hit |
| Old flags still referenced after deprecation | High | Confusion in docs | `DeprecationWarning` + grep CI check |

---

## Appendix A: Documents to retire (complete list)

| Current path | New path | Banner text |
|---|---|---|
| `docs/p1-temporal-experiment.md` | `docs/retired/p1-temporal-experiment.md` | "This document described the metadata-anchored + intent-router approach to temporal retrieval.  Superseded by Temporal Linguistic Geometry (`docs/temporal-linguistic-geometry.md`).  See `docs/p1-plan.md` for the integration plan." |

No other docs retire in this pass.  `research-longmemeval-temporal.md`,
`p1-experiment-diary.md`, `tlg-scale-validation.md`,
`temporal-linguistic-geometry.md` all remain active.

---

## Appendix B: Code retirement / deprecation (complete list)

### B.1 Verify already-removed

```bash
grep -r "apply_ordinal_rerank" src/ tests/                    # expect: 0 hits
grep -r "apply_subject_scoped_ordinal_rerank" src/ tests/     # expect: 0 hits
```

### B.2 Retire / deprecate (per audit)

See §2.2 for the full rationale.  Summary table — every file
that needs modification in Phases 1 / 3 / 7:

**Domain layer:**

| Path | Action | Phase |
|---|---|---|
| `src/ncms/domain/temporal_intent.py` | `DeprecationWarning` on import; remove 1 release later | 7 |
| `src/ncms/domain/temporal_parser.py` | Keep.  TLG range-intent uses it. | — |
| `src/ncms/domain/temporal_normalizer.py` | Keep.  TLG consumes `NormalizedInterval`. | — |
| `src/ncms/domain/entity_extraction.py` | Keep `TEMPORAL_LABELS` + `add_temporal_labels()`. | — |

**Application layer:**

| Path | Lines | Action | Phase |
|---|---:|---|---|
| `application/memory_service.py::_extract_query_range` | 527–561 | Remove.  TLG consumes GLiNER output directly. | 3 |
| `application/memory_service.py::_apply_ordinal_if_eligible` | 563–631 | Remove.  TLG ordinal/sequence/predecessor handlers replace. | 3 |
| `application/memory_service.py::_apply_range_filter_if_eligible` | 632–727 | Remove.  TLG range-intent handler replaces. | 3 |
| `application/memory_service.py::compute_temporal_arithmetic` | 1159–1445 | Keep (orthogonal).  Behind its own flag. | — |
| `application/retrieval/pipeline.py::apply_ordinal_ordering` | 315–399 | Remove.  TLG replaces. | 3 |
| `application/retrieval/pipeline.py::apply_range_filter` | 449–511 | Remove.  TLG replaces. | 3 |
| `application/retrieval/pipeline.py::split_entity_and_temporal_spans` | 288–313 | Keep (helper for TLG range-intent). | — |
| `application/retrieval/pipeline.py::resolve_temporal_range` | 435–447 | Keep (helper for TLG range-intent). | — |
| `application/scoring/pipeline.py::compute_temporal_proximity` call chain | 41, 374–381, 481–490, 552–553 | Remove (scalar temporal signal).  TLG grammar gating replaces. | 3 |
| `application/scoring/pipeline.py::_resolve_event_time` | 405–425 | Keep — TLG reuses. | — |

**Infrastructure layer:**

| Path | Action |
|---|---|
| `infrastructure/extraction/gliner_extractor.py` (temporal-label split 222-260) | Keep unchanged |
| `infrastructure/indexing/exemplar_intent_index.py` | Keep (coarse intent classifier runs before TLG) |
| `infrastructure/llm/intent_classifier_llm.py` | Keep (LLM fallback for non-TLG intents) |
| `infrastructure/storage/sqlite_store.py` (save/get_content_range, lines 339-381) | Keep (TLG range-intent uses) |

### B.3 Tests (audit surfaced 18 temporal-adjacent test files)

**Remove in Phase 3 (superseded by TLG equivalents):**

| Test file | Reason |
|---|---|
| `tests/unit/domain/test_temporal_intent.py` | `TemporalIntent` enum removed |
| `tests/unit/application/test_apply_ordinal_ordering.py` | `apply_ordinal_ordering` removed |
| `tests/unit/application/test_apply_range_filter.py` | `apply_range_filter` removed (TLG has own tests) |
| `tests/integration/test_explicit_range_primitive.py` | Replaced by `test_tlg_range_intent.py` |
| `tests/integration/test_ordinal_sequence_primitive.py` | Replaced by `test_tlg_sequence_intent.py` |
| `tests/integration/test_arithmetic_resolver.py` | Keep but rename/reclassify; orthogonal to TLG (arithmetic resolver stays) |

**Keep (still validate live functionality):**

| Test file | Reason |
|---|---|
| `tests/unit/domain/test_temporal_normalizer.py` | Still validates span→interval normalization |
| `tests/integration/test_temporal_range_extraction.py` | Still validates ingest-side range extraction |
| `tests/integration/test_bitemporal_wiring.py` | Still validates observed_at threading |
| `tests/unit/infrastructure/storage/test_content_range_store.py` | CRUD on `memory_content_ranges` |
| `tests/unit/infrastructure/extraction/test_label_budget.py` | GLiNER label budget (unchanged) |
| `tests/integration/test_named_entity_no_regression.py` | Baseline entity regression guard |

**New (added in Phase 2 / 3):**

| Test file | Content |
|---|---|
| `tests/unit/domain/grammar/` (directory) | Port all unit tests from `experiments/temporal_trajectory/tests` |
| `tests/integration/test_tlg_pipeline.py` | Full retrieval pipeline with TLG on/off |
| `tests/integration/test_tlg_reconciliation.py` | Ingest → reconcile → retires_entities → grammar retrieves |
| `tests/integration/test_tlg_adversarial.py` | 15 adversarial queries; zero-confidently-wrong CI gate |
| `tests/integration/test_tlg_parity.py` | Experiment vs. integration parity |
| `tests/integration/test_tlg_scale.py` | Query latency < 100 ms at 1 k memories |

### B.4 Benchmarks

| Path | Action |
|---|---|
| `benchmarks/longmemeval/temporal_diagnostic.py` | **Retire.**  Diagnostic for pre-TLG Phase B primitives; scores obsolete approaches.  Replace with `benchmarks/longmemeval/tlg_diagnostic.py` that exercises TLG intents. |
| `benchmarks/results/temporal_diagnostic/*.json` | Keep as historical records; stop generating new runs. |
| `benchmarks/results/longmemeval/features_on/*.{json,md}` | Re-run with `NCMS_TLG_ENABLED=True` in Phase 6; replace current artifacts. |

### B.5 Configuration (consolidated)

Deprecated (one-release backward-compat, warn on use):
- `NCMS_TEMPORAL_ENABLED` (legacy Phase B intent routing; repurposed name available under `NCMS_TLG_ENABLED`)
- `NCMS_SCORING_WEIGHT_TEMPORAL` (scalar temporal signal — TLG replaces with grammar gating)
- `NCMS_SCORING_WEIGHT_RECENCY` (scalar recency decay — subsumed by TLG `current`/`origin`)
- `NCMS_RECENCY_HALF_LIFE_DAYS` (tied to deprecated recency signal)

Repurposed (same name, new semantics under TLG):
- `NCMS_TEMPORAL_RANGE_FILTER_ENABLED` → TLG range-intent gate
- `NCMS_TEMPORAL_MISSING_RANGE_POLICY` → TLG range-intent candidate-set policy

New:
- `NCMS_TLG_ENABLED`
- `NCMS_TLG_CONFIDENCE_MIN`
- `NCMS_TLG_SHAPE_CACHE_PERSIST`
- `NCMS_TLG_CACHE_MAX_SKELETONS`

Keep unchanged (orthogonal to retrieval temporal):
- `NCMS_EPISODE_WEIGHT_TEMPORAL` (episode formation proximity)
- `NCMS_DREAM_REHEARSAL_WEIGHT_RECENCY` (dream-cycle rehearsal)

Hard dependencies (must be ON for TLG):
- `NCMS_INTENT_CLASSIFICATION_ENABLED` (coarse router)
- `NCMS_RECONCILIATION_ENABLED` (typed edges)

---

## Appendix C: P2 plan revision

Current `docs/p2-plan.md` proposes preference extraction — regex-
scan conversations for "I like X" / "User prefers Y" patterns
and emit synthetic preference memories.

### C.1 What transfers from TLG

Three TLG lessons that apply to preference extraction:

1. **Production-rule extraction with slot validation.**  Instead
   of regex-alternation on preference patterns ("I like X", "I
   prefer X to Y", "My favorite X is Y"), use a small set of
   production matchers that validate the full structure.  A
   production rejects when the slot doesn't resolve to a non-
   generic entity.
2. **Confident abstention.**  Don't emit a preference memory if
   the production's slot didn't cleanly resolve.  Better to miss
   a preference than emit a wrong one.
3. **Data/structural layer split.**  The *structural* layer is
   English preference-statement grammar; the *data* layer is
   user-specific preference vocabulary (their hobbies, foods,
   projects).  Induce the data layer from corpus.

### C.2 Does P2 need its own TLG-style pre-experiment?

**Honest answer: probably yes, at smaller scope.**

TLG succeeded because we:
1. Built a standalone corpus experiment *before* integration.
2. Iterated on the grammar via hand-labeled ground truth.
3. Measured zero-confidently-wrong against adversarial queries.
4. Validated at LongMemEval scale.

Preference extraction has the same structural risk as temporal
retrieval: hand-coded extractors fail on edge cases and
confidently emit wrong facts.  A small experiment — say 50 hand-
labeled preferences from a LongMemEval conversation corpus —
would catch those failure modes before integration.

Recommended P2 structure (proposal, separate from this P1 plan):

* **P2a — Preference-grammar pre-experiment** (1 week).  Hand-
  curated 50-query corpus of preference extraction vs. gold
  extracted facts.  Build a small grammar similar to TLG's
  architecture.  Validate zero-confidently-wrong on adversarial
  queries.
* **P2b — Integration** (2 weeks).  Port to
  `src/ncms/application/consolidation/preference_extractor.py`.
  Run behind `NCMS_PREFERENCE_EXTRACTION_ENABLED`.
* **P2c — Validation** (1 week).  Re-run LongMemEval with P2
  enabled; measure delta on single-session-preference (30 Q,
  currently the benchmark's weakest category for BM25).

### C.3 Recommended order

1. **Ship P1 (TLG integration) first.**  Complete per this plan.
2. **Run LongMemEval again post-integration** to see if TLG
   alone moves the single-session-preference category.  (Hypothesis:
   probably not, because preference lookup is a fact-lookup not a
   trajectory query.)
3. **Start P2 pre-experiment** after P1 is shipped.  Apply TLG's
   lessons (grammar + abstention + data/structural split) to
   preference extraction.
4. **Revise `docs/p2-plan.md`** with the pre-experiment outcomes
   before starting integration.

This keeps P1 and P2 independently scoped and lets each benefit
from a proper pre-experiment.

---

## Appendix D: What NOT to do

Things that might seem reasonable but would break the
integration — noted so future agents don't accidentally do them:

1. **Don't remove `reconciliation_service.py` or its heuristics.**
   TLG depends on reconciliation producing typed edges.  We
   EXTEND reconciliation, we don't replace it.
2. **Don't move temporal logic to the scoring layer.**  TLG's
   composition guarantee (Proposition 1) depends on grammar
   running as a separate stage, not mixed into scalar scoring.
3. **Don't tune BM25 weights per query intent when TLG is on.**
   The composition pattern is "grammar decides rank-1 or
   defers" — changing BM25 weights under the grammar defeats
   the zero-confidently-wrong guarantee.
4. **Don't cache grammar answers without the skeleton key.**
   The shape cache is keyed on the skeleton (post-normalization);
   caching raw-query → answer would miss cross-variant
   generalization.
5. **Don't skip the parity test (§7.4).**  If experiment and
   integration diverge, it's a bug in the integration, not in
   TLG.  Parity is the bridge that catches wiring errors.

---

## Appendix E: Open questions for review

Before starting execution, these need answers:

1. **Q: Should we integrate behind `NCMS_TLG_ENABLED=False` for
   multiple releases, or cut over once all tests pass?**
   Recommendation: gated for at least one full release cycle.
2. **Q: Do we want the shape-cache to persist across processes or
   just per-process?**  Recommendation: per-process for v1 (simpler);
   SQLite persistence in Phase 8 (out of scope for P1).
3. **Q: Does the dashboard need a new view for grammar
   confidence, or do existing pipeline-event views suffice?**
   Recommendation: add grammar confidence to the existing
   `dashboard_events` stream as a new event type; dedicated
   view is P2-era.
4. **Q: Should the production induction research (TLG milestone
   M7 in `docs/temporal-linguistic-geometry.md` §7.6) happen in
   NCMS or remain in `experiments/`?**
   Recommendation: research stays in `experiments/` until
   results warrant integration.
5. **Q: Should TLG's Layer 1 vocabulary share infrastructure with
   NCMS's `infrastructure/extraction/label_detector.py` or stay
   independent?**  Recommendation: independent for v1; unify in
   Phase 2 refactor (orthogonal to integration).
6. **Q: Does `temporal_intent.py` deprecation break any external
   consumer (demo, CLI, bus agent)?**  Needs audit.  Action:
   `grep -r "from ncms.domain.temporal.intent" src/ benchmarks/ tests/`.

---

## Appendix F: Complete temporal-feature audit (as of 2026-04-18)

This audit covers EVERY temporal-touching feature in NCMS —
not just the retrieval-side temporal boosting that TLG replaces.
Readers should cross-reference §2.4 (orthogonal features) when
deciding what stays vs. what retires.

**Scope markers throughout:**

* ⊗  **Retires / deprecates** under TLG integration
* ⊕  **Keeps** — orthogonal or hard dependency
* ⊙  **Extends** — TLG adds behavior without changing existing

### F.1 Config flags (complete inventory, 22 flags)

**Retrieval-side temporal (this integration's target):**

| Flag | Default | Used at | Disposition |
|---|---|---|---|
| `temporal_enabled` | False | `scoring/pipeline.py:158–159` | ⊗ Deprecate |
| `scoring_weight_temporal` | 0.2 | `scoring/pipeline.py:487–490` | ⊗ Deprecate |
| `temporal_range_filter_enabled` | False | `ingestion/pipeline.py:568`, `retrieval/pipeline.py:185`, `memory_service.py:536,584,667,1272` | ⊙ Repurpose under TLG |
| `temporal_missing_range_policy` | "include" | `memory_service.py:717` | ⊙ Repurpose under TLG |
| `scoring_weight_recency` | 0.0 | `scoring/pipeline.py:231, 371–372` | ⊗ Deprecate |
| `recency_half_life_days` | 30.0 | `scoring/pipeline.py:371–372` | ⊗ Deprecate |

**ACT-R activation (orthogonal):**

| Flag | Default | Used at | Disposition |
|---|---|---|---|
| `actr_decay` | 0.5 | `domain/scoring.py::base_level_activation` | ⊕ Keep |
| `actr_noise` | 0.25 | `domain/scoring.py::activation_noise` | ⊕ Keep |
| `actr_threshold` | -2.0 | `domain/scoring.py::retrieval_probability` | ⊕ Keep |

**Episode formation (orthogonal — Phase 3):**

| Flag | Default | Used at | Disposition |
|---|---|---|---|
| `episode_window_minutes` | 1440 | `episode_service.py:291-297` | ⊕ Keep |
| `episode_close_minutes` | 1440 | `episode_service.py:857-881` | ⊕ Keep |
| `episode_weight_temporal` | 0.10 | `episode_service.py:291-297` | ⊕ Keep |

**Dream cycle (orthogonal — Phase 8):**

| Flag | Default | Used at | Disposition |
|---|---|---|---|
| `dream_rehearsal_weight_centrality` | 0.40 | `consolidation_service.py:605-649` | ⊕ Keep |
| `dream_rehearsal_weight_staleness` | 0.30 | `consolidation_service.py:605-649` | ⊕ Keep |
| `dream_rehearsal_weight_importance` | 0.20 | `consolidation_service.py:605-649` | ⊕ Keep |
| `dream_rehearsal_weight_access_count` | 0.05 | `consolidation_service.py:605-649` | ⊕ Keep |
| `dream_rehearsal_weight_recency` | 0.05 | `consolidation_service.py:605-649` | ⊕ Keep |
| `dream_staleness_days` | 7 | `consolidation_service.py:605-649` | ⊕ Keep |
| `dream_importance_drift_window_days` | 14 | `consolidation_service.py:899-960` | ⊕ Keep |
| `dream_importance_drift_rate` | 0.1 | `consolidation_service.py:899-960` | ⊕ Keep |
| `dream_forgetting_decay_rate` | 0.05 | `consolidation_service.py:1035-1121` | ⊕ Keep |
| `dream_forgetting_access_prune_days` | 90 | `consolidation_service.py:1035-1121` | ⊕ Keep |
| `dream_forgetting_conflict_age_days` | 14 | `consolidation_service.py:1035-1121` | ⊕ Keep |

**Abstract staleness (orthogonal — Phase 5):**

| Flag | Default | Disposition |
|---|---|---|
| `abstract_refresh_days` | 7 | ⊕ Keep |

**Maintenance scheduler (orthogonal):**

| Flag | Default | Disposition |
|---|---|---|
| `maintenance_consolidation_interval_minutes` | 360 | ⊕ Keep |
| `maintenance_dream_interval_minutes` | 1440 | ⊕ Keep |
| `maintenance_episode_close_interval_minutes` | 60 | ⊕ Keep |
| `maintenance_decay_interval_minutes` | 720 | ⊕ Keep |

**Admission / ephemeral (orthogonal):**

| Flag | Default | Disposition |
|---|---|---|
| `admission_ephemeral_ttl_seconds` | 3600 | ⊕ Keep |

**Bus / snapshot (orthogonal):**

| Flag | Default | Disposition |
|---|---|---|
| `bus_heartbeat_interval_seconds` | 30 | ⊕ Keep |
| `bus_heartbeat_timeout_seconds` | 90 | ⊕ Keep |
| `snapshot_ttl_hours` | 168 | ⊕ Keep |
| `auto_snapshot_on_disconnect` | False | ⊕ Keep |

**Indexing (orthogonal):**

| Flag | Default | Disposition |
|---|---|---|
| `index_drain_timeout_seconds` | 30 | ⊕ Keep |

**New (TLG introduces):**

| Flag | Default | Disposition |
|---|---|---|
| `NCMS_TLG_ENABLED` | False | ⊙ New |
| `NCMS_TLG_CONFIDENCE_MIN` | "medium" | ⊙ New |
| `NCMS_TLG_SHAPE_CACHE_PERSIST` | True | ⊙ New |
| `NCMS_TLG_CACHE_MAX_SKELETONS` | 10000 | ⊙ New |

### F.2 Domain modules

| Module | LOC | Exports | Consumers | Disposition |
|---|---:|---|---|---|
| `domain/temporal_parser.py` | 336 | `TemporalReference`, `parse_temporal_reference()`, `compute_temporal_proximity()` | `memory_service.py:43`, `scoring/pipeline.py:41` | **Keep — TLG reuses** |
| `domain/temporal_intent.py` | 240 | `TemporalIntent` enum, `classify_temporal_intent()`, `parse_arithmetic_spec()` | `memory_service.py:579,658,1204`, `retrieval/pipeline.py` | **Retire** |
| `domain/temporal_normalizer.py` | 605 | `RawSpan`, `NormalizedInterval`, `normalize_spans()`, `merge_intervals()`, `resolve_temporal_range()` | `ingestion/pipeline.py:39`, `memory_service.py:662`, `retrieval/pipeline.py:289,436` | **Keep — TLG reuses** |
| `domain/entity_extraction.py::TEMPORAL_LABELS` + `add_temporal_labels()` | (lines 25–41, 114–135) | Constants + helper | `gliner_extractor.py:26` | **Keep unchanged** |

### F.3 Scoring pipeline temporal logic

`application/scoring/pipeline.py`:

| Function | Lines | What it does | Disposition |
|---|---:|---|---|
| `_resolve_event_time()` | 405–425 | Extract `observed_at` from MemoryNode/Memory | Keep (TLG reuses) |
| `_compute_raw_signals()::temporal_raw` | 374–381 | Call `compute_temporal_proximity(event_time, temporal_ref)` | Remove |
| `_normalize_and_combine()::temporal_n` | 481–490 | Min-max normalize, apply `w_temporal` | Remove |
| `_score_one_candidate()::temporal_contrib` | 552–553 | Add `temporal_n * w_temporal` to combined score | Remove |
| Import `compute_temporal_proximity` | 41 | — | Remove after Phase 3 |

### F.4 Retrieval pipeline temporal logic

`application/retrieval/pipeline.py`:

| Function | Lines | What it does | Disposition |
|---|---:|---|---|
| `split_entity_and_temporal_spans()` | 288–313 | Partition GLiNER output | Keep (TLG range-intent consumer) |
| `apply_ordinal_ordering()` | 315–399 | Reorder by `observed_at` for ordinal intents | **Remove** |
| `resolve_temporal_range()` | 435–447 | Merge spans → range interval | Keep (TLG range-intent consumer) |
| `apply_range_filter()` | 449–511 | Hard-filter by `[a,b) ∩ [c,d)` overlap | **Remove** |
| `retrieve_candidates()::temporal labels` | 182–186 | Inject temporal labels for GLiNER | Keep (always-on behavior) |

### F.5 Memory service temporal hooks

`application/memory_service.py`:

| Call site | Lines | What it does | Disposition |
|---|---:|---|---|
| Imports | 43–45, 579–582, 658–661, 1204–1206 | Temporal modules | Adjust (some retire) |
| `_extract_query_range()` | 521–561 | Query-side span → range resolver (wraps `retrieval.split_entity_and_temporal_spans`) | **Remove** |
| `_apply_ordinal_if_eligible()` | 563–631 | Ordinal dispatch on temporal intent | **Remove** |
| `_apply_range_filter_if_eligible()` | 632–727 | Range-filter dispatch | **Remove** |
| `compute_temporal_arithmetic()` | 1159–1445 | Arithmetic resolver | Keep (orthogonal) |
| Call: `_apply_ordinal_if_eligible()` | 938–940 | Retrieval dispatch | **Remove** (replaced by TLG pipeline) |
| Call: `_apply_range_filter_if_eligible()` | 948–950 | Retrieval dispatch | **Remove** (replaced by TLG pipeline) |

### F.6 Ingestion temporal hooks

`application/ingestion/pipeline.py`:

| Hook | Lines | What it does | Disposition |
|---|---:|---|---|
| Import `add_temporal_labels` | 34–36 | — | Keep |
| Import `temporal_normalizer` | 39–41 | — | Keep |
| `_extract_entities()::add_temporal` | 566–569 | Inject temporal labels into GLiNER | Keep |
| `_persist_content_range()` | 645–708 | Write `memory_content_ranges` | Keep (TLG range-intent consumes) |
| `_ingest_memory_nodes()::observed_at` | 813–820 | Carry `observed_at` to L1 node | Keep |

`application/index_worker.py`:

| Hook | Lines | What it does | Disposition |
|---|---:|---|---|
| `observed_at` preservation | 557–560 | MemoryNode L1 gets source `observed_at` | Keep |

### F.7 Infrastructure

`infrastructure/extraction/gliner_extractor.py`:

| Detail | Lines | What it does | Disposition |
|---|---:|---|---|
| `TEMPORAL_LABELS` import | 26 | — | Keep |
| Label dedup | 188–190 | Allow same token as entity + temporal | Keep |
| Dual-GLiNER split | 222–260 | Split call if labels > 15 | Keep |

### F.8 Database schema — complete temporal inventory (34 columns across 28 tables)

Complete roll-call of every temporal column in the schema.
Schema version: **11** (plus the v13 TLG migration).

| Table | Temporal columns | Indexes on temporal columns | Disposition |
|---|---|---|---|
| `memories` | `created_at`, `updated_at`, `observed_at` | `idx_memories_observed_at` | ⊕ Keep all |
| `entities` | `created_at`, `updated_at` | — | ⊕ Keep |
| `relationships` | `valid_at`, `invalid_at`, `created_at` | none (**gap** — see F.20) | ⊕ Keep (bitemporal entity-graph) |
| `memory_entities` | — | — | — |
| `access_log` | `accessed_at` | `idx_access_memory (memory_id, accessed_at)` | ⊕ Keep (ACT-R critical) |
| `snapshots` | `timestamp`, `created_at` | `idx_snapshots_agent (agent_id, timestamp DESC)` | ⊕ Keep (snapshot TTL) |
| `consolidation_state` | `updated_at` | — | ⊕ Keep (last-run tracking) |
| `memory_nodes` | `valid_from`, `valid_to`, `observed_at`, `ingested_at`, `created_at` | none (**gap** — see F.20) | ⊕ Keep (bitemporal — TLG reads) |
| `graph_edges` | `created_at` | — | ⊙ **TLG extends** with `retires_entities` (v13) |
| `ephemeral_cache` | `created_at`, `expires_at` | `idx_ephemeral_expires (expires_at)` | ⊕ Keep (admission tier) |
| `search_log` | `timestamp` | `idx_search_log_ts (timestamp)` | ⊕ Keep (dream PMI) |
| `association_strengths` | `updated_at` | — | ⊕ Keep (PMI associations) |
| `documents` | `created_at` | — | ⊕ Keep |
| `document_links` | `created_at` | — | ⊕ Keep |
| `dashboard_events` | `timestamp` | `idx_devents_ts (timestamp)` | ⊕ Keep (SSE stream) |
| `projects` | `created_at`, `updated_at` | `idx_projects_created (created_at)` | ⊕ Keep |
| `pipeline_events` | `timestamp` | `idx_pipeline_ts (timestamp)` | ⊕ Keep (audit) |
| `review_scores` | `created_at` | — | ⊕ Keep |
| `approval_decisions` | `timestamp` | — | ⊕ Keep |
| `guardrail_violations` | `timestamp` | — | ⊕ Keep |
| `grounding_log` | `timestamp` | — | ⊕ Keep |
| `llm_calls` | `timestamp` | — | ⊕ Keep |
| `agent_config_snapshots` | `timestamp` | — | ⊕ Keep |
| `bus_conversations` | `timestamp` | — | ⊕ Keep |
| `pending_approvals` | `created_at`, `decided_at` | — | ⊕ Keep |
| `users` | `created_at` | — | ⊕ Keep |
| `schema_version` | — | — | ⊕ Keep |
| `memory_content_ranges` | `range_start`, `range_end` | `idx_mcr_range (range_start, range_end)` | ⊙ Keep (TLG range-intent consumer) |

**Totals:** 28 tables, 34 temporal columns, 9 indexed temporal columns.

**New in v13 (this integration adds):**

| Element | Purpose |
|---|---|
| `graph_edges.retires_entities` (JSON array) | Structural extractor output on SUPERSEDES edges |
| `grammar_shape_cache` (table) | Persisted query-shape cache |
| `grammar_transition_markers` (table) | Persisted Layer 2 verb inventory |

### F.8a Graph edge types (14 total, from `domain/models.py:39-60`)

| Edge type | Semantic | Has temporal behavior? | TLG disposition |
|---|---|---|---|
| `BELONGS_TO_EPISODE` | Atomic fragment → episode | — | ⊕ Keep |
| `ABSTRACTS` | Episode → abstract summary | — | ⊕ Keep |
| `DERIVED_FROM` | L1→L2 provenance | — | ⊕ Keep |
| `SUMMARIZES` | Abstract → episodes | — | ⊕ Keep |
| `MENTIONS_ENTITY` | Node → entity | — | ⊕ Keep |
| `RELATED_TO` | Generic | — | ⊕ Keep |
| **`SUPPORTS`** | Reconciliation: same state | — | ⊕ Keep (TLG reads) |
| **`REFINES`** | Reconciliation: narrower scope | — | ⊕ Keep (TLG reads) |
| **`SUPERSEDES`** | Reconciliation: new replaces old | **Yes — sets `valid_to=now`, `is_current=False`** | ⊙ **TLG extends** (`retires_entities`) |
| **`SUPERSEDED_BY`** | Reciprocal of SUPERSEDES | **Yes** | ⊕ Keep (TLG reads) |
| **`CONFLICTS_WITH`** | Parallel-truth conflict | — | ⊕ Keep |
| `CURRENT_STATE_OF` | Entity → current state | — | ⊕ Keep |
| `PRECEDES` | Temporal ordering (reserved) | Yes — but NOT currently emitted | ⊕ Keep (reserved) |
| `CAUSED_BY` | Event causality (reserved) | Yes — but NOT currently emitted | ⊕ Keep (reserved) |

**Reconciliation emits** (`reconciliation_service.py`):
`SUPPORTS`, `REFINES`, `SUPERSEDES`, `SUPERSEDED_BY`, `CONFLICTS_WITH`.

**No production path currently emits** `PRECEDES` or `CAUSED_BY`.
They are reserved for future consolidation / causal-reasoning work.
TLG's `sequence` / `predecessor` / `transitive_cause` intents
traverse `SUPERSEDES` / `REFINES` edges, not `PRECEDES`.

### F.8b Graph node types (4 total, from `domain/models.py:30-36`)

| Node type | Temporal fields | Role | TLG disposition |
|---|---|---|---|
| `ATOMIC` (L1) | `observed_at`, `created_at`, `ingested_at` (valid_from/valid_to nullable) | Raw ingest fragment | ⊕ Keep |
| `ENTITY_STATE` (L2) | `valid_from`, `valid_to`, `is_current`, `observed_at`, `created_at`, `ingested_at` | State snapshot with bitemporal validity | ⊕ Keep — TLG zone computation depends |
| `EPISODE` (L3) | `created_at`, `metadata.closed_at`, `metadata.status` | Temporal container for atomics | ⊕ Keep |
| `ABSTRACT` (L4) | `created_at`, `metadata.refresh_due_at` | Topic cluster from episodes | ⊕ Keep |

### F.8c Bitemporal query methods (4, in `sqlite_store.py:582-654`)

| Method | Signature | Used by | Disposition |
|---|---|---|---|
| `get_state_at_time(entity_id, state_key, timestamp)` | Point-in-time query: `valid_from ≤ t < valid_to` | Reserved for historical queries; TLG's `current` intent depends on the simpler `is_current=True` filter | ⊕ Keep |
| `get_state_changes_since(timestamp)` | Range query: all `ENTITY_STATE` nodes created after t | Dream cycle incremental runs | ⊕ Keep |
| `get_state_history(entity_id, state_key)` | Full chronological history (current + superseded) | `ncms state history` CLI | ⊕ Keep |
| `get_current_entity_states(entity_id, state_key)` | Current (`is_current=True`) states | Reconciliation input; TLG current-zone detection | ⊕ Keep |

### F.8d ACT-R activation (`domain/scoring.py:23-339`)

| Component | Function | Input timestamp | TLG disposition |
|---|---|---|---|
| Base-level `B_i` | `base_level_activation(access_ages, decay)` lines 23-48 | `access_log.accessed_at` → age_seconds | ⊕ Keep (orthogonal) |
| Spreading `S_i` (legacy) | `spreading_activation()` lines 51-100 | none | ⊕ Keep |
| Graph spreading | `graph_spreading_activation()` lines 103-201 | none | ⊕ Keep |
| PPR | `ppr_graph_score()` lines 204-238 | none | ⊕ Keep |
| Recency | `recency_score()` lines 289-314 | `memory.created_at` → age_days | ⊕ Keep (weight 0.0 default) |
| Activation noise | `activation_noise()` lines 241-254 | none | ⊕ Keep |
| Supersession penalty | `supersession_penalty()` lines 322-329 | reads SUPERSEDED_BY edges | ⊕ Keep (TLG compatible) |
| Conflict penalty | `conflict_annotation_penalty()` lines 332-339 | reads CONFLICTS_WITH edges | ⊕ Keep |
| Retrieval prob | `retrieval_probability()` lines 267-281 | computed from A_i | ⊕ Keep |

### F.8e Episode-formation temporal (`episode_service.py`)

| Hook | Lines | Behavior | TLG disposition |
|---|---:|---|---|
| Temporal proximity signal | 291-297 | Linear decay within `episode_window_minutes` using `last_member_time` (1 of 7 linker weights, weight 0.10) | ⊕ Keep |
| `_get_last_member_time()` | 958 | Derive episode's newest-member timestamp | ⊕ Keep |
| Edge `assigned_at` | 750 | ISO timestamp on episode-assignment edge | ⊕ Keep |
| `close_stale_episodes()` | 857-881 | Auto-close after `episode_close_minutes` of inactivity | ⊕ Keep |
| `check_resolution_closure()` | 883-897 | Content-based closure | ⊕ Keep |
| Episode metadata `closed_at` | 906 | ISO timestamp on closure | ⊕ Keep |

### F.8f Consolidation / dream cycle temporal (`consolidation_service.py`)

| Hook | Lines | Behavior | TLG disposition |
|---|---:|---|---|
| Decay pass (ACT-R) | 83 | Applies base-level decay to importance | ⊕ Keep |
| `run_dream_rehearsal` | 560-730 | 5-signal weighted selection | ⊕ Keep |
| `_compute_dream_signals` | 605-649 | Staleness = access_ages / 86400 days | ⊕ Keep |
| `learn_association_strengths` | 732-823 | All-time PMI from `search_log` | ⊕ Keep |
| `adjust_importance_drift` | 899-960 | ±0.1 per 14-day comparison | ⊕ Keep |
| `active_forgetting` | 1035-1121 | 90-day prune, 14-day conflict age | ⊕ Keep |
| `run_dream_cycle` orchestrator | 1122-1163 | Maintenance entry point | ⊕ Keep |
| Abstract `refresh_due_at` creation | 1226-1228 | `now + abstract_refresh_days` | ⊕ Keep |
| `_is_stale()` check | 1268-1277 | `now >= refresh_due_at` | ⊕ Keep |
| Consolidation state | 122, 195-197 | `last_knowledge_consolidation` timestamp | ⊕ Keep |

### F.8g Maintenance scheduler (`maintenance_scheduler.py:200-219`)

All 4 background loops are orthogonal to retrieval-side TLG:

| Loop | Interval | Service call | TLG disposition |
|---|---|---|---|
| `consolidation` | 360 min | `consolidation_svc.run_consolidation_pass()` | ⊕ Keep |
| `dream` | 1440 min | `consolidation_svc.run_dream_cycle()` | ⊕ Keep |
| `episode_close` | 60 min | `episode_svc.close_stale_episodes()` | ⊕ Keep |
| `decay` | 720 min | `consolidation_svc.run_decay_pass()` | ⊕ Keep |

`TaskStatus` records `last_run_at`, `next_run_at`, `last_duration_ms` per loop.

### F.8h Admission temporal_salience (`admission_service.py:133-145`)

1 of 4 admission features — orthogonal to retrieval.

| Signal | Score | TLG disposition |
|---|---|---|
| ISO date in content | +0.40 | ⊕ Keep |
| Informal date in content | +0.40 | ⊕ Keep |
| Temporal markers (capped 0.30) | +0.15 each | ⊕ Keep |
| Temporal verbs (capped 0.30) | +0.10 each | ⊕ Keep |

### F.8i Reconciliation bitemporal (`reconciliation_service.py`, expanded)

| Behavior | Lines | Temporal effect | TLG disposition |
|---|---:|---|---|
| Classify states | 55-134 | No temporal check — state_key/value/scope only | ⊕ Keep |
| `_apply_supports` | 141 | No valid_* change | ⊕ Keep |
| `_apply_refines` | 164 | No valid_* change | ⊕ Keep |
| `_apply_supersedes` | 182-229 | **Sets `old.valid_to=now`, `old.is_current=False`, `new.valid_from=now`** | ⊙ **TLG extends** — adds `retires_entities` emission |
| `_apply_conflicts` | 235 | No valid_* change | ⊕ Keep |
| `get_current_entity_states` | 51 | `is_current=True` filter | ⊕ Keep |

### F.8j Bus / snapshot / agent temporal

| Component | File:line | Temporal behavior | TLG disposition |
|---|---|---|---|
| Agent `last_seen` | `async_bus.py:126-137` | Updated on every visibility change | ⊕ Keep |
| Heartbeat loop | `bus_service.py:220-320` | 30s interval, 90s timeout | ⊕ Keep |
| Snapshot TTL enforcement | `snapshot_service.py:77-93` | 168h default | ⊕ Keep |
| Snapshot age in surrogate | `snapshot_service.py:143` | `(now - ts).total_seconds()` | ⊕ Keep |

### F.8k CLI temporal commands (`interfaces/cli/main.py`)

| Command | Lines | Temporal behavior | TLG disposition |
|---|---:|---|---|
| `ncms state get/history/list` | 1022-1165 | Reads ENTITY_STATE nodes, displays `created_at` | ⊕ Keep |
| `ncms episodes list/show` | 1178-1283 | Shows episode `created_at`, `closed_at` | ⊕ Keep |
| `ncms maintenance status/run` | 1296-1459 | Scheduler state | ⊕ Keep |
| `ncms watch` | 562-741 | 2.0s debounce; file-hash dedup | ⊕ Keep |
| `ncms lint` | 1464-1554 | Stale-episode check | ⊕ Keep |

### F.8l Dashboard / observability

| Component | File:line | Temporal role | TLG disposition |
|---|---|---|---|
| `DashboardEvent.timestamp` | `event_log.py:28-43` | Emission time (ISO UTC) | ⊕ Keep |
| `dashboard_events` persistence | `event_log.py:134-149` | Async write queue | ⊕ Keep |
| `/api/agents` `last_seen` | `dashboard.py:79-90` | Per-agent timestamp | ⊕ Keep |
| SSE keepalive | `dashboard.py:44-75` | 30s | ⊕ Keep |
| Pipeline event emission | Multiple services | Emits events with timestamps | ⊕ Keep |

### F.9 Storage API

`infrastructure/storage/sqlite_store.py`:

| Function | Lines | Purpose | Disposition |
|---|---:|---|---|
| INSERT INTO `memories` (observed_at) | 67–83 | Persist source event time | Keep |
| INSERT INTO `memory_nodes` (observed_at) | 458–473 | Persist node event time | Keep |
| `save_content_range()` | 339–353 | Write content range | Keep |
| `get_content_range()` | 355–366 | Read single range | Keep |
| `get_content_ranges_batch()` | 368–381 | Batch read (range filter) | Keep |

### F.10 Tests (18 total)

| File | Focus | Disposition |
|---|---|---|
| `tests/unit/domain/test_temporal_intent.py` | P1b classify_temporal_intent() | **Retire** |
| `tests/unit/domain/test_temporal_normalizer.py` | P1a span normalization | Keep (TLG reuses) |
| `tests/unit/application/test_apply_ordinal_ordering.py` | P1b ordinal rerank | **Retire** |
| `tests/unit/application/test_apply_range_filter.py` | P1 range filter | **Retire** (TLG has own tests) |
| `tests/unit/infrastructure/storage/test_content_range_store.py` | CRUD `memory_content_ranges` | Keep |
| `tests/unit/infrastructure/extraction/test_label_budget.py` | GLiNER budget | Keep |
| `tests/integration/test_bitemporal_wiring.py` | observed_at threading | Keep |
| `tests/integration/test_temporal_range_extraction.py` | Ingest → range | Keep |
| `tests/integration/test_explicit_range_primitive.py` | Range filter end-to-end | **Retire** |
| `tests/integration/test_ordinal_sequence_primitive.py` | Ordinal end-to-end | **Retire** |
| `tests/integration/test_arithmetic_resolver.py` | Arithmetic resolver | Keep (orthogonal) |
| `tests/integration/test_named_entity_no_regression.py` | Baseline | Keep |
| `tests/unit/domain/test_models_phase1.py` | Model schema | Keep |
| `tests/unit/test_admission_service.py` | Admission (orthogonal) | Keep |
| `tests/unit/test_scoring_admission.py` | Admission (orthogonal) | Keep |
| `tests/unit/test_contradiction_detector.py` | Contradiction (orthogonal) | Keep |
| `tests/unit/infrastructure/storage/test_sqlite_store_phase1.py` | Storage basic | Keep |
| `tests/unit/infrastructure/storage/test_sqlite_store_phase2.py` | Storage extended | Keep |

### F.11 Benchmarks

| Path | Purpose | Disposition |
|---|---|---|
| `benchmarks/longmemeval/temporal_diagnostic.py` | P1 temporal diagnostic regression | **Retire** (replaced by `tlg_diagnostic.py`) |
| `benchmarks/results/temporal_diagnostic/*.json` | Historical P1 runs | Keep as archive; stop generating new |
| `benchmarks/results/longmemeval/features_on/*` | Features-on benchmark results | Re-run with TLG in Phase 6 |

### F.12 Index audit and recommendations (new for v13)

The audit surfaced two **missing-but-queried temporal indexes**.
These are NOT required for TLG but would benefit any
point-in-time bitemporal query (TLG-related or otherwise).
Adding in v13 is optional; recommended as part of integration.

| Missing index | Table | Columns | Queries that benefit |
|---|---|---|---|
| `idx_mnodes_valid_range` | `memory_nodes` | `(valid_from, valid_to)` | `get_state_at_time`, TLG zone computation |
| `idx_mnodes_entity_state` | `memory_nodes` | `(metadata.entity_id, state_key, valid_from)` | Bitemporal state-timeline queries |
| `idx_relationships_valid` | `relationships` | `(valid_at, invalid_at)` | Entity-graph bitemporal reads |

### F.13 Retirement summary counts (comprehensive)

**Modules:**
* Retired (full delete): 1 — `src/ncms/domain/temporal_intent.py`
* Extended (TLG adds behavior): 1 — `reconciliation_service.py`
* Kept unchanged: 3 — `temporal_parser.py`, `temporal_normalizer.py`, `entity_extraction.py`
* New: ~8 modules in `src/ncms/domain/grammar/`

**Methods / functions removed (query-time retrieval temporal only):**
* `memory_service.py`: `_extract_query_range`, `_apply_ordinal_if_eligible`, `_apply_range_filter_if_eligible` (3)
* `retrieval/pipeline.py`: `apply_ordinal_ordering`, `apply_range_filter` (2)
* `scoring/pipeline.py`: `compute_temporal_proximity` call chain (3 sites)
* **Total: ~8 removals, ~350 LOC**

**Methods / functions kept (orthogonal or TLG reuses):**
* `memory_service.py::compute_temporal_arithmetic` (arithmetic resolver)
* `retrieval/pipeline.py::split_entity_and_temporal_spans` (TLG range-intent helper)
* `retrieval/pipeline.py::resolve_temporal_range` (TLG range-intent helper)
* `scoring/pipeline.py::_resolve_event_time` (TLG zone handlers reuse)
* `domain/scoring.py::base_level_activation` (ACT-R)
* `domain/scoring.py::recency_score` (keep, weight 0.0)
* All of `episode_service.py`, `consolidation_service.py`, `maintenance_scheduler.py`, `admission_service.py`, `snapshot_service.py`, `bus_service.py`

**Config flags:**
* Retrieval-temporal deprecated: 4 (`NCMS_TEMPORAL_ENABLED`, `NCMS_SCORING_WEIGHT_TEMPORAL`, `NCMS_SCORING_WEIGHT_RECENCY`, `NCMS_RECENCY_HALF_LIFE_DAYS`)
* Retrieval-temporal repurposed under TLG: 2 (`NCMS_TEMPORAL_RANGE_FILTER_ENABLED`, `NCMS_TEMPORAL_MISSING_RANGE_POLICY`)
* New: 4 (`NCMS_TLG_*`)
* **Unchanged orthogonal:** 21 temporal flags (ACT-R, episode, dream, consolidation, maintenance, admission ephemeral TTL, bus heartbeat, snapshot TTL, index drain, abstract refresh, etc.)

**Tests:**
* Retired: 6 (P1a/P1b specific)
* Kept: 12+ (including bitemporal, content-range CRUD, label budget, normalizer, arithmetic)
* New: 6 (TLG unit, integration, adversarial, parity, scale)

**Schema changes (v13):**
* Tables added: 2 (`grammar_shape_cache`, `grammar_transition_markers`)
* Columns added: 1 (`graph_edges.retires_entities`)
* Indexes added: 3 recommended (see F.12)
* Tables unchanged: **all 27** existing (memories, memory_nodes, memory_content_ranges, graph_edges rows, reconciliation fields, access_log, search_log, ephemeral_cache, dashboard_events, pipeline_events, relationships, snapshots, consolidation_state, ...)
* Temporal columns unchanged: **all 34**

**Graph-level:**
* Edge types unchanged: 14 (including reserved `PRECEDES` / `CAUSED_BY`)
* Node types unchanged: 4 (`ATOMIC`, `ENTITY_STATE`, `EPISODE`, `ABSTRACT`)
* Bitemporal query methods unchanged: 4 (`get_state_at_time`, `get_state_changes_since`, `get_state_history`, `get_current_entity_states`)

**Benchmark:**
* Retired: 1 harness (`temporal_diagnostic.py`)
* Kept: all other benchmarks (unchanged, just re-run with TLG on)

**CLI / dashboard / bus:**
* All temporal CLI commands (`state`, `episodes`, `maintenance`, `watch`, `lint`) — unchanged
* All dashboard event types — unchanged
* Bus heartbeats and snapshot surrogate logic — unchanged

**Net effect on code size:**
* Lines removed: ~350
* Lines added (grammar package from experiment port): ~4,000
* Lines changed (reconciliation extension): ~50
* Net: **+~3,700 LOC** across `src/` for the new grammar subsystem.

---

## Appendix G: Verification log

All file:line citations and behavior claims in this document were
verified against the NCMS codebase on **2026-04-18**.  If you are
reviewing or executing this plan more than ~2 weeks later, assume
code may have drifted and re-verify the critical hook points
(§3.4.5) before starting Phase 1.

### G.1 What was verified

| Claim | Verified how | Status |
|---|---|---|
| NodeType enum has 4 values | Read `domain/models.py:30-36` | ✓ (ATOMIC, ENTITY_STATE, EPISODE, ABSTRACT) |
| EdgeType enum has 14 values | Read `domain/models.py:39-59` | ✓ (includes reserved PRECEDES / CAUSED_BY) |
| Reranker gated on {FACT_LOOKUP, PATTERN_LOOKUP, STRATEGIC_REFLECTION} | Read `retrieval/pipeline.py:558-569` | ✓ |
| Reconciliation `_apply_supersedes` sets valid_from/valid_to/is_current + emits SUPERSEDES + SUPERSEDED_BY | Read `reconciliation_service.py:182-229` | ✓ |
| Document profile memory stored in memory store via `_store_bypassing_classification()` | Read `section_service.py:204-217` | ✓ |
| Section memories go to document store only | Read `section_service.py:167-177` | ✓ |
| Config defaults: `reconciliation_enabled=False`, `temporal_enabled=False`, `reranker_enabled=False`, `temporal_range_filter_enabled=False`, etc. | Read `config.py` lines 111, 115, 121, 158, 163, 224 | ✓ |
| Dream rehearsal weights sum to 1.0 (0.40+0.30+0.20+0.05+0.05) | Read `config.py:215-219` | ✓ |
| `_apply_ordinal_if_eligible` at memory_service.py:563-631 | Read file + awk for end-of-method | ✓ (corrected from 563-630) |
| `_apply_range_filter_if_eligible` at memory_service.py:632-727 | Read file + awk for end-of-method | ✓ (**corrected** from 643-722) |
| `retrieval/pipeline.py::apply_ordinal_ordering` at 315-400 | Read file + awk | ✓ |
| `retrieval/pipeline.py::apply_range_filter` at 449-513 | Read file + awk | ✓ |
| `retrieval/pipeline.py::split_entity_and_temporal_spans` at 289 | grep | ✓ |
| 28 tables in schema | `grep CREATE TABLE` in migrations.py | ✓ (**corrected** from 27) |
| DERIVED_FROM edge creation in ingest path | Read `ingestion/pipeline.py:914-921` and `index_worker.py:650-655` | ✓ |
| Reconciliation call site in `ingestion/pipeline.py` at 842 | grep | ✓ |
| Reconciliation call in `index_worker.py` at 667 | grep | ✓ |

### G.2 Claims that WERE WRONG and have been corrected

| Wrong claim | Correction |
|---|---|
| `_split_entity_and_temporal_spans` exists in `memory_service.py` | **Actual method name:** `_extract_query_range` at line 521-561.  That method wraps `retrieval/pipeline.py::split_entity_and_temporal_spans` (at line 289). |
| `_apply_range_filter_if_eligible` spans 643-722 | **Actual:** 632-727 |
| `_apply_ordinal_if_eligible` ends at line 630 | **Actual:** 631 |
| 27 tables in schema | **Actual:** 28 (discrepancy with CLAUDE.md; the extra table is `association_strengths` or similar) |
| `NCMS_ASYNC_INDEXING_ENABLED=True` is the default | **Actual:** no such flag.  Async is always on when `IndexWorkerPool` is initialized; fallback to inline happens on queue backpressure, not flag toggle |
| "Document content gets no TLG state tracking" (early §2.4 claim) | **Corrected:** document profile memory DOES go through reconciliation; section memories do not. |
| "Rehydration gap is an oversight" (implicit §3.4 claim) | **Corrected:** per institutional memory, avoiding rehydration was an intentional choice to prevent graph memory explosion from bidirectional edges |

### G.3 Claims NOT re-verified (lower-priority citations)

These citations were pulled from the earlier Explore-agent audits
and not re-read.  Low-risk (supporting detail, not hook points):

* `consolidation_service.py` lines for dream cycle — relied on audit
* `episode_service.py` lines for episode linker — relied on audit
* `domain/scoring.py` line numbers for ACT-R components — relied on audit
* `maintenance_scheduler.py:200-219` for loops — relied on audit
* `admission_service.py:133-145` for temporal_salience — relied on audit
* `bus_service.py:220-320` for heartbeats — relied on audit
* `snapshot_service.py:77-93` for TTL — relied on audit
* `event_log.py:28-43` for DashboardEvent — relied on audit
* `async_bus.py:126-137` for `last_seen` — relied on audit
* `sqlite_store.py:582-654` for bitemporal query methods — relied on audit

These are supporting facts that would not change the integration
design if off by a few lines.  Re-verify as part of Phase 0 if
making pull requests touching those files.

### G.4 Ongoing verification plan

Phase 0 verification checklist (before starting Phase 1 PRs):

1. Re-run `grep apply_ordinal_rerank\|apply_subject_scoped_ordinal_rerank src/ tests/ benchmarks/` — must return zero hits.
2. Confirm all G.1 table entries still apply (code hasn't drifted).
3. Spot-check G.3 entries if Phase 0 touches those files.
4. Document any new drift findings in this Appendix so the
   integration sequence doesn't hit surprise conflicts.

---

## Status

* **Plan authored:** 2026-04-18
* **Status:** Draft — awaiting review
* **Owner:** TBD
* **Dependencies:** None; ready to start Phase 0 once approved
* **Next action:** Review Appendix E open questions; approve or
  amend; kick off Phase 0.
