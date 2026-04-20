# Intent-Slot Distillation — Sprint 4 integration findings

*Status: shipped · 2026-04-20 · companion to
[`docs/p2-plan.md`](p2-plan.md) and
[`docs/intent-slot-sprints-1-3.md`](intent-slot-sprints-1-3.md).*

---

## 1. Executive summary

Sprint 4 wired the LoRA multi-head classifier from sprints 1–3
fully into NCMS ingest.  Every gate-PASS adapter published at
`~/.ncms/adapters/<domain>/v4/` now **replaces** (not supplements)
the brittle regex paths on the hot path:

| Old path | Replacement | Kicks in when |
|---|---|---|
| `admission_service.score_admission` (4-feature regex heuristic, 65.9% accuracy on labeled set) | `admission_head` (softmax over persist/ephemeral/discard) | Confidence ≥ `intent_slot_confidence_threshold` (default 0.7) |
| `_has_state_declaration` (3 regex patterns — 8/8 false-positive rate in NemoClaw audit) | `state_change_head` (declaration/retirement/none) | Confidence ≥ threshold, same knob |
| Manual `Memory.domains: list[str]` tagging | `topic_head` → auto-populates `Memory.domains` from adapter taxonomy | `intent_slot_populate_domains=True` + topic confident |
| (Never shipped) regex preference extractor | `intent_head` + `slot_head` | Always, when flag is on |

All four replacements are **SLM-first, regex-fallback**:
confident SLM output wins; on abstain / low-confidence / flag-off,
the original regex / heuristic still runs.  The zero-confidently-
wrong invariant from TLG carries forward.

**8 integration tests green, 35 TLG+pipeline regression tests
green, 922 unit tests green, ruff clean.**

---

## 2. Architecture delivered

### 2.1 Where the classifier sits in the pipeline

Before Sprint 4 the SLM ran *inside* `run_inline_indexing`
alongside BM25 / SPLADE / GLiNER.  That was additive — results
landed in the DB but didn't actually gate any ingest decisions.
Sprint 4 moves extraction **before** admission so the classifier
outputs can replace the regex gates:

```
MemoryService.store_memory
    ↓
pre_admission_gates   (dedup + content classifier — kept, cheap)
    ↓
★ run_intent_slot_extraction  (NEW — returns ExtractedLabel)
    ↓
gate_admission         (admission_head wins when confident, else regex)
    ↓
save_memory            (columns + structured["intent_slot"] persisted)
    ↓
run_inline_indexing    (BM25 + SPLADE + GLiNER in parallel — no SLM now)
    ↓
create_memory_nodes    (state_change_head wins when confident, else regex)
    ↓
save_memory_slots      (BIO slot surface forms)
    ↓
event_log.intent_slot_extracted (dashboard)
```

### 2.2 Clean domain layer

Entire production surface lives under `src/ncms/` — no
dependency on the experiment package at runtime:

- `domain/protocols.py::IntentSlotExtractor` — the protocol
- `domain/models.py::ExtractedLabel` — the output shape (Pydantic, 5 heads + confidences + latency + method)
- `domain/intent_slot_taxonomy.py` — constants (INTENT_CATEGORIES, ADMISSION_DECISIONS, STATE_CHANGES, INTENT_LABEL_DESCRIPTIONS) + `build_slot_bio_labels()` helper

### 2.3 Infrastructure — 3-tier fallback chain

```
src/ncms/infrastructure/extraction/intent_slot/
├── adapter_loader.py      — AdapterManifest + verify_adapter_dir (fails loud)
├── lora_model.py          — LoraJointModel (nn.Module) + LoraJointBert (inference)
├── lora_adapter.py        — IntentSlotExtractor wrapper around LoraJointBert
├── e5_zero_shot.py        — cold-start intent-only fallback
├── heuristic_fallback.py  — always-available null-output (admission=persist)
├── factory.py             — build_extractor_chain + ChainedExtractor
└── __init__.py            — public API
```

GLiNER is **not** in the intent-slot chain — its slot extraction
was strictly worse than the LoRA BIO head on every trained
domain (see `intent-slot-sprints-1-3.md` §9.5).  GLiNER stays in
NCMS for entity NER (a separate pipeline feeding the knowledge
graph).

### 2.4 Schema v13

Added columns on `memories`:
- `intent`, `intent_confidence`
- `topic`, `topic_confidence`
- `admission_decision`, `state_change`, `intent_slot_method`

New tables:
- `memory_slots` — per-memory slot surface forms from BIO head
- `intent_slot_adapters` — per-deployment adapter registry

Plus indexes on `topic`, `state_change`, `memory_slots.slot_value`,
`memory_slots.slot_name`.

### 2.5 Training driver split

Training lives at `src/ncms/training/intent_slot/` (package
exists, training driver moves over in a follow-up commit — for
now the experiment's `train_adapter.py` remains the authoritative
entry point until we add `ncms train-adapter` CLI).

---

## 3. "Dynamic topics" design

The user's explicit ask: *"if we remove topics from config and
store dynamic in our table so our dashboard can still display
topics."*  Delivered end-to-end:

- **No global topic taxonomy** in the codebase.  Each adapter
  manifest carries its own `topic_labels` + object-to-topic map;
  swapping adapters swaps topics.
- **Topic persisted as a free-form string** in the `memories.
  topic` column — no foreign key to any enum table.  Dashboard
  reads topics via `SQLiteStore.list_topics_seen()` which does a
  `GROUP BY topic` aggregate on whatever has been ingested.
- **Per-deployment adapter** at `~/.ncms/adapters/<domain>/<ver>/`
  — operators train their own against their corpus.  The
  benchmark helper `benchmarks/intent_slot_adapter.py::
  get_intent_slot_chain(domain=...)` resolves the path
  automatically.

Validated by `test_dynamic_topic_enumeration_without_config`:
ingest 3 diverse-topic memories, assert `list_topics_seen()`
returns >= 2 distinct topics **without any config coupling**.

---

## 4. Benchmark integration

### 4.1 LongMemEval runner

`benchmarks/longmemeval/run_longmemeval.py` gains
`--intent-slot-domain`:

```bash
# Baseline (no SLM)
uv run python -m benchmarks longmemeval --features-on

# With conversational adapter (SLM on — admission / state /
# topic gates come from the classifier)
uv run python -m benchmarks longmemeval --features-on \
    --intent-slot-domain conversational

# Output routes to benchmarks/results/longmemeval/features_on/
#   conversational_slm/ so results don't collide with baseline
```

Inside the harness:
- Adapter loads ONCE at startup (shared across all questions —
  avoids BERT reload × 500 questions)
- Each question's fresh `MemoryService` receives the shared
  chain via `intent_slot=` kwarg
- `intent_slot_enabled=True` is set on the config when a domain
  is supplied

### 4.2 Benchmark helper API

`benchmarks/intent_slot_adapter.py`:

```python
from benchmarks.intent_slot_adapter import get_intent_slot_chain

chain = get_intent_slot_chain(
    domain="conversational",     # or software_dev / clinical / custom
    version=None,                 # None → newest version
    confidence_threshold=0.7,
    include_e5_fallback=False,    # deterministic for benchmarks
)
```

Automatic path resolution via `~/.ncms/adapters/<domain>/<version>/`
with fallback to the `NCMS_ADAPTER_ROOT` env var.

---

## 5. Test coverage

### 5.1 End-to-end scenarios (5/5 PASS)

`tests/integration/test_intent_slot_e2e.py`:

1. **store_with_conversational_adapter_populates_all_five_heads** — every head lands in the DB + `memory_slots` + dashboard event
2. **switch_adapter_changes_taxonomy_at_runtime** — two services, two adapters, two different topics
3. **dynamic_topic_enumeration_without_config** — `list_topics_seen()` reads from DB, no config coupling
4. **heuristic_fallback_when_no_adapter** — admission=persist, intent=none, topic=None, no crash
5. **adapter_listing_sees_published_v4** — benchmark helper resolves paths

### 5.2 Fitness tests (3/3 PASS)

`tests/integration/test_intent_slot_replaces_regex.py`:

1. **admission_routing_comes_from_slm_not_regex** — pipeline event's `route_source` reads `"intent_slot"` (not `"regex"`) when SLM confident
2. **topic_auto_populates_domains_without_caller_config** — caller omits `domains=` entirely; topic head fills it in
3. **no_l2_node_when_slm_says_no_state_change** — confident `state_change=none` skips L2 ENTITY_STATE creation

### 5.3 Regression

- **922 unit tests** green (bumped `test_schema_version.py` from v12 → v13)
- **35 sampled integration tests** green (memory_pipeline + tlg_memory_service + tlg_dispatch)
- **Ruff clean** across `src/ncms/`, `benchmarks/`, `tests/`

---

## 6. Performance observed

Smoke profile on Apple Silicon MPS (from E2E test logs):

| Stage | Latency |
|---|---|
| LoRA adapter load (once per service) | ~4–5 s |
| SLM forward pass (per memory, MPS) | ~20–250 ms depending on content length |
| Admission gate (when SLM confident) | ~1 ms overhead (uses cached label) |
| State-change head read (per memory) | <1 ms (dict lookup on `memory.structured`) |
| Full store_memory (with SLM) | ~200–600 ms |

Benchmark-scale measurement (LongMemEval 500 Q, shared adapter)
to follow once the A/B run completes — that's the first real
"what does SLM ingest buy us" data point.

---

## 7. Known sharp edges

1. **Adapter load time at service startup.**  `~4-5 s` on MPS
   for BERT + LoRA.  Acceptable for single-adapter deployments;
   if we ever support per-request adapter switching in
   production, this becomes a hot-reload problem (cache the
   adapter in-process, swap atomically).

2. **Cross-domain calls** — e.g. passing domain=`"legal"` to a
   conversational adapter.  The classifier still runs and
   produces *some* output; the adapter logs a debug message but
   doesn't refuse.  Operators should train an adapter per
   domain; cross-domain fallback is the zero-shot E5 tier.

3. **Admission features still computed even when SLM wins.**
   `gate_admission` runs the 4-feature heuristic unconditionally
   (it's cheap, ~1 ms) so the `admission_scored` dashboard event
   stays informative.  The routing decision uses SLM when
   confident, but observability never loses the feature signal.

4. **Topic → domains auto-populate is append-only.**  We add the
   classifier's topic to `Memory.domains`; we don't replace
   caller-supplied entries.  This is intentional — preserves
   operator-controlled routing tags — but means a miscalibrated
   adapter could pollute the domains list.  Mitigate with
   `intent_slot_populate_domains=False` during migration.

---

## 8. What ships with this PR

| Artifact | Location |
|---|---|
| Protocol + domain model | `src/ncms/domain/{protocols.py, models.py, intent_slot_taxonomy.py}` |
| Infrastructure backends | `src/ncms/infrastructure/extraction/intent_slot/` (7 files) |
| Schema v13 | `src/ncms/infrastructure/storage/migrations.py` |
| Store methods | `SQLiteStore.save_memory_slots`, `get_memory_slots`, `save_intent_slot_adapter`, `set_active_intent_slot_adapter`, `get_active_intent_slot_adapter`, `list_intent_slot_adapters`, `list_topics_seen` |
| Config flags | 6 `NCMS_INTENT_SLOT_*` entries in `src/ncms/config.py` |
| Ingestion integration | `IngestionPipeline.run_intent_slot_extraction`, gate-admission SLM wiring, L2 state-change SLM wiring (pipeline + `index_worker`) |
| MemoryService orchestration | `store_memory` calls SLM before admission, bakes label into memory.structured, emits dashboard event |
| Dashboard event | `EventLog.intent_slot_extracted` |
| Benchmark helper | `benchmarks/intent_slot_adapter.py` |
| LongMemEval integration | `--intent-slot-domain` flag + shared-extractor wiring |
| E2E tests | `tests/integration/test_intent_slot_e2e.py` (5 tests) |
| Fitness tests | `tests/integration/test_intent_slot_replaces_regex.py` (3 tests) |
| Schema version test | `tests/unit/infrastructure/storage/test_schema_version.py` (renamed from `_v12.py`) |
| Published adapters | `~/.ncms/adapters/{conversational,software_dev,clinical}/v4/` (2.4 MB each) |

---

## 9. Follow-ups (nice-to-have, not P2 blockers)

- **`ncms train-adapter` CLI** — thin wrapper around the
  experiment's `train_adapter.py` so operators don't leave NCMS
  to retrain.
- **`ncms adapter-{list,promote,show}`** CLIs — ops inspection
  + the `active=1` flip.
- **Drift detection** — dashboard watches per-head confidence
  distributions, warns when OOD content pushes mean confidence
  below a threshold.
- **Generic-domain adapter** — the "tier 2" in the original
  3-tier pre-paper proposal; would train on a broad mixed corpus
  and serve as fallback for cross-domain calls.
- **ALTER TABLE upgrade path** for existing v12 databases (not
  needed today — fresh DB for benchmarks).

---

## 10. LongMemEval A/B result (2026-04-20)

Ran `--features-on` with and without `--intent-slot-domain
conversational` on the full 500-question LongMemEval benchmark.
Result: **bit-identical recall, ~5 % latency overhead** — the
axis-mismatch thesis confirmed in production.

| | Baseline | SLM (conversational) | Δ |
|---|---:|---:|---:|
| Recall@5 | 0.4680 | 0.4680 | **0.0000** |
| Contains | 0.4680 | 0.4680 | 0.0000 |
| F1 | 0.0130 | 0.0130 | 0.0000 |
| Questions | 500 | 500 | — |
| Total memories | 10,960 | 10,960 | — |
| Elapsed | 10,562 s | 11,099 s | +537 s |
| Per-memory SLM overhead | — | — | ~48 ms avg |

Per-category breakdown: **bit-identical** across all six
(`knowledge-update 0.7436`, `single-session-user 0.8429`,
`single-session-assistant 0.6429`, `multi-session 0.3308`,
`temporal-reasoning 0.2782`, `single-session-preference
0.0000`).  Adapter verified once at startup, zero extraction
failures, zero tracebacks, zero HTTP 401/403 across both runs.

### Why bit-identical?

The result is what the pre-paper and `docs/p2-plan.md` §10
predicted:

* **admission head** — LongMemEval turns are overwhelmingly
  persist-worthy conversational content.  SLM and regex
  agree; no diff in admitted memories.
* **state-change head** — Conversational content has near-
  zero state declarations or retirements.  Both return
  `state_change="none"`.  No L2 ENTITY_STATE nodes build
  either way.
* **topic head auto-populate** — Tags land in
  `Memory.domains`, but LongMemEval's retrieval uses BM25 +
  SPLADE + graph on the `content` field.  Domain tags don't
  shift ranking on these queries.
* **intent / slot heads** — Emit per-memory preference
  metadata that ends up in `memories.intent` and
  `memory_slots`, but the retrieval pipeline doesn't consume
  those columns yet on LongMemEval's retrieval path.

### What this confirms

1. **No regression.**  The SLM does not break any category.
2. **Latency is in budget.**  48 ms/memory × ~22 memories per
   question ≈ 1 s/question overhead.  Well within
   `NCMS_INTENT_SLOT_LATENCY_BUDGET_MS=200` per-memory soft
   limit.
3. **Adapter loads once + survives.**  10,960 classifier
   forward passes without a failure; MPS stayed stable; no
   re-load churn between questions.
4. **Pipeline plumbing is sound.**  `.env` auto-load works,
   `--intent-slot-domain` routes to the right
   `~/.ncms/adapters/conversational/v4/`, output goes to the
   right sub-directory, schema v13 columns populate.

### Artifacts

* `benchmarks/results/longmemeval/features_on/longmemeval_20260420T150644Z.{md,json}`
  — baseline
* `benchmarks/results/longmemeval/features_on/conversational_slm/longmemeval_20260420T152653Z.{md,json}`
  — SLM
* `benchmarks/run-logs/baseline-20260420T061004.log` +
  `benchmarks/run-logs/slm-conversational-20260420T062152.log`
  — durable full-logs

### Next benchmark

The interesting axis is state-evolution retrieval, which
LongMemEval doesn't measure.  Candidates in priority order:

1. **SWE-bench Django** (`benchmarks/swebench/`) — existing
   retrieval benchmark with AR / TTL / LRU / CR metrics.
   Covers software-dev content with more declaration /
   retirement / supersession patterns than LongMemEval.
   Published baselines already comparable with Mem0 + Letta
   (NCMS wins 3 of 4).  Wiring `--intent-slot-domain
   software_dev` through its 9 `MemoryService` instantiation
   sites is the natural next A/B.
2. **MemoryAgentBench** (`benchmarks/memoryagentbench/`) —
   existing harness with AR / TTL / LRU / selective-
   forgetting axes.  TTL and selective-forgetting should
   exercise the admission head more than LongMemEval does.
3. **P3 SWE state-evolution corpus**
   ([`docs/p3-swe-state-benchmark.md`](p3-swe-state-benchmark.md))
   — purpose-built state-change corpus, ~2-week build.  This
   is the headline benchmark where both TLG and the SLM
   should land real wins; deferred until SWE-bench Django +
   MAB A/B data is in hand.
