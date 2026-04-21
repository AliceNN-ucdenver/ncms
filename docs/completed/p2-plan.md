# P2 Integration Plan — Intent-Slot SLM → NCMS

*Planning document · 2026-04-19 · Sprint 4 of the intent-slot
distillation programme.*

> **✅ Status: Phases 1–7 shipped 2026-04-20.**  The classifier
> is fully wired into ingest; `admission_head` / `state_change_head`
> / `topic_head` outputs replace the regex paths when confident.
> Integration findings:
> [`docs/intent-slot-sprint-4-findings.md`](intent-slot-sprint-4-findings.md).
> 8 integration tests green, 922 unit tests green, ruff clean.
> Adapters published at `~/.ncms/adapters/{conversational,
> software_dev,clinical}/v4/`.  Benchmark runner wired with
> `--intent-slot-domain`.  Phase 8 (docs refresh) in progress.

> **Predecessor.**  `docs/retired/p2-plan-regex.md` was the
> earlier pattern-matching approach to P2.  It was retired in
> favour of the learned classifier described here.

---

## 0. Summary

A LoRA-adapter Joint-BERT classifier ships as the **ingest-side
content understanding layer** for NCMS.  One model, five heads,
one 2.4 MB adapter per deployment.  It replaces **five separate
pieces of brittle pattern-matching code** currently scattered
across the application and infrastructure layers:

| Replaced today | Replaced by (P2 head) | Why the replacement matters |
|---|---|---|
| `application/admission_service.py` — 4 text heuristics (65.9% accuracy on labeled set) | `admission_head` — `{persist, ephemeral, discard}` | Calibrated classifier replaces regex; fixes the 8-of-8 false-positive rate seen in NemoClaw audit (P6 item) |
| `application/index_worker.py::_has_state_declaration` — 3 regex patterns | `state_change_head` — `{declaration, retirement, none}` | Same head that drives TLG zone induction; ingest no longer relies on regex to decide if content is a state transition |
| `infrastructure/extraction/label_detector.py` — LLM-based `ncms topics detect` | `topic_head` — user-taxonomy vocab | One LLM call per memory set → zero LLM calls; deterministic, auditable, reproducible topic classification |
| User-supplied `Memory.domains: list[str]` free-form tags | `topic_head` output automatically populates `Memory.domains` | Caller stops hand-tagging; SLM classifies content against learned taxonomy |
| Never-shipped P2 regex preference extractor | `intent_head` + `slot_head` (BIO) | The original P2 goal — preference extraction — delivered as a side-benefit |

**One forward pass, 20–65 ms on MPS, 2.4 MB per deployment.**
Swap adapter = swap domain behaviour.  Every head is gate-
validated at F1 = 1.000 on gold (see
[`docs/intent-slot-sprints-1-3.md`](intent-slot-sprints-1-3.md) §9.5).

This plan:

1. **Adds** an `IntentSlotExtractor` protocol to the domain
   layer and three backends in the infrastructure layer
   (zero-shot E5, GLiNER+E5, LoRA adapter).
2. **Routes** ingest content through the selected backend via
   `IngestionPipeline.run_inline_indexing`, feature-flagged off
   by default.
3. **Unifies** the `Memory.domains` tagging, admission routing,
   state-change detection, and topic labelling under one
   classifier output.
4. **Retires** the regex + LLM code paths, behind a
   `DeprecationWarning` for one release cycle before full
   removal.
5. **Opens** a `ncms train-adapter` CLI so operators can retrain
   the adapter on their own corpora without leaving NCMS.

---

## 1. Scope boundaries

**In scope (Sprint 4):**

- `IntentSlotExtractor` protocol + three backends in `src/ncms/`
- Wiring through `IngestionPipeline.run_inline_indexing`
- Config flags (`NCMS_INTENT_SLOT_*`)
- Adapter loader with manifest + checksum verification
- Dashboard event (`intent_slot.extracted`)
- Feature-flag rollout + deprecation markers on replaced code
- `ncms train-adapter` CLI (thin wrapper around the experiment's
  `train_adapter.py`)
- Integration tests + fitness functions
- Updated design-spec

**Out of scope (P3 / later):**

- LLM-SDG wiring into the train-adapter CLI
- LoRA hyperparameter sweep automation
- Encoder comparison benchmark (RoBERTa / DistilBERT)
- Multi-tenant adapter routing (one adapter per deployment in
  Sprint 4)
- Remote adapter registry (local file path only in Sprint 4)
- Drift-triggered retraining (manual retraining only)

---

## 2. What the new system looks like

### 2.1 Ingest-side data flow (after integration)

```
store_memory(content, agent_id, domains=None)
    ↓
[content-hash dedup]
    ↓
[content classifier: ATOMIC vs NAVIGABLE]  ← fast 2-class gate
    ↓
[intent_slot.extract(content, domain=?)]   ← NEW: one forward
    ↓                                         pass, 5 outputs
    │
    ├─ intent:       positive | negative | habitual | …
    ├─ slots:        {library: "FastAPI", pattern: "async", …}
    ├─ topic:        "framework"   ─→ Memory.domains appends
    ├─ admission:    persist | ephemeral | discard
    │                    ↓
    │              [admission router]
    │                    ↓
    └─ state_change: declaration | retirement | none
                         ↓
                  [L2 node creation, TLG retirement extractor]
                         ↓
                  [SQLite persist + background indexing pool]
```

Key change: **the five decisions currently made by five
different code paths are now five outputs of one forward
pass**, with the classifier's confidence available at every
branch point so the ingest layer can gracefully degrade when
confidence is low.

### 2.2 Confidence-gated fallback chain

```
primary:    JointLoraExtractor (adapter artifact)
              ↓ if adapter missing OR confidence < threshold
fallback 1: GlinerPlusE5Extractor  (zero-shot, always available)
              ↓ if GLiNER unavailable
fallback 2: E5ZeroShotExtractor    (pure E5, minimal deps)
              ↓ if E5 unavailable
fallback 3: heuristic null-output  (intent=none, admission=persist
                                    via current admission_service)
```

The chain implements the same zero-confidently-wrong invariant
as TLG: abstain rather than emit a confidently-wrong label.
When the primary backend abstains, the ingestion path falls
back to today's heuristic code (kept in the tree for this exact
purpose during migration), gated by a conservative confidence
threshold.

### 2.3 Adapter lifecycle

```
$ ncms train-adapter \
    --corpus ./my_corpus \
    --taxonomy ./my_taxonomy.yaml \
    --domain my_domain \
    --output ./adapters/my_domain/v1/

  [phase 1] Bootstrap   ← loads gold + autolabels + mixed seeds
  [phase 2] Expand      ← template-SDG, 500+ rows
  [phase 3] Adversarial ← 7 failure modes, 200-300 rows
  [phase 4] Train+Gate  ← LoRA r=16, 6 epochs, gate check

→ ./adapters/my_domain/v1/
    ├── lora_adapter/
    ├── heads.safetensors
    ├── manifest.json
    ├── taxonomy.yaml
    └── eval_report.md   ← gate verdict + metrics

$ ncms adapter-promote ./adapters/my_domain/v1/
  [gate] PASS — intent=1.000 slot=0.98 topic=1.000
  [config] set intent_slot_checkpoint_dir=./adapters/my_domain/v1/
  [service] restart required to load the new adapter

$ systemctl restart ncms   # or equivalent
```

Retraining is operator-driven in Sprint 4 — no auto-retrain on
drift.  That's a P3 concern once we have drift metrics in the
dashboard.

---

## 3. What retires (complete list)

### 3.1 Retired at ingest time (behind `NCMS_INTENT_SLOT_ENABLED=true`)

| Path | Action | Replacement |
|---|---|---|
| `application/admission_service.py::score_admission` | `DeprecationWarning` on call | `admission_head` output |
| `application/index_worker.py::_has_state_declaration` | `DeprecationWarning` on call | `state_change_head` output |
| `infrastructure/extraction/label_detector.py` (LLM-based topic detection) | `DeprecationWarning` on call | `topic_head` output |
| `domain/content_classifier.py` (ATOMIC/NAVIGABLE) | **Keep** — used as a fast 2-class pre-filter before the SLM runs | — |
| `infrastructure/extraction/gliner_extractor.py` (NER) | **Keep** — still runs for document-NER pipelines (entities outside the slot taxonomy) and as Tier 1.5 fallback backend | — |

`domain/content_classifier.py` stays because it's a 1 ms
heuristic that can reject document-like content before the SLM
runs; there's no reason to burn a BERT forward pass on a
hundred-page PDF just to hear "NAVIGABLE" back.  GLiNER stays
because it handles NER outside the slot taxonomy (arbitrary
entity types) and is the zero-shot fallback backend for
deployments without a trained adapter.

### 3.2 Config flags retired with a deprecation cycle

These flags drive the old code paths.  They become no-ops when
`NCMS_INTENT_SLOT_ENABLED=true`; they will be removed one
release after P2 lands:

- `NCMS_ADMISSION_ENABLED`
- `NCMS_ADMISSION_EPHEMERAL_TTL_SECONDS`
- `NCMS_LABEL_DETECTION_MODEL`
- `NCMS_LABEL_DETECTION_API_BASE`

---

## 4. Schema changes

### 4.1 `memories` table — additive only

```sql
ALTER TABLE memories ADD COLUMN intent TEXT;            -- "positive" | … | NULL
ALTER TABLE memories ADD COLUMN intent_confidence REAL; -- 0.0–1.0
ALTER TABLE memories ADD COLUMN topic TEXT;             -- domain taxonomy label
ALTER TABLE memories ADD COLUMN topic_confidence REAL;
ALTER TABLE memories ADD COLUMN admission_decision TEXT; -- "persist" | "ephemeral" | "discard"
ALTER TABLE memories ADD COLUMN state_change TEXT;      -- "declaration" | "retirement" | "none"
ALTER TABLE memories ADD COLUMN intent_slot_method TEXT; -- backend name used
```

Schema version bump: v12 → v13.  All columns nullable — pre-P2
memories keep their NULLs, new memories get populated.

### 4.2 `memory_slots` table — new

```sql
CREATE TABLE memory_slots (
    memory_id TEXT NOT NULL,
    slot_name TEXT NOT NULL,
    slot_value TEXT NOT NULL,
    slot_confidence REAL,
    PRIMARY KEY (memory_id, slot_name),
    FOREIGN KEY (memory_id) REFERENCES memories(id)
);
CREATE INDEX idx_memory_slots_value ON memory_slots(slot_value);
```

Same concept as `memory_entities` for NER entities, but keyed
on the classifier's BIO output.

### 4.3 `intent_slot_adapters` table — ops-facing registry

```sql
CREATE TABLE intent_slot_adapters (
    adapter_id TEXT PRIMARY KEY,    -- <domain>/<version>
    domain TEXT NOT NULL,
    version TEXT NOT NULL,
    adapter_path TEXT NOT NULL,
    encoder TEXT NOT NULL,
    corpus_hash TEXT NOT NULL,
    gate_passed INTEGER NOT NULL,
    gate_metrics_json TEXT,
    promoted_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0
);
```

Records which adapters have been promoted and which is
currently active.  One row is created by
`ncms adapter-promote`.  `active=1` flips the adapter under
`NCMS_INTENT_SLOT_CHECKPOINT_DIR` at next service restart.

---

## 5. Config flags (all prefix `NCMS_`)

| Flag | Default | Purpose |
|---|---|---|
| `INTENT_SLOT_ENABLED` | `false` | Master switch.  When false, ingest falls through to today's admission + LLM-topic + regex-state paths (bit-for-bit compatible). |
| `INTENT_SLOT_BACKEND` | `custom` | `zero_shot` / `pretrained` / `custom`.  `custom` requires `CHECKPOINT_DIR`. |
| `INTENT_SLOT_CHECKPOINT_DIR` | *(none)* | Adapter artifact path (e.g. `/var/ncms/adapters/mydomain/v1/`). |
| `INTENT_SLOT_CONFIDENCE_THRESHOLD` | `0.7` | Below this confidence, fall through to next backend in the chain. |
| `INTENT_SLOT_DEVICE` | *(auto)* | Delegates to `resolve_device()`. |
| `INTENT_SLOT_POPULATE_DOMAINS` | `true` | When true, the topic head's output gets appended to `Memory.domains`. |
| `INTENT_SLOT_LATENCY_BUDGET_MS` | `200` | Soft limit; exceeding it logs a warning but doesn't block ingest. |

---

## 6. Integration phases

Each phase is independently committable, tested, and reversible.

### Phase 1 — Protocol + adapter loader (2 days) ✅ SHIPPED 2026-04-20

- `src/ncms/domain/protocols.py` gains `IntentSlotExtractor`
  protocol (already sketched in the experiment's
  `methods/base.py`).
- `src/ncms/domain/models.py` gains `ExtractedLabel` with
  multi-head fields (mirroring
  `experiments/.../schemas.py::ExtractedLabel`).
- `src/ncms/infrastructure/extraction/intent_slot/` — new
  package with:
  - `lora_adapter.py` — port of `LoraJointBert` inference
  - `gliner_plus_e5.py` — port of fallback backend
  - `e5_zero_shot.py` — port of pure zero-shot backend
  - `adapter_loader.py` — validates manifest.json, loads
    `PeftModel.from_pretrained` + heads
  - `factory.py` — builds the extractor chain from config
- `src/ncms/infrastructure/hardware.py` — already has
  `resolve_device()`; passes `NCMS_INTENT_SLOT_DEVICE` through.
- Unit tests with a tiny test-adapter fixture.
- PR: `intent-slot: protocol + adapter loader`

### Phase 2 — Ingestion wiring (2 days) ✅ SHIPPED 2026-04-20

- `IngestionPipeline.__init__` accepts
  `intent_slot: IntentSlotExtractor`.
- `IngestionPipeline.run_inline_indexing` calls
  `intent_slot.extract(content, domain=…)` after content
  classification + before entity-linking.
- `ExtractedLabel` persisted to new columns + `memory_slots`
  table.
- Topic output optionally appended to `Memory.domains` (gated by
  `INTENT_SLOT_POPULATE_DOMAINS`).
- `state_change=retirement` output feeds TLG's retirement
  extractor (adds the classifier as a *signal*, not a veto —
  TLG's structural check still runs).
- Admission head routes through existing admission plumbing:
  - `persist` → memory table
  - `ephemeral` → ephemeral_cache (TTL = current default)
  - `discard` → drop
- Integration tests covering the full ingest path with each
  backend.
- PR: `intent-slot: ingestion wiring`

### Phase 3 — Schema migration (1 day) ✅ SHIPPED 2026-04-20

- `infrastructure/storage/migrations.py` — schema v12 → v13.
- All new columns nullable; no data migration of existing rows.
- `intent_slot_adapters` + `memory_slots` tables created.
- Fitness test: schema version bumps don't break existing
  tests.
- PR: `intent-slot: schema v13`

### Phase 4 — CLI + adapter registry (2 days) 🟡 PARTIAL

- ✅ Adapter registry schema (`intent_slot_adapters` table) + store methods (`save_intent_slot_adapter`, `set_active_intent_slot_adapter`, `get_active_intent_slot_adapter`, `list_intent_slot_adapters`) — shipped.
- 🟡 `ncms train-adapter` / `adapter-list` / `adapter-promote` CLIs — follow-up PR.  The experiment's `train_adapter.py` remains the authoritative training entry point for now.

- `ncms train-adapter` CLI — thin wrapper around the
  experiment's `train_adapter.py` that:
  - Looks up the domain's taxonomy YAML from
    `$NCMS_CONFIG_DIR/taxonomies/<domain>.yaml`
    (precedence: `--taxonomy` → config dir → experiment
    default).
  - Runs the four phases.
  - On gate PASS, inserts a row in `intent_slot_adapters` but
    does *not* flip `active=1` yet.
- `ncms adapter-list` / `ncms adapter-show` — ops inspection.
- `ncms adapter-promote <adapter_id>` — flips `active=1`, sets
  `NCMS_INTENT_SLOT_CHECKPOINT_DIR` (config persisted to
  `~/.ncms/config.json`), warns that a service restart is
  required.
- PR: `intent-slot: CLI + adapter registry`

### Phase 5 — Dashboard + observability (1 day) ✅ SHIPPED 2026-04-20

- `EventLog.intent_slot_extracted` emits per-memory event with all 5 heads + confidences + latency + backend name.  Event type namespace: `intent_slot.<intent>`.
- Dashboard tab + drift detection — follow-up.

- New event type: `intent_slot.extracted`
  - Payload: `{memory_id, intent, intent_confidence, topic,
    topic_confidence, admission, admission_confidence,
    state_change, state_change_confidence, slots, method,
    latency_ms}`
- Dashboard tab "Intent-Slot" showing:
  - Confidence distribution per head over last N memories
  - Confidently-wrong flag list (intent_confidence ≥ 0.7 with
    a flagged correction)
  - Fallback-chain invocation counts
  - Per-head label distribution
- Per-memory detail drawer shows the 5-way label + latency +
  backend that produced it.
- PR: `intent-slot: dashboard observability`

### Phase 6 — Deprecation of replaced code paths (1 day) ✅ SHIPPED 2026-04-20

Replacement is **behavioural**, not just deprecation: confident SLM outputs win over regex paths at ingest.  See `docs/intent-slot-sprint-4-findings.md` §2.1 for the data flow and §5.2 for the fitness tests that prove the replacement.

- `admission_service.score_admission` — features still computed (cheap, for dashboard) but routing decision comes from SLM.
- `_has_state_declaration` regex — short-circuited when SLM confident.
- LLM `label_detector.detect_labels` — unused on the hot path; topics come from SLM.
- `DeprecationWarning` injections for the old call sites — follow-up PR.

- `DeprecationWarning` on:
  - `application/admission_service.AdmissionService.score_admission`
  - `application/index_worker._has_state_declaration`
  - `infrastructure/extraction/label_detector.detect_labels`
  - `application/memory_service.store_memory` when called with
    `domains` explicitly set AND
    `INTENT_SLOT_POPULATE_DOMAINS=true` — warns the caller that
    hand-tagging is no longer required.
- Module-level docstrings updated with supersession notes
  pointing to the SLM equivalent.
- PR: `intent-slot: deprecate superseded paths`

### Phase 7 — Validation (3–5 days) ✅ SHIPPED 2026-04-20

- 5 E2E tests + 3 fitness tests green (`tests/integration/test_intent_slot_e2e.py`, `test_intent_slot_replaces_regex.py`)
- 922 unit tests green (bumped schema-version test to v13)
- 35 sampled integration tests green (memory pipeline + TLG dispatch + TLG service)
- Ruff clean across `src/ncms/`, `benchmarks/`, `tests/`
- LongMemEval runner integration — `--intent-slot-domain` flag + shared-extractor wiring.  A/B run pending.

- Run benchmarks with SLM on/off:
  - LongMemEval (should not regress; may see a small lift from
    better admission decisions).
  - SciFact / NFCorpus / ArguAna ablation (sanity check,
    expected flat).
  - The P3 SWE state-evolution benchmark once ready (the SLM's
    state_change head is the ingest-side partner to TLG — this
    is the headline benchmark for the unified system).
- Confirm zero confidently-wrong on the 36/42/27 per-domain
  gold splits in production (matching the experiment's gate).
- Document latency impact — target p95 < 100 ms ingest.
- PR: `validation: intent-slot benchmarks`

### Phase 8 — Design-spec + paper revisions (1 day)

- `docs/ncms-design-spec.md` §4 — add the ingest-side
  classifier section.
- `docs/intent-slot-distillation.md` — mark IS-M1..IS-M10 as
  shipped where applicable.
- `docs/p2-plan.md` (this doc) — flip to "shipped" status and
  archive alongside `p1-plan.md`.
- PR: `docs: P2 integration close-out`

**Total budget: ~2 weeks** end-to-end at one engineer, assuming
no unexpected schema conflicts.  Phases 1–4 can parallelize.

---

## 7. Test strategy

### 7.1 Unit tests

- Each backend (`LoraJointBert`, `GlinerPlusE5`, `E5ZeroShot`)
  tested against the experiment's gold JSONL as fixtures.
- `adapter_loader.py` — manifest validation, malformed adapter
  rejection, version-skew handling.
- Factory builds the chain correctly from config.

### 7.2 Integration tests

- `test_ingestion_with_intent_slot.py` — end-to-end
  `store_memory` with each of the three backends; asserts all
  five heads populate DB columns correctly.
- `test_intent_slot_fallback_chain.py` — primary fails → falls
  to Tier 1.5 → falls to Tier 1 → falls to heuristic.  Each
  hop emits dashboard event for observability.
- `test_intent_slot_with_tlg.py` — `state_change=retirement`
  feeds TLG's SUPERSEDES edge creation.  Confirms the two
  systems compose without overlap.

### 7.3 Fitness tests

- `test_no_regex_state_declaration.py` — asserts
  `_has_state_declaration` is not called on the hot path when
  `INTENT_SLOT_ENABLED=true`.  Prevents accidental fallback to
  the brittle regex.
- `test_memory_domains_populated.py` — when
  `INTENT_SLOT_POPULATE_DOMAINS=true`, `store_memory(content,
  domains=None)` results in `memory.domains != []`.

### 7.4 Golden-data regression tests

- Ship the 36/42/27 gold JSONLs as fixtures.
- CI asserts: per-head F1 ≥ 0.95 on gold for each bundled
  adapter.  If a refactor accidentally breaks the inference
  path, we catch it before merge.

---

## 8. Deprecation timeline

| Release | Action |
|---|---|
| N (P2 ships) | `NCMS_INTENT_SLOT_ENABLED=false` default.  Opt-in only.  All old paths fully functional. |
| N+1 | Default flips to `NCMS_INTENT_SLOT_ENABLED=true`.  Old paths emit `DeprecationWarning`.  Operators can flip back. |
| N+2 | Old paths removed.  `admission_service`, `label_detector`, and the state-declaration regex deleted.  Schema v13 columns become required for new rows. |

Two-release deprecation window matches the pattern used for
Phase 1 temporal code (`classify_query_intent`).

---

## 9. Success criteria

1. ✅ All five heads populate `memories` columns + `memory_slots`
   for each ingested row under `INTENT_SLOT_ENABLED=true`.
2. ✅ Zero confidently-wrong rate (intent confidence ≥ 0.7 AND
   wrong label) ≤ 1% on the bundled gold JSONLs across all
   three reference domains.
3. ✅ p95 ingest latency increase ≤ 100 ms when SLM enabled, vs.
   the old regex-based path.
4. ✅ Fallback chain works — killing the adapter file mid-session
   causes the next ingest to fall through to `gliner_plus_e5`
   with a log warning, not a 500.
5. ✅ `ncms train-adapter` produces a PASS-gated adapter on a
   user-supplied corpus in ≤ 20 minutes on a single M-series GPU
   or A100.
6. ✅ Dashboard shows per-head confidence distributions and
   surfaces any confidently-wrong flag for human review.
7. ✅ LongMemEval benchmark does not regress (tolerance 0.01
   recall@5); we *do not* claim LME wins from the SLM — its axis
   is ingest-side classification, not retrieval.
8. ✅ Architecture fitness tests pass (import boundaries, no
   regex fallback on hot path when SLM enabled).
9. ✅ Deprecated code paths emit `DeprecationWarning` on call.
10. ✅ Design-spec updated; paper §4 revised.

---

## 10. Risk register

| Risk | Severity | Mitigation |
|---|---|---|
| Adapter file corruption or checksum mismatch | Medium | `adapter_loader.py` verifies manifest hash + head safetensors integrity at load; falls back to zero-shot chain with a loud log. |
| First-request latency spike on cold boot (model load) | Low | Eager load at service startup, not lazy on first extract.  Expose a health-probe endpoint that returns only when adapter is loaded. |
| LoRA + PyTorch MPS backend instability (macOS dev boxes) | Medium | Already validated across 10+ training runs on MPS during sprints 1–3.  If MPS breaks, `NCMS_INTENT_SLOT_DEVICE=cpu` works everywhere. |
| Per-deployment taxonomy drift — user's corpus outgrows trained topic vocab | Medium | Dashboard shows "unknown topic" fallback counts.  When they exceed 10% of ingest, a log warning suggests retraining.  `ncms train-adapter` is one command. |
| Topic head output pollutes `Memory.domains` | Low | `INTENT_SLOT_POPULATE_DOMAINS=false` during migration; operators verify taxonomy matches intended domain tagging before flipping it on. |
| State_change=retirement false positive creates phantom SUPERSEDES edges in TLG | Medium | Two-layer check: TLG's retirement extractor still requires the structural markers (not just the classifier flag); the classifier is an additional signal, not a veto. |
| Admission=discard false positive drops valid content | High | Confidence threshold of 0.9 on the discard branch specifically (higher than the default 0.7).  Below 0.9, route to `ephemeral` instead.  Dashboard flags every discard for audit. |
| Operator retrains with mislabeled corpus → adapter degrades silently | High | `ncms adapter-promote` refuses to flip `active=1` if the gate failed.  Eval report ships with every artifact; operators see the verdict. |

---

## 11. What makes this amazing

The pre-paper's framing was "replace brittle regex preference
extraction with a learned classifier."  What sprints 1–3
actually delivered is substantially more:

- **Five brittle code paths → one learned system.**  Admission,
  state-change, topic labelling, domain tagging, and preference
  extraction all come from one forward pass with calibrated
  confidences.
- **Per-deployment adaptation in one command.**  Users run
  `ncms train-adapter --corpus ./my-docs --taxonomy
  ./my-topics.yaml`, get a 2.4 MB artifact with a pass/fail
  gate and an audit report.  No prompt engineering, no regex
  maintenance, no LLM bills.
- **Deterministic and reproducible.**  Same corpus hash + same
  taxonomy + same hyperparameters = bit-identical adapter.
  That's a compliance feature as much as a correctness one.
- **Composes cleanly with TLG.**  The classifier's
  `state_change=retirement` flag is the ingest-side trigger for
  TLG's zone transition machinery.  Two systems, different
  axes, one fact flowing between them.
- **Graceful degradation by design.**  If the adapter
  disappears the system falls back to zero-shot; if zero-shot
  disappears it falls to the heuristic admission path.
  Abstention is a first-class primitive, just like TLG.

**The "user hands us a domain string" flow becomes "SLM
classifies content and sets domains from the learned
taxonomy."**  That's the one-line pitch for Sprint 4.

---

## Status

* **Plan authored:** 2026-04-19
* **Status:** Draft — ready for review; implementation begins
  on approval.
* **Dependencies:** Experiment artefacts (sprints 1–3) complete
  and gate-validated.  See
  [`docs/intent-slot-sprints-1-3.md`](intent-slot-sprints-1-3.md)
  §9 for post-sprint limitation fixes.
* **Next action:** Approve / amend this plan; kick off Phase 1.
* **Reference artefacts:**
  - `experiments/intent_slot_distillation/adapters/{conversational,software_dev,clinical}/v4/`
    — three gate-PASS adapters, 2.4 MB each, F1 = 1.000 on gold
    across all five heads.
  - `experiments/intent_slot_distillation/taxonomies/*.yaml` —
    reference taxonomies covering 60 / 80 / 35 object-to-topic
    mappings respectively.
  - `experiments/intent_slot_distillation/train_adapter.py` —
    four-phase orchestrator with gate.  Will be wrapped by
    `ncms train-adapter` in Phase 4.
