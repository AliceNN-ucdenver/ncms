# Research — LongMemEval Temporal Reasoning: What's Known, What's Tried

**Date:** 2026-04-18
**Scope:** What the LongMemEval paper actually does for temporal-reasoning,
directly quoted from the paper (arXiv 2410.10813, ICLR 2025). Aimed at
deciding whether NCMS's two failed rerank attempts are fixing the
wrong problem.

**Evidence base:** Direct text from the paper PDF. Quotes below are
verbatim unless marked "paraphrase."

---

## 1. What the paper actually proposes for temporal reasoning

Section 5.4 is titled **"Query: Time-aware query expansion improves
temporal reasoning."** It is the paper's entire answer to
temporal-reasoning performance. The recipe has three parts, and none
is a pool rerank:

### 1.1 Indexing-time: timestamp keys

> *"We introduce a simple yet effective **time-aware indexing and
> query expansion** scheme. Specifically, values are additionally
> indexed by the **dates of the events they contain**."* (§5.4)

So at ingest, each memory's key is augmented with the dates *inside
its content*, not just its metadata timestamp. If the memory says *"I
went to the hospital on June 5th"*, then "June 5th" becomes part of
the searchable key.

### 1.2 Retrieval-time: LLM-extracted time range filter

> *"During retrieval, an LLM M_T extracts a **time range** for
> time-sensitive queries, which is used to **filter out a large
> number of irrelevant values**."* (§5.4)

This is a **hard filter**, not a scoring bonus. "Last weekend" → LLM
→ concrete date range → candidates outside the range are dropped.
Their M_T is GPT-4o in the headline numbers; smaller LLMs underperform.

> *"Llama 8B, on the other hand, struggles to generate accurate time
> ranges, often hallucinating or missing temporal cues even with
> numerous in-context examples."* (§5.4)

### 1.3 Read-time: sort by timestamp for LLM presentation

> *"In the reading stage, the retrieved items are **always sorted by
> their timestamp** to help the reader model maintain temporal
> consistency."* (§5.1)

This is the "sort" that sounded like an ordinal rerank when read out
of context — **it is a presentation-order step for the LLM reader,
not a retrieval rerank**. The items sent to the reader are already
retrieved; sorting them by timestamp is a coherence aid for Chain-of-
Note reasoning, not a ranking signal.

---

## 2. Table 4 — the numbers they actually report

Direct from Table 4 (temporal-reasoning subset, LongMemEval_M, Recall@5):

| Key Setting | Value=Round R@5 | Value=Session R@5 |
|---|---|---|
| K = V (baseline) | 0.421 | 0.639 |
| K = V + Query Expansion (GPT-4o) | **0.526** (+0.105) | 0.654 |
| K = V + fact | 0.489 | 0.684 |
| **K = V + fact + Query Expansion (GPT-4o)** | **0.722** (+0.233) | **0.722** |
| K = V + fact + Query Expansion (Llama 8B) | 0.570 | 0.677 |

**Paper claim:** *"this simple design improves recall by an average
of 11.3% when using rounds as the value and by 6.8% when using
sessions as the value"* (§5.4).

Critical point the ablation makes visible:

- **Query expansion alone** (round-value, no fact expansion): **+10.5
  points R@5.**
- **Query expansion + fact-based key expansion** (full "Our Design"):
  **+23.3 points R@5.** The two optimizations compound.
- **Weak LLM for M_T (Llama 8B)** cuts the gain roughly in half.

---

## 3. What the paper does NOT do

- **No pool-wide observed_at rerank.** They don't sort the retrieval
  pool by date for "first/last" queries. The only sort-by-timestamp
  is read-stage presentation order.
- **No entity-scoped observed_at rerank.** Not discussed. Their
  approach is time-range filtering + fact-key indexing.
- **No ordinal intent classifier.** They don't branch on "first"
  vs. "last" vs. "between" vs. "range" — all temporal queries go
  through the same time-range extraction.
- **No multi-pathway retrieval merging at retrieval stage** (per
  footnote 4, they tried this and it *underperformed*): *"We also
  experimented with merging at the retrieval stage by combining the
  ranks from different pathways, but it underperformed compared to
  indexing-stage merging."*

This last point is the most damaging to the ranks-merge-style designs
NCMS has been building around (RRF, intent supplements, ordinal
rerank). The paper ran the experiment and it lost.

---

## 4. How "Our Design" in the paper is configured

From Table 2, their final recommended stack:

| Control Point | Value |
|---|---|
| Value (what's stored as a retrievable unit) | `round` (one user turn + one assistant turn) |
| Key (what's indexed) | `V + fact` (value + extracted user facts) |
| Query (what's searched against the index) | `question + time` (question + LLM-extracted time range) |
| Retrieval | `flat` (single pass, not multi-stage) |
| Time-aware | **Yes** |
| Reading | `CoN` (Chain-of-Note with JSON format) |

It's a different architecture than "BM25 + SPLADE + graph + rerank."
It's "round-sized values + fact-augmented keys + LLM query
expansion + time-range filter + CoN reading."

---

## 5. Other factual anchors from the paper

- **Ground truth is human-annotated answer locations** (§3.3): *"As
  LONGMEMEVAL contains human-annotated answer location labels,
  intermediate retrieval metrics can be easily calculated if the
  chat system exposes its retrieval results. We report Recall@k and
  NDCG@k, where k is the number of top items retrieved by the
  system."*
- **Primary end-to-end metric is an LLM judge** (§3.3): GPT-4o-
  2024-08-06 as evaluator, *"more than 97% agreement with human
  experts"*.
- **Seven question types**, not five (§3.2): single-session-user,
  single-session-assistant, single-session-preference, multi-
  session, knowledge-update, temporal-reasoning, abstention. The
  five in the intro are *abilities* (IE/MR/KU/TR/ABS); ABS is
  overlaid on top of the first four.
- **Temporal-reasoning questions carry timestamped answers** (§3.2):
  *"For questions involving temporal information, we then manually
  add timestamps to both the evidence sessions and the questions."*
- **No sub-taxonomy published for temporal-reasoning.** The 12-
  pattern regex classification NCMS built (COMPARE_FIRST,
  ORDINAL_FIRST, ARITH_BETWEEN, etc.) is not in the paper. The
  paper treats temporal-reasoning as one bucket.

---

## 6. What our two reranks got wrong, in the paper's frame

### 6.1 Pool-wide ordinal rerank (P1b-v1)

Does not exist in the paper. The paper's ordering-by-timestamp is
read-stage coherence, not retrieval reranking. Our version sorted
the retrieval pool by observed_at when the query contained "first"
or "last"; the paper never does this. Measured regression (−0.014
overall R@5, −0.333 on ORDINAL_FIRST) is consistent with doing
something the paper's experiments implicitly rejected.

### 6.2 Subject-scoped ordinal rerank (P1b-v2)

Also does not exist in the paper. Closer to correct in that it
filters to subject-linked candidates, but still reranks by
observed_at. The failure mode our diagnostic saw — "first X" being
*semantic* (first recommendation) not *chronological* (first
mention) — is entirely consistent with the paper's framing that
ordinal reasoning happens in the reader (LLM with CoN), not in
retrieval.

### 6.3 Range-filter temporal scoring (P1a, shipped)

This is the only NCMS temporal signal that directionally matches the
paper — but the paper implements it as a **hard filter** via LLM-
extracted ranges, not a **soft scoring bonus** via regex-parsed
ranges with observed_at weighting. Our P1a had zero measurable
LongMemEval impact; the paper's version moves Recall@5 by +10.5 to
+23.3 points. The delta between our version and theirs:

| Dimension | NCMS P1a | Paper §5.4 |
|---|---|---|
| Range source | regex parser on query | LLM extracts range |
| Date on memory | `observed_at` metadata only | content-dates extracted at ingest AND metadata |
| How applied | soft scoring weight | hard filter |
| Compound with key-expansion | no | yes (fact extraction in key) |

---

## 7. What the paper doesn't answer

1. **Recall@K decomposition by temporal sub-type.** Only aggregate
   Recall@5/10 on the whole temporal-reasoning subset. Our 12-
   pattern diagnostic is finer than anything they publish.
2. **Arithmetic-ceiling question count.** The paper acknowledges
   arithmetic questions exist but doesn't say how many of the 133
   have answers not present as substrings in any memory. Our 68%
   figure appears to be novel information.
3. **First-mention vs first-event formal distinction.** Not named
   in the paper. Implicit in their choice to sort at read-stage
   rather than retrieval-stage.
4. **Whether weak-LLM query expansion is actively harmful or just
   less helpful.** Table 4 row shows Llama 8B drops gains by ~half
   but is still above the no-expansion baseline.

---

## 8. Sources

- Wu et al., *LongMemEval: Benchmarking Chat Assistants on Long-Term
  Interactive Memory*, ICLR 2025 / arXiv:2410.10813 v2 (4 Mar 2025).
  Full paper text confirmed via direct PDF extraction on 2026-04-18.
  Quotes above are verbatim from that PDF.
- Repo: https://github.com/xiaowu0162/LongMemEval (referenced in
  paper abstract).
- Table 4 raw numbers transcribed directly from page 9 of the ICLR
  camera-ready.
