# CTLG Migration Audit

**Status:** design
**Companion:** [`ctlg-design.md`](./ctlg-design.md), [`ctlg-grammar.md`](./ctlg-grammar.md)
**Owner:** NCMS core

The CTLG pivot affects existing code, docs, and data assets. This doc audits each one and assigns a disposition: **KEEP**, **EXTEND**, **REFRAME**, **ARCHIVE**, or **RETIRE**. Nothing is deleted without justification; the priority is to preserve working code paths while clearly marking what the pivot supersedes.

---

## 0. Dispositions

| Label | Meaning |
|-------|---------|
| **KEEP** | Correct as-is; no change needed |
| **EXTEND** | Works today; CTLG adds new functionality alongside |
| **REFRAME** | Semantics change but interface stays backward-compatible |
| **ARCHIVE** | Old-design artifact; moved to `adapters/_archive/` or `docs/completed/` |
| **RETIRE** | Functionally obsolete; deprecated API with shim for one release cycle, then removed |

---

## 1. Documents

| Doc | Disposition | Action |
|-----|-------------|--------|
| `docs/temporal-linguistic-geometry.md` (P1 TLG, 2432 lines) | **KEEP** + cross-link | Add a brief header note pointing to CTLG extension docs; body stays intact — it's the authoritative base. |
| `docs/research/ctlg-design.md` (pivot overall design) | **NEW** | Already written |
| `docs/research/ctlg-grammar.md` (formal grammar extension) | **NEW** | Already written |
| `docs/research/ctlg-cue-guidelines.md` (cue-labeling annotator contract) | **NEW** | Already written |
| `docs/research/ctlg-migration-audit.md` (this doc) | **NEW** | Already written |
| `docs/completed/failed-experiments/shape-intent-classification.md` | **NEW** (retrospective) | Already written |
| `docs/forensics/v7.1-tlg-forensics.md` | **KEEP** | It's the artifact that motivated the pivot — leave as-is |
| `docs/slm-entity-extraction-deep-audit.md` | **EXTEND** | Append §13: "shape_intent retrospective + CTLG pivot" |
| `docs/slm-tlg-architecture.md` | **REFRAME** | Replace the `shape_intent_head` block with a pointer to CTLG docs; note head 6 is being re-purposed |
| `docs/mseb-results.md` §5 | **EXTEND** | Note: v7.1/v7.2 shape_intent metrics superseded; new CTLG benchmarks planned |
| `docs/completed/p1-experiment-diary.md` | **KEEP** | Historical record; references DP longest-chain algo that CTLG `h_parsimony` generalizes |
| `docs/completed/p2-plan.md` | **KEEP** | P2 plan; CTLG is P3 material |
| `docs/intent-slot-distillation*.md` | **KEEP** | Historical record of the SLM work |
| `CLAUDE.md` | **EXTEND** (done) | Added design decisions #28 (self-evolving catalog) + #29 (CTLG pivot) |

---

## 2. Source code

### 2.1 Domain layer

| File | Disposition | Rationale |
|------|-------------|-----------|
| `src/ncms/domain/models.py` | **EXTEND** | Add `EdgeType.CAUSED_BY`, `EdgeType.ENABLES`; no existing enum values removed |
| `src/ncms/domain/tlg/zones.py` | **EXTEND** | Add `CausalZone`, `Trajectory` types alongside existing `Zone`; production rules unchanged |
| `src/ncms/domain/tlg/grammar.py` (existing TLG grammar) | **EXTEND** | Extend trajectory grammar with `G_tr,c`; add causal productions; existing productions untouched |
| `src/ncms/domain/tlg/heuristics.py` | **NEW** | Create — the 5 causal heuristics (§4 of ctlg-grammar.md) |
| `src/ncms/domain/tlg/semantic_parser.py` | **NEW** | Create — the cue-tag → TLGQuery synthesizer |
| `src/ncms/domain/tlg/cue_taxonomy.py` | **NEW** | Create — the BIO cue label enum + CueLabel/TaggedToken dataclasses |
| `src/ncms/domain/tlg/retirement_extractor.py` | **REFRAME** | Add causal-justification scoring via `h_explanatory`; backward-compat interface |

### 2.2 Application layer

| File | Disposition | Rationale |
|------|-------------|-----------|
| `src/ncms/application/tlg/dispatch.py` | **REFRAME** | 10 walkers become grammar-production parsers; scoring lifted to typed heuristics; interfaces preserved |
| `src/ncms/application/ingestion/pipeline.py` | **EXTEND** | Add causal-cue-tagger pass + CAUSED_BY edge creation after L2 creation |
| `src/ncms/application/memory_service.py` | **KEEP** | `retrieve_lg` / `compose_with_lg` signatures unchanged; consumes extended `LGTrace` |
| `src/ncms/application/adapters/methods/joint_bert_lora.py` | **REFRAME** | Replace `shape_intent_head` (pooled Linear) with `shape_cue_head` (per-token Linear); keep role_head + other 4 heads unchanged |
| `src/ncms/application/adapters/schemas.py` | **EXTEND** | Add `CueLabel`, `TaggedToken`, keep `ShapeIntent` with `@deprecated` for one cycle |
| `src/ncms/application/adapters/sdg/catalog/` | **KEEP** | Catalog structure correct; feeds CTLG training data via gazetteer REFERENT bootstrap |
| `src/ncms/application/reconciliation_service.py` | **EXTEND** | Add causal-edge creation helpers; retirement logic reuses existing path |

### 2.3 Infrastructure layer

| File | Disposition | Rationale |
|------|-------------|-----------|
| `src/ncms/infrastructure/storage/sqlite_store.py` | **EXTEND** | Add `list_graph_edges_by_type("caused_by")` query; ensure `novel_surfaces` table via new migration |
| `src/ncms/infrastructure/storage/migrations.py` | **EXTEND** | New migration: `novel_surfaces` table |
| `src/ncms/infrastructure/extraction/intent_slot/lora_model.py` | **REFRAME** | Mirror the `shape_cue_head` change from experiment-side model; keep v6/v7.x compat via manifest-driven dispatch |
| `src/ncms/infrastructure/extraction/intent_slot/adapter_loader.py` | **EXTEND** | Add `cue_labels` field to manifest; mark `shape_intent_labels` deprecated |

### 2.4 CLI layer

| File | Disposition | Rationale |
|------|-------------|-----------|
| `src/ncms/interfaces/cli/adapters.py` | **KEEP** | Existing `list/status/generate-sdg/train/deploy` commands work unchanged |
| `src/ncms/interfaces/cli/catalog.py` | **NEW** | New CLI: `ncms catalog {review,suggest,auto-merge}` for self-evolving taxonomy |

---

## 3. Data artifacts

### 3.1 Corpora

| Path | Disposition | Action |
|------|-------------|--------|
| `adapters/corpora/gold_shape_intent_*.jsonl` | **ARCHIVE** | Move to `adapters/_archive/pre_ctlg/` — text content re-used as seed for CTLG cue-tagging gold via LLM relabeling; classification labels themselves are obsolete |
| `adapters/corpora/gold_shape_intent_software_dev.jsonl.pre_v7.2.bak` | **ARCHIVE** | Already a backup; move to `adapters/_archive/pre_ctlg/` |
| `adapters/corpora/gold_software_dev.jsonl` | **KEEP** | Ingest-side gold, still used by all 5 content heads + v7.2 role head |
| `adapters/corpora/gold_clinical.jsonl` / `gold_conversational.jsonl` / `gold_swe_diff.jsonl` | **KEEP** | Same — other domain content-side gold |
| `adapters/corpora/sdg_*.jsonl` | **KEEP** | Regenerated on every train; will pick up CTLG changes automatically |
| `adapters/corpora/adversarial_train_*.jsonl` | **KEEP** | Regenerated on every train |
| `adapters/corpora/gold_cue_tagging_software_dev.jsonl` | **NEW** | Will be generated by Phase 2 (LLM-tag existing 485 rows → 3000 rows) |
| `adapters/corpora/gold_memory_cues_software_dev.jsonl` | **NEW** | Will be generated by Phase 2 (memory-voice cue gold for ingest-side causal edge extraction) |
| `adapters/corpora/gold_counterfactual_software_dev.jsonl` | **NEW** | Will be generated by Phase 2 (counterfactual queries for modal axis) |

### 3.2 Checkpoints

| Path | Disposition | Action |
|------|-------------|--------|
| `adapters/checkpoints/software_dev/v4/` | **ARCHIVE** | Legacy, pre-catalog; move to `adapters/_archive/pre_ctlg/` |
| `adapters/checkpoints/software_dev/v6/` | **ARCHIVE** | Legacy BIO slot head; move to `adapters/_archive/pre_ctlg/` |
| `adapters/checkpoints/software_dev/v7/` | **ARCHIVE** | Initial v7 role head (pre-fix); preserved as `v7_initial`; can move to archive |
| `adapters/checkpoints/software_dev/v7_initial/` | **ARCHIVE** | Already preserved; move to archive |
| `adapters/checkpoints/software_dev/v7.1/` | **ARCHIVE** | Shape_intent classifier failed at 0.26 held-out; role head work superseded by v7.2 |
| `adapters/checkpoints/software_dev/v7.1_frozen/` | **ARCHIVE** | Same |
| `adapters/checkpoints/software_dev/v7.2/` | **KEEP until v8 ships** | Current best ingest-side; 5 content heads still the v8 foundation. The shape_intent head lives in the manifest but is NOT consumed by dispatcher post-pivot |
| `adapters/checkpoints/clinical/` / `conversational/` / `swe_diff/` | **KEEP** | Other domains on v4/v6 — retrained in CTLG Phase 8 |

### 3.3 Deployed adapters

| Path | Disposition | Action |
|------|-------------|--------|
| `~/.ncms/adapters/software_dev/v7.1/` | **DEPRECATE on v8** | Works today; ships v8 when ready; then archive |
| `~/.ncms/adapters/<domain>/v6/` | **KEEP** | Running deployments remain valid |

### 3.4 Taxonomies

| Path | Disposition | Action |
|------|-------------|--------|
| `adapters/taxonomies/*.yaml` | **EXTEND** | Add `cue_labels` field alongside existing `topic_labels` / `admission_labels` / `state_change_labels`; no existing entries touched |

---

## 4. Schema + API changes

### 4.1 `ExtractedLabel` (cross-layer)

- **KEEP** all existing fields (intent, topic, admission, state_change, slots, role_spans, shape_intent, shape_intent_confidence)
- **ADD** `cue_tags: tuple[TaggedToken, ...]` — first-class CTLG output
- **REFRAME** `shape_intent: ShapeIntent | None` from a trained head's argmax to a `@property` computed from `cue_tags + synthesize()`. Returns the same Literal values when synthesizer hits; returns `"none"` when synthesizer abstains. Emits `DeprecationWarning` on property access starting v8.
- **RETIRE** `shape_intent` + `shape_intent_confidence` after one release cycle (v9). Callers migrate to `cue_tags`.

### 4.2 `AdapterManifest`

- **KEEP** `shape_intent_labels: list[str]` field (empty list on v8+ adapters; non-empty on v7.x legacy adapters)
- **ADD** `cue_labels: list[str]` — list of BIO cue labels
- **DISPATCH RULE**: if `cue_labels` non-empty → use CTLG inference path. Else if `shape_intent_labels` non-empty → use legacy classifier path (for v7.x adapters loaded into v8+ code). Else → abstain.

### 4.3 `LGTrace`

- **KEEP** `intent`, `confidence`, `grammar_answer`, `zones`
- **ADD** `trajectory: Trajectory | None`, `ranked_trajectories`, `heuristic_weights`, `production_trace` (see `ctlg-grammar.md` §7.1)

### 4.4 `EdgeType`

- **KEEP** all existing values: `DERIVED_FROM`, `SUPERSEDES`, `SUPERSEDED_BY`, `REFINES`, `CONFLICTS`, `SUPPORTS`
- **ADD** `CAUSED_BY`, `ENABLES`

### 4.5 `GoldExample` (corpus schema)

- **KEEP** all existing fields
- **ADD** `cue_tags: list[TaggedToken]` — when present, row is a CTLG cue-tagging training row

### 4.6 `NCMSConfig` (settings)

- **KEEP** `temporal_enabled`, `slm_enabled`
- **ADD** `tlg_llm_fallback_enabled` (default False), `tlg_llm_fallback_model`, `tlg_heuristic_weights_path`
- **ADD** `catalog_automerge_enabled` (default False), `catalog_automerge_confidence_threshold` (default 0.9)

---

## 5. Retired / deprecated items (explicit removal timeline)

| Item | Deprecated in | Removed in |
|------|---------------|------------|
| `ExtractedLabel.shape_intent` (direct trained argmax) | v8 | v9 |
| `ExtractedLabel.shape_intent_confidence` | v8 | v9 |
| `ShapeIntent` Literal type | v8 | v9 (replaced by implicit TLGQuery.relation) |
| `LoraJointModel.shape_intent_head` | v8 (replaced by `shape_cue_head`) | v9 |
| `AdapterManifest.shape_intent_labels` | v8 (set to `[]` on new trains) | kept indefinitely for legacy adapter compat |
| Hand-coded regex query parser (`ncms.domain.tlg.query_parser`) | already deleted in v6 | — |

---

## 6. Reframed (interface preserved, semantics changed)

These don't break callers but their internal behavior shifts:

### 6.1 `_dispatch_transitive_cause` (application/tlg/dispatch.py:404-448)

- **Before**: exhaustive predecessor DFS by timestamp
- **After**: CAUSED_BY edge traversal via `G_tr,c`; timestamp path used only as fallback when no causal edges exist for subject
- **Interface unchanged**: returns (memory_id, Confidence) as today

### 6.2 `_dispatch_concurrent / _dispatch_interval / _dispatch_range`

- **Before**: fixed 7-day window (`_CONCURRENT_WINDOW`)
- **After**: scenario-parameterized scope; if `TLGQuery.temporal_anchor` is set, use that interval; otherwise fall to current default
- **Interface unchanged**: same return type

### 6.3 Zone index construction (application/tlg/dispatch.py:70 `_load_subject_zones`)

- **Before**: inverts SUPERSEDES/REFINES edges to zone direction
- **After**: same, plus builds parallel CausalZone index from CAUSED_BY edges
- **Interface unchanged**: zones are loaded the same way; causal zones are an additive output

### 6.4 Retirement extractor (`domain/tlg/retirement_extractor.py`)

- **Before**: stem-matches retires_entities against edges
- **After**: same, plus augment with `h_explanatory` over any CAUSED_BY edges targeting the retirement node (provides causal justification for the retirement)
- **Interface unchanged**: returns retirement edge + (new) `causal_justification: Trajectory | None`

---

## 7. What this audit does NOT change

Explicitly preserved, not reframed, not extended:

- The **four-level confidence enum** (HIGH / MEDIUM / LOW / ABSTAIN / NONE)
- The **zero-confidently-wrong composition invariant** (§6 of CTLG grammar doc tightens it, doesn't loosen it)
- The **three-layer induction architecture** (L1 data / L2 data / L3 structural) from the P1 TLG paper
- The **13-intent target family** — new intents are additive; old intents keep their semantics
- The **feature-flag gating** under `NCMS_TEMPORAL_ENABLED` / `NCMS_SLM_ENABLED`
- The **catalog-first, gazetteer-first** retrieval pass on the ingest side (v7+ validated architecture)
- The **5 content heads** (intent / topic / admission / state_change / role) — v8 uses them unchanged
- The **zone definition** as refines-connected-component bounded by supersedes/retires (a second, parallel causal zone type is added; the original is untouched)

---

## 8. Physical cleanup checklist (for Phase 0 PR)

```bash
# Create archive subtree
mkdir -p adapters/_archive/pre_ctlg/{corpora,checkpoints,snapshots}

# Archive failed shape_intent corpora (keep a symlink at original path so
# that v7.x adapter loading doesn't break during the transition)
git mv adapters/corpora/gold_shape_intent_*.jsonl.pre_v7.2.bak \
       adapters/_archive/pre_ctlg/corpora/

# Archive failed/superseded checkpoints
git mv adapters/checkpoints/software_dev/v4 \
       adapters/_archive/pre_ctlg/checkpoints/
git mv adapters/checkpoints/software_dev/v6 \
       adapters/_archive/pre_ctlg/checkpoints/
git mv adapters/checkpoints/software_dev/v7 \
       adapters/_archive/pre_ctlg/checkpoints/
git mv adapters/checkpoints/software_dev/v7_initial \
       adapters/_archive/pre_ctlg/checkpoints/
git mv adapters/checkpoints/software_dev/v7.1_frozen \
       adapters/_archive/pre_ctlg/checkpoints/

# Archive: v7.1 stays where it is for NOW; move to archive on v8 ship.
# v7.2 stays active as the pre-pivot baseline until v8 lands.

# Snapshot pre-corrected SDG templates for historical reference
cp src/ncms/application/adapters/sdg/templates.py \
   adapters/_archive/pre_ctlg/snapshots/templates_pre_ctlg.py

# Snapshot the TLG dispatcher pre-reframe (in case we want to A/B)
cp src/ncms/application/tlg/dispatch.py \
   adapters/_archive/pre_ctlg/snapshots/dispatch_pre_ctlg.py

# Add a README to the archive explaining what's there and why
```

`adapters/_archive/pre_ctlg/README.md` contents:

> This directory contains adapter artifacts and code snapshots from the pre-CTLG era (through v7.2, 2026-04-23). Retained for:
>
> 1. **Historical comparison** — MSEB baselines were measured with these adapters; reproducing old numbers needs old code + old adapters.
> 2. **Compatibility** — the loader still supports these manifests, and operators running old deployments may upgrade incrementally.
> 3. **Research record** — the v6 BIO slot tagger, v7 role classifier, and v7.1/v7.2 shape_intent classifier are documented experiments. The shape_intent retrospective at `docs/completed/failed-experiments/shape-intent-classification.md` references these artifacts.
>
> Do NOT delete without a quorum decision. If space is needed, compress the checkpoint directories rather than removing them.

---

## 9. Testing commitments during migration

Because the pivot touches the dispatcher (central to `retrieve_lg`), we commit to:

1. **No existing test regresses.** Every test in `tests/integration/test_tlg_*.py` and `tests/architecture/test_*.py` must pass before and after the reframe.
2. **New tests land alongside new code.** Every new grammar production, every new heuristic, every new trajectory type has a unit test. Target: 100 new tests by Phase 7.
3. **Feature flag default OFF for one release.** `NCMS_TEMPORAL_ENABLED` remains off in v8.0; flipped on by default in v8.1 after operational validation.
4. **MSEB regression gate.** MSEB softwaredev r@1 ≥ current v7.2 baseline on the pre-CTLG run. A regression blocks the merge.

---

## 10. Change log

| Date | Change |
|------|--------|
| 2026-04-23 | Initial audit covering code / docs / corpora / checkpoints / schemas |
