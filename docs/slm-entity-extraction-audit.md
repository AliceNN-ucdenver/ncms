# Option D' Correctness Audit — softwaredev mini

Status: **in-progress**, commit `78b9fc9` under forensic review, NOT ready for full-12.
Inputs: 3 audit scripts at `benchmarks/audit_*`, outputs at `benchmarks/results/audit/`.

All numbers below are measured, not estimated.

---

## TL;DR

My commit `78b9fc9` regresses softwaredev r@1 from **0.745 → 0.230** (−51.5pp absolute, −69% relative) with **zero compensating gains**. The root cause is Part 4 over-creating L2 ENTITY_STATE nodes: one per memory instead of one per true state transition.

Parts 1 (UUID→name) and 2 (SLM slot head) are semantically correct in isolation. Part 4's design flaw is **conflating "caller says memory is ABOUT entity X" with "memory IS a state of entity X"**. For multi-section ADR content, those are different things, and treating every section as a state floods the graph.

**Recommendation: revert Part 4's unconditional L2 creation, keep Parts 1+2, re-measure.**

---

## Evidence

### Regression is real, large, and uniform

From `benchmarks/audit_predictions_diff.py` comparing old `tlg-on_20260421T123020Z` vs new `temporal-on_20260422T134805Z` on identical softwaredev mini queries:

```
qids: old=165 new=165 common=165

R@1 transition matrix:
  old=hit_new=hit                  37
  old=hit_new=miss                 86
  old=miss_new=hit                  0   <-- zero compensating gains
  old=miss_new=miss                42
```

Per-shape breakdown (new r@1):

| Shape | Before my commit (r@1) | After my commit (r@1) | Delta |
|---|---|---|---|
| retirement | 8/9 | 8/9 = 88.9% | unchanged |
| predecessor | ~8/9 | 7/9 = 77.8% | −1 |
| before_named | 15/15 | 9/15 = 60.0% | −6 |
| transitive_cause | 10/11 | 6/11 = 54.5% | −4 |
| concurrent | 15/15 | 4/15 = 26.7% | −11 |
| ordinal_first | 10/15 | 2/15 = 13.3% | −8 |
| current_state | 15/15 | 1/15 = 6.7% | −14 |
| **origin** | 15/15 | **0/15 = 0.0%** | **−15** |
| **ordinal_last** | 9/15 | **0/15 = 0.0%** | **−9** |
| **sequence** | 8/15 | **0/15 = 0.0%** | **−8** |
| **causal_chain** | 10/11 | **0/11 = 0.0%** | **−10** |
| noise | 0/20 | 0/20 = 0.0% | unchanged |

Four entire shape classes dropped to **zero** r@1.

### The squatter memory

From top-1 frequency histograms across all 165 predictions:

**OLD run top-1 concentration (most frequent memory at rank 1):**
```
  9  sdev-adr_jph-programming-code-editors-sec-00
  9  sdev-adr_jph-rust-programming-language-sec-06
  6  sdev-adr_jph-timestamp-format-sec-00
  5  sdev-adr_jph-docker-swarm-container-orchestration-sec-01
  4  sdev-adr_jph-css-framework-sec-02
```

**NEW run top-1 concentration:**
```
 19  sdev-adr_jph-high-trust-teamwork-sec-00
 18  sdev-adr_jph-rust-programming-language-sec-06
 11  sdev-adr_jph-high-trust-teamwork-sec-01
 10  sdev-adr_jph-high-trust-teamwork-sec-04
  9  sdev-adr_jph-mysql-database-sec-00
```

The `high-trust-teamwork` ADR, not in the old top-12 at all, now owns **40 of 165 queries (24%)** as top-1 across three of its sections. `rust-programming-language-sec-06` doubled from 9 → 18.

### What changed in the knowledge graph (ingest trace)

From `benchmarks/audit_ingest_trace.py` running the new code on softwaredev mini (186 memories):

| Metric | Value |
|---|---|
| Memories total | 186 |
| SLM method (ingest-time) | `joint_bert_lora × 186` (100% primary) |
| Memories that hit E5 or heuristic-primary | 0 |
| Entity source: `slm_slot` (Part 2) | 76 |
| Entity source: `caller_subject` (Part 4) | 186 |
| Entity source: other (GLiNER legacy) | 1379 |
| L2 ENTITY_STATE nodes created | **186** |
| L2 source: `caller_subject` | **186** |
| L2 source: regex-extracted | 0 |
| L2 source: SLM state_change | 0 |
| Memories with 0 linked entities | 0 |
| `extract_entity_state_meta` regex calls during ingest | **0** |
| `heuristic_fallback.extract` calls during ingest | 186 (by design: floor pass in ChainedExtractor) |

**One L2 node per memory.** For a corpus where ADRs have 7–21 sections each, this means every section is treated as a distinct state of the ADR.

Sibling-state clustering by subject prefix:

```
 21 states -> secrets-storage                 (16 "status", 3 "infra", 1 "framework", 1 "language_runtime")
 13 states -> environment-variable-config      (9 "status", 4 "tooling")
 12 states -> svelte-components                (11 "status", 1 "framework")
 12 states -> timestamp-format                 (10 "status", 2 "framework")
 11 states -> browser-automation-playwright   (6 "status", 5 "testing")
 11 states -> css-framework                   (10 "status", 1 "language_runtime")
  9 states -> mysql-database                   (8 "status", 1 "framework")
  7 states -> high-trust-teamwork              (most "status", some "tooling")
  …
```

Within a single (subject, state_key) pair — e.g. `(secrets-storage, "status")` with 16 members — reconciliation runs `C(16,2) = 120` pairwise comparisons. Across 25 subjects, total pairwise reconciliation work ≈ **400–500 comparisons per ingest**, each potentially writing a `supersedes` / `superseded_by` / `supports` edge to the graph.

Pre-commit baseline: on the same corpus, L2 creation was gated by regex or state_change_head signals; the ADR prose rarely matches, so L2 count was near-zero. We went from ~0 L2 → 186.

### SLM head calibration (ingest-time)

From the same trace, distribution of SLM topic-head output across 186 memories:

```
131  topic=None               (low confidence, fell to default)
 18  topic='framework'
 14  topic='infra'
 12  topic='tooling'
  6  topic='language_runtime'
  5  topic='testing'
```

71% of memories got no confident topic. The remaining 29% got coarse domain labels that are **not per-ADR semantic keys**. Most importantly, the `high-trust-teamwork-sec-01` memory (content: "The decision to adopt High Trust Teamwork as a core value") got `topic='tooling'` at confidence 0.81 — a clear misclassification that my code promoted into `state_key="tooling"` on its L2 node.

### Query-side grammar trace

From `benchmarks/audit_query_trace.py` running 11 target queries with monkey-patched fallback counters:

**Fallback counts during query pass (across all 11 queries):**
```
heuristic_fallback.extract : 22   (2 per query — SLM classified twice per query)
tlg.analyze_query          : 11   (1 per query — query entity-extraction)
extract_entity_state_meta  :  0   (zero — SLM-first path holds on queries too)
e5_zero_shot.extract       :  0
exemplar_intent_index      :  0
```

**Subject-lookup pollution:** 11 target queries, grammar subject resolved to `sdev-adr_jph-high-trust-teamwork` on **7 of 11**, across 7 different shapes (causal_chain, origin, ordinal_last, current_state, ordinal_first, before_named, transitive_cause). The "decision" entity token dominantly maps to high-trust-teamwork because that ADR has the most "decision" language across its 7 sections.

Representative records:

```
qid=softwaredev-origin-001  shape=origin
  text: (origin query re: CSS framework)
  gold_mid:                         sdev-adr_jph-css-framework-sec-02
  SLM shape_intent='origin'         (conf 0.996)   -- correct
  Grammar subject:                  sdev-adr_jph-high-trust-teamwork   -- WRONG
  Grammar entity:                   decision
  Grammar conf:                     high
  Search top-1:                     sdev-adr_jph-tailwind-css-sec-03   -- also wrong
  Rank of gold in search top-10:    2
```

```
qid=softwaredev-current_state-001  shape=current_state
  gold_mid:                         sdev-adr_jph-css-framework-sec-02
  SLM shape_intent='current_state'  (conf 0.99)   -- correct
  Grammar subject:                  sdev-adr_jph-high-trust-teamwork   -- WRONG
  Grammar proof:                    terminal of zone 2 (chain: d29f9077-…)
  Search top-1:                     sdev-adr_jph-tailwind-css-sec-02
  Rank of gold:                     2
```

### What's NOT broken

- **SLM shape_intent on queries**: `shape_intent` head fires with ~0.99 confidence on every target query, producing the correct shape label. No regression here.
- **SLM admission head**: 100% "persist" with high confidence — correct behaviour for a gold-query corpus.
- **Regex fallbacks on ingest**: the 0-count on `extract_entity_state_meta` confirms Parts 2+4 don't leak back into regex.
- **ChainedExtractor primary selection**: 100% LoRA primary; no E5 / heuristic promotion.
- **Part 1 (UUID→name)**: entity vocabulary has 1298 real names, 0 UUIDs.

---

## Root cause (stated precisely)

Part 4's design invariant was:

> "When subject is provided, the ingest pipeline forces creation of an L2 ENTITY_STATE node with entity_id = subject."

That invariant is wrong for content where **subject means "about"** rather than **"state transition of"**. For MSEB ADR sections (and analogously, long-form documents, tickets with multi-observation threads, patient records with multi-observation reports), a caller-asserted subject indicates aboutness, not a state change. Treating every section as a state creates a false state-evolution trajectory, which:

1. Floods the graph with sibling-subject entries that reconciliation links as SUPERSEDES/SUPPORTS.
2. Makes the subject entity a multi-memory hub (up to 21 memory-links for one subject in this corpus), which graph-spreading activation amplifies.
3. Creates so many `(subject, state_key)` clusters that subject-lookup's distinctiveness metric favours the noisiest subject (high-trust-teamwork) because its section-count makes its entity tokens over-represented.

The 186-node L2 inflation is the single biggest delta vs pre-commit state. Everything downstream (reconciliation edges, subject hub, walker subject pollution, search ranking) follows from it.

---

## Fix plan (4 steps, staged)

### Step 1 — Decouple subject-vocabulary seeding from L2 creation

The real reason Part 4 created L2 nodes: `vocabulary_cache._rebuild` iterates ENTITY_STATE nodes to learn subjects. Fix that by also accepting **memories linked to a subject-typed entity** as input to vocabulary induction:

```python
# vocabulary_cache._rebuild:
# 1. Find every memory with a linked entity of type='subject'
# 2. Group by entity name → subject
# 3. Union that set with the existing ENTITY_STATE-derived subjects
```

This separates two concerns cleanly:
- **Subject vocabulary** ← subject-entity links (cheap, 1 per memory)
- **State trajectory** ← ENTITY_STATE nodes (only when there's a real state transition)

### Step 2 — Tighten L2 creation in the subject-kwarg path

Change `_detect_and_create_l2_node` to require BOTH `subject` AND `(slm_state_change ∈ {declaration, retirement} with confidence OR regex fires)` before creating an L2 node. When `subject` is provided but no state-change signal, skip L2 creation — the subject entity is already linked via merged_entities.

Result: L2 count drops from 186 to a small number (only memories with genuine state-change content).

### Step 3 — Don't promote misclassified SLM topic into state_key

On the subject-asserted path, always use `state_key="status"`. The SLM topic head outputs coarse domain labels (framework/infra/tooling) that are not semantic keys for a specific ADR's state — promoting them into reconciliation lookup is actively harmful.

### Step 4 — Re-measure with a focused audit pass

After Steps 1–3 land:
- Re-run ingest trace: expect L2 count ≈ few vs 186, subject-entity count unchanged at 186.
- Re-run per-qid diff: expect r@1 to recover toward 0.745 baseline.
- Re-run query trace: expect subject resolution to distribute across 25 subjects, not collapse onto high-trust-teamwork.

Only then kick the MSEB full-12 overnight.

---

## Not done in this commit (flagged for follow-up)

- **`heuristic_fallback.extract: 2` per query** — suggests the SLM chain runs twice per query path. Perf + behavioural audit needed; possibly search() and retrieve_lg() both classify instead of sharing one result.
- **`tlg.analyze_query` firing** — pending check on whether `analyze_query` still has regex machinery or is fully SLM-driven.
- **3-domain coverage** — this audit is softwaredev-only. Clinical, SWE, convo will have different graph topologies; same fix should help them but needs verification.
- **Deadlock in `benchmarks/tlg_trajectory_trace.py`** — still unexplained; ran into 0% CPU hang after "starting trace". Unblocked by using a simpler diagnostic. Not blocking benchmark runs because the harness works.

## Artifacts

- `benchmarks/audit_predictions_diff.py` — per-qid old vs new
- `benchmarks/audit_ingest_trace.py` — per-memory SLM heads + entity source + L2 metadata
- `benchmarks/audit_query_trace.py` — per-query grammar trace with fallback counters
- `benchmarks/results/audit/softwaredev_old_vs_new.txt` — diff report
- `benchmarks/results/audit/softwaredev_ingest_trace.{jsonl,log}` — ingest trace
- `benchmarks/results/audit/softwaredev_query_trace.{jsonl,log}` — query trace
