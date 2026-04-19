# P1b Redesign — Temporal Boosting Research & Proposal

**Status:** Research → proposal (no code yet)
**Date:** 2026-04-16
**Prerequisite reads:** `docs/p1-measurement.md`, `docs/p1-temporal-findings.md`
**Related:** MemPalace `knowledge_graph.py` (ref: GitHub), LongMemEval paper (arXiv 2410.10813)

---

## Why we're here

P1a shipped a bitemporal scoring signal (observed_at + reference_time + compute_temporal_proximity). Zero impact on LongMemEval because the questions aren't range-filters — they're *ordinal* ("first X", "last X") and *arithmetic* ("how many weeks between X and Y").

P1b as built (pool-wide `apply_ordinal_rerank`) **regresses** LongMemEval:

| Category | R@5 baseline | R@5 with P1b | Δ |
|---|---|---|---|
| ORDINAL_FIRST | 0.333 | 0.000 | **−0.333** |
| COMPARE_FIRST | 0.414 | 0.276 | **−0.138** |
| Overall | 0.468 | 0.454 | −0.014 |

Root cause: when a question says *"What was the **first** X-ray after the surgery?"*, "first" is **subject-scoped** ("first *X-ray*"), not **pool-scoped** ("earliest candidate in the whole top-K"). Sorting the whole pool by date pushes off-topic early memories ahead of the actual answer.

The user then asked the right question: **is there nothing inherent in our full system that could unlock any ideas here? We already have reranker, contradiction detection — RAG validation seems heavy.**

This doc answers that by mapping what others do → what we already have → what we'd actually build.

---

## 1. How others do it

### 1.1 MemPalace — temporal knowledge graph

MemPalace's pitch is "verbatim storage + temporal triples in SQLite." The interesting half is `knowledge_graph.py`:

```python
CREATE TABLE triples (
    subject TEXT, predicate TEXT, object TEXT,
    valid_from TEXT, valid_to TEXT,
    confidence REAL,
    source_closet TEXT  -- back-reference to verbatim memory
);
```

Key query primitive — **as-of temporal filter**:

```python
def query_entity(name, as_of=None):
    # SELECT ... WHERE subject=?
    #   AND (valid_from IS NULL OR valid_from <= ?)
    #   AND (valid_to   IS NULL OR valid_to   >= ?)
```

And `invalidate(subject, predicate, object, ended)` which sets `valid_to` — their version of supersession.

The LongMemEval "0.966" number from their repo is retrieval-only Recall-flavored; the paper's primary metric is an LLM judge (>97% human agreement) — so the number is indicative, not apples-to-apples with paper results. Multiple open issues on their repo contest the methodology. What's *real* about MemPalace is the data model: **they made temporal facts first-class graph objects with validity windows**, not a score at retrieval time.

### 1.2 Temporal IR literature

Short version of the last ~18 months:

- **Time-aware reranking** (Kanhabua/Berberich et al., extended by 2025 RAG freshness work): after lexical retrieval, rerank with a signal proportional to `exp(-|t_event - t_query| / τ)`. Works when the query has an explicit temporal reference.
- **Entity-scoped ranking** (Zep's Graphiti, Letta, OpenAI Cookbook "Temporal Agents with Knowledge Graphs"): resolve the query's *subject entity* first, then filter/rank *within* that entity's timeline. This is the pattern that solves the "first X after Y" class.
- **Multi-signal fusion for freshness-sensitive RAG** (arXiv 2509.19376): stack lexical + dense + temporal + currency flags; currency typically comes from a `is_current` marker derived from contradiction detection or supersession.
- **LongMemEval paper's own recommendations** (arXiv 2410.10813): "session decomposition for value granularity, fact-augmented key expansion for indexing, and time-aware query expansion." Their *primary* metric is GPT-4o judge; Recall@k is secondary.

The two patterns that consistently win: **(a) put temporal validity into the graph** and **(b) scope the rerank to the subject entity**.

---

## 2. What NCMS already has (and why P1b-as-built fought it)

NCMS didn't arrive at this from zero. The inventory:

| Asset | What it does today | Temporal relevance |
|---|---|---|
| `MemoryNode(node_type=ENTITY_STATE)` with `is_current`, `valid_from`, `valid_to`, `observed_at` | L2 state nodes with bitemporal fields | **This is MemPalace's triple model.** `state_key`/`state_value` in metadata = predicate/object. The `entity_id` edge = subject. We already have it. |
| `ReconciliationService` (supports/refines/supersedes/conflicts) | Closes `valid_to`, flips `is_current=False`, creates SUPERSEDES edges | **This is MemPalace's `invalidate()`.** We already have it, heuristic + LLM-backed. |
| Bitemporal `observed_at` on `Memory` (P1a) | Event time carried into scoring | Unused by retrieval path when node_type is entity_state — we scored the atomic, not the state. |
| `IntentClassifier` (7 classes) | `current_state_lookup`, `historical_lookup`, `change_detection` already routed | **Already the right taxonomy** — we just don't use it to pick temporal semantics. |
| `HTMG._traverse_temporal` | Walks state timelines | **Already exists.** Not called from `search()` default path. |
| GLiNER entity extraction | Runs at ingest AND at query | **We already extract query subjects.** Not used for scoping rerank. |
| Cross-encoder reranker (Phase 10) | Selective by intent — disabled for `current_state_lookup`/`historical_lookup` because it destroys date ordering | **Correctly scoped.** Proves intent-gated reranking is already the architectural pattern. |
| Contradiction detector | Detects conflicts at ingest | Already feeds into reconciliation; effectively the "currency" signal. |

**The uncomfortable truth:** the broken P1b implementation ignored every one of these and shipped a pool-wide date-sort. It fought the existing architecture instead of leaning on it.

MemPalace's whole pitch — bitemporal triples with `valid_to` supersession — is a **subset** of what `MemoryNode(ENTITY_STATE) + ReconciliationService` already does. We didn't realize we already had it because the retrieval path doesn't use it.

---

## 3. What the right NCMS design looks like

The research surveyed above gives us three durable ideas — we don't port any of them, we design from them.

> 1. **Temporal facts belong in structure, not only in score.** Whether you call them triples or entity-state nodes, a fact with a validity window is a first-class object, not a scalar boost.
> 2. **"First/last X" is a subject-scoped question, not a pool-scoped one.** The pool-wide date sort we shipped is architecturally wrong for this class.
> 3. **Currency (is-this-still-true?) is a property of the graph, not the query.** It's produced by supersession at ingest time and *read* at retrieval time.

Applied to NCMS, which already has bitemporal `ENTITY_STATE` nodes and a `ReconciliationService` that maintains them, the right P1b is a **retrieval-path change, not a data-model change**:

### 3.1 Intent → retrieval-strategy table

We already classify intents; we just don't branch retrieval on them. The branch we're missing:

| Intent | Needed behavior (what the user wants) | Mechanism (NCMS-native) |
|---|---|---|
| `fact_lookup` | Lexical/semantic match — current path is fine | No change |
| `current_state_lookup` | Prefer facts still true right now | Read `is_current=True` from L2 nodes already on the candidate; apply existing `supersession_penalty` to those with `is_current=False` on the combined score path |
| `historical_lookup` (ordinal) | The **first/last *thing-of-type-X*** | Subject-scoped ordinal — §3.2 |
| `historical_lookup` (range) | Things that happened in a window | Temporal scoring already ships (P1a); works when observed_at is populated, which it now is |
| `change_detection` | What changed about X | Walk SUPERSEDES edges from the subject's state nodes |

This is a routing table, not five new subsystems. Each cell is either "already built" or "small function over existing data."

### 3.2 Subject-scoped ordinal — the actual bug fix

The pool-wide sort is wrong because "first" attaches to the subject, not the pool. The fix:

1. Temporal parser flags ordinal — *already emitted* as `TemporalReference.ordinal`.
2. GLiNER at query time returns subject entities — *already running in retrieval*.
3. If ordinal ∧ subjects non-empty: restrict the rerank slice to candidates whose entity-links intersect the query's subjects. Rank *that slice* by `observed_at`. Splice back into the pool ahead of un-scoped candidates of lower activation.
4. If no subjects extracted: do nothing (degrade, don't guess).

That's the whole change. It fits under `RetrievalPipeline` as a candidate-set transformation, not as a scoring signal. Unit-testable in isolation, compatible with the existing ADR integration test (which passes because that pool is already subject-homogeneous).

### 3.3 Currency as a read-path signal

`ReconciliationService` already writes `is_current=False` and `valid_to` when a state is superseded. Retrieval doesn't use those fields unless ACT-R is on. The fix is a single additive term in the combined score — not a new signal, an existing penalty moved to a path that's actually active.

### 3.4 What this is *not*

- Not a new `triples` table. Our `MemoryNode(ENTITY_STATE)` + graph edges already encode (subject, predicate, object, valid_from, valid_to).
- Not a schema migration. `observed_at` shipped in schema v10 for P1a.
- Not an LLM-at-query-time feature. All mechanisms are regex + local NER + already-indexed graph lookups.
- Not a full graph-walker rewrite. `HTMG._traverse_temporal` exists; if we use it here it's as a read, not a new primitive.

### 3.2 Why this beats the pool-wide rerank

On the failing LongMemEval case — *"What was the first X-ray of the patient?"*:

- **Pool-wide rerank (broken):** top-K contains 20 memories; ~3 mention X-rays, the rest are other medical events. Sorting all 20 by `observed_at` buries the oldest *X-ray mention* under older non-X-ray mentions. Recall@5 collapses.
- **Entity-scoped rerank (proposed):** GLiNER tags "X-ray" as subject. We fetch memories that mention X-ray (entity-linked), sort *those* by observed_at ascending. The actual first X-ray surfaces regardless of how many non-X-ray memories existed.

On the winning ADR case (test `test_latest_adr_on_authentication`):

- **Both** approaches pass because the query is single-subject ("authentication") and all 5 candidates are on-subject. The integration test we wrote stays green.

### 3.3 Why this beats RAG validation

The user's instinct was right: **RAG-mode evaluation is heavy, flaky, and measures the LLM-judge composition rather than the retrieval change**. Worse, we spent an evening chasing what we thought was Spark downtime but was actually a mDNS/DNS resolution issue on the client — validation infrastructure for a mechanism we haven't built.

What we should measure instead:

| Target | Benchmark |
|---|---|
| P1b doesn't regress | Existing `test_ordinal_rerank_adr.py` (5 tests) + LongMemEval Recall@5 must be ≥ baseline |
| P1b fixes the ordinal class | Synthetic entity-scoped benchmark (like P2 Option B): ~15 "first/last X" scenarios with seeded entity timelines. Pass rate → 100% before shipping. |
| P1b helps something real | LongMemEval temporal-reasoning on the ~12 remaining non-arithmetic questions (the ones that *aren't* below the arithmetic ceiling). We now know exactly which 12 from the temporal diagnostic. |
| Production observation | Dashboard: rate of `historical_lookup` intents + rate of entity-scoped rerank firing |

No RAG. No LLM judge. No Spark dependency at eval time. The arithmetic-ceiling questions stay unscoreable under Recall@k — that's a metric limitation we've already documented; we don't need to spend RAG hours to re-confirm it.

---

## 4. Proposed build, in order of risk

### 4.1 Revert (critical, immediate)

Remove the pool-wide `apply_ordinal_rerank` call from the default `search()` path. It's a −1.4% regression on the benchmark we care about. The method stays in `ScoringPipeline` (the ADR integration tests use it and it's correct *when the pool is already subject-scoped*) but it's no longer invoked unconditionally.

Single call-site change in `memory_service.py` or `scoring/pipeline.py` — no data migration, no schema change.

### 4.2 P1b-v2 core: entity-scoped ordinal rerank

New method on `RetrievalPipeline` (not scoring — this is a candidate-selection step, not a rescore):

```python
async def scope_to_subject_entities(
    self,
    query: str,
    candidates: list[ScoredMemory],
    temporal_ref: TemporalReference | None,
    ordinal: Literal["first", "last"] | None,
) -> list[ScoredMemory]:
    """If query has ordinal intent AND named subjects, restrict pool to
    memories linked to those subjects, then sort by observed_at."""
```

Called only when `ordinal is not None` AND `temporal_ref is not None`. When GLiNER returns zero subject entities, fall back to the existing pool order (degrade, don't regress).

### 4.3 State-aware currency boost (small)

In `ScoringPipeline._compute_raw_signals`, when `intent == current_state_lookup` and the candidate's L2 node has `is_current=False`, apply a small penalty (already have a knob: `scoring_reconciliation_supersession_penalty`). We're currently checking this via ACT-R mismatch only; extend to the combined score path when ACT-R weight is 0.

### 4.4 Test matrix

- `tests/unit/application/test_entity_scoped_rerank.py` — new, ~6 tests for the scoping function in isolation
- `tests/integration/test_ordinal_rerank_adr.py` — keep passing (5 tests green today)
- `tests/integration/test_entity_scoped_medical_timeline.py` — new, seed an entity with 3 state transitions, assert "first state of entity X" returns the earliest
- LongMemEval temporal-reasoning: R@5 must be ≥ 0.468 baseline and R@5 on the 12 non-arithmetic questions must improve

### 4.5 What we are NOT building

- No new graph table (we already have `memory_nodes` + `graph_edges`)
- No new scoring weight (re-use `scoring_weight_temporal`, which already exists but is underused)
- No RAG-mode evaluation gate
- No schema migration (schema v10 with `observed_at` is already sufficient)
- No LLM calls at query time (temporal parser is regex; GLiNER is local)

---

## 5. Answering the user's three questions directly

**Q1. How are people doing this?**
MemPalace stores (subject, predicate, object, valid_from, valid_to) triples and filters queries by `as_of`. IR lit adds two orthogonal ideas: time-aware reranking (scalar boost by Δt) and entity-scoped ranking (filter the pool to the subject before ranking). The consensus-winning combination is **entity-scoped + as-of-filtered**, not pool-wide date sort.

**Q2. Is there nothing inherent in our full system that could unlock ideas?**
Yes — and the honest finding is that we already *structurally* match the pattern the field converged on. `MemoryNode(ENTITY_STATE)` with bitemporal fields and `is_current` is the same shape as a temporal triple with a validity window. `ReconciliationService` is our supersession. GLiNER runs at query time. Intent classification already distinguishes `current_state_lookup` / `historical_lookup` / `change_detection`. `HTMG._traverse_temporal` walks timelines. None of this is wired to the retrieval branch. The P1b fix is a routing change + subject-scoped rerank + one currency term — small, and entirely on top of existing pieces.

**Q3. What were we trying to prove with RAG — seems heavy?**
We were trying to answer "does P1 help LongMemEval categories that Recall@k can't score." The answer from P1/P2 analysis is: **Recall@k underscores two categories (arithmetic-reasoning and prose-rubric preferences) by construction**. Running RAG with LLM judge would confirm this, but we don't need confirmation to build the next mechanism. Drop RAG as a gate. Keep it as an optional once-at-milestone sanity check, not a per-change validation loop.

---

## 6. Open questions before build

1. **Where does GLiNER run at query time?** Confirmed in `RetrievalPipeline` already; just need to expose the subject-entity list to the new scope function.
2. **What to do when a query has multiple subjects?** Proposal: scope to union of their timelines, rank by observed_at within, then re-merge. Alternatively, keep pool-wide order when subjects > 3 (likely a broad query).
3. **Threshold for "ordinal intent"?** Temporal parser already emits `ordinal ∈ {"first","last",None}` with high precision. No threshold needed — presence gates the behavior.
4. **Should this deprecate `apply_ordinal_rerank` entirely?** No — it's correct for already-scoped inputs (ADR test proves this). Keep it as a primitive; call it from the new entity-scoped function after filtering.

---

## 7. TL;DR for the next session

- Broken rerank comes out of default path (one line).
- New entity-scoped rerank lives in `RetrievalPipeline`, uses GLiNER subjects + existing L2 bitemporal nodes.
- Currency boost extends existing reconciliation penalty to the combined-score path.
- Validation is synthetic entity-scoped benchmarks + LongMemEval R@5 non-regression, not RAG.
- Build time: ~1 day. Measurement cost: minutes, not hours.

---

## 8. What we actually shipped (2026-04-18)

Built and merged:

- **`ScoringPipeline.apply_subject_scoped_ordinal_rerank`** — partitions the top-K head into subject-linked vs. other, sorts the subject-linked slice by `observed_at` (ascending for `first`, descending for `last`), preserves the "other" slice in relevance order behind it, leaves the tail untouched.
- **Dual subject-match rule** — a candidate is considered subject-linked when *either* (a) its graph entity-links include any `context_entity_id` from the query, *or* (b) its content (case-insensitive) contains any GLiNER subject name of length ≥ 3. The text-fallback matters because GLiNER is non-deterministic across similar-worded documents — the ADR integration test exposed a case where only 3 of 5 ADRs linked to the "authentication" entity at ingest, even though all 5 contain the word.
- **Degrade rules** — no ordinal → no-op; no subjects at all → no-op; subjects exist but nothing in the head touches them → no-op. Never fall back to pool-wide sort.
- **Call site** — single line in `memory_service.search()`, replacing the prior pool-wide call. `apply_ordinal_rerank` stays on the class as a primitive (used internally by the subject-scoped version after filtering); it's just no longer invoked from the default path.
- **Tests** — 14 unit tests on the new method (pool-scoped negative cases, text-fallback, multi-subject union, mutation check, rerank-k bounds), 5 ADR integration tests (all green), 293-test unit+architecture suite green, complexity gate green (new method is C(18)).

Deferred to a follow-up PR:

- The `current_state_lookup` currency-penalty extension (§3.3) — small change, but wants its own test matrix and separate measurement so the subject-scoped rerank delta is cleanly attributable.
- Updating the `design-query-performance.md` P1b row — done in the same PR as the code, but called out here for the reader.

LongMemEval temporal diagnostic run on the new build is in flight at write time; the non-regression target is R@5 ≥ 0.468 and ORDINAL_FIRST R@5 ≥ 0.333.
