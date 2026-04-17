# P1 Temporal — LongMemEval Measurement

**Date:** 2026-04-17
**Commits:** `094f47f` (wiring), `6015d7d` (regression fix)
**Config:** `--features-on` bundle, temporal_enabled=true, scoring_weight_temporal=0.2

---

## TL;DR

P1 wiring was built, tested end-to-end, and shipped. Running LongMemEval
with it enabled produced **zero change** in Recall@5 per category.

The infrastructure works — verified in a controlled diagnostic and by 13
integration tests. But **LongMemEval's temporal-reasoning category isn't
answerable by time-range retrieval**, which is what P1 does. The 133
temporal questions in the benchmark are almost all *ordinal* ("which
came first") or *arithmetic* ("how many days between") — both of which
require reasoning over already-retrieved content, not filtering retrieval
by time.

This is a design-assumption failure, not an implementation bug. The
design doc assumed LongMemEval's weak temporal-reasoning score was due
to missing range-filter support. It isn't.

---

## Results

### Category comparison

| Category | Questions | Baseline Recall@5 | With P1 Recall@5 | Delta |
|---|---:|---:|---:|---:|
| single-session-user | 70 | 0.8429 | 0.8429 | 0.0000 |
| knowledge-update | 78 | 0.7436 | 0.7436 | 0.0000 |
| single-session-assistant | 56 | 0.6429 | 0.6429 | 0.0000 |
| multi-session | 133 | 0.3308 | 0.3308 | 0.0000 |
| temporal-reasoning | 133 | 0.2782 | 0.2782 | 0.0000 |
| single-session-preference | 30 | 0.0000 | 0.0000 | 0.0000 |
| **Overall** | **500** | **0.4680** | **0.4680** | **0.0000** |

Byte-for-byte identical. Elapsed: 7,506s (2h 5m).

### End-to-end wiring verification

A controlled diagnostic confirmed the wiring works in isolation:

```
Config: temporal_enabled=True, scoring_weight_temporal=0.2
reference_time = 2023-05-01

Memory A: "vacation plans with Alice"   observed_at=2023-04-10
Memory B: "vacation plans with Bob"     observed_at=2023-04-30

Query: "what did we discuss yesterday about vacations"
  Result 1: Memory B  temp_score=0.2000  total=0.8000
  Result 2: Memory A  temp_score=0.0000  total=0.6000
```

- `observed_at` persists through the Memory object and round-trips from
  SQLite.
- The L1 atomic node inherits `observed_at` from the Memory at node
  creation.
- `parse_temporal_reference(query, now=reference_time)` correctly
  resolves "yesterday" relative to the passed reference time.
- `compute_temporal_proximity` returns a non-zero score for a memory
  whose `observed_at` falls inside the parsed range.
- Ranking actually changes: the near-reference_time memory ranks above
  the distant one, with the `temp_score` contribution visible on
  `ScoredMemory`.

The infrastructure is working.

---

## Why zero impact on LongMemEval

Sampling the 133 temporal-reasoning questions revealed the actual
pattern:

| Pattern | Example | Needs |
|---|---|---|
| Ordinal comparison | *"Which event did I attend first, X or Y?"* | retrieve both, compare timestamps |
| Ordinal "first" | *"What was the first issue I had with my car?"* | age-weighted ranking among matches |
| Arithmetic between events | *"How many days between Holi and Sunday mass?"* | retrieve both events, compute delta |
| Arithmetic "days before" | *"How many days before the team meeting did I attend the workshop?"* | retrieve both, compute delta |

None of these are "retrieve memories from the last 3 weeks" queries.

P1 handles range expressions (`"yesterday"`, `"3 weeks ago"`,
`"last month"`, `"in January"`, `"Q1 2026"`) well. It also has partial
support for ordinals — `parse_temporal_reference` returns
`TemporalReference(ordinal="first")` for queries containing "first /
initial / earliest / original", which triggers an age-weighted boost in
`compute_temporal_proximity`. But:

- The ordinal boost at weight 0.2 is insufficient to promote the correct
  answer into the top-5 when BM25 has placed it deeper.
- For arithmetic-over-events questions, retrieval returning the right
  candidates is half the problem — the arithmetic itself needs a RAG
  layer, not a scoring signal.

The **Recall@5 metric itself** is also a ceiling here. Even if temporal
scoring were perfectly reordering candidates, Recall@5 only measures
membership in the top-5, not rank within. Improvements that promote the
correct answer from rank 3 to rank 1 are invisible at k=5.

---

## What was actually accomplished

**Infrastructure that wasn't there before:**

- `Memory.observed_at` field (new) — bitemporal data model realized end-to-end.
- `store_memory(observed_at=...)` kwarg + `search(reference_time=...)` kwarg.
- SQLite column + index on `memories.observed_at` (schema v10).
- L1 MemoryNode inherits `observed_at` from Memory in both inline and
  background indexing paths.
- Scoring fallback chain: `MemoryNode.observed_at > Memory.observed_at >
  Memory.created_at`.
- LongMemEval harness parses `haystack_dates` → `observed_at`, and
  `question_date` → `reference_time`.

**13 new integration tests** locking in the round-trip and the ranking
shift. All 931 tests pass; 122 architecture fitness tests pass; no D+
methods in application code.

**Incidentally fixed** three stale `_get_cached_labels` references from
the Phase 0 label_cache extraction that were failing the background
indexing path on every request (the benchmark caught it).

---

## What this means for the design doc

P1 in `docs/design-query-performance.md` should be revised:

- The +0.42 to +0.57 delta projection for temporal-reasoning was based
  on an incorrect understanding of what the category tests. It needs to
  be downgraded to "marginal or zero, pending rework."
- A **new feature** (call it P1b) is warranted: *ordinal temporal
  reasoning with comparative retrieval*. Given a query with "first" /
  "before" / "after" / "between", retrieve both events and return them
  as a pair rather than filtering. This is closer to P3 Session Storage
  or a new "event retrieval" capability.
- The underlying `observed_at` infrastructure is valuable regardless —
  it's a prerequisite for most other temporal features and completes
  the bitemporal data model.

---

## Next steps

Two real options:

1. **Move on to P2 (Preference Extraction).** It targets the
   single-session-preference category currently at 0.0000, where the
   failure mode (vocabulary mismatch between "I prefer…" and "What does
   the user prefer?") is well-understood and the expected delta is
   +0.70 to +0.95. High confidence, short path to visible impact.

2. **Investigate LongMemEval temporal-reasoning properly.** Build a
   diagnostic that for each temporal question, dumps the top-20
   retrieved memories and records whether the ground-truth answer is
   anywhere in the candidate set. If it is, the problem is ranking and
   a bigger temporal weight might help. If not, the problem is
   candidate generation and we need a larger top-K or a different
   strategy entirely.

Recommendation: **option 1**. We have a clean base, P2 is well-scoped,
and the investigation in option 2 can inform a proper P1b redesign
later.
