# P2 Preference Extraction — Build Plan

**Status:** Pre-build planning
**Date:** 2026-04-17
**Prerequisite reads:** `docs/design-query-performance.md` §5, `docs/research-longmemeval-temporal.md`

---

## TL;DR

The original P2 design projected "+0.70 to +0.95 on
single-session-preference" based on the assumption that benchmark
answers would be short terms (*"blue"*, *"the subway"*) that synthetic
preference memories could surface via substring match. Sampling the
actual dataset revealed that's wrong.

**All 30 LongMemEval single-session-preference answers are 200–600
character prose rubrics** like *"The user would prefer responses that
suggest resources specifically tailored to Adobe Premiere Pro,
especially those that delve into its advanced settings…"* — zero of
them appear as substrings in any haystack memory. The category's
Recall@5 = 0.0000 is a **hard metric ceiling**, identical in
character to the arithmetic ceiling found in P1 (90/133
temporal-reasoning).

This doesn't kill P2's production value, but it does kill
LongMemEval-Recall@5 as the validation mechanism. Three options below.

---

## 1. What the data actually shows

Sampled all 30 preference questions, normalized both answer and
haystack content, checked for substring containment:

| Metric | Value |
|---|---|
| Total questions | 30 |
| Answer length (avg) | 391 chars |
| Answer length (min/max) | 201 / 604 chars |
| Answers found as substring in haystack | **0 / 30** |
| Ceiling under Recall@K | **hard** — cannot be crossed by any retrieval change |

Example:

> **Q:** *"Can you recommend some resources where I can learn more about video editing?"*
>
> **A:** *"The user would prefer responses that suggest resources specifically tailored to Adobe Premiere Pro, especially those that delve into its advanced settings. They might not prefer general video editing resources or resources related to other video editing software."* (263 chars)
>
> **Haystack contains:** *"I'm trying to learn more about some advanced settings for video editing with Adobe Premiere Pro, which I enjoy to use."* + assistant responses.

The phrase *"Adobe Premiere Pro"* is retrievable. The 263-char rubric
is not. The LongMemEval benchmark was clearly designed for a
RAG-mode evaluation where an LLM generates a recommendation and a
judge grades it against the rubric — not for retrieval-only
substring matching.

---

## 2. What P2 actually delivers (regardless of LongMemEval)

Even though Recall@5 can't score it, P2's production value is real:

### Mechanism

At ingest time, regex-scan content for preference statements. When
found, emit a **synthetic memory** alongside the original with
normalized vocabulary that bridges the query–document gap at
retrieval time.

Pattern families (from the design doc):

| Family | Example input | Synthetic memory content |
|---|---|---|
| Positive | *"I really like hiking"* | `User likes: hiking` |
| Negative | *"I can't stand cold weather"* | `User dislikes: cold weather` |
| Habitual | *"I usually take the subway"* | `User habit: takes the subway` |
| Difficulty | *"I've been having trouble sleeping"* | `User difficulty: sleeping` |
| Choice | *"I went with the vegetarian option"* | `User choice: vegetarian option` |

### Where it helps production

Same mapping as P1b — what LongMemEval undervalues, production
needs:

1. **Software-dev agent preferences** — *"I prefer async over threads"*,
   *"I don't like adding dependencies"*, *"I always use pytest"*. Ingested
   into NCMS as synthetic memories, they surface on later queries like
   *"What testing framework should I use for this?"*
2. **User preference continuity across sessions** — if the agent
   learned three sessions ago that the user prefers Python over
   JavaScript, a synthetic memory keeps that surfaceable even when
   the current session has no lexical overlap.
3. **RAG context enrichment** — when the LLM has to make a
   recommendation, retrieving synthetic preference memories gives it
   better context than the raw conversational transcripts alone.

None of this is measured by Recall@5 on LongMemEval's
single-session-preference category.

---

## 3. Measurement options

We now know Recall@5 is unmeasurable. Three honest measurement paths:

### Option A — LongMemEval RAG mode (most direct for this benchmark)

The harness already supports `--rag`:

```python
# benchmarks/longmemeval/run_longmemeval.py
--rag                         # enables LLM answer + LLM judge
--answer-model openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
--answer-api-base http://spark-ee7d.local:8000/v1
```

In RAG mode the judge uses question-type-specific prompts and the
output includes `Judge_single-session-preference` — a 0-1 score
against the prose rubric. That's the metric that actually measures
whether the system surfaces enough preference context for the LLM
to hit the rubric.

**Cost:** ~3-4 hours of runtime (500 × LLM round-trip × 2 for answer + judge). Requires
Spark DGX Nemotron to be running.

**Value:** Direct measurement of P2's LongMemEval impact. Also gives
us RAG-mode baselines for *every* category — including P1's 90
arithmetic questions that couldn't be scored retrieval-only.

### Option B — Synthetic software-dev benchmark

Build ~20 synthetic preference scenarios that mirror production:

```
- Agent stores: "I prefer async over threads"
- Query later: "What's the right concurrency model for this code?"
- Assertion: the preference memory (or a synthetic derivative) appears in top-5
```

**Cost:** ~1 hour to build, minutes to run.

**Value:** Directly measures the production case. Less authoritative
than LongMemEval but more targeted.

### Option C — Ship P2 behind a flag, measure in production

Enable `NCMS_PREFERENCE_EXTRACTION_ENABLED=true` on the hub and
observe dashboard metrics for synthetic-preference memory counts,
retrieval hit rates, and whether agents behave differently.

**Cost:** No additional benchmark work; production observation.

**Value:** Real usage data. Slowest to get definitive numbers.

**My recommendation:** do Option A (RAG baseline) first because it's
a one-time cost that also unlocks P1's arithmetic measurement. Then
build P2 and re-run RAG to see the delta. Add a small Option B
benchmark for CI regression protection.

---

## 4. Implementation — file-by-file

> **⚠ Regex extraction superseded (2026-04).**  §4.2 below proposes
> hand-written regex families (`_POSITIVE`, `_NEGATIVE`, `_HABITUAL`,
> `_DIFFICULTY`, `_CHOICE`) for preference detection.  We are
> replacing that approach with a learned intent + slot classifier —
> see `docs/intent-slot-distillation.md` (pre-paper) and
> `experiments/intent_slot_distillation/` (experiment folder).  The
> experiment will decide between three tiers (E5 zero-shot / NeMo
> Joint pre-trained / NeMo Joint user-fine-tuned).  Sections §4.2
> and §4.3 below stay as the *original* plan for reference but
> will be rewritten after the experiment converges on a winner.
>
> Summary of what changes:
> - `preference_extractor.py` → `intent_slot_extractor.py` behind
>   the `IntentSlotExtractor` protocol.
> - Regex families deleted.
> - New flag: `NCMS_INTENT_SLOT_BACKEND={zero_shot,pretrained,custom}`.
> - §5.1 test harness targets + §6 scale analysis carry forward
>   unchanged; only the extractor internals change.

### 4.1 Domain model

`src/ncms/domain/models.py`

Add a dataclass (or TypedDict) for extracted preferences. Purely
optional — we could also use plain dicts.

```python
class ExtractedPreference(BaseModel):
    category: Literal["likes", "dislikes", "habit", "difficulty", "choice"]
    subject: str                 # What the preference is about
    original_text: str           # Source sentence
    normalized: str              # "User likes: X" form
```

Domain purity preserved — no infrastructure deps introduced.

### 4.2 Extractor module

`src/ncms/infrastructure/extraction/preference_extractor.py` (new)

Follows the `gliner_extractor.py` pattern — stateless function,
no model loading, no external calls.

```python
def extract_preferences(text: str) -> list[ExtractedPreference]:
    """Scan first-person sentences for preference statements."""
```

Pattern families (compiled regex per family, applied in order):

```python
_POSITIVE = [
    re.compile(r"\bI\s+(?:really\s+)?(?:like|love|enjoy|prefer|adore)\s+(.+?)(?:\.|,|$)", re.I),
    re.compile(r"\bmy\s+(?:favorite|favourite)\s+(?:\w+\s+)?is\s+(.+?)(?:\.|,|$)", re.I),
    re.compile(r"\bI(?:'m| am)\s+(?:a\s+)?(?:big\s+)?fan\s+of\s+(.+?)(?:\.|,|$)", re.I),
]
_NEGATIVE = [...]
_HABITUAL = [...]
_DIFFICULTY = [...]
_CHOICE = [...]
```

Processing rules:
- Split text into sentences via existing `infrastructure/text/chunking.py`.
- First-person only (sentence must start with `I`/`I'm`/`I've` or similar) to avoid quoted speech.
- First match wins per sentence (no double-counting across families).
- Deduplicate by normalized form.

### 4.3 Ingestion hook

`src/ncms/application/ingestion/pipeline.py::run_inline_indexing`

After the existing entity extraction block (GLiNER), add:

```python
if self._config.preference_extraction_enabled:
    preferences = extract_preferences(memory.content)
    if preferences:
        synthetic_content = "User preferences:\n" + "\n".join(
            f"- {p.normalized}" for p in preferences
        )
        await self._svc.store_memory(  # or direct store.save, TBD
            content=synthetic_content,
            memory_type="fact",
            source_agent=memory.source_agent,
            domains=memory.domains,
            tags=["synthetic", "preference"],
            importance=memory.importance,
            observed_at=memory.observed_at,
            structured={
                "synthetic": True,
                "source_memory_id": memory.id,
                "preference_count": len(preferences),
                "categories": list({p.category for p in preferences}),
            },
        )
```

**Design question:** recursive call to `store_memory` vs. direct
`store.save_memory`. The recursive call goes through admission,
classification, and indexing — which re-runs preference extraction
on the synthetic memory itself. Easy infinite loop risk. Guard:
skip preference extraction when `tags` contains `"synthetic"`.

Matching update in `src/ncms/application/index_worker.py` for the
background path. Same guard.

### 4.4 Config

`src/ncms/config.py`

```python
preference_extraction_enabled: bool = False
```

Single toggle, no weights or thresholds needed.

### 4.5 Tests

Unit: `tests/unit/infrastructure/extraction/test_preference_extractor.py`
- Each pattern family with 3+ variants
- First-person gate ("He said he likes X" → not extracted)
- Negation ("I don't like X" → dislikes, not positive)
- Multi-preference sentences
- Quoted speech exclusion

Integration: `tests/integration/test_preference_pipeline.py`
- Store a memory containing `"I prefer async code in Python"`
- Verify a synthetic memory exists with tag `"synthetic"` and
  content containing `"User likes: async code"`
- Query `"What code style should I use?"` with the feature flag on
  and verify the synthetic memory surfaces in top-5

### 4.6 Fitness-function check

- `apply` ordinal-rerank-style: no new D+ methods
- Import boundaries: the new extractor imports domain models only
- `tests/architecture/` should pass unchanged

---

## 5. Risks

| Risk | Mitigation |
|---|---|
| False-positive extractions (quoted speech, hypotheticals, negation) | First-person gate; first-match-wins order; unit tests on adversarial inputs |
| Infinite synthetic-memory loop | Skip extraction when `"synthetic"` tag already present |
| Storage bloat | Synthetic memories are small (<200 chars typically); budget ~10% size increase at 100K memories |
| Stale preferences | Not addressed in P2; Phase 2 reconciliation already has supersession machinery — future P2b can link preference updates |
| LongMemEval Recall@5 won't move (ceiling) | Validate via RAG mode (Option A) or synthetic benchmark (Option B) |

---

## 6. Recommendation & asks

1. **Run LongMemEval `--rag` baseline first.** ~3-4 hour one-time cost,
   establishes judge-accuracy baselines for every category including
   the 90 arithmetic questions from P1 and the 30 preference rubrics
   from P2. Without it we're building P2 with no way to know if it
   worked.

2. **Then build P2 behind a flag** per §4. ~1 day end-to-end.

3. **Re-run `--rag`** with the flag on and compare
   `Judge_single-session-preference` delta.

If you want to move faster without waiting for the RAG baseline, we
can build P2 first and run one RAG eval at the end — risk is we get
a number but can't cleanly attribute the delta without a baseline.

Before-you-build asks:
- OK with running RAG mode (needs Spark DGX up)?
- OK scoping P2 measurement to RAG judge rather than Recall@5?
- Any production software-dev scenarios you want explicitly covered in the synthetic benchmark (Option B)?
