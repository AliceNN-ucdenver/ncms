# P1 Temporal — Use Cases Beyond LongMemEval

**Purpose:** LongMemEval is one benchmark.  The real target for NCMS is
agent memory for software development workflows (NemoClaw and similar).
This document enumerates the temporal questions an agent would actually
ask in that setting, maps each to our pattern taxonomy, and shows where
the current P1a + proposed P1b/P1c leave us.

The two populations overlap but aren't identical.  Designing P1 around
LongMemEval alone would miss several patterns that matter heavily in
the software-dev context and over-invest in ones that don't.

---

## Software-dev temporal queries

### Category A — Decision records

| Query | Pattern | Coverage |
|-------|---------|----------|
| *"What was the last decision on the authentication flow?"* | ORDINAL_LAST + entity | needs rerank |
| *"What was the original design for the cache layer?"* | ORDINAL_FIRST + entity | needs rerank |
| *"Show me the most recent ADR about security"* | ORDINAL_LAST + entity | needs rerank |
| *"Which architecture came first, event-sourcing or CQRS?"* | COMPARE_FIRST | needs rerank + multi-anchor |
| *"What was the first proposal for the billing service?"* | ORDINAL_FIRST + entity | needs rerank |
| *"What did we originally plan for retry policy?"* | ORDINAL_FIRST + entity | needs rerank |

**These are the most common questions and the highest-value target.**
ADRs accumulate; agents need "latest" / "original" semantics constantly.
P1b (ordinal rerank over candidates) directly addresses this whole
category.

### Category B — Evolution and change history

| Query | Pattern | Coverage |
|-------|---------|----------|
| *"How has the user schema evolved?"* | ORDER_OF_EVENTS + entity | needs rerank + multi-anchor |
| *"What changed in the auth system between v1 and v2?"* | ARITH_BETWEEN (anchored by version, not date) | needs anchor resolution |
| *"When did we deprecate the v1 API?"* | TIME_OF_EVENT | P1a range filter doesn't help; need memory retrieval with date in content |
| *"Show me the status history of the migration"* | ORDER_OF_EVENTS | HTMG `_traverse_temporal` already supports this; needs surfacing |

### Category C — Relative time windows (P1a territory)

| Query | Pattern | Coverage |
|-------|---------|----------|
| *"What did we discuss yesterday about rate limiting?"* | RANGE_FILTER (relative) | ✅ P1a |
| *"What changes landed this week?"* | RANGE_FILTER | ✅ P1a |
| *"What decisions did we make last quarter?"* | RANGE_FILTER | ✅ P1a |
| *"What was agreed in the Q3 planning session?"* | RANGE_FILTER | ✅ P1a |

**P1a already handles these** — the LongMemEval run confirmed the wiring
works end-to-end.

### Category D — Anchored pre/post queries

| Query | Pattern | Coverage |
|-------|---------|----------|
| *"What was decided before the v2 launch?"* | ARITH_ANCHORED (anchor = event, not date) | needs anchor resolution |
| *"What issues came up after the Postgres upgrade?"* | ARITH_ANCHORED | needs anchor |
| *"Anything new since the architecture review last week?"* | ARITH_ANCHORED + RANGE | needs anchor + P1a |
| *"What happened in the week before the outage?"* | ARITH_ANCHORED + RANGE | compound |

Category D is the hardest.  The anchor (*"v2 launch"*, *"Postgres
upgrade"*) is itself a retrieved event whose `observed_at` defines the
reference point.  Requires a **two-step retrieval**: (1) find the
anchor event's `observed_at`, (2) apply range filter relative to it.
P1c (multi-anchor retrieval) gets us halfway there; true anchor
resolution is a larger design change.

### Category E — Time-deltas and durations

| Query | Pattern | Coverage |
|-------|---------|----------|
| *"How long ago was the last deployment incident?"* | AGE_OF_EVENT | answer is computed; Recall@K ceiling, RAG needed |
| *"How many days between the schema change and the migration?"* | ARITH_BETWEEN | answer computed; ceiling |
| *"How long have we been running the hotfix?"* | DURATION_SINCE | answer computed; ceiling |
| *"When was the last time we touched rate limiting?"* | ORDINAL_LAST | needs rerank; answer is retrievable (a memory date) |

Pure arithmetic has an intrinsic Recall@K ceiling — the number
*"14 days"* doesn't appear in any source memory.  These are addressable
only in **RAG mode**, where the LLM computes the arithmetic after
retrieving both anchor events.

### Category F — Current state (not temporal-reasoning, but often confused)

| Query | Pattern | Coverage |
|-------|---------|----------|
| *"What is the current status of the migration?"* | CURRENT_STATE_LOOKUP | ✅ existing intent path + L2 entity_state |
| *"What TODOs are still open?"* | CURRENT_STATE_LOOKUP | ✅ existing |
| *"What's pending from yesterday's standup?"* | CURRENT_STATE + RANGE | compound |

These look temporal but are actually state-lookup questions already
covered by NCMS's existing intent pipeline and HTMG entity_state
retrieval.  We should keep clear framing so we don't accidentally
re-solve them in the temporal pipeline.

---

## What each pattern needs, by implementation scope

| Pattern | Implementation | Effort |
|---------|---------------|--------|
| RANGE_FILTER | ✅ P1a (shipped) | done |
| ORDINAL_FIRST / ORDINAL_LAST | **P1b** — re-rank candidate pool by `observed_at` when ordinal intent detected | 2-3 hours |
| COMPARE_FIRST / COMPARE_LAST | **P1b + P1c** — both anchors in result set, then ordinal rerank | 4-6 hours |
| ORDER_OF_EVENTS | **P1c + traversal** — multi-anchor + HTMG temporal traversal | 6-8 hours |
| AGE_OF_EVENT | P1b (ranking) + RAG for the numeric answer | 2 hrs retrieval + RAG separate |
| ARITH_BETWEEN / ANCHORED / DURATION | **P1c** for retrieval + RAG for computation | 4 hrs + RAG |
| TIME_OF_EVENT | ordinary retrieval; needs memory content to include the date | no code change |
| CURRENT_STATE_LOOKUP | ✅ existing | done |

---

## Why this matters for the P1 design decision

**LongMemEval's distribution is skewed toward arithmetic/compare.**  Our
production distribution (decision records, evolution, status lookups)
is skewed toward **ORDINAL + anchored queries**.

Concretely: what helps the production agent the most is **P1b (ordinal
rerank)**.  Every *"last ADR on X"*, *"most recent change to Y"*,
*"original design for Z"* is an ordinal query over a filtered candidate
set.  P1b is the single highest-leverage feature for the actual
workload and — as a bonus — moves the LongMemEval COMPARE_FIRST and
ORDINAL_FIRST buckets.

**P1c (multi-anchor retrieval)** is what LongMemEval's arithmetic
category needs, and what our Category D (anchored pre/post) needs.
Still valuable but second priority.

**Pure arithmetic answers** (*"14 days"*) cannot be scored by Recall@K.
If we want to measure improvement there, we need LongMemEval's RAG
mode (`--rag`) or a separate RAG benchmark.  That's a measurement
problem, not a retrieval problem.

---

## Recommendation

Build P1b first, measure on BOTH LongMemEval (where it helps
COMPARE_FIRST + ORDINAL_FIRST, ~35 of 133 temporal questions) AND a
handful of synthetic software-dev scenarios (*"last ADR on
auth"*-style) where we know the ordering impact directly.

Then P1c for comparative retrieval.

The diagnostic run that's about to finish will tell us the exact
breakdown of where each bucket's answers land in the candidate pool,
which finalizes the P1b/P1c sizing.
