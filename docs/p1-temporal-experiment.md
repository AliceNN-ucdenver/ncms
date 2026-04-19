# P1-Temporal Experiment — Zero-LLM Time-Aware Retrieval

**Status:** Phase A instrumentation shipped (2026-04-18). **Architecture revised** based on Phase A coverage measurement — see §13–§15 for the current design. §0–§12 below document the original design; they're retained for context but **§13–§15 supersede where they conflict**.
**Date:** 2026-04-18 (initial); 2026-04-18 revised after Phase A
**Prerequisite reads:** `docs/research-longmemeval-temporal.md` (paper evidence), `docs/design-query-performance.md` §9.1 (P1 status history)

---

## 0. TL;DR

LongMemEval §5.4 reports **+10.5 to +23.3 R@5** on the temporal-reasoning category from one mechanism: **hard-filter the retrieval pool by a time range extracted from the query**, against memory keys **pre-augmented with dates extracted from their content at ingest**. The paper uses GPT-4o to extract the query range; with Llama 8B the gain roughly halves.

NCMS's pitch is **zero LLM at query time**. This experiment ports the paper's recipe using the SLMs we already load:

1. **Ingest-time date extraction** — GLiNER (already loaded, 209M) with temporal label set, plus `dateparser` (pure-Python rules) to normalize spans to ISO dates.
2. **Query-time range extraction** — same label set, same parser, same normalization. No LLM round-trip.
3. **Hard candidate filter** — drop memories with zero temporal overlap to the query range; keep the existing scoring pipeline for the rest.

Expected outcome: ≥50% of the paper's gain (i.e. +5–12 R@5 pts on temporal-reasoning) without any new LLM dependency. If the ingest extraction is noisy we degrade to baseline (no filter), never below.

---

## 1. Why a rules + NER stack can deliver this

The paper's own Table 4 shows the LLM choice is a dial, not a switch. GPT-4o → +23.3 pts, Llama 8B → +10-12 pts. The mechanism works even when the extractor is imperfect because the filter is *permissive*: if we can't extract a range, no filter is applied.

Temporal expressions are well-studied: >90% of real-world expressions fall into a small number of families (ISO dates, month names, "yesterday/today/last week", "N days/weeks ago", "between X and Y"). Rules + small NER cover the long tail the paper's weak-LLM baseline covered, without the hallucination failure mode the paper explicitly flagged:

> *"Llama 8B, on the other hand, struggles to generate accurate time ranges, often **hallucinating or missing temporal cues** even with numerous in-context examples."* (paper §5.4)

Rules can miss. Rules can't hallucinate. Miss → no filter → we serve baseline retrieval. Hallucinate → wrong filter → recall drop. The failure-mode asymmetry favors rules for retrieval-filter use.

---

## 2. Label taxonomy for GLiNER

Rather than a single opaque `date` label, use a TIMEX3-adjacent typology. Separate labels let GLiNER's zero-shot head focus per category and let the downstream normalizer know how to resolve the span.

Proposed label set (both ingest *and* query extraction — same taxonomy on both sides so content and queries encode in the same vocabulary):

| Label | Matches | Example |
|---|---|---|
| `date` | Absolute calendar dates | "April 18, 2026", "2026-04-18", "06/15/25" |
| `relative date` | Deictic expressions resolved against `reference_time` | "yesterday", "last Thursday", "next Monday" |
| `duration` | Spans of time | "three days", "6 months", "a couple of weeks" |
| `start date` | Interval openers | "since June 5", "starting Monday", "from last week" |
| `end date` | Interval closers | "until yesterday", "through Friday", "by end of March" |
| `time of day` | Clock times, dayparts | "2pm", "in the morning", "around noon" |
| `event anchor` | Textual references to other events | "after the surgery", "before the meeting" |

The first five labels are the load-bearing ones for the paper's mechanism. `time of day` and `event anchor` are cheap additions with modest extra gain. The full taxonomy is a single GLiNER call (we already run GLiNER per-memory and per-query — this just adds labels to the existing request).

**Important failure mode:** `event anchor` extracts spans like "after the surgery" but can't resolve them without either an LLM or an event graph. Proposal: extract the span at ingest, store it with the memory, and surface it as a secondary retrieval signal (entity-linked to the "surgery" event) without attempting to resolve it to a calendar range. The range-filter only fires when we have at least one calendar-resolvable temporal reference.

### 2.1 Additive composition with the existing label system

Temporal labels are a **separate, orthogonal dimension** from domain/universal entity labels. Entity labels answer "*what* is this about" (person, ADR, technology); temporal labels answer "*when* is this about". They compose additively — never replace — so the same extraction pass yields both entity spans and temporal spans, and downstream consumers route them by label.

Today, `src/ncms/domain/entity_extraction.py::resolve_labels()` returns either:

- `UNIVERSAL_LABELS` (10 universal entity types), or
- Domain-specific labels (from `topics` cache), optionally merged with universals when `_keep_universal=True`.

Temporal labels must layer on top of **whichever set is already in play**. They neither replace domain labels nor get replaced when a domain-specific set is active. Implementation:

```python
# src/ncms/domain/entity_extraction.py  (additions)

TEMPORAL_LABELS: list[str] = [
    "date", "relative date", "duration",
    "start date", "end date",
    "time of day", "event anchor",
]

def add_temporal_labels(labels: list[str]) -> list[str]:
    """Additively merge temporal-extraction labels with an existing set.

    Keeps ``labels`` order (entity labels first, temporal second) and
    deduplicates case-insensitively so domain configs that already
    include a ``date`` label don't get a duplicate.
    """
    seen = {label.lower() for label in labels}
    extended = list(labels)
    for t in TEMPORAL_LABELS:
        if t.lower() not in seen:
            extended.append(t)
            seen.add(t.lower())
    return extended
```

Callers (ingestion pipeline, retrieval pipeline) do:

```python
labels = resolve_labels(domains, cached_labels=cached)
if config.temporal_range_filter_enabled:
    labels = add_temporal_labels(labels)
```

No changes to `resolve_labels()` itself. `UNIVERSAL_LABELS` stays untouched — temporal is its own feature-flagged concern, not a universal default.

### 2.2 Cost of the extra labels

GLiNER's zero-shot head scales linearly with label count in a single forward pass; each added label is ~1–3 ms on a medium-size chunk at our existing MPS/CUDA deployment. Adding 7 temporal labels to a typical 10-label call is a ~70% increase in label count and roughly a 20–30% increase in per-call wall time (in practice GLiNER sub-linear amortizes part of this).

Mitigations baked into the design:

1. **Feature-flag gate.** Temporal labels only attach to the call when `temporal_range_filter_enabled=true`. Domains with no temporal queries pay no cost.
2. **Cheap pre-filter (optional, Phase B).** Before adding temporal labels to a memory's ingest call, run a cheap regex check for calendar tokens (digit runs ≥ 2, month names, "today/yesterday/tomorrow/weekday names"). If the content has no such token, skip temporal labels for that call. Query side always attaches temporal labels (queries are short; cost is negligible) — the optimization is ingest-only.
3. **Same-pass extraction.** We do **not** do a second GLiNER call for temporal — the labels go into the same list. One forward pass, same chunking, merged results. Downstream split by label.

Phase A instrumentation (§7.1) captures per-call latency with and without the temporal labels to confirm this budget holds. If it doesn't, we iterate on the label list (drop the two cheap-gain tail labels: `time of day`, `event anchor`).

---

## 3. Normalization layer: GLiNER spans → ISO intervals

**This is the hardest-to-get-right part of the experiment and gets its own module.** GLiNER returns raw string spans with a label; we need deterministic, testable `(start: datetime, end: datetime)` intervals. The normalization logic is non-trivial and deserves to be an explicitly architected component, not an incidental line of code inside retrieval.

### 3.1 Module boundary

New pure module: `src/ncms/domain/temporal_normalizer.py` (domain layer — no infrastructure deps).

Public API:

```python
from datetime import datetime
from typing import Literal

TemporalLabel = Literal[
    "date", "relative date", "duration",
    "start date", "end date",
    "time of day", "event anchor",
]

@dataclass(frozen=True)
class RawSpan:
    text: str                 # Raw span as extracted by GLiNER
    label: TemporalLabel
    char_start: int           # For provenance / dedup
    char_end: int

@dataclass(frozen=True)
class NormalizedInterval:
    start: datetime           # Always timezone-aware (UTC)
    end: datetime             # Exclusive upper bound
    confidence: float         # 0–1, from the normalizer's view of the span
    source_span: RawSpan      # For debugging / observability

def normalize_spans(
    spans: list[RawSpan],
    reference_time: datetime,     # For relative-expression resolution
) -> list[NormalizedInterval]:
    """Deterministic span → interval mapping.  Unparseable spans are
    dropped (returned list is shorter than input).  Never raises on
    bad input; returns empty list if nothing resolves."""

def merge_intervals(
    intervals: list[NormalizedInterval],
) -> NormalizedInterval | None:
    """Reduce a memory's (or query's) multiple intervals to one range.
    Returns None when the input is empty."""
```

Two pure functions. No side effects. Every edge case covered by unit tests.

### 3.2 What the normalizer does, in order

The pipeline is a sequence of post-processing stages applied to each raw span:

1. **Trim and lowercase** for matching.  Keep original string in `RawSpan.text`.
2. **Duplicate suppression.** GLiNER occasionally returns the same span with two labels. Deduplicate by `(char_start, char_end)` keeping the higher-priority label (`date` > `relative date` > `duration` > `start date`/`end date` > `time of day` > `event anchor`).
3. **Partial-date expansion.** "June 2024" → full interval `[2024-06-01, 2024-07-01)`. "2024" → `[2024-01-01, 2025-01-01)`. "the 15th" (when reference_time is in a month) → that month's 15th as a day-wide interval. Rule: resolve to the **smallest unambiguous containing interval**.
4. **Relative-date anchor.** `dateparser.parse(text, settings={"RELATIVE_BASE": reference_time, "PREFER_DATES_FROM": "past"})` for anything labeled `relative date`. Historical memories resolve "yesterday" against `memory.observed_at`, not wall clock.
5. **Duration resolution.** Parse "N units" with `re` + a unit lookup table: `{day, week, month, year, hour, minute}`. Pairs a duration with an adjacent date span (within ± 50 chars) to form a `[anchor, anchor + duration)` interval. A bare duration with no anchor in the span set is dropped (it doesn't form an interval on its own).
6. **Start/end date folding.** `start date` = interval begins here, goes to `reference_time`. `end date` = interval ends here, begins at `datetime.min`. When a `start date` and `end date` co-occur in the same text, fold them into one bounded interval.
7. **Timezone.** Everything is converted to UTC with `dateparser`'s timezone settings. Naive datetimes default to UTC.
8. **Confidence scoring.** High confidence when the span matches a canonical form (ISO date, explicit month name); lower when `dateparser` falls through to fuzzy matching. Dropped entirely below a configurable threshold (default 0.3).
9. **Reject gates.** If the resolved interval spans more than a fixed horizon (default: 100 years), it's treated as a parse failure and dropped. Prevents absurd outputs from mis-parsed tokens like "12" being interpreted as year 0012.

### 3.3 Why not just call `dateparser.parse()` in-line

`dateparser` is the engine, not the whole normalizer. It gives us a single datetime, not an interval, and it doesn't handle:

- Label-specific semantics (`start date` vs `end date` vs `duration`)
- Interval widening for partial dates (`dateparser("June 2024")` returns June 1, not the month interval)
- Duration pairing
- Span deduplication across GLiNER's overlapping extractions
- Confidence scoring and reject gates
- Deterministic behavior under ambiguity (D/M/Y vs M/D/Y) — we pin locale via config

Every one of those is a unit-testable pure function. The normalizer module is the right place for them to live together.

### 3.4 Testing the normalizer

`tests/unit/domain/test_temporal_normalizer.py` — this is where the coverage effort goes. Test matrix:

- **Absolute dates** — ISO, slash, month-name, ordinal-number variants
- **Partial dates** — year only, year + month, month + day (in a known month context)
- **Relative expressions** — "yesterday", "last Monday", "3 days ago", reference time in past / present / future
- **Durations** — "3 days", "a week", "six months", "a couple of years", duration + absolute date pairing
- **Start/end dates** — "since June", "until yesterday", "from X to Y"
- **Combinations** — "I went on June 5 for three days", "every Monday since March"
- **Ambiguity** — "05/06/25" under different locale configs → deterministic output
- **Reject cases** — "12" alone, "Q1" without year, "the day before" without anchor — all drop, no raise
- **Timezone** — naive vs aware datetimes on the reference_time side

This is a high-test-count module by design. The LongMemEval gain we're chasing depends entirely on the normalizer producing stable, aligned intervals on both sides of the index.

### 3.5 What ends up persisted

For each memory with at least one resolved span:

```
memory_content_ranges:
  memory_id:   <uuid>
  range_start: <ISO 8601, UTC>
  range_end:   <ISO 8601, UTC>
  span_count:  <N — how many spans contributed>
  source:      'gliner'
```

The raw spans themselves are *not* persisted in P1-exp — they can be recovered by rerunning extraction on `memory.content`. If debugging needs them persistently, a follow-up adds a `memory_content_spans` table; not in scope here.

For each query:

The interval is transient, scoped to the request. No persistence.

### 3.6 When extraction yields nothing

Both sides (ingest and query) have a clean "no range" fallthrough:

- Memory: `content_range = None`, no row inserted into `memory_content_ranges`
- Query: `query_range = None`, no filter applied — retrieval runs as today

The `temporal_missing_range_policy` config controls how memories *with* no content_range are treated when a query *does* produce one. Default `include` (recall-safe). Alternative `exclude` (precision-safe, tested for comparison in Phase A).

---

## 4. End-to-end data flow

```
INGEST:
  content → GLiNER(labels=DATE_FAMILY + existing entities)
         → date spans + entity mentions
         → dateparser.normalize(spans, reference_time=memory.observed_at)
         → ISO interval (start, end)
         → store in memory_content_ranges table (new)
         → existing Memory path (BM25 + SPLADE + graph) unchanged

QUERY:
  query → GLiNER(labels=DATE_FAMILY + existing entities)
       → date spans
       → dateparser.normalize(spans, reference_time=reference_time or now)
       → ISO query interval
       → if interval exists:
             fetch candidates from BM25/SPLADE/graph as usual
             pre-score filter: keep candidates whose content_range overlaps query_interval
       → if no interval:
             no filter (current behavior)
       → continue through existing scoring pipeline
```

**Filter semantics:** overlap, not containment. Memory range `[A, B]` matches query range `[C, D]` when `A <= D AND B >= C`. This handles:
- Point dates matching wider ranges ("June 5" mentioned in a memory, query asks about "June")
- Wider memory ranges containing query points ("June 1 – 15" in memory, query asks "June 5")
- Partial overlaps ("June 1 – 10" memory, "June 5 – 15" query)

**Memories with no range (content_range is NULL):** by default, *pass* the filter — treat absence of temporal information as "could apply any time." This is the safer default for recall, at the cost of some precision. Tunable via `NCMS_TEMPORAL_MISSING_RANGE_POLICY=include|exclude`.

---

## 5. Schema change

One new table:

```sql
CREATE TABLE IF NOT EXISTS memory_content_ranges (
    memory_id   TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    range_start TEXT NOT NULL,   -- ISO 8601
    range_end   TEXT NOT NULL,
    span_count  INTEGER NOT NULL,  -- how many temporal spans contributed
    source      TEXT NOT NULL      -- 'gliner' | 'metadata' | 'mixed'
);
CREATE INDEX IF NOT EXISTS idx_mcr_range ON memory_content_ranges(range_start, range_end);
```

Schema v11. No migration — per project convention (`CLAUDE.md`), rebuild DB from scratch for schema changes.

**Why a separate table vs a JSON column on memories:** the index on `(range_start, range_end)` is the whole point. Filter lookups become a btree range scan, not a Python-side iteration over every memory.

Alternative considered: push the range into `memory_nodes` as bitemporal fields on L1 atomic nodes. Rejected because not every memory has an L1 node (content classification can route to ephemeral or document store), and the range filter must work for all memories regardless of node type.

---

## 6. Where the code lands

Landing zones follow the Phase 0 pipeline boundaries. No cross-pipeline imports.

| Concern | File | Change |
|---|---|---|
| New label taxonomy | `src/ncms/domain/entity_extraction.py` | Add `TEMPORAL_LABELS` constant and helper to merge with domain labels |
| GLiNER call (no API change) | `src/ncms/infrastructure/extraction/gliner_extractor.py` | Labels are a parameter; retrieval + ingestion already pass them |
| Span → interval normalizer | `src/ncms/domain/temporal_normalizer.py` (new) | Pure module: `RawSpan` + `NormalizedInterval` dataclasses, `normalize_spans()` and `merge_intervals()` functions. Handles label-specific semantics, partial-date expansion, duration pairing, reject gates, timezone, confidence. See §3. |
| Query-intent parser | `src/ncms/domain/temporal_parser.py` | Existing regex-based `TemporalReference` parser stays for ordinal intent detection; no longer the normalizer. |
| Ingest-side range | `src/ncms/application/ingestion/pipeline.py::run_inline_indexing` | After GLiNER merge, resolve temporal spans → persist to `memory_content_ranges` |
| Query-side range | `src/ncms/application/retrieval/pipeline.py::retrieve_candidates` | After GLiNER merge on query, resolve temporal spans → attach to candidate-set call |
| Hard filter | `src/ncms/application/retrieval/pipeline.py::expand_candidates` | New step between graph expansion and node preload: `filter_by_temporal_range(candidates, query_range)` |
| Schema | `src/ncms/infrastructure/storage/migrations.py` | `memory_content_ranges` table; bump to v11 |
| Store API | `src/ncms/domain/protocols.py` + `infrastructure/storage/sqlite_store.py` | `save_content_range` / `get_memories_in_range` |
| Config | `src/ncms/config.py` | `temporal_range_filter_enabled`, `temporal_missing_range_policy` |
| Dep | `pyproject.toml` | `dateparser` (pure Python, small) |

**Fitness-function checks** (per `docs/fitness-functions.md`):

- Complexity: every new method ≤ C (20). The normalizer is the risk — if it crosses C, extract per-label helpers.
- Import boundary: `domain/temporal_parser.py` imports only stdlib + `dateparser`. Infrastructure modules don't reach across pipelines.
- Dead code: no reintroduction of rerank methods; the retired `apply_ordinal_rerank` / `apply_subject_scoped_ordinal_rerank` stay deleted.

---

## 7. Experiment plan

### 7.1 Phase A — instrumentation (no filter active)

Flag `temporal_range_filter_enabled=false` while we:

1. Run ingest over LongMemEval's `_M` split; measure how many memories get a non-null `content_range`. Target: >60%. If lower, tune labels.
2. Run queries; measure how many extract a non-null query range. Target: >80% on temporal-reasoning questions. Below this, extend the taxonomy before filtering.
3. For each query, log `(has_query_range, content_range_overlap_count)` without filtering. This gives the expected filter rejection rate per question without committing to reject.

Output: `benchmarks/results/temporal_diagnostic/ranges_coverage.md` — extraction coverage by pattern class and by LongMemEval question type.

**Gate to Phase B:** query-range extraction rate ≥ 80% on temporal-reasoning. If we're below, the filter can't help enough questions to move R@5 — debug extraction first.

### 7.2 Phase B — filter on, measure non-regression

Enable the flag, run LongMemEval. Expected outcomes:

| Metric | Baseline | Target | Must-not-fall-below |
|---|---|---|---|
| Overall R@5 | 0.4680 | ≥ 0.4680 | 0.4680 (no regression) |
| Temporal-reasoning R@5 | 0.2782 | ≥ 0.32 (+4 pts, half of Llama 8B's gain in paper) | 0.2782 |
| RANGE_FILTER R@5 | 0.538 | **biggest upside here** — aim 0.60+ | 0.538 |
| Non-temporal categories | — | unchanged | −0.01 per category |
| Query p95 latency | — | +10ms or less | +30ms |

### 7.3 Phase C — compound with fact-augmented keys (paper §5.3)

Paper §5.3 reports a separate +5–10 pts from fact-augmented keys. The two optimizations compound to +23 in Table 4. NCMS already extracts "facts" in ingestion indirectly via GLiNER entities and L2 state nodes; §5.3 would be an explicit document-expansion step at ingest.

Deferred to a follow-up experiment — don't entangle with Phase B measurement.

---

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| GLiNER misses a temporal expression we'd want to filter on | Log misses in Phase A; if a family is missing, add a label or extend regex fallback |
| `dateparser` resolves ambiguously (e.g. "05/06/25" as M/D/Y vs D/M/Y) | Default to locale-from-config; surface as metadata for debugging |
| Memories without observed_at metadata can't resolve relative expressions in their content | Memory-side: if GLiNER finds only relative spans and metadata has no `observed_at`, content_range = NULL (fall through to unfiltered path) |
| Filter is too aggressive, kills recall on non-temporal queries | Only fire when query has a non-null range; default "missing range" policy is `include` |
| GLiNER latency goes up from extra labels | GLiNER takes labels as a list — 7 labels vs current ~4 adds negligible cost at the same inference step. Measure in Phase A. |
| `dateparser` is a new dep | Pure Python, MIT, small footprint, widely used. Adopt. |
| Content content-date extraction doesn't apply to ADR/structured content | Structured docs path already has parent/child document store; extracting dates from headings is orthogonal. Out of scope for P1-exp. |

---

## 9. What we are NOT building

- **No LLM at query time.** Hard line.
- **No pool rerank.** Paper §5.4 doesn't rerank; we don't either. The retired `apply_ordinal_rerank` code is gone.
- **No new scoring weight.** The existing `scoring_weight_temporal` stays for the soft P1a boost. The filter is a hard pre-scoring candidate pruning step, not another signal to combine.
- **No fact-augmented key expansion** in P1-exp scope — that's a separate paper §5.3 optimization, additive to ours.
- **No dense embeddings.** Tracked separately as P4.

---

## 10. Success criteria

The experiment ships if:

1. Phase A extraction coverage ≥ 80% on temporal-reasoning queries.
2. Phase B achieves ≥ +4 pts R@5 on temporal-reasoning with zero category regressing more than 0.01.
3. p95 query latency rise ≤ 30 ms.
4. No new LLM dependency.
5. No new D+ complexity methods (fitness gate green).
6. No new config flag that isn't `temporal_range_filter_enabled` or `temporal_missing_range_policy`.

Ship criteria not met → this doc gets moved to `docs/retired/` with a "what we learned" note, like P1b.

---

## 11. Open design questions

1. **Should the filter run before or after graph expansion?** Current proposal: after BM25/SPLADE retrieval, after graph expansion, before node preload + intent supplement. Rationale: graph expansion can surface neighbors that share the temporal range even if they didn't hit BM25 directly. But: filtering earlier reduces node-preload cost. Profile in Phase A before committing.
2. **Do we unify P1a and P1-exp?** P1a's soft temporal score currently adds a small boost but doesn't filter. With the filter active, the boost is redundant for candidates that pass the filter. Proposal: keep P1a as a fallback signal for queries where range extraction fails but temporal intent is parseable (e.g. ordinal-only questions). If measurement shows P1a is fully subsumed by the filter, retire it.
3. **Ingest-time resolution of relative expressions in conversation replays.** When benchmarks replay historical sessions via `store_memory(..., observed_at=session_date)`, the memory content's relative expressions ("yesterday") should resolve against `observed_at`, not wall-clock. Already wired through `reference_time`; just need ingestion to pass `memory.observed_at` as the reference when calling the normalizer.
4. **Multi-subject ranges in queries.** "What happened between surgery and the follow-up X-ray?" — two event anchors, no absolute dates. Deferred: P1-exp skips filtering when only `event anchor` labels are present (no calendar-resolvable range). Event-anchor resolution is a separate, bigger problem.

---

## 12. Measurement artifacts to commit

- `benchmarks/longmemeval/range_coverage.py` — Phase A instrumentation script
- `benchmarks/results/temporal_diagnostic/ranges_coverage_*.md` — coverage report per run
- Update to `benchmarks/longmemeval/temporal_diagnostic.py` — add a per-pattern column for "filter would fire"
- New integration test: `tests/integration/test_temporal_range_filter.py` — seed a small fixture with dated memories, confirm filter behavior end-to-end

---

## 13. Phase A findings (2026-04-18)

First implementation shipped (GLiNER additive temporal labels, `temporal_normalizer` module, `memory_content_ranges` table, log-only wiring through retrieval + ingestion). A 2-minute coverage script on 10 LongMemEval temporal-reasoning questions surfaced one finding that reframes the entire experiment.

### 13.1 Measured coverage

| Metric | Observed | Gate | Status |
|---|---|---|---|
| Query-side extraction rate | **0.0%** (10 / 10 questions produced no calendar range) | ≥ 80% | **Fail** |
| Memory-side content-date extraction rate | **2.0%** (1 / 50 sampled memories had a resolvable content date) | ≥ 60% | **Fail** |
| GLiNER latency with temporal labels added | p50 185 ms, p95 268 ms | ≤ +30 ms vs baseline | Within budget |

The extraction stack works — the normalizer tests pass, the wiring is end-to-end — but the **data doesn't contain the temporal references** the design assumed.

### 13.2 Why the coverage is low

LongMemEval's temporal-reasoning questions are **event-anchored, not calendar-anchored**. Examples from the failing set:

> *"How many days passed between my visit to the Museum of Modern Art (MoMA) and the 'Ancient Civilizations' exhibit?"*

No dates in the question. The dates live in the session envelopes (`question_date`, `haystack_dates`), which the benchmark harness maps into `memory.observed_at` at ingest.

> *"Which three events happened in the order from first to last: the day I helped my friend prepare the nursery, …"*

Three event names, zero calendar references. The ordering is recoverable by sorting the retrieved memories' `observed_at` timestamps — nothing in the question text helps a query-range extractor.

> *"How many weeks ago did I meet up with my aunt?"*

Implicit "today" anchor, no explicit date. Could in principle be computed from `(reference_time − aunt_meetup_memory.observed_at)`, but not by extracting a date from the query.

Memory content is similarly prose-heavy and date-light: turns like *"I'm looking for some recommendations on event spaces…"* contain no calendar tokens.

### 13.3 What this implies

The paper's §5.4 mechanism ("values additionally indexed by the dates of the events they contain") works on LongMemEval **because the paper's M_T is an LLM that infers implicit ranges from reference_time and event-name context**. Our regex+NER stack can't do that inference; it reads only what's on the page.

But the paper's concluding benefit (+10–23 R@5 pts) doesn't require LLM inference. It requires **ranking by event timestamp** — which in our system is `memory.observed_at`, available on 100% of ingested memories via the session envelope.

The pivot: stop extracting dates from prose. Use `observed_at` as the primary temporal anchor. Use GLiNER for what it's good at — extracting *entities* (people, events, places) — and do the date math on metadata.

---

## 14. Revised architecture: metadata-anchored + intent-routed

### 14.1 The two primitives

The zero-LLM temporal story has exactly two primitives, and each of the user-supplied scenarios (MoMA-exhibit, aunt-meetup, three-events) maps to a composition of them:

1. **Metadata-anchored retrieval.** Every memory carries `observed_at`. Retrieval returns this field alongside content. Downstream consumers (scoring, ordering, filtering) treat `observed_at` as the ground-truth event time.

2. **Entity-aware query routing.** GLiNER extracts entities from the query (already runs). When the query has temporal intent (ordinal, range, relative anchor), the retrieval strategy branches on intent type:
   - **Range intent** → hard-filter candidates to those with `observed_at ∈ query_range`
   - **Ordinal intent** ("first/last X") → entity-scope to subject X's memories, sort by `observed_at`
   - **Relative anchor** ("N units ago") → compute `reference_time − N units` as a range, hard-filter
   - **Entity-set ordering** ("order of X, Y, Z") → entity-scope to each, sort by `observed_at`, return as ordered set
   - **Pure arithmetic** ("how many days between X and Y") → **accept the Recall@K ceiling**, don't try

### 14.2 Ingest-time change: `observed_at` as fallback range

Single-point change in `ingestion/pipeline.py::_persist_content_range`:

```python
# Current behavior: persist only when GLiNER extracts resolvable spans.
# Revised: persist observed_at as a day-wide range when no content
# dates extract.  This gives 100% memory coverage at near-zero cost —
# the metadata is already on the memory.
if merged is None:
    anchor = memory.observed_at or memory.created_at
    if anchor is not None:
        merged = NormalizedInterval(
            start=_midnight(anchor),
            end=_midnight(anchor) + timedelta(days=1),
            confidence=0.6,     # lower than GLiNER-extracted content dates
            source_span=RawSpan("<metadata>", "date"),
            origin="metadata",
        )
        source = "metadata"
    else:
        return entities_only
else:
    source = "gliner"
```

Effect: memory-side coverage 2% → ~100% on LongMemEval. Zero additional NER/normalizer cost.

Content-date refinement still works where present — when GLiNER extracts a date from the prose (e.g., "I went on June 5th"), the content range *replaces* the metadata fallback (higher confidence wins at merge time).

### 14.3 Query-time change: intent-routed

The current `_extract_query_range` helper assumes every temporal query has a parseable range. It should become `_classify_temporal_intent` → one of four branches:

```python
class TemporalQueryIntent(Enum):
    NONE = "none"                    # no temporal signal → default retrieval
    RANGE = "range"                  # "since June", "last week" → hard filter
    RELATIVE_ANCHOR = "anchor"       # "weeks ago", "days since" → reference_time delta
    ORDINAL_SUBJECT = "ordinal"      # "first X", "last Y" → entity-scope + metadata sort
    ENTITY_ORDER = "order"           # "order of X, Y, Z" → multi-entity sort
    ARITHMETIC = "arithmetic"        # "how many days between" → retrieval ceiling, skip
```

Routing decisions (cheap, regex + token inspection):

- `range` or `anchor`: temporal parser already detects these. Resolve via normalizer.
- `ordinal`: ordinal-word regex + single subject entity from GLiNER.
- `order`: ordinal-word regex + 2+ subject entities from GLiNER.
- `arithmetic`: query contains "how many" / "how long" + duration unit. Fast-fail, no filter applied.

### 14.4 What this unlocks per pattern

| LongMemEval pattern | Count | Intent route | Expected recoverable? |
|---|---|---|---|
| ARITH_BETWEEN | 17 | ARITHMETIC | **No** — Recall@K ceiling = 0 |
| DURATION_SINCE | 13 | ARITHMETIC | **No** — same |
| ARITH_ANCHORED | 15 | ARITHMETIC | **No** — same |
| AGE_OF_EVENT | 19 | RELATIVE_ANCHOR | Partial — retrieves the anchor memory; the "3 weeks" answer still isn't in any memory |
| COMPARE_FIRST | 29 | ORDINAL_SUBJECT or ENTITY_ORDER | **Yes** — retrieve both subjects, keep both in result |
| COMPARE_LAST | 3 | ORDINAL_SUBJECT or ENTITY_ORDER | **Yes** |
| ORDER_OF_EVENTS | 7 | ENTITY_ORDER | **Yes** |
| ORDINAL_FIRST | 6 | ORDINAL_SUBJECT | **Yes** |
| ORDINAL_LAST | 5 | ORDINAL_SUBJECT | **Yes** |
| RANGE_FILTER | 13 | RANGE | **Yes** — what the original design handles |
| TIME_OF_EVENT | 1 | NONE or ORDINAL | **Yes** |
| OTHER | 5 | NONE | Mixed |

Retrievable ceiling: **64 of 133 ≈ 48%**. Arithmetic floor: **44 of 133 ≈ 33%** where Recall@K cannot score at any depth. Current overall R@5 baseline on temporal-reasoning is 0.2782; a retrievable-subset gain of +0.25 R@5 would lift the aggregate by ~+0.12.

### 14.5 Revised data flow

```
INGEST (unchanged from §4, plus metadata fallback):
  content + observed_at
      │
      ▼
  GLiNER → (entity spans, temporal spans)
      │
      ▼
  normalize(temporal spans, reference_time=observed_at) → intervals
      │
      ▼
  if intervals non-empty: content_range = merge(intervals), source='gliner'
  elif observed_at:         content_range = day-wide @ observed_at, source='metadata'
  else:                     content_range = None
      │
      ▼
  persist to memory_content_ranges

QUERY (new, intent-routed):
  query → GLiNER → (entity spans, temporal spans)
       │
       ▼
  classify_temporal_intent(query, temporal spans, entity_count)
       │
       ├── NONE        → default retrieval, no changes
       ├── ARITHMETIC  → default retrieval + flag "arithmetic ceiling"
       ├── RANGE       → normalize(temporal spans) → hard filter on content_range
       ├── RELATIVE    → reference_time delta → hard filter on content_range
       ├── ORDINAL     → entity-scope retrieval, sort by memory.observed_at
       └── ORDER       → multi-entity retrieval, union, sort
```

### 14.6 What stays, what changes, what goes

**Stays (already built, correct):**
- `TEMPORAL_LABELS` + `add_temporal_labels()`
- `temporal_normalizer` module and 37 tests
- `memory_content_ranges` table + schema v11
- `dateparser` dep
- GLiNER extractor extended with char positions

**Changes:**
- `_persist_content_range` gets the `observed_at` fallback
- `_extract_query_range` becomes `_classify_temporal_intent` with branches
- Ordinal and ENTITY_ORDER branches need entity-scoped retrieval that pulls `observed_at` and sorts — this is the primitive we retired as "subject-scoped rerank," now brought back **gated on classified intent**, not applied on every ordinal-word detection

**Goes (never built, don't build):**
- Pool-wide range filter fired on every query with a temporal label. Too narrow to matter, too aggressive when wrong.

---

## 15. Revised Phase A / B / C plan

### 15.1 Phase A revised — measure the metadata fallback + label ablation

Two measurements in one short run:

1. **Metadata fallback impact.** Single 30-line change — `observed_at` as the fallback range when no content dates extract. Rerun the coverage script with the fix active.
2. **Label-set ablation.** Current temporal-aware ingest runs 17 labels per GLiNER call (10 universal entity + 7 temporal). Test a slimmer, domain-focused set against the current one:

   | Ablation | Labels | Rationale |
   |---|---|---|
   | Full (current) | 17 = universal (10) + temporal (7) | Baseline — everything we might extract |
   | Slim-LME | 4 = `event`, `location`, `person`, `temporal relative` | Scenario-tuned — LongMemEval questions are life/event-anchored, not technical. Fewer labels → faster per-call, higher per-label precision. |

   Empirical question: does Slim-LME lose important entities, or does it keep coverage while halving latency? If coverage holds, ship with the slim set as the LME-style domain preset and let domain-specific `topics` configs still compose on top via `add_temporal_labels()`.

**Expected after fix:**
- Memory coverage: ≥ 95% (overwhelmingly from the `observed_at` fallback path; content-date extraction remains rare but meaningful where present)
- Query coverage: still low (~10–15%), which is now *fine* — the value at query time comes from intent routing + entity retrieval, not from range extraction alone.
- Latency p95: Slim-LME should come in meaningfully under Full (target: ≤ 60% of Full).

### 15.2 Phase B revised — intent routing

Replace `_extract_query_range` with `_classify_temporal_intent`. Build three branches (skip ARITHMETIC — it's a no-op; skip ORDER for v1 if multi-entity retrieval is complex):

1. `RANGE` / `RELATIVE_ANCHOR`: hard filter on `content_range` overlap. Reuses the work that's mostly already in place.
2. `ORDINAL_SUBJECT`: entity-scoped retrieval + metadata sort.

Measure per-pattern R@5 delta. Gate: ≥ +4 pts R@5 on RANGE_FILTER + ORDINAL_FIRST/LAST subsets with zero regression elsewhere.

### 15.3 Phase C revised — multi-entity order

ENTITY_ORDER branch for COMPARE_FIRST/LAST and ORDER_OF_EVENTS. Multi-entity retrieval (union of per-entity top-K), then metadata sort. This is the gnarliest branch because "retrieve both X and Y" requires a strategy for when each has its own ranking.

### 15.4 Revised success criteria

The feature ships if:

1. Phase A post-fix memory coverage ≥ 95%.
2. Phase B achieves +4 pts R@5 on the **retrievable subset** (71 questions, not 133).
3. No category regressing more than 0.01.
4. No LLM dependency.
5. No new D+ complexity methods.
6. Arithmetic-ceiling questions (~44) accepted as not-measurable under Recall@K; their evaluation is deferred to a RAG-mode run, tracked separately.

---

## 16. Acceptance of the arithmetic ceiling

Previously this was a risk item; the Phase A finding confirms it as a hard ceiling at the benchmark level:

- 44 questions have answers like "7 days" / "3 weeks" that appear as substrings in zero haystack memories.
- No retrieval change (range filter, entity scope, sort) can raise their Recall@K above 0.
- They can be answered by retrieval + arithmetic over `observed_at` timestamps — but the *answer string* itself is a computation, not a retrievable document.

Two disposition options, both non-blocking for P1-exp:

- **Option A:** inject a synthetic "answer memory" at query time when the arithmetic route fires. Feels like gaming the benchmark; probably off-spec for LongMemEval.
- **Option B:** Accept the ceiling and eval these via `longmemeval --rag` whenever we need the end-to-end score. Our research doc already recommends this.

Go with Option B. Don't spend build time on Option A.

---

## 17. Phase A conclusions and the committed Phase B path

Phase A measurement + the 4-cell label/strategy ablation pinned down the mechanism LongMemEval's temporal-reasoning category actually rewards. Phase B reshapes around that.

### 17.1 What Phase A actually proved

1. **Range extraction doesn't scale on conversational prose.** Even with the full 17-label GLiNER set, query coverage sits at ~3% on LongMemEval temporal-reasoning. Dropping to the focused 4-label `slim` preset (`event`, `location`, `person`, `temporal relative`) drops coverage to 0%. Tuning the label list further won't recover the bulk of questions — the text simply doesn't contain the calendar references.

2. **Memory-side coverage is a metadata problem, not an extraction problem.** The 30-line `observed_at` fallback (§14.2, shipped 2026-04-18) takes memory coverage from 2% → ~100%. The session envelope is the ground-truth clock.

3. **GLiNER per-call latency scales badly with label count.** 17 labels → p95 3589 ms. Splitting into two calls at the same label total → p95 1280 ms (−64%). Below ~10 labels, combined is fine. This is a calling-pattern concern, not a feature design concern.

### 17.2 The committed Phase B shape — three primitives

Replacing the five-branch intent classifier (§14.3) with three named primitives, each individually measurable:

| Primitive | Matches | Retrieval operation |
|---|---|---|
| **Explicit-range** | Query has a parseable date/range span (`since June`, `last week`, `between X and Y`) | Normalize spans → hard-filter candidates by `content_range` overlap |
| **Named-entity** | Query has subject entities (person, event, location) and **no** temporal signal | Retrieve entity-linked memories, return in relevance order; metadata timestamps already surface for downstream consumers |
| **Ordinal-sequence** | Query has subject entities **and** ordinal intent (`first`, `last`, `which came first`, `in what order`) | Retrieve entity-scoped memories, sort by `observed_at`, return as ordered set |

Arithmetic questions (~44 of 133) hit none of these primitives. They fall through to default retrieval and accept the Recall@K = 0 ceiling.

The three-primitive framing matters because **each ships and measures independently**. We don't wait for all three to light up the whole feature. Ordinal-sequence is likely the biggest single win on LongMemEval (COMPARE_FIRST + COMPARE_LAST + ORDINAL_FIRST + ORDINAL_LAST + ORDER_OF_EVENTS = ~50 questions).

### 17.3 Range-filter is a production feature, not a LongMemEval lever

Keep the infrastructure (the `memory_content_ranges` table, the normalizer, the GLiNER temporal labels) but **decouple its success criterion** from LongMemEval. Range-filter's expected production use:

- ADR / audit-log / decision-record retrieval ("show me decisions made in Q1 2024")
- Medical / care-timeline retrieval ("what happened in the two weeks before surgery")
- Structured notes / meeting minutes ("last week's standup")

On those workloads, query text typically *does* contain calendar references, and memory content has explicit dates threaded through. Measure range-filter there, not on LongMemEval.

### 17.4 GLiNER label-budget rule

From the ablation's p95 evidence, codify as a retrieval-pipeline helper:

```
extract_with_label_budget(text, labels, max_labels_per_call=10):
    if len(labels) <= max_labels_per_call:
        return extract_entities_gliner(text, labels=labels)
    # Split by label-group affinity: entity labels in one call,
    # temporal labels in a second call.  Results are concatenated.
    ...
```

Both the retrieval and ingestion pipelines call through this helper so the split behavior is consistent. Threshold 10 comes from the ablation: below it, `combined` wins; above it, the p95 tail dominates.

### 17.5 Arithmetic resolver — now in Phase B scope

**Scope update (2026-04-18):** the arithmetic resolver is no longer deferred. A fully LLM-free temporal story has to answer arithmetic questions ("how many days between X and Y") deterministically when the infrastructure already has the data. Ships as **Phase B.5**.

**What it does.** Takes an arithmetic-intent query, extracts the two anchor entities, retrieves their memories, pulls `observed_at` from each, computes the delta in the unit the question asks for, returns a structured answer.

**Infrastructure readiness (already shipped):**
- `observed_at` persisted on every memory
- Metadata fallback ensures ~100% memory-side temporal coverage
- Entity linkage memory → subject via graph
- GLiNER extracts entity names from queries

**New surface area:**
- Intent classifier distinguishes ARITHMETIC_BETWEEN (two anchors) / DURATION_SINCE / AGE_OF_EVENT (one anchor + reference_time) patterns
- New public method `MemoryService.compute_temporal_arithmetic(query, reference_time) → TemporalArithmeticResult | None` on the service
- New domain model `TemporalArithmeticResult` with `answer_value`, `unit`, `anchor_memories`, `confidence`
- `None` return when the pattern doesn't match or anchors can't be resolved — always a graceful fallback

**Explicit note on LongMemEval scoring.** This primitive does *not* improve Recall@K on the 44 arithmetic questions — the answer string ("7 days") is not a retrievable memory. Its value is product-facing:
- MCP/API consumers can ask arithmetic temporal questions without an LLM
- Dashboard can surface computed deltas for timelines
- If we later run `longmemeval --rag`, the resolver output can feed the judge context and make the arithmetic trivial

**Out of scope even with B.5:**
- Injecting synthetic "answer memories" to game Recall@K on arithmetic questions. Bad benchmark hygiene, shippable feature, but we don't do it.

### 17.6 Phase B success criteria (final)

Feature ships when **all** of the following pass:

1. **Explicit-range primitive:** measured separately on a seeded ADR-style fixture or production workload; ≥ 40% of queries with a calendar reference get the right result in top-5. LongMemEval is not the measurement surface.
2. **Named-entity primitive:** no regression on LongMemEval categories where the retired subject-scoped work already measured well.
3. **Ordinal-sequence primitive:** ≥ +4 pts R@5 on the 50-question ordinal subset of LongMemEval temporal-reasoning (ORDINAL + COMPARE + ORDER patterns). Zero regression elsewhere.
4. **Arithmetic resolver:** deterministic answers on a ≥10-question synthetic arithmetic fixture with 100% correctness (dates in, date math out, no LLM). No LongMemEval Recall@K claim.
5. GLiNER p95 latency with temporal labels active stays ≤ 1500 ms via the label-budget split.
6. Zero LLM dependency at query time (non-negotiable).
7. All primitives independently togglable; production deployments can opt into explicit-range without waiting for the others.

### 17.7 What's next in order

1. **Phase B.1 — label-budget utility.** Small, isolated. Lands first so later work inherits the safer calling pattern.
2. **Phase B.2 — ordinal-sequence primitive.** Biggest R@K lever; uses the same GLiNER-for-entities + sort-by-observed_at we already have pieces of. Single-subject first, then multi-subject.
3. **Phase B.3 — named-entity primitive.** Validates no-regression on non-temporal entity queries when ordinal-sequence is on.
4. **Phase B.4 — explicit-range primitive.** Wire the query-side range filter against the persisted `memory_content_ranges` with metadata fallback. Measured on a separate production-style benchmark, not LongMemEval.
5. **Phase B.5 — arithmetic resolver.** New `compute_temporal_arithmetic` method on `MemoryService` that consumes Phase B.2's entity-scoped retrieval and emits structured arithmetic answers. Closes the LLM-free temporal story.

Each sub-phase is a separate PR, separate measurement, separate go/no-go decision. Feature declared done only when all five ship and Phase B success criteria §17.6 are all green.
