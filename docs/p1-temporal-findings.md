# P1 Temporal — Diagnostic Findings

**Date:** 2026-04-17
**Script:** `benchmarks/longmemeval/temporal_diagnostic.py`
**Input:** 133 LongMemEval temporal-reasoning questions
**Config:** features-on bundle, top-50 retrieval, observed_at + reference_time wired

## 1. Per-pattern retrieval table

| Pattern | # | R@5 | R@20 | R@50 | Upside 20\5 | Upside 50\5 | Arith ceiling |
|---|---:|---:|---:|---:|---:|---:|---:|
| COMPARE_FIRST | 29 | 0.414 | 0.448 | 0.448 | 1 | 1 | 16 |
| AGE_OF_EVENT | 19 | 0.158 | 0.210 | 0.210 | 1 | 1 | 15 |
| ARITH_BETWEEN | 17 | 0.000 | 0.000 | 0.000 | 0 | 0 | 17 |
| ARITH_ANCHORED | 15 | 0.267 | 0.400 | 0.400 | 2 | 2 | 9 |
| DURATION_SINCE | 13 | 0.000 | 0.000 | 0.000 | 0 | 0 | 13 |
| RANGE_FILTER | 13 | 0.538 | 0.538 | 0.538 | 0 | 0 | 6 |
| ORDER_OF_EVENTS | 7 | 0.000 | 0.000 | 0.000 | 0 | 0 | 7 |
| ORDINAL_FIRST | 6 | 0.333 | 0.333 | 0.333 | 0 | 0 | 4 |
| ORDINAL_LAST | 5 | 0.600 | 0.800 | 1.000 | 1 | 2 | 0 |
| OTHER | 5 | 0.600 | 0.600 | 0.600 | 0 | 0 | 2 |
| COMPARE_LAST | 3 | 1.000 | 1.000 | 1.000 | 0 | 0 | 0 |
| TIME_OF_EVENT | 1 | 0.000 | 0.000 | 0.000 | 0 | 0 | 1 |
| **Totals** | **133** | — | — | — | **6** | **6** | **90** |

R@5 for the 133 questions sums to 37/133 = 0.2782 — confirms the baseline.

## 2. Key findings

### 2.1 The arithmetic ceiling dominates

**90 of 133 temporal-reasoning questions (68%) have the answer substring
in zero memories in the haystack.** These questions ask for a computed
number (*"How many days between X and Y?"* → *"21 days"*) or an age
(*"How long ago was Z?"* → *"3 weeks ago"*). The answer is never in a
source memory, so **Recall@K cannot score them at any retrieval depth
under any scheme**. They're only measurable in RAG mode where the LLM
computes the delta after retrieval.

Ceiling breakdown by pattern:

- **100% ceiling**: ARITH_BETWEEN (17), DURATION_SINCE (13), ORDER_OF_EVENTS (7), TIME_OF_EVENT (1) = 38 questions
- **≥50% ceiling**: ARITH_ANCHORED (9/15), COMPARE_FIRST (16/29), AGE_OF_EVENT (15/19), ORDINAL_FIRST (4/6) = 44 more
- **Low/no ceiling**: RANGE_FILTER (6/13), OTHER (2/5), ORDINAL_LAST (0/5), COMPARE_LAST (0/3) = 8 more

### 2.2 The retrieval bottleneck is tiny

For the 43 questions where the answer *is* retrievable (in the
haystack somewhere), **top-50 retrieval already finds all of them**.
There are **zero** questions where the answer is in the haystack but
outside top-50.

This means **multi-anchor retrieval (P1c) has zero upside** on
LongMemEval's Recall@5 metric — the candidate pool is already complete
for every retrievable question.

### 2.3 Rerank upside is small — and concentrated

Only **6 questions** have the answer in top-50 but outside top-5.
That's the entire ceiling for any reranking strategy on LongMemEval:

- ORDINAL_LAST: 2 questions (R@5 = 0.600, R@50 = 1.000) — clean win
- ARITH_ANCHORED: 2 questions (a couple with lexical anchors findable but not ranked top-5)
- COMPARE_FIRST: 1 question
- AGE_OF_EVENT: 1 question

Perfect reranking would lift temporal-reasoning from 0.2782 → 0.3233,
which is **+0.0451 on the category and +0.012 on overall Recall@5** on
LongMemEval. That's real but it's the **absolute ceiling** for any
rerank scheme on this benchmark.

### 2.4 P1a (range filter) already does what it can

RANGE_FILTER questions score R@5 = 0.538 — highest of any pattern
with meaningful sample size (13). Of the 7 retrievable ones, all 7
are already in the top-5. P1a is not leaving value on the table in
this category; it's limited by the other 6 that hit the ceiling.

## 3. What to build — revised

The diagnostic inverts the earlier recommendation.

### For LongMemEval alone:

- **P1b (ordinal rerank) ceiling: +0.012 overall Recall@5.** Small.
- **P1c (multi-anchor retrieval) ceiling: zero.** No benefit.
- **The real gap is RAG-mode measurement**, not retrieval. 68% of
  temporal-reasoning needs the LLM to compute an arithmetic answer.

### For the software-dev workload (the actual target):

P1b is **still worth building**, for reasons independent of LongMemEval:

- Our production queries (ADRs, evolution, state history) are heavily
  ordinal. See `docs/p1-temporal-usecases.md` §A–B.
- Production answers are usually *"the actual text of the latest ADR"*,
  not *"how many days ago was it written"*. No arithmetic ceiling.
- The rerank ceiling on LongMemEval (+0.012) undersells production
  impact because LongMemEval's questions skew to arithmetic while
  production skews to ordinal-over-filtered-candidates.

P1c is **not worth building right now.** It targets a problem LongMemEval
doesn't exhibit and that production hasn't surfaced either — we have
no data showing multi-anchor retrieval is where our real workload
fails.

## 4. Cross-reference with software-dev use cases

Mapping findings back to `docs/p1-temporal-usecases.md`:

| Use case | Pattern | LongMemEval finding | P1b helps? |
|---|---|---|---|
| *"Last ADR on authentication"* | ORDINAL_LAST | R@5=0.6→R@50=1.0, **clean rerank win** | **Yes — direct hit** |
| *"Original design for cache layer"* | ORDINAL_FIRST | R@5=0.33, ceiling on 4 of 6 questions | **Partial — helps the 2 retrievable ones** |
| *"Most recent change to user model"* | ORDINAL_LAST | same pattern | **Yes** |
| *"Which came first, CQRS or event-sourcing?"* | COMPARE_FIRST | R@5=0.414, 16/29 arithmetic ceiling | **Yes for the 13 non-ceiling cases** |
| *"What changes this quarter?"* | RANGE_FILTER | R@5=0.538, already near ceiling | No (P1a already works) |
| *"What was decided before the v2 launch?"* | ARITH_ANCHORED (event-anchor flavour) | R@5=0.267, ceiling on 9/15 | Needs **anchor resolution** — separate design |
| *"How long ago was the last incident?"* | AGE_OF_EVENT | 79% ceiling | Needs **RAG** |

### Net: P1b is the right next build, with modest LongMemEval impact and high production upside.

## 5. Recommendation

**Build P1b (ordinal rerank).**

Scope:
- When the classified intent is ordinal (`first / last / earliest /
  latest / most recent`), re-rank the top-K candidates by their
  `observed_at` timestamp (ascending for "first", descending for
  "last") with the BM25/SPLADE combined score as a tie-breaker.
- No new data model, no new pipeline. ~50 lines in
  `application/scoring/pipeline.py` (the scoring module already owns
  final ranking).

Measurement plan:
- Re-run LongMemEval — expect +0.03 to +0.045 on temporal-reasoning
  (+0.008 to +0.012 overall). Will confirm wiring; won't be a big
  number.
- Add a small synthetic benchmark of ADR-style ordinal queries to
  directly measure the production case. ~20 questions, 30 min to
  build once, re-usable.

**Don't build P1c yet.** The diagnostic shows zero Recall@K upside from
multi-anchor retrieval on LongMemEval, and we have no production
evidence it's needed. Revisit only if a real NemoClaw query fails in a
way that a single retrieval pass can't explain.

**Park the arithmetic questions for RAG mode.** The 90 ceiling-bound
questions cannot improve under any retrieval change. If we want
credit for them, it has to come through `benchmarks longmemeval --rag`
where an LLM computes the numeric answer from retrieved context. Worth
doing eventually, but it's a separate evaluation layer (the LLM's
arithmetic ability), not a retrieval feature.

## 6. Effort estimate

- **P1b implementation:** 2-3 hours (scoring pipeline edit + unit tests + architecture-test sanity check)
- **Synthetic ADR benchmark:** 30 minutes (generate 20 questions against a seeded NCMS)
- **LongMemEval re-run for confirmation:** ~30 minutes (just temporal-reasoning subset)

Total: half a day. Low risk, bounded scope, clear measurement plan.

## 7. Artifacts

- Raw data: `benchmarks/results/temporal_diagnostic/temporal_diagnostic_latest.json`
- Markdown report: `benchmarks/results/temporal_diagnostic/temporal_diagnostic_latest.md`
- Script: `benchmarks/longmemeval/temporal_diagnostic.py`
