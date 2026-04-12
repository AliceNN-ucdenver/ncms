# NCMS Query Performance: LongMemEval Improvement Plan

**Status:** Proposed
**Date:** 2026-04-12
**Authors:** Shawn McCarthy, with analysis assistance from Claude (Anthropic)
**Context:** NCMS scores Recall@5=0.4680 on LongMemEval (500 questions across 6 categories). Competitive analysis of MemPalace (96-99% on this benchmark) identified five targeted improvements to close the gap. Each feature addresses a specific category weakness with measurable expected impact.

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

**Priority: 1 (highest)**
**Target category:** temporal-reasoning (0.2782 -> 0.80+)
**Estimated effort:** 3-4 days

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

Integrated into the scoring loop in `application/memory_service.py`, after the existing weighted combination and before final ranking.

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

**Step 1: Domain layer — `src/ncms/domain/temporal_parser.py` (new file)**

Pure module in the domain layer (zero infrastructure deps). Contains:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

@dataclass(frozen=True)
class TemporalConstraint:
    """Parsed temporal reference from a query."""
    target_date: datetime
    window_days: float
    expression: str
    confidence: float

# Compiled regex patterns — evaluated in specificity order
_PATTERNS: list[tuple[re.Pattern, Callable]] = [
    # "3 days ago", "yesterday", "5 weeks ago", "a couple months back"
    (re.compile(r"(\d+|a|a couple of?|a few|several)\s+(day|week|month|year)s?\s+(ago|back)", re.I), _parse_relative),
    # "last week", "last month", "last year"
    (re.compile(r"last\s+(week|month|year)", re.I), _parse_last),
    # "in January", "back in October", "in March 2025"
    (re.compile(r"(?:back\s+)?in\s+(January|February|...)\s*(\d{4})?", re.I), _parse_named_month),
    # "yesterday", "today", "the other day", "recently"
    (re.compile(r"\b(yesterday|today|recently|the other day)\b", re.I), _parse_vague),
    # ... additional patterns
]

def parse_temporal_expression(
    query: str,
    reference_time: datetime | None = None,
) -> TemporalConstraint | None:
    """Extract the most specific temporal reference from a query.

    Returns None if no temporal expression detected.
    reference_time defaults to datetime.utcnow() if not provided.
    """
```

Each pattern handler returns `(target_date, window_days, confidence)`. The parser tries all patterns, picks the most specific (smallest window). Ambiguous year resolution: prefer the most recent past occurrence relative to `reference_time`.

**Tests:** `tests/unit/domain/test_temporal_parser.py` — covers:
- Relative offsets: "3 days ago", "2 weeks ago", "a month ago", "a few weeks back"
- Named months: "in January" (year inference), "in October 2025" (explicit year)
- Vague: "recently" (7-day window), "the other day" (3-day window), "yesterday" (1-day)
- Fuzzy quantities: "a couple weeks" → 2, "a few months" → 3, "several days" → 5
- No temporal expression → returns None
- Multiple expressions → picks most specific
- Edge: reference_time at year boundary, month boundary

**Step 2: Scoring function — `src/ncms/domain/scoring.py` (extend existing)**

Add pure function alongside existing `activation()` and `admission_score()`:

```python
def temporal_proximity_score(
    memory_date: datetime,
    target_date: datetime,
    window_days: float,
) -> float:
    """Gaussian proximity score: 1.0 at target, decays with distance.

    score = exp(-(delta_days / window_days)^2)

    Uses observed_at if available, falls back to ingested_at.
    """
    delta = abs((memory_date - target_date).total_seconds()) / 86400.0
    return math.exp(-(delta / window_days) ** 2)
```

**Tests:** extend `tests/unit/domain/test_scoring.py` — covers:
- Memory exactly at target → 1.0
- Memory 1 window away → ~0.37 (1/e)
- Memory 2 windows away → ~0.02 (near zero)
- Edge: same day, same hour

**Step 3: Pipeline integration — `src/ncms/application/memory_service.py`**

Insert temporal parsing early in the search method, apply boost in the scoring loop:

```python
async def search(self, query: str, ..., domain: str = "", ...) -> list[ScoredMemory]:
    # ... existing intent classification, entity extraction ...

    # NEW: Parse temporal expression from query
    temporal_constraint = None
    if self._config.temporal_query_parsing_enabled:
        temporal_constraint = parse_temporal_expression(query, reference_time=reference_time)
        if temporal_constraint:
            self._emit_event("pipeline.search.temporal_parse", {
                "expression": temporal_constraint.expression,
                "target_date": temporal_constraint.target_date.isoformat(),
                "window_days": temporal_constraint.window_days,
                "confidence": temporal_constraint.confidence,
            })

    # ... existing BM25, SPLADE, RRF fusion, graph expansion ...

    # In scoring loop, after existing weighted combination:
    for memory_id, candidate in candidates.items():
        # ... existing: bm25_score, splade_score, graph_score, actr ...
        combined = w_bm25 * bm25_n + w_splade * splade_n + w_graph * graph_n  # existing

        # NEW: Temporal proximity boost
        if temporal_constraint and temporal_constraint.confidence >= confidence_threshold:
            memory_date = memory.observed_at or memory.created_at
            if memory_date:
                t_score = temporal_proximity_score(memory_date, temporal_constraint.target_date, temporal_constraint.window_days)
                combined += w_temporal * t_score
```

The temporal score enters the same per-query min-max normalization as other signals. Pipeline event emitted for dashboard observability.

**Step 4: Configuration — `src/ncms/config.py`**

```python
temporal_query_parsing_enabled: bool = False
temporal_proximity_weight: float = 0.4
temporal_proximity_timestamp: str = "observed_at"  # or "ingested_at"
```

**Step 5: Benchmark harness — `benchmarks/longmemeval/harness.py`**

Pass `reference_time` from the benchmark question metadata (LongMemEval provides a question timestamp) to the search call so temporal parsing resolves relative dates correctly against the question's time context, not wall-clock time.

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

**Priority: 2**
**Target category:** single-session-preference (0.0000 -> 0.90+)
**Estimated effort:** 2-3 days

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

**Step 2: Synthetic memory creation — `src/ncms/application/index_worker.py`**

In the index pipeline, after entity extraction and before episode linking:

```python
# index_worker.py — inside _index_memory()
async def _index_memory(self, memory: Memory) -> None:
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

**Index pipeline integration:** Parallel with BM25/SPLADE/GLiNER in `index_worker.py`:

```python
# Current parallel indexing (index_worker.py)
bm25_task = index_bm25(memory)
splade_task = index_splade(memory)
gliner_task = extract_entities(memory)
dense_task = index_dense(memory)  # NEW — runs in parallel
await asyncio.gather(bm25_task, splade_task, gliner_task, dense_task)
```

**Search pipeline integration:** Dense candidates enter RRF fusion alongside BM25 and SPLADE:

```python
# memory_service.py search pipeline
bm25_results = await self._index.search(query, top_k)
splade_results = await self._splade.search(query, top_k) if splade else []
dense_results = await self._dense.search(query, top_k) if dense else []  # NEW

# RRF fusion across all candidate sets
fused = rrf_fuse(bm25_results, splade_results, dense_results)

# Per-query normalization + weighted scoring
# w_bm25=0.6, w_splade=0.3, w_graph=0.3, w_dense=0.2 (new)
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

| Priority | Feature | Target Category | Questions | Expected Delta | Effort |
|----------|---------|----------------|-----------|----------------|--------|
| P1 | Temporal Query Parsing | temporal-reasoning | 133 | +0.42 to +0.57 | 3-4 days |
| P2 | Preference Extraction | single-session-preference | 30 | +0.70 to +0.95 | 2-3 days |
| P3 | Session-Level Storage | multi-session | 133 | +0.17 to +0.37 | 4-5 days |
| P4 | Dense Embedding Signal | all | 500 | +0.02 to +0.08 | 5-7 days |
| P5 | Query Sanitization | all | 500 | +0.01 to +0.05 | 1-2 days |
| P6 | Entity State False Positive Reduction | all | 500 | quality improvement | 4h |
| P7 | Admission Content-Type Prefix Classifier | all | 500 | quality improvement | 3h |
| P8 | User/Assistant Retrieval Asymmetry | single-session-assistant | 56 | +0.05 to +0.15 | 3h |

### 9.2 Items Adopted from Resilience Doc Phase 7

The following items were originally tracked in `docs/ncms-resilience-update.md` Phase 7 and are now owned by this design:

**P6: Entity State False Positive Reduction (4h)**
Current regex patterns in `memory_service.py` fire on YAML template fields like `status: not_started` in compliance checklists, `status: accepted` in ADR metadata headers, and `vulnerabilities: []` in empty YAML arrays. All 8 entity states in the latest NemoClaw run were false positives. Needs a less brittle approach — either a blocklist of common template patterns, or requiring state declarations to have a preceding entity mention within N sentences, or restricting to `store_memory` path (not `publish_document`).

**P7: Admission Content-Type Prefix Classifier (3h + 4h dataset)**
Admission scoring achieves 65.9% accuracy on 44 labeled examples. Per-category breakdown shows `atomic_memory` at 41.7% and `episode_fragment` at 50.0%. Adding a content-type prefix classifier (detect announcement, status update, question, etc.) before feature scoring would improve routing accuracy. Requires expanding the labeled dataset from 44 → 200+ examples. Files: `admission_service.py`, `benchmarks/tuning/`.

**P8: User/Assistant Retrieval Asymmetry (3h)**
MemPalace's two-pass approach: search user turns → find sessions → search assistant responses. Valuable for multi-agent queries like "what did the architect recommend?" where the answer is in an assistant turn but the query vocabulary matches user turns better. Implementation in `memory_service.py` — optionally tag memories with `role` metadata and boost role-matching candidates.

### 9.3 Phase Plan

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
