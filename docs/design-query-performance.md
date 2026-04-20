# NCMS Query Performance: LongMemEval Improvement Plan

> **⚠️  Partially superseded 2026-04-19.**  The P1 temporal
> track and the P2 preference track in this document have
> been replaced.  Retained here in-place because §6 (session
> storage), §7 (dense embedding), §8 (query sanitization), and
> the P6/P7/P8 ingestion-quality items are still the
> authoritative designs for those features.  See the banners
> on §4 and §5 for the redirects.
>
> **What superseded what:**
>
> | Old (this doc) | Superseded by | Why |
> |---|---|---|
> | §4 Temporal Query Parsing + Proximity Boost | **TLG** — [`temporal-linguistic-geometry.md`](temporal-linguistic-geometry.md), integration in [`p1-plan.md`](p1-plan.md) (closed 2026-04-19) | Regex proximity boost couldn't resolve intent (current / predecessor / causal / etc.); TLG's structural grammar hits 32/32 top-5 and rank-1 on the state-evolution axis. |
> | §5 Preference Extraction (regex synthetic docs) | **Intent-Slot Distillation** — [`intent-slot-distillation.md`](intent-slot-distillation.md), sprints [`intent-slot-sprints-1-3.md`](intent-slot-sprints-1-3.md), integration [`p2-plan.md`](p2-plan.md) | Regex families fail on phrasing drift, quoted speech, negation scoping, and maintenance burden.  P2 is now a LoRA multi-head BERT classifier unifying admission + state-change + topic + domain tagging + preference into one 2.4 MB adapter per deployment.  All 5 heads gate-PASS at F1 = 1.000 on gold across 3 reference domains. |
> | LongMemEval as headline benchmark | **SWE state-evolution benchmark** — [`p3-swe-state-benchmark.md`](p3-swe-state-benchmark.md) | LongMemEval is conversational recall, not state evolution.  TLG is inactive on it (L1 induction finds 0 subjects); axis mismatch is documented in [`tlg-validation-findings.md`](tlg-validation-findings.md) §3. |
>
> **Numbering note.**  "P1/P2/P3" in §9.1 below refers to the
> *priority ordering within this document*, not to the current
> project-level phase numbering.  Project-level P1 is TLG (closed);
> project-level P2 is intent-slot distillation (experiment live,
> integration pending); project-level P3 is the SWE benchmark
> curation.

---

**Status:** P0 complete; P1a shipped (zero LongMemEval impact); P1b retired; **P1-temporal-experiment retired (superseded by TLG)**; P1c cut.  P2 regex-preference retired (superseded by intent-slot distillation).  Features 3/4/5 and P6/P7/P8 still open.
**Date:** 2026-04-12 (initial); **updated 2026-04-19** after TLG ship + P2 pivot.
**Authors:** Shawn McCarthy, with analysis assistance from Claude (Anthropic)
**Context:** NCMS scores Recall@5=0.4680 on LongMemEval (500 questions across 6 categories). Competitive analysis of MemPalace (96-99% on this benchmark) identified five targeted improvements to close the gap. Each feature addresses a specific category weakness with measurable expected impact.

**What changed since the initial draft:**

1. **P0 (Code Quality) shipped.** See §9.3 Phase 0.  `memory_service.py`
   is now a 1,243-line composition root delegating to five focused
   pipeline packages (`scoring`, `retrieval`, `enrichment`, `ingestion`,
   `traversal`).  Three architectural fitness functions lock the
   structure against regression.  Details in `docs/fitness-functions.md`.
2. **P1 redesigned three times; final answer is TLG.**  The
   original "+0.42 to +0.57 delta" projection assumed LongMemEval's
   temporal category needed range-filter retrieval.  It doesn't —
   68% of the category's 133 questions are arithmetic with a hard
   Recall@K ceiling, and even the non-arithmetic ones need
   *structural* temporal understanding (current / predecessor /
   causal / sequence), not a scalar proximity boost.
   - **P1a** (range-filter scoring + bitemporal `observed_at`
     end-to-end) shipped cleanly.  LongMemEval delta: 0.0000.
     Infrastructure kept and consumed by TLG.
   - **P1b** (ordinal rerank over the candidate pool) built,
     measured, and **retired**.  Both pool-wide and subject-scoped
     variants regressed the benchmark.  Dead code removed.
   - **P1c** (multi-anchor retrieval) cut — diagnostic showed zero
     candidate-generation gap on LongMemEval.
   - **P1-temporal-experiment** (regex intent-router + hard-filter
     range query) built into `domain/temporal/` + `apply_range_filter`,
     then **retired 2026-04-19**.  The research follow-up (TLG) gave
     a strictly stronger structural framework.  The experiment's
     normalizer + range primitives are kept as the baseline
     `temporal_range_filter_enabled=true, tlg_enabled=false` path
     and consumed by TLG itself.  Doc is at
     [`docs/retired/p1-temporal-experiment.md`](retired/p1-temporal-experiment.md).
   - **TLG (Temporal Linguistic Geometry)** — shipped 2026-04-19.
     Structural-proof retrieval over subject chains with 11 intent
     shapes and a zero-confidently-wrong composition invariant.
     32/32 top-5 and rank-1 on the ADR state-evolution corpus vs.
     BM25 41%/16%.  Integration plan closed in
     [`docs/p1-plan.md`](p1-plan.md).
3. **P2 regex preference extraction retired.**  Pattern families
   (§5 below) fail on phrasing drift, quoted speech ("I love it
   when someone says 'I hate sprouts'"), negation scoping ("I used
   to love X, now I prefer Y"), and on any domain outside
   conversational English.  Replaced by the intent-slot
   distillation experiment, which trains a BERT joint classifier
   (intent + BIO slot) per-domain with synthetic data from
   template expansion + LLM labeling.  See
   [`docs/intent-slot-distillation.md`](intent-slot-distillation.md)
   + `experiments/intent_slot_distillation/`.
4. **Landing zones added.**  §9.5 maps each feature to its owning
   pipeline package, with a fitness-function checklist for PRs.
5. **Implementation plans refer to pipeline packages, not
   `memory_service.py`.**  Post-Phase 0, feature code lives in the
   matching pipeline; `memory_service.py` changes minimally.
6. **The "LLM-free at query time" pitch is a hard constraint, not a
   preference.**  Any design that needs an LLM at query time gets
   rejected at gate.  TLG respects this (pure grammar + L1/L2 index
   lookup).  Intent-slot distillation also respects this (inference
   against a fine-tuned BERT runs in ~5ms on MPS/CUDA).

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Baseline Results](#2-baseline-results)
3. [Gap Analysis](#3-gap-analysis)
4. [Feature 1: Temporal Query Parsing & Proximity Boost](#4-feature-1-temporal-query-parsing--proximity-boost)
5. [Feature 2: Preference Extraction & Synthetic Documents](#5-feature-2-preference-extraction--synthetic-documents)
6. [Feature 3: Session-Level Storage](#6-feature-3-session-level-storage)
7. [Feature 4: Dense Embedding Signal](#7-feature-4-dense-embedding-signal)
8. [Feature 5: Query Sanitization](#8-feature-5-query-sanitization)
9. [Priority & Phase Plan](#9-priority--phase-plan)
10. [Success Criteria](#10-success-criteria)
11. [Appendix: LongMemEval Benchmark Details](#appendix-longmemeval-benchmark-details)

---

## 1. Problem Statement

NCMS retrieval is optimized for factual recall from structured agent knowledge (ADRs, PRDs, state changes). LongMemEval tests a different workload: retrieval from multi-session human conversations where queries involve temporal references, personal preferences, and cross-session context. The current pipeline (BM25 + SPLADE + Graph spreading activation + ACT-R + cross-encoder reranking) has no mechanisms for temporal query understanding, preference modeling, or session-level context preservation. Three of six benchmark categories score below 0.35, and one scores exactly zero.

The gap is not in retrieval quality for factual content. SciFact nDCG@10=0.7206 demonstrates the pipeline works well for its designed workload. The gap is in missing capabilities that conversational memory requires: temporal reasoning, preference extraction, and session-level retrieval units.

---

## 2. Baseline Results

Current NCMS results on LongMemEval (500 questions, Recall@5):

| Category | Recall@5 | Questions | Assessment |
|----------|----------|-----------|------------|
| single-session-user | 0.8429 | 70 | Strong. Minor gains possible. |
| knowledge-update | 0.7436 | 78 | Good. Existing reconciliation helps. |
| single-session-assistant | 0.6429 | 56 | Moderate. Vocabulary gap likely. |
| multi-session | 0.3308 | 133 | Weak. Retrieval unit too small. |
| temporal-reasoning | 0.2782 | 133 | Weak. No temporal query support. |
| single-session-preference | 0.0000 | 30 | Zero. No preference model at all. |
| **Overall** | **0.4680** | **500** | |

The two largest categories (multi-session and temporal-reasoning, 133 questions each) are also the two weakest. Together with single-session-preference (30 questions), these three categories account for 296 of 500 questions and represent the primary improvement opportunity.

---

## 3. Gap Analysis

### 3.1 Temporal Reasoning (0.2782)

NCMS stores bitemporal metadata on every memory: `observed_at` (when the source event happened) and `ingested_at` (when NCMS stored it). The intent classification system (Phase 4) recognizes `historical_lookup` and `change_detection` intents. But there is no mechanism to parse temporal expressions from the query itself. When a user asks "What did we discuss 3 weeks ago?", the pipeline treats "3 weeks ago" as lexical tokens for BM25/SPLADE matching. No temporal filter or boost is applied. The bitemporal data exists but is unused at query time.

### 3.2 Preference Retrieval (0.0000)

NCMS has no concept of "preference" as a memory type or attribute. The content classifier (`domain/content_classifier.py`) routes content as either ATOMIC (facts, observations) or NAVIGABLE (structured documents). Preferences embedded in conversation ("I prefer dark mode", "I usually take the train") are stored as raw text with no special handling. At query time, a question like "What is the user's preferred mode of transportation?" has no vocabulary overlap with "I usually take the train", so BM25/SPLADE return nothing relevant.

### 3.3 Multi-Session Retrieval (0.3308)

NCMS stores individual memories (facts, fragments, document profiles). When ingesting conversational sessions, each message or observation becomes a separate memory. A query like "What did we discuss about authentication across our conversations?" must find and assemble multiple small fragments scattered across sessions. The retrieval unit is too granular. There is no session-level document that captures the full conversational context in a single retrievable artifact.

### 3.4 Vocabulary Gap

BM25 and SPLADE are both term-based (sparse) retrieval methods. SPLADE learns term expansions but operates in the same vocabulary space. Queries using different vocabulary than the stored content ("battery life problems" vs "phone dying fast") may not surface the right memories. Dense embeddings project both into a shared semantic space that handles vocabulary mismatch.

### 3.5 Query Contamination

When agents query NCMS, the query string sometimes contains system prompt fragments, tool call context, or other noise that dilutes the actual information need. No preprocessing step exists to extract the core question from a contaminated query.

---

## 4. Feature 1: Temporal Query Parsing & Proximity Boost

> **⚠️  Superseded 2026-04-19 by Temporal Linguistic Geometry (TLG).**
> The regex temporal parser + Gaussian proximity boost described
> in this section was implemented as `domain/temporal/parser.py`
> + `apply_range_filter` + `apply_ordinal_ordering`; it shipped
> as the `temporal_range_filter_enabled` path but delivered 0.0
> LongMemEval delta and could never resolve intent (current /
> predecessor / causal / sequence), only proximity.
>
> The current design is **TLG** — see
> [`temporal-linguistic-geometry.md`](temporal-linguistic-geometry.md)
> for the grammar architecture and
> [`p1-plan.md`](p1-plan.md) for the integration history (closed
> 2026-04-19).  TLG's grammar layer consumes the normalizer
> primitives from the retired experiment (kept active) and
> replaces the scoring boost with a structural dispatcher that
> hits 32/32 top-5 and rank-1 on the ADR state-evolution
> corpus.
>
> The normalizer + range filter remain in the codebase as the
> baseline `tlg_enabled=false, temporal_range_filter_enabled=true`
> deployment; slated for removal after TLG benchmark parity is
> demonstrated on the SWE corpus (see
> [`p3-swe-state-benchmark.md`](p3-swe-state-benchmark.md)).
> Retained in this document as historical record of the
> priority rank and the original problem framing.

**Priority: 1 (highest, original ranking)**
**Target category:** temporal-reasoning (0.2782 -> 0.80+)
**Estimated effort:** 3-4 days
**Status:** retired, superseded by TLG

### 4.1 Problem

133 temporal-reasoning questions score 0.2782. These queries contain explicit time references ("3 weeks ago", "last month", "in January", "recently") that should constrain or boost retrieval results based on when memories were created. NCMS has the metadata (`observed_at`, `ingested_at`) but no query-time mechanism to use it.

### 4.2 Design

A two-stage approach: (1) parse temporal expressions from the query into a target datetime range, then (2) apply a proximity boost to memories whose timestamps fall near that range.

**Stage 1: Temporal Expression Parser**

New pure module `src/ncms/domain/temporal_parser.py`. Regex-based extraction of temporal expressions from query text. No LLM dependency. Returns a `TemporalConstraint` dataclass:

```python
@dataclass
class TemporalConstraint:
    """Parsed temporal reference from a query."""
    target_date: datetime          # Center of the temporal reference
    window_days: float             # Uncertainty window (half-width)
    expression: str                # Original matched text
    confidence: float              # Parser confidence (0-1)
```

Supported patterns (ordered by specificity):

| Pattern | Example | Target | Window |
|---------|---------|--------|--------|
| Absolute date | "on March 15th" | March 15 of current/recent year | 1 day |
| Named month | "in January", "back in October" | 15th of that month | 15 days |
| Relative days | "3 days ago", "yesterday" | N days before now | 1 day |
| Relative weeks | "2 weeks ago", "last week" | N*7 days before now | 3 days |
| Relative months | "last month", "2 months ago" | N*30 days before now | 7 days |
| Vague recency | "recently", "the other day" | 7 days before now | 7 days |
| Ordinal reference | "first time we discussed", "initially" | Earliest matching memory | 30 days |

Regex patterns handle natural language variation: "a few weeks ago" -> 3 weeks, "a couple months back" -> 2 months, "early last month" -> 25th of previous month, "late January" -> January 25.

Edge cases:
- Multiple temporal expressions in one query: use the most specific (smallest window).
- Ambiguous year: prefer the most recent past occurrence. "In January" when current month is April means January of the current year. "In October" when current month is April means October of the previous year.
- No temporal expression detected: return `None`, skip boost entirely.

**Stage 2: Temporal Proximity Boost**

Integrated into the scoring loop in `application/scoring/pipeline.py` (post-Phase 0; this was `memory_service.py` in the original draft), after the existing weighted combination and before final ranking.

For each candidate memory with a non-null timestamp:

```
temporal_score = exp(-(|memory_date - target_date| / window_days)^2)
```

This is a Gaussian decay centered on `target_date` with width proportional to `window_days`. Memories exactly at the target get score 1.0; memories outside 2x the window get near-zero boost.

The temporal score is added as a new weighted signal:

```
final_score = (existing_combined_score) + w_temporal * temporal_score
```

Where `w_temporal` is configurable via `NCMS_TEMPORAL_PROXIMITY_WEIGHT` (default: 0.4).

The timestamp used is `observed_at` if present, falling back to `ingested_at`. This matches the user's mental model: "3 weeks ago" refers to when the event happened, not when NCMS indexed it.

### 4.3 Implementation Plan

**Current state: infrastructure already landed in earlier work.**  When
the doc was first drafted this was a greenfield build.  The temporal
plumbing was then implemented as part of Phase 4 (intent-aware
retrieval) before Phase 0 began, so the remaining work is validation
and tuning rather than construction.

**Already in place:**

| Component | Location | State |
|-----------|----------|-------|
| `TemporalReference` dataclass | `domain/temporal_parser.py` | Exists (`range_start`, `range_end`, `recency_bias`, `ordinal`). |
| `parse_temporal_reference(query)` | `domain/temporal_parser.py` | Exists.  Allowlisted in the complexity gate (CC=31 is regex dispatch density, not real complexity). |
| `compute_temporal_proximity(event_time, ref)` | `domain/temporal_parser.py` | Exists; imported by `scoring/pipeline.py`. |
| Query parse at search entry | `memory_service.search()` | Already calls `parse_temporal_reference(query)` when `NCMS_TEMPORAL_ENABLED=true` and passes the result as `temporal_ref` through retrieval and scoring. |
| Temporal signal computation | `scoring/pipeline.py::_compute_raw_signals` | Already computes `temporal_raw = compute_temporal_proximity(event_time, temporal_ref)` per candidate. |
| Per-query normalization | `scoring/pipeline.py::_normalize_and_combine` | Already normalizes `max_temporal` and combines `temporal_contrib = temporal_n * w_temporal`. |
| Config flags | `config.py` | `temporal_enabled: bool = False`, `scoring_weight_temporal: float = 0.2` already present. |
| Pipeline event | `memory_service.search()` | Already emits `temporal_parse` stage with `range_start`, `range_end`, `recency_bias`, `ordinal`. |

**Remaining work (1-2 days):**

**Step 1: End-to-end validation on LongMemEval (half day).**
Flip `NCMS_TEMPORAL_ENABLED=true` and run the benchmark.  Compare
per-category Recall@5 against the current baseline (temporal-reasoning
= 0.2782).  This tells us whether the existing patterns cover the
LongMemEval distribution before any tuning.

**Step 2: Weight grid search (half day).**
Sweep `NCMS_SCORING_WEIGHT_TEMPORAL` over `[0.1, 0.2, 0.3, 0.4, 0.5]`.
The current default 0.2 was chosen without benchmark pressure.  Use
SciFact as a non-regression guard — nDCG@10 must stay ≥ 0.70.  Pick
the weight that maximizes temporal-reasoning Recall@5 without
regressing the strong categories.

**Step 3: Pattern expansion where failures cluster (half day).**
Analyze the temporal-reasoning questions that still miss after Step 2.
Extract the temporal expressions that `parse_temporal_reference`
failed to recognize.  Add regex patterns to the parser for each
failure class.  The parser lives in `domain/`, so this is pure-function
work with unit tests in `tests/unit/domain/test_temporal_parser.py`.

**Step 4: Benchmark harness reference-time wiring (half day).**
LongMemEval provides a question timestamp, but the current harness
uses wall-clock time for relative expressions like "3 weeks ago".  If
the benchmark timestamps are historical, the resolution is wrong.
Add a `reference_time` parameter threaded from the harness metadata
through `memory_service.search()` (may require a minor API extension
— currently `parse_temporal_reference(query)` has no reference-time
override).  File: `benchmarks/longmemeval/harness.py` and optionally
`domain/temporal_parser.py`.

**Landing zone:** all work is in `domain/` (parser patterns) and
`benchmarks/longmemeval/` (reference-time wiring).  The scoring
pipeline does not need edits — the signal is already plumbed.

**Fitness check:** the complexity gate already allowlists
`parse_temporal_reference` because adding patterns to a regex
dispatch grows CC but not real complexity.  New patterns should keep
the allowlist honest: if a pattern requires non-trivial logic, extract
it into a helper function rather than bloating the main parser.

### 4.4 Expected Impact

Temporal-reasoning category should move from 0.2782 to 0.80+ because the temporal proximity boost will strongly favor memories from the correct time period. Many temporal-reasoning questions have a narrow correct window; even approximate parsing should surface the right memories.

Conservative estimate: 0.2782 -> 0.70 (if some temporal expressions are too ambiguous for regex).
Optimistic estimate: 0.2782 -> 0.85 (if most LongMemEval temporal queries use standard patterns).

Overall Recall@5 impact: +0.11 to +0.15 (133 questions * delta / 500).

### 4.5 Risks

- **Over-boosting**: A high temporal weight could suppress relevant non-temporal results. Mitigation: the boost is additive, not a filter. Non-temporal memories still compete on their retrieval scores. The Gaussian decay means only very close temporal matches get a meaningful boost.
- **Parse errors**: Regex may misparse complex expressions ("the week before last Christmas"). Mitigation: confidence field allows gating; low-confidence parses can be ignored or given reduced weight.
- **Timezone handling**: LongMemEval timestamps may be timezone-naive. Mitigation: treat all timestamps as UTC-equivalent; the proximity window (days) is coarse enough that timezone offsets (hours) are insignificant.

---

## 5. Feature 2: Preference Extraction & Synthetic Documents

> **⚠️  Superseded 2026-04-19 by Intent-Slot Distillation (P2 pivot).**
> The regex pattern families described below (positive /
> negative / habitual / difficulty / choice) fail on phrasing
> drift ("I've grown fond of X"), quoted speech ("I love it when
> someone tells me 'I hate X'"), negation scoping ("I used to
> love X, now I prefer Y"), third-person ("my sister prefers
> X"), double negation, sarcasm, and on any domain outside
> conversational English.  Maintenance burden scales linearly
> with every new phrasing a user invents.
>
> The replacement is an **intent + slot classifier per domain**,
> trained via BERT joint intent-head + BIO slot-head with
> synthetic data from template expansion plus LLM labeling.
> Three-tier design (E5 zero-shot / BERT pre-trained on SNIPS /
> user-fine-tuned) so teams can pick the accuracy / training-
> cost trade-off that matches their domain.
>
> - **Research plan:** [`docs/intent-slot-distillation.md`](intent-slot-distillation.md)
> - **Experiment code:** `experiments/intent_slot_distillation/`
>   (schemas, gold corpora across conversational / software_dev /
>   clinical domains, template expander, LLM labeler, four
>   methods: E5 zero-shot, GLiNER+E5, Joint BERT pre-trained,
>   Joint BERT fine-tuned, evaluation harness with intent F1 /
>   slot F1 / joint accuracy / latency / confidently-wrong rate).
>
> P2 status: experiment live, integration into
> `IntentSlotExtractor` protocol in `application/ingestion/`
> pending selection of the best method from the evaluation
> matrix.  Regex-preference code is **not shipped** — this
> section is historical only.

**Priority: 2 (original ranking)**
**Target category:** single-session-preference (0.0000 -> 0.90+)
**Estimated effort:** 2-3 days
**Status:** retired, superseded by intent-slot distillation

### 5.1 Problem

30 preference questions score exactly 0.0000. NCMS stores the original conversational text containing preferences but does not recognize or index them as preferences. When queried "What is the user's favorite color?", BM25/SPLADE search for "favorite color" and find nothing because the stored memory says "I really like blue, it reminds me of the ocean."

### 5.2 Design

**Ingest-time preference detection and synthetic document creation.**

At ingest time (in the index pipeline, after entity extraction), scan the memory content for preference indicators using regex patterns. When detected, create a synthetic preference document that normalizes the vocabulary for retrieval.

**Preference Detector**

New module `src/ncms/infrastructure/extraction/preference_extractor.py`.

Pattern families (each with multiple regex variants):

| Family | Patterns | Example Input | Extracted Preference |
|--------|----------|---------------|---------------------|
| Positive preference | "I prefer X", "I like X", "I love X", "I enjoy X", "my favorite is X" | "I really like hiking on weekends" | User likes: hiking on weekends |
| Negative preference | "I don't like X", "I hate X", "I dislike X", "I can't stand X", "I never X" | "I can't stand cold weather" | User dislikes: cold weather |
| Habitual | "I usually X", "I always X", "I tend to X", "I often X" | "I usually take the subway to work" | User habit: takes the subway to work |
| Difficulty | "I've been having trouble with X", "I struggle with X", "X is hard for me" | "I've been having trouble sleeping" | User difficulty: sleeping |
| Choice | "I'd rather X", "I chose X over Y", "I went with X" | "I went with the vegetarian option" | User choice: vegetarian option |

**Output model:**

```python
@dataclass
class ExtractedPreference:
    """A preference detected in memory content."""
    category: Literal["likes", "dislikes", "habit", "difficulty", "choice"]
    subject: str                 # What the preference is about
    original_text: str           # Source sentence
    normalized: str              # "User prefers X" / "User dislikes Y" form
```

**Synthetic Document Creation**

When one or more preferences are detected, create a synthetic memory alongside the original:

- **Content**: Normalized preference statements concatenated. E.g., "User preferences: User likes hiking on weekends. User usually takes the subway to work."
- **Type**: `preference` (new memory type, or use existing `fact` type with `preference` tag in metadata)
- **Metadata**: `{"synthetic": true, "source_memory_id": "<original_id>", "preference_category": "likes"}`
- **Agent/Domain**: Same as the source memory
- **Importance**: Same as source memory (preferences are not inherently more or less important)

The synthetic document ensures BM25/SPLADE can match "What does the user prefer for transportation?" against "User usually takes the subway to work" via lexical overlap on "user" and semantic expansion on "transportation/subway".

### 5.3 Implementation Plan

**Step 1: Extraction module — `src/ncms/infrastructure/extraction/preference_extractor.py` (new file)**

Follows the same pattern as `gliner_extractor.py` — stateless extraction function, no model loading:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class ExtractedPreference:
    category: Literal["likes", "dislikes", "habit", "difficulty", "choice"]
    subject: str
    original_text: str
    normalized: str  # "User prefers X" / "User dislikes Y"

# Pattern families — each captures a subject group
_POSITIVE = [
    re.compile(r"\bI\s+(?:really\s+)?(?:like|love|enjoy|prefer|adore)\s+(.+?)(?:\.|,|$)", re.I),
    re.compile(r"\bmy\s+(?:favorite|favourite)\s+(?:\w+\s+)?is\s+(.+?)(?:\.|,|$)", re.I),
    re.compile(r"\bI(?:'m| am)\s+(?:a\s+)?(?:big\s+)?fan\s+of\s+(.+?)(?:\.|,|$)", re.I),
]
_NEGATIVE = [
    re.compile(r"\bI\s+(?:don't|do not|never)\s+(?:like|enjoy|use)\s+(.+?)(?:\.|,|$)", re.I),
    re.compile(r"\bI\s+(?:hate|dislike|can't stand|avoid)\s+(.+?)(?:\.|,|$)", re.I),
]
_HABITUAL = [
    re.compile(r"\bI\s+(?:usually|always|typically|normally|often|tend to)\s+(.+?)(?:\.|,|$)", re.I),
    re.compile(r"\bI(?:'ve| have)\s+been\s+(.+?)(?:\.|,|$)", re.I),
    re.compile(r"\bI\s+(?:go with|stick with|stick to|opt for)\s+(.+?)(?:\.|,|$)", re.I),
]
_DIFFICULTY = [
    re.compile(r"\bI(?:'ve| have)\s+(?:been\s+)?having\s+(?:trouble|difficulty|issues)\s+(?:with\s+)?(.+?)(?:\.|,|$)", re.I),
    re.compile(r"\bI\s+(?:struggle|struggled)\s+with\s+(.+?)(?:\.|,|$)", re.I),
    re.compile(r"\b(.+?)\s+is\s+(?:hard|difficult|tough)\s+for\s+me", re.I),
]
_CHOICE = [
    re.compile(r"\bI(?:'d| would)\s+rather\s+(.+?)(?:\s+than\b|\.|,|$)", re.I),
    re.compile(r"\bI\s+(?:chose|went with|picked|selected)\s+(.+?)(?:\.|,|$)", re.I),
]

def extract_preferences(text: str) -> list[ExtractedPreference]:
    """Extract preference statements from text.

    Scans sentence-by-sentence. Only first-person statements
    (subject "I") to avoid detecting quoted/third-party preferences.
    Returns deduplicated list ordered by position in text.
    """
```

Processing:
1. Split text into sentences (re-use `infrastructure/text/chunking.py` sentence splitter)
2. For each sentence, try pattern families in order: positive → negative → habitual → difficulty → choice
3. First match wins per sentence (no double-counting)
4. Normalize: `"likes"` → `"User likes: {subject}"`, `"dislikes"` → `"User dislikes: {subject}"`, etc.
5. Deduplicate by normalized form (same preference stated twice = one entry)

**Tests:** `tests/unit/infrastructure/extraction/test_preference_extractor.py` — covers:
- Each pattern family with 3+ variants
- First-person only: "He said he likes X" → not extracted
- Negation handling: "I don't like X" → dislikes (not positive)
- Multi-preference sentences: "I like coffee and I hate tea" → 2 preferences
- Short/noisy input → empty list
- Quoted speech exclusion: `"I prefer X," she said` → not extracted (no leading "I")

**Step 2: Synthetic memory creation — `src/ncms/application/ingestion/pipeline.py`**

Post-Phase 0, the inline indexing path is `IngestionPipeline.run_inline_indexing`.
The background path is `index_worker.py::_IndexWorkerPool._run_worker`,
which calls the same indexing logic but on the queued task.  Either
path can emit preferences; the cleanest place is right after entity
extraction in both, so the synthetic memory benefits from the same
entity linking.

In the `run_inline_indexing` method, after the GLiNER merge and before
`build_cooccurrence_edges`:

```python
# ingestion/pipeline.py — inside run_inline_indexing(), after entity extraction
async def run_inline_indexing(self, memory, content, domains, ...):
    # ... existing: BM25 index, SPLADE index, GLiNER entity extraction ...

    # NEW: Preference extraction
    if self._config.preference_extraction_enabled:
        preferences = extract_preferences(memory.content)
        if preferences:
            # Build synthetic preference document
            pref_lines = [p.normalized for p in preferences]
            synthetic_content = "User preferences:\n" + "\n".join(f"- {line}" for line in pref_lines)

            await self._store.store_memory(
                content=synthetic_content,
                agent=memory.agent,
                importance=memory.importance,
                domains=memory.domains,
                tags=["synthetic", "preference"],
                metadata={
                    "synthetic": True,
                    "source_memory_id": memory.id,
                    "preference_count": len(preferences),
                    "categories": list({p.category for p in preferences}),
                },
            )
            self._emit_event("pipeline.index.preference_extraction", {
                "memory_id": memory.id,
                "preferences_found": len(preferences),
                "categories": list({p.category for p in preferences}),
            })

    # ... existing: entity linking, nodes, episodes ...
```

The synthetic memory goes through the normal store → index pipeline (BM25 + SPLADE + GLiNER). It gets its own memory_id, content hash (different from source), and HTMG node. The `"synthetic"` tag and metadata flag allow filtering in consolidation and dashboard.

**Step 3: Pipeline guards — synthetic memory exclusion**

```python
# consolidation_service.py — skip synthetic memories
if memory.metadata.get("synthetic"):
    continue

# Deduplication: synthetic content hash differs from source — no conflict

# Dashboard: show synthetic badge in memory detail
# http/static/index.html — if memory.tags includes "synthetic", show pill
```

**Step 4: Configuration — `src/ncms/config.py`**

```python
preference_extraction_enabled: bool = False
```

Single toggle. No weight tuning needed — preferences are stored as normal memories and compete in the standard retrieval pipeline. The vocabulary normalization ("User likes: hiking") is the mechanism, not a scoring boost.

**Step 5: Benchmark harness update**

No harness changes needed. Preference extraction runs at ingest time in the index pipeline. When the harness stores session messages via `store_memory()`, the index worker automatically extracts preferences and creates synthetic documents. The benchmark query then finds the synthetic document via BM25/SPLADE because "What does the user prefer for transportation?" matches "User usually: takes the subway to work" on "user" + "transportation"/"subway" overlap.

### 5.4 Expected Impact

single-session-preference should move from 0.0000 to 0.90+ because the normalized vocabulary in synthetic documents directly bridges the query-document vocabulary gap that causes zero recall today.

Conservative estimate: 0.0000 -> 0.70 (if some preference patterns are too nuanced for regex).
Optimistic estimate: 0.0000 -> 0.95 (if LongMemEval uses straightforward preference language).

Overall Recall@5 impact: +0.04 to +0.06 (30 questions * delta / 500).

### 5.5 Risks

- **False positive preferences**: Regex may detect preferences in quoted speech, hypotheticals, or negated contexts ("He said he prefers X" is not the user's preference). Mitigation: restrict to first-person statements; require "I" as subject in most patterns.
- **Storage bloat**: Each preference-bearing memory creates one additional synthetic memory. Mitigation: synthetic memories are small (normalized text, typically under 200 chars). At scale, this is negligible.
- **Stale preferences**: User preferences change over time. Mitigation: the reconciliation system (Phase 2) can detect conflicting preferences if enabled. Future enhancement: link preference synthetic memories to their source and supersede when the source is superseded.

---

## 6. Feature 3: Session-Level Storage

**Priority: 3**
**Target category:** multi-session (0.3308 -> 0.60+)
**Estimated effort:** 4-5 days

### 6.1 Problem

133 multi-session questions score 0.3308. These queries ask about topics discussed "across our conversations" or reference content from specific past sessions. NCMS stores individual memories (single facts, observations, state changes) but has no session-level artifact that captures the full conversational context of a session. When searching for "what we discussed about authentication", the pipeline must assemble scattered fragments rather than finding a single session summary.

### 6.2 Design

**Session summary documents, following the existing document_profile pattern.**

The existing document ingestion path (NAVIGABLE content classification) creates a rich vocabulary document profile alongside stored sections. Session-level storage reuses this same pattern for conversational sessions.

**Session Model**

A session is a bounded conversational interaction with a temporal start/end and a set of messages. The caller (benchmark harness, agent, or API) provides the session boundary.

New fields in the store API:

```python
async def store_session(
    self,
    messages: list[dict],       # [{"role": "user/assistant", "content": "..."}]
    session_id: str | None = None,
    agent: str = "unknown",
    domain: str = "general",
    metadata: dict | None = None,
) -> Memory:
```

**Session Document Construction**

From the raw messages, construct a session summary document:

1. **Header**: Session metadata (agent, domain, timestamp range, message count)
2. **Topic extraction**: Extract key topics/entities from the full conversation using GLiNER or keyword extraction
3. **Content summary**: Concatenate user messages (assistant messages typically contain reasoning, not facts). Truncate to 2,000 chars if needed.
4. **Vocabulary enrichment**: Include both the raw conversational vocabulary and topic-level keywords to maximize BM25/SPLADE findability.

The session document is stored as a `session_summary` type memory with high importance (7.0+) so it survives admission scoring. It is also indexed in the document store as a parent document, with individual significant messages stored as child sections (reusing the `document_service.py` infrastructure).

**Retrieval Behavior**

At search time, session summaries participate in normal BM25/SPLADE retrieval. Because they are vocabulary-dense (containing terms from across the full session), they match broader queries than individual fragments. At recall time, `_expand_document_sections()` can fetch individual messages from the session via the document store, providing both the session overview and specific message-level detail.

### 6.3 Implementation Plan

1. Add `store_session()` method to `application/memory_service.py`:
   - Accept message list + session metadata
   - Build session summary document (concatenate user messages, extract topics)
   - Store as `session_summary` type via document_service (creates profile + sections)
   - Return the session summary Memory

2. Extend `application/section_service.py` (or create `application/session_service.py`):
   - Session document builder logic
   - Topic extraction from conversation (reuse GLiNER entity extraction or keyword extraction)
   - Message selection heuristics (skip greetings, short acknowledgments)

3. Update the benchmark harness to call `store_session()` when ingesting conversational data:
   - LongMemEval provides sessions as message lists
   - Harness currently stores each message individually; add a session-level store call

4. Update MCP tools:
   - New `store_session` tool (or extend `publish_document` with a session mode)
   - Expose session_id in memory metadata for traceability

5. Configuration:
   - `NCMS_SESSION_STORAGE_ENABLED` (default: `false`)
   - `NCMS_SESSION_SUMMARY_MAX_CHARS` (default: `2000`)

### 6.4 Expected Impact

multi-session should move from 0.3308 to 0.60+ because session summaries provide a single high-recall retrieval unit for cross-session queries. Instead of needing 5 individual fragments from 5 different sessions, a single session summary can match the query.

Conservative estimate: 0.3308 -> 0.50 (if session summaries are too diluted for specific queries).
Optimistic estimate: 0.3308 -> 0.70 (if vocabulary-dense summaries consistently match multi-session queries).

Overall Recall@5 impact: +0.04 to +0.10 (133 questions * delta / 500).

### 6.5 Risks

- **Summary quality**: Naive concatenation of user messages may produce incoherent documents. Mitigation: sentence-level selection (skip short messages, greetings); topic-based sectioning.
- **Index dilution**: Session summaries are long, vocabulary-rich documents that could dominate BM25 results for broad queries. Mitigation: use the document_profile pattern (500-800 char profile for indexing, full content in document store). This is the same approach used for ADRs and PRDs.
- **Duplicate retrieval**: A query might match both a session summary and an individual fragment from the same session. Mitigation: deduplication in the scoring loop (if a fragment's source session is already in results, suppress the fragment). Alternatively, accept this as a feature: session summary provides context, fragment provides specificity.
- **Session boundary detection**: NCMS does not currently know where sessions start and end. Mitigation: require the caller to provide session boundaries. The benchmark harness has this information. For production use, agents can demarcate sessions via the API.

---

## 7. Feature 4: Dense Embedding Signal — E5-small-v2 Experiment

**Priority: 4 (experiment)**
**Target category:** all categories (broad improvement)
**Estimated effort:** 5-7 days (including experiment)

### 7.1 Problem

BM25 and SPLADE are both sparse/term-based retrieval methods. While SPLADE v3 (110M params, DistilBERT) learns term expansions, it still operates in discrete token space — activating related vocabulary terms but not capturing deep semantic similarity across vocabulary gaps. Dense embeddings project queries and documents into a shared continuous vector space where semantically similar content has high cosine similarity regardless of vocabulary overlap. "Battery life problems" and "phone dying fast" may not overlap in sparse space but are close in dense space.

### 7.2 Model Selection: E5-small-v2

After evaluating candidates, **intfloat/e5-small-v2** is the recommended first experiment:

| Model | Params | Dim | Disk | CPU Latency | Notes |
|-------|--------|-----|------|-------------|-------|
| all-MiniLM-L6-v2 | 22.7M | 384 | 90 MB | ~15ms | MemPalace baseline, mature |
| **intfloat/e5-small-v2** | **33.4M** | **384** | **133 MB** | **~16ms** | **Stronger BEIR scores, query/passage prefixes** |
| BAAI/bge-small-en-v1.5 | 33M | 384 | 133 MB | ~16ms | Comparable to E5-small |
| BAAI/bge-m3 | 568M | 1024 | 1.1 GB | ~75ms | Too heavy, sparse head weaker than SPLADE v3 |

**Why E5-small-v2 over all-MiniLM-L6-v2:**
- Slightly stronger BEIR benchmark scores
- Asymmetric encoding via prefixes (`"query: "` for queries, `"passage: "` for documents) — mirrors SPLADE v3's asymmetric `encode_query()`/`encode_document()` pattern we already use
- Same footprint (384-dim, ~16ms per query, 133 MB)

**Why NOT BGE-M3:**
- 568M params (4.3x the combined weight of SPLADE v3 + E5-small-v2)
- Single forward pass produces dense+sparse+ColBERT but the sparse head is *weaker* than dedicated SPLADE v3 for English (optimized across 100 languages + 3 objectives)
- XLM-RoBERTa vocabulary (250K tokens) is incompatible with our 30K BERT-vocab Tantivy sparse index — would require full index rebuild
- ~75ms CPU latency vs ~40ms for SPLADE + E5 sequential
- Cannot replace SPLADE v3 without regression; adds weight without proportional quality gain

**Cost of adding E5-small-v2 alongside existing models:**

| Model | Params | Already Loaded | Purpose |
|-------|--------|---------------|---------|
| SPLADE v3 | 110M | ✅ | Sparse semantic retrieval |
| GLiNER medium v2.1 | 209M | ✅ | Zero-shot NER |
| Cross-encoder MiniLM L-6 | 22M | ✅ (when enabled) | Reranking |
| **E5-small-v2** | **33M** | **New** | **Dense semantic retrieval** |
| **Total delta** | | | **+133 MB disk, +~200 MB runtime, +16ms/query** |

### 7.3 SPLADE vs Dense: Complementary, Not Redundant

SPLADE v3 and dense embeddings capture different semantic signals:

| Signal Type | SPLADE v3 | Dense (E5-small-v2) |
|-------------|-----------|---------------------|
| Representation | Sparse ~30K-dim (few hundred non-zero) | Dense 384-dim (all dimensions active) |
| Semantic mechanism | Learned term expansion ("car" → "vehicle", "automobile") | Continuous vector similarity |
| Vocabulary gap handling | Bridges via related terms in BERT vocab | Bridges via geometric proximity in embedding space |
| Weakness | Still term-level — unrelated vocabulary = no overlap | Loses lexical precision, can hallucinate similarity |
| Example miss | "feeling blue" ↔ "depression" (no term path) | "JWT authentication" ↔ "token-based auth" (SPLADE catches this) |

They are complementary: SPLADE handles term-adjacent gaps (synonyms, morphological variants), dense handles conceptual gaps (paraphrases, distant semantic similarity). The experiment will measure whether the incremental gain justifies the added complexity.

### 7.4 Experiment Design

**Phase 1: Isolated E5-small-v2 baseline (1 day)**

Run LongMemEval with E5-small-v2 as the *only* retrieval signal (no BM25, no SPLADE, no graph). This establishes the dense-only baseline and identifies which categories benefit most from dense retrieval. Compare category-by-category against the sparse-only baseline (0.4680).

**Phase 2: Fusion experiment (1-2 days)**

Add E5-small-v2 as a fourth retrieval signal alongside BM25 + SPLADE + Graph. Test weight configurations:

```
# Experiment grid (holding BM25=0.6, SPLADE=0.3, Graph=0.3 fixed)
w_dense = [0.1, 0.2, 0.3, 0.4]
```

Measure:
- Per-category Recall@5 delta vs sparse-only baseline
- Overall Recall@5 delta
- Latency impact (p50, p95)
- Which specific questions flip from miss → hit (vocabulary gap analysis)

**Phase 3: Weight rebalancing (1 day)**

If dense adds value, run a grid search across all four weights to find the optimal combination. Use SciFact as a non-regression constraint (nDCG@10 >= 0.70).

**Success gate:** Dense is kept if it improves overall Recall@5 by >= 0.02 without regressing any individual category by more than 0.01, and latency p95 stays under 50ms.

### 7.5 Implementation

**New module:** `src/ncms/infrastructure/indexing/dense_engine.py`

```python
class DenseEngine:
    """Dense embedding retrieval via sentence-transformers.

    Uses E5-small-v2 with asymmetric encoding:
    - Documents encoded with "passage: " prefix
    - Queries encoded with "query: " prefix
    """

    def __init__(self, model_name: str = "intfloat/e5-small-v2") -> None: ...
    def _ensure_model(self) -> None: ...  # Lazy load, MPS/CUDA auto-detect

    async def index(self, memory_id: str, text: str) -> None:
        """Embed and store a document vector."""
        ...

    async def search(self, query: str, top_k: int = 50) -> list[tuple[str, float]]:
        """Embed query and return top-K by cosine similarity."""
        ...

    async def remove(self, memory_id: str) -> None: ...
    async def rebuild(self, memories: list[tuple[str, str]]) -> None: ...
```

**In-memory index:** numpy cosine similarity for stores < 50K memories. At 384-dim × float32, 50K memories = ~75 MB. For larger stores, add hnswlib (embeddable, no external service). Rebuild on startup from SQLite `dense_embeddings` table.

**Embedding storage:** New SQLite table in V5 migration:

```sql
CREATE TABLE IF NOT EXISTS dense_embeddings (
    memory_id TEXT PRIMARY KEY,
    vector BLOB NOT NULL,  -- float32 array, 384 * 4 = 1,536 bytes per row
    model TEXT NOT NULL DEFAULT 'intfloat/e5-small-v2'
);
```

**Index pipeline integration:** Parallel with BM25/SPLADE/GLiNER in
`application/ingestion/pipeline.py::IngestionPipeline.run_inline_indexing`
(and matching code in `index_worker.py` for the background pool path):

```python
# ingestion/pipeline.py — run_inline_indexing
bm25_task = self._do_bm25(memory)
splade_task = self._do_splade(memory)
gliner_task = self._do_gliner(memory, domains)
dense_task = self._do_dense(memory)  # NEW — runs in parallel
await asyncio.gather(bm25_task, splade_task, gliner_task, dense_task)
```

**Search pipeline integration:** Dense candidates enter RRF fusion
alongside BM25 and SPLADE in `application/retrieval/pipeline.py::retrieve_candidates`:

```python
# retrieval/pipeline.py — retrieve_candidates
bm25_results = await asyncio.to_thread(self._index.search, query, top_k)
splade_results = await self._splade_task()  # existing gated call
dense_results = await self._dense_task()    # NEW gated call

# RRF fusion across all candidate sets (rrf_fuse is already on RetrievalPipeline)
fused = self.rrf_fuse(bm25_results, splade_results, dense_results)
```

The scoring weight and per-query normalization for the new signal land
in `application/scoring/pipeline.py` alongside the existing BM25/SPLADE/
graph/temporal signals:

```
# scoring/pipeline.py — _compute_raw_signals adds dense_raw per candidate
# _normalize_and_combine adds max_dense and dense_n * w_dense to combined
# w_bm25=0.6, w_splade=0.3, w_graph=0.3, w_dense=0.2 (new, tuneable)
```

### 7.6 Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `NCMS_DENSE_EMBEDDING_ENABLED` | `false` | Enable dense embedding retrieval |
| `NCMS_DENSE_MODEL` | `intfloat/e5-small-v2` | Dense embedding model |
| `NCMS_DENSE_TOP_K` | `50` | Dense candidates per search |
| `NCMS_SCORING_WEIGHT_DENSE` | `0.2` | Dense weight in combined score |

### 7.7 Expected Impact

Broad improvement across all categories, strongest where vocabulary gaps are the bottleneck.

Conservative estimate: +0.02 overall (vocabulary gap is secondary to temporal/preference/session gaps).
Optimistic estimate: +0.08 overall (vocabulary gap more widespread than expected, especially in single-session-assistant and multi-session).

### 7.8 Risks

- **Redundancy with SPLADE**: SPLADE v3 may already capture most vocabulary gaps. The experiment phase explicitly tests this — if dense-only baseline scores *lower* than sparse-only on categories where SPLADE is strong, redundancy is confirmed and we save the complexity.
- **Latency**: +16ms per query (E5-small-v2 on CPU). On MPS: ~5ms. Within the 50ms p95 budget.
- **Memory footprint**: +133 MB disk (model weights) + ~75 MB runtime (50K vectors in memory). Within the 200 MB total budget.
- **Breaks "no dense vectors" design principle**: NCMS was deliberately designed without dense vectors (CLAUDE.md Key Design Decision #1). This is a philosophical concession. Mitigation: feature-flagged off by default; the core pipeline remains sparse-first. Dense is additive, not a replacement. If the experiment shows < 0.02 improvement, we drop it and the principle stands.

---

## 8. Feature 5: Query Sanitization

**Priority: 5**
**Target category:** all categories (defensive improvement)
**Estimated effort:** 1-2 days

### 8.1 Problem

When agents query NCMS, the query string may contain system prompt contamination, tool call context, chain-of-thought reasoning, or other noise:

```
You are a helpful assistant. Based on the conversation history, answer the following question:
What did the user say about their travel preferences?
Please be concise and accurate.
```

The actual information need is "What did the user say about their travel preferences?" but BM25/SPLADE also match on "helpful assistant", "conversation history", "concise and accurate" -- terms that appear in many memories and dilute retrieval precision.

### 8.2 Design

A preprocessing step at the top of the search pipeline that extracts the core question from potentially contaminated input.

**Extraction heuristics (applied in order, first match wins):**

1. **Last question extraction**: Find the last sentence ending in `?`. If it exists and is >10 chars, use it as the query.
2. **Quoted question extraction**: Find text in quotes that ends in `?`. Use the longest such match.
3. **Instruction stripping**: Remove common system prompt patterns:
   - Lines starting with "You are", "Based on", "Please", "Remember to", "Make sure"
   - Lines containing "assistant", "conversation history", "the following"
   - Leading/trailing whitespace and empty lines
   - Use remaining text as query
4. **Tail extraction**: If query is >500 chars and no question mark found, use the last 200 chars (the actual question is usually at the end of a prompt).
5. **Passthrough**: If query is <200 chars, skip all processing (short queries are unlikely to be contaminated).

**Module:** `src/ncms/domain/query_sanitizer.py`

```python
def sanitize_query(raw_query: str) -> str:
    """Extract the core information need from a potentially contaminated query.

    Returns the cleaned query string. If no cleaning is needed or possible,
    returns the original query unchanged.
    """
```

Pure function, no dependencies, no LLM. Deterministic and fast.

### 8.3 Implementation Plan

1. Create `src/ncms/domain/query_sanitizer.py`:
   - `sanitize_query(raw_query: str) -> str`
   - Unit tests with contaminated query examples from real agent interactions

2. Integrate at top of `application/memory_service.py` search/recall pipeline:
   - Call `sanitize_query()` before any retrieval
   - Log the original and sanitized query for observability
   - Emit pipeline event with both versions

3. Configuration:
   - `NCMS_QUERY_SANITIZATION_ENABLED` (default: `false`)

### 8.4 Expected Impact

Modest but broad improvement. Query sanitization removes noise that causes false positive BM25/SPLADE matches, improving precision across all categories.

Conservative estimate: +0.01 overall (benchmark queries may already be clean).
Optimistic estimate: +0.05 overall (if contamination is widespread in the benchmark harness).

### 8.5 Risks

- **Over-stripping**: Aggressive sanitization could remove query terms that are actually informative. Mitigation: the heuristics are conservative (prefer last question, not aggressive regex replacement). Passthrough for short queries.
- **Benchmark-specific**: LongMemEval may use clean queries, making this feature irrelevant for the benchmark. However, it has clear value for production agent queries where contamination is common.

---

## 9. Priority & Phase Plan

### 9.1 Priority Ranking

Features ordered by expected impact per unit effort, targeting the largest category weaknesses first:

| Priority | Feature | Target Category | Questions | Expected Delta | Effort | Current State |
|----------|---------|----------------|-----------|----------------|--------|---------------|
| P0 | Code Quality Refactoring | all | — | enabler | **Done** | Shipped (Phase 0 complete) |
| P1a | Range-filter temporal + bitemporal model | temporal-reasoning | 133 | 0.000 | **Done** | Shipped; infrastructure only (observed_at/reference_time), no LongMemEval gain. |
| ~~P1b~~ | ~~Ordinal rerank (post-retrieval)~~ | ~~temporal-reasoning~~ | — | ~~negative~~ | ~~Retired~~ | Both variants (pool-wide and subject-scoped) regressed LongMemEval; paper §5.4 does not rerank the retrieval pool. Dead code removed. |
| ~~P1-exp~~ | ~~Time-aware indexing + hard-filter range query~~ | ~~temporal-reasoning~~ | ~~133~~ | ~~(not measured in isolation)~~ | ~~3-5 days~~ | **Retired 2026-04-19** — superseded by TLG.  Doc moved to [`docs/retired/p1-temporal-experiment.md`](retired/p1-temporal-experiment.md).  The normalizer + `apply_range_filter` primitives remain in tree as the baseline `tlg_enabled=false` path. |
| **P1-TLG** | **Temporal Linguistic Geometry** | **state-evolution (on-axis)** | **32** (ADR corpus) | **32/32 top-5 and rank-1** vs. BM25 41%/16% | **2 weeks** | **✅ Shipped 2026-04-19.**  11-intent structural grammar, zero-confidently-wrong composition invariant, scale curve ≤50 ms through 10 k memories.  See [`temporal-linguistic-geometry.md`](temporal-linguistic-geometry.md), [`p1-plan.md`](p1-plan.md), [`tlg-validation-findings.md`](tlg-validation-findings.md). |
| ~~P1c~~ | ~~Multi-anchor retrieval~~ | ~~arithmetic~~ | — | ~~0~~ | ~~Cut — diagnostic showed zero Recall@K upside~~ | Not pursuing |
| ~~P2 (regex)~~ | ~~Regex Preference Extraction + synthetic docs~~ | ~~single-session-preference~~ | ~~30~~ | ~~+0.70 to +0.95~~ | ~~2-3 days~~ | **Retired 2026-04-19** — superseded by intent-slot distillation.  Pattern families don't generalize across phrasing drift / quoted speech / negation scope / non-English domains. |
| **P2-IS** | **Intent-Slot Distillation** (LoRA multi-head BERT classifier) | preference + domain intent + admission + state-change + topic | per-domain (3 reference domains) | **F1 = 1.000 on gold across all 5 heads; adversarial intent +0.33 vs baseline** | ~12 working days (sprints 0–3 shipped); Sprint 4 integration ~2 weeks | **Experiment complete — 3 gate-PASS adapters shipped.**  Research: [`intent-slot-distillation.md`](intent-slot-distillation.md).  Sprint findings: [`intent-slot-sprints-1-3.md`](intent-slot-sprints-1-3.md).  Integration plan: [`p2-plan.md`](p2-plan.md).  Code: `experiments/intent_slot_distillation/`.  Architecture: one forward pass → {intent, slot, topic, admission, state_change} with confidence gating.  Replaces five brittle code paths (admission regex, state-change regex, LLM topic labeller, user-supplied `Memory.domains`, regex preference extractor) with one 2.4 MB LoRA adapter per deployment. |
| P3 | Session-Level Storage | multi-session | 133 | +0.17 to +0.37 | 4-5 days | Greenfield, still open |
| P4 | Dense Embedding Signal | all | 500 | +0.02 to +0.08 | 5-7 days | Greenfield (research + build), still open |
| P5 | Query Sanitization | all | 500 | +0.01 to +0.05 | 1-2 days | Greenfield, still open |
| P6 | Entity State False Positive Reduction | all | 500 | quality improvement | 4h | Targets existing regex in ingestion/, still open |
| P7 | Admission Content-Type Prefix Classifier | all | 500 | quality improvement | 3h + 4h dataset | Extends existing admission_service, still open |
| P8 | User/Assistant Retrieval Asymmetry | single-session-assistant | 56 | +0.05 to +0.15 | 3h | Greenfield, still open |
| **PX-bench** | **SWE state-evolution benchmark** (reusable artifact) | state-evolution | ~6k memories / ~100 queries | measurement surface, not a retrieval feature | 2 weeks | **Planned.**  Curation plan: [`p3-swe-state-benchmark.md`](p3-swe-state-benchmark.md).  Gates paper M3 ("confidently-wrong = 0 at scale"). |

**P1 status — twice revised based on empirical measurement.**  The
original estimate (+0.42 to +0.57 on temporal-reasoning) assumed
LongMemEval's temporal questions needed range-filter retrieval.  The
first revision (§9.1 row P1a/P1b) assumed they needed ordinal rerank.
Both were wrong in different ways; the paper's actual recipe is
neither.

Timeline:

1. **P1a — range-filter scoring + bitemporal `observed_at` end-to-end
   (shipped).**  Correct infrastructure (memories, L1/L2 nodes, and the
   scoring signal all carry `observed_at`; `search()` accepts
   `reference_time`).  Zero LongMemEval delta, because the signal is a
   *soft scoring boost* where the paper uses a *hard filter*, and
   because content-date extraction at ingest isn't wired.
2. **P1b — ordinal rerank (built, measured, retired).**  Two variants
   both regressed LongMemEval.  Pool-wide rerank: COMPARE_FIRST R@5
   0.414 → 0.276, ORDINAL_FIRST 0.333 → 0.000.  Subject-scoped
   variant: 0.414 → 0.345, 0.333 → 0.167.  Better than pool-wide,
   still worse than doing nothing.  Root cause: **the paper explicitly
   does not rerank the retrieval pool by `observed_at`**.  §5.4
   time-awareness lives in indexing and query-range filtering; the
   §5.1 timestamp sort is read-stage LLM presentation order, not a
   retrieval rerank.  Paper footnote 4 also reports that merging
   rank pathways at retrieval stage underperformed merging at
   indexing stage — exactly the design we kept reaching for.  All
   rerank code and tests have been removed.  Full quotes and
   numerical evidence in `docs/research-longmemeval-temporal.md`.
3. **P1c — multi-anchor retrieval (cut).**  Every retrievable
   LongMemEval answer already lands in top-50 today; there is no
   candidate-generation gap to close.
4. **P1-temporal-experiment (built, shipped as baseline, then
   superseded by TLG 2026-04-19).**  Implemented the paper's
   §5.4 recipe under NCMS's zero-LLM-at-query-time constraint:
   GLiNER `date` label ingest-side, `temporal_parser.py` +
   `dateparser` query-side, hard range filter
   (`apply_range_filter`).  Kept in-tree as the
   `temporal_range_filter_enabled=true, tlg_enabled=false`
   baseline; consumed as a primitive by TLG's `range` intent.
   Design doc retired to
   [`docs/retired/p1-temporal-experiment.md`](retired/p1-temporal-experiment.md).
5. **P1-TLG (shipped 2026-04-19).**  Structural grammar over
   subject chains with 11 intent shapes; zero-confidently-wrong
   composition invariant with BM25.  32/32 top-5 and rank-1 on
   the ADR state-evolution corpus.  See
   [`temporal-linguistic-geometry.md`](temporal-linguistic-geometry.md)
   and the closed [`p1-plan.md`](p1-plan.md).
6. **Arithmetic questions (~90 of 133).**  Answer strings absent from
   every source memory — a retrieval-only ceiling at Recall@K = 0.
   Scoring these requires RAG + LLM judge; parked as a
   measurement-stack concern, not a retrieval one.

§4.3 below retains the original P1a design for reference; the
retired temporal experiment is at
[`docs/retired/p1-temporal-experiment.md`](retired/p1-temporal-experiment.md);
the current temporal path is TLG — see
[`temporal-linguistic-geometry.md`](temporal-linguistic-geometry.md).

### 9.2 Items Adopted from Resilience Doc Phase 7

The following items were originally tracked in `docs/ncms-resilience-update.md` Phase 7 and are now owned by this design:

**P6: Entity State False Positive Reduction (4h)**
Current regex patterns in `memory_service.py` fire on YAML template fields like `status: not_started` in compliance checklists, `status: accepted` in ADR metadata headers, and `vulnerabilities: []` in empty YAML arrays. All 8 entity states in the latest NemoClaw run were false positives. Needs a less brittle approach — either a blocklist of common template patterns, or requiring state declarations to have a preceding entity mention within N sentences, or restricting to `store_memory` path (not `publish_document`).

**P7: Admission Content-Type Prefix Classifier (3h + 4h dataset)**
Admission scoring achieves 65.9% accuracy on 44 labeled examples. Per-category breakdown shows `atomic_memory` at 41.7% and `episode_fragment` at 50.0%. Adding a content-type prefix classifier (detect announcement, status update, question, etc.) before feature scoring would improve routing accuracy. Requires expanding the labeled dataset from 44 → 200+ examples. Files: `admission_service.py`, `benchmarks/tuning/`.

**P8: User/Assistant Retrieval Asymmetry (3h)**
MemPalace's two-pass approach: search user turns → find sessions → search assistant responses. Valuable for multi-agent queries like "what did the architect recommend?" where the answer is in an assistant turn but the query vocabulary matches user turns better. Implementation lands in `application/retrieval/pipeline.py` (role filter in `expand_candidates`, or a new role-aware filter pass) and optionally `application/scoring/pipeline.py` (role-match bonus as a scoring signal). Requires `role` metadata at ingest — wire through `application/ingestion/pipeline.py`.

### 9.3 Phase Plan

**Phase 0: Code Quality (Complete)**

Phase 0 delivered more than complexity reduction — it established a
durable architecture under `src/ncms/application/` that every
subsequent feature lands into.

Headline metrics:

- `memory_service.py`: 3,522 → 1,243 lines, MI C (0.00) → **A (20.73)**.
  It is now a composition root holding public API and delegating to
  five focused pipelines.
- 14 D-grade methods in application code → **0**, enforced by a
  fitness function test.
- Average cyclomatic complexity across 1,089 analyzed blocks:
  **A (3.49)**.
- 918 tests pass, including 122 architectural fitness tests.

The five extracted pipeline packages each have a single responsibility:

| Package | File | Owns |
|---------|------|------|
| `application/scoring/` | `pipeline.py` | Multi-signal scoring (BM25 + SPLADE + Graph + ACT-R + CE + temporal + recency), two-pass normalize + combine. |
| `application/retrieval/` | `pipeline.py` | Candidate discovery (parallel BM25/SPLADE/GLiNER + RRF), selective cross-encoder rerank, expansion (entity resolution, PMI query expansion, graph expansion, intent supplement). |
| `application/enrichment/` | `pipeline.py` | Recall bonuses (state/historical/event expansion) and RecallResult context decoration (entity states, episode membership, causal chains, document sections). |
| `application/ingestion/` | `pipeline.py` | Pre-admission gates (dedup, size, classification), admission scoring, inline indexing (BM25/SPLADE/GLiNER/entity linking/co-occurrence), HTMG node creation (L1/L2), reconciliation, episode assignment, deferred contradiction. |
| `application/traversal/` | `pipeline.py` | HTMG traversal (top-down, bottom-up, temporal, lateral) and topic-map clustering. |

Shared utilities live outside any pipeline:

- `application/label_cache.py` — `load_cached_labels(store, domains)` is
  a free async function used by `memory_service.recall`, retrieval, and
  ingestion (was a callable passed through three constructors, now
  imported directly).

Three **fitness functions** under `tests/architecture/` run on every
test pass and lock the architecture against regression:

1. **Complexity gate** (`test_complexity_gate.py`, 101 parametrized
   cases) — fails if any method in `src/ncms/` (except demo/) regresses
   to D+ cyclomatic complexity. One documented allowlist entry
   (`parse_temporal_reference`, a regex dispatch parser).
2. **Import boundaries** (`test_import_boundaries.py`, 20 parametrized
   cases) — enforces domain purity (domain → no application/
   infrastructure imports) and pipeline isolation (the five packages
   may not import each other or `memory_service`).
3. **Dead code** (`test_dead_code.py`) — Vulture at 80% confidence
   must return clean against `.vulture_whitelist.py`.

The full rationale is in `docs/fitness-functions.md`.

**What this means for the features below.** Each P1-P8 feature has a
specific landing zone in one of the five pipelines. New code must pass
the complexity gate (no D-grade methods) and the import-boundary test
(don't reach across pipelines). The table in §9.5 maps each feature to
its owning pipeline.

**Phase A: Quick Wins (Week 1)**
- P1: Temporal Query Parsing & Proximity Boost
- P2: Preference Extraction & Synthetic Documents
- P5: Query Sanitization

These three features are pure code (no LLM, no new ML models), address the two lowest-scoring categories, and can be implemented and tested independently. Combined expected impact: Recall@5 from 0.4680 to 0.63-0.75.

**Phase B: Session Context & Quality (Week 2)**
- P3: Session-Level Storage
- P6: Entity State False Positive Reduction
- P7: Admission Content-Type Prefix Classifier

Session storage requires integration with document_service. Entity state and admission fixes are independent quality improvements. Expected cumulative impact: Recall@5 to 0.67-0.80.

**Phase C: Research & Signals (Week 3)**
- P4: Dense Embedding Signal (research first, implement if justified)
- P8: User/Assistant Retrieval Asymmetry

Research phase may conclude SPLADE already covers the vocabulary gap, in which case P4 is deprioritized. P8 is a targeted fix for single-session-assistant queries. Expected cumulative impact if all implemented: Recall@5 to 0.70-0.85.

### 9.4 Dependency Graph

```
P5 (Query Sanitization)     -- no dependencies, deploy first
    |
P1 (Temporal Parsing)       -- no dependencies, can parallel with P2
    |
P2 (Preference Extraction)  -- no dependencies, can parallel with P1
    |
P3 (Session Storage)        -- depends on document_service being stable
    |
P6 (Entity State FP)        -- independent, parallel with P3
    |
P7 (Admission Classifier)   -- independent, parallel with P3/P6
    |
P4 (Dense Embeddings)       -- depends on research phase results
    |
P8 (User/Asst Asymmetry)    -- independent, parallel with P4
```

All features are feature-flagged and independent. Any subset can be deployed without the others.

### 9.5 Landing Zones — Where Each Feature Adds Code

Post-Phase 0, each feature has a specific owning pipeline.  New code
lives in that pipeline's package; `memory_service.py` should change
minimally (only to wire a new constructor arg or emit a new config).

| Feature | Pipeline / New Module | Specific Functions Touched |
|---------|----------------------|----------------------------|
| P1a Range-filter temporal | **scoring/pipeline.py** + **domain/temporal_parser.py** — shipped | `_compute_raw_signals` (temporal_raw), `_normalize_and_combine` (max_temporal). Also landed the bitemporal `observed_at` / `reference_time` wiring end-to-end. Ships the infrastructure; no LongMemEval delta. |
| ~~P1b Ordinal rerank~~ | **retired** | Both variants (`apply_ordinal_rerank`, `apply_subject_scoped_ordinal_rerank`) regressed LongMemEval. Paper §5.4 uses time-aware indexing + hard-filter range query rather than post-retrieval rerank. All rerank code and tests removed 2026-04-18. |
| ~~P1-temporal-experiment~~ (shipped, superseded) | **infrastructure/extraction/gliner_extractor.py** (`date` label), **domain/temporal/** (normalizer + intent classifier), **retrieval/pipeline.py::apply_range_filter**, **infrastructure/storage/migrations.py** (content-range table) | Shipped as the baseline `temporal_range_filter_enabled=true` path; retired from the primary retrieval flow on 2026-04-19 when TLG landed.  See [`docs/retired/p1-temporal-experiment.md`](retired/p1-temporal-experiment.md). |
| **P1-TLG** (current temporal path) | **domain/tlg/** (grammar layer — retirement extractor, L1 vocab, L2 markers, content markers, aliases, zones, query parser, shape cache), **application/tlg/** (dispatch, VocabularyCache, ShapeCacheStore, induction), **application/memory_service.search** (composition hook), **infrastructure/storage/sqlite_store.py** (`find_memory_ids_by_entity`, schema v12 `grammar_shape_cache`) | See [`temporal-linguistic-geometry.md`](temporal-linguistic-geometry.md). |
| ~~P2 Preference (regex)~~ | ~~`preference_extractor.py` regex families~~ | **Retired 2026-04-19** — superseded by P2-IS (intent-slot distillation).  No regex extractor was ever shipped. |
| **P2-IS** Intent-Slot Distillation | `experiments/intent_slot_distillation/` (**shipped**, 3 gate-PASS adapters); Sprint 4 integration lands in **domain/protocols.py** (`IntentSlotExtractor`), **infrastructure/extraction/intent_slot/** (3 backends + adapter loader), **application/ingestion/pipeline.py** (extractor call after content classification), schema v13 (5 new `memories` columns + `memory_slots` + `intent_slot_adapters`), new `ncms train-adapter` / `adapter-promote` CLIs, dashboard `intent_slot.extracted` event. | LoRA multi-head BERT; unifies admission scoring + state-change detection + topic labelling + domain tagging + preference extraction.  Plan: [`p2-plan.md`](p2-plan.md). |
| P3 Session Storage | **application/session_service.py** (new, follow `section_service.py` pattern); **ingestion/pipeline.py** (session entry point) | New `store_session()` on `MemoryService`, delegates to `session_service.build_session_profile()`, which calls `document_service.publish_document` under the hood. |
| P4 Dense Embeddings | **infrastructure/indexing/dense_engine.py** (new); **retrieval/pipeline.py** (`retrieve_candidates` — add parallel retriever); **scoring/pipeline.py** (new signal in raw + combine) | Three touchpoints: index writer in ingestion's `run_inline_indexing`, parallel retriever in retrieval, signal term in scoring. |
| P5 Query Sanitization | **domain/query_sanitizer.py** (new); **memory_service.search / recall** (preprocessing) | Single pure function called at top of `search()` and `recall()`. No pipeline internals touched. |
| P6 Entity State FP | **ingestion/pipeline.py::_detect_and_create_l2_node** | Tighten regex list, add template-pattern blocklist, require co-located entity mention. Edits inside one private method. |
| P7 Admission Classifier | **application/admission_service.py** (extend); **ingestion/pipeline.py::gate_admission** (no change needed) | Add content-type prefix classifier to `compute_features`; admission scoring already consumes features. |
| P8 User/Asst Asymmetry | **retrieval/pipeline.py** (role filter in `expand_candidates` or new filter pass); **scoring/pipeline.py** (optional role bonus) | Tag memories with `role` metadata at ingest, boost role-matched candidates in retrieval or scoring. |

**Fitness-function checklist for every feature PR:**

- Runs `pytest tests/architecture/` green (complexity, import
  boundaries, dead code).
- No new method above cyclomatic C (≤ 20).  If a natural implementation
  is D, extract helpers until each is ≤ C.
- New files follow the pipeline-package boundary rule — no imports
  *from* another pipeline package, only *from* domain, infrastructure,
  or sibling services outside the five pipelines.
- Feature flag defaults to `false` in `config.py` so a release can ship
  with the feature dark and turn it on via env var.

---

## 10. Success Criteria

### 10.1 Per-Category Targets

| Category | Current | Phase A Target | Phase B Target | Stretch |
|----------|---------|---------------|---------------|---------|
| single-session-user | 0.8429 | 0.85 | 0.87 | 0.90 |
| knowledge-update | 0.7436 | 0.75 | 0.78 | 0.80 |
| single-session-assistant | 0.6429 | 0.68 | 0.72 | 0.80 |
| multi-session | 0.3308 | 0.35 | 0.55 | 0.70 |
| temporal-reasoning | 0.2782 | 0.70 | 0.75 | 0.85 |
| single-session-preference | 0.0000 | 0.80 | 0.85 | 0.95 |
| **Overall** | **0.4680** | **0.63** | **0.70** | **0.80** |

### 10.2 Non-Regression Constraints

- SciFact nDCG@10 must remain >= 0.70 (currently 0.7206). All new features are feature-flagged; SciFact benchmark runs with new features disabled should produce identical results.
- Search latency p95 must remain < 50ms for stores under 10K memories. Temporal parsing adds <1ms. Dense embedding search adds ~10ms when enabled.
- Memory footprint increase must be < 200MB for stores under 100K memories (dense embeddings are the primary concern).

### 10.3 Measurement

Run the LongMemEval benchmark after each feature lands:

```bash
# Baseline (all new features disabled)
uv run python -m benchmarks.run_longmemeval

# After Phase A (temporal + preference + sanitization)
NCMS_TEMPORAL_QUERY_PARSING_ENABLED=true \
NCMS_PREFERENCE_EXTRACTION_ENABLED=true \
NCMS_QUERY_SANITIZATION_ENABLED=true \
uv run python -m benchmarks.run_longmemeval

# After Phase B (add session storage)
NCMS_SESSION_STORAGE_ENABLED=true \
... \
uv run python -m benchmarks.run_longmemeval
```

Per-category results tracked in `benchmarks/results/longmemeval/` with timestamped JSON and markdown reports, consistent with existing benchmark infrastructure.

---

## Appendix: LongMemEval Benchmark Details

LongMemEval evaluates long-term memory retrieval from conversational sessions. 500 questions across 6 categories:

- **single-session-user** (70q): Facts stated by the user in a single session. Tests basic factual recall.
- **single-session-assistant** (56q): Facts stated by the assistant in a single session. Tests recall of AI-generated content.
- **single-session-preference** (30q): User preferences expressed in a single session. Tests preference understanding.
- **knowledge-update** (78q): Facts that were updated or corrected across sessions. Tests state reconciliation.
- **multi-session** (133q): Topics discussed across multiple sessions. Tests cross-session retrieval.
- **temporal-reasoning** (133q): Questions requiring temporal context ("What did we discuss 2 weeks ago?"). Tests time-aware retrieval.

The benchmark ingests conversational sessions (user/assistant message pairs) into the memory system, then evaluates retrieval quality on held-out questions. Recall@K measures whether the correct source memory appears in the top K results.

MemPalace achieves 96-99% on this benchmark by using dense embeddings, session-level storage, and LLM-based query understanding. NCMS's sparse-first approach trades some semantic flexibility for interpretability and zero-LLM query-time operation. The features in this design close the gap where specific capabilities are missing (temporal, preference, session) without abandoning the sparse-first philosophy.
