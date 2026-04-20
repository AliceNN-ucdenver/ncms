# P3 — SWE state-evolution benchmark

*Planning document · 2026-04-19 · follow-up to [tlg-validation-findings.md](tlg-validation-findings.md) §4.*

---

## 0. Why this exists

The ADR validation (`experiments/temporal_trajectory/`) proves TLG
wins 32/32 on the state-evolution axis, but the corpus is hand-
curated by the paper's author — 50 memories across three
subjects.  Reviewers will (correctly) want numbers on a corpus
that is:

1. **Not curated by TLG's author.**  Derived mechanically from a
   public dataset.
2. **Realistically sized.**  Thousands of memories, not fifty.
3. **Publicly reproducible.**  Other memory systems can run the
   same benchmark without knowing anything about NCMS.
4. **On-axis.**  State-change content with explicit supersedes /
   refines / causal structure — *not* conversational like
   LongMemEval.

SWE-bench Verified is the cleanest candidate: 500 GitHub issues
with their resolving PRs, across 12 mature Python projects.
Every issue is a multi-turn evolution (report → diagnose → fix
attempt → supersede → merge) with real timestamps and real
causal structure.

---

## 1. Deliverables

| Artifact | Location | Format |
|---|---|---|
| Labeled corpus | `benchmarks/swe_state/corpus/` | JSONL, one memory per line |
| Gold query set | `benchmarks/swe_state/queries.jsonl` | JSONL, ≈100 queries |
| Extraction harness | `benchmarks/swe_state/mine.py` | Python CLI |
| Labeler | `benchmarks/swe_state/label.py` | Python CLI (LLM-assisted) |
| Runner | `benchmarks/swe_state/run.py` | Python CLI, emits tables |
| Results write-up | `docs/tlg-swe-benchmark.md` | Markdown |
| Summary row | `benchmarks/results/swe_state/` | JSON + MD |

All artifacts checked in — the corpus + gold set are deterministic
(same labeler input ⇒ same JSONL output), so the benchmark is
byte-reproducible for anyone who downloads SWE-bench Verified.

---

## 2. Scale target

- **500 issues × mean 12 memories per issue ≈ 6,000 memories.**
  Large enough to sit past the "cold-cache 100-memory" regime
  and into the "warm-vocabulary" regime where TLG's shape cache
  matters.  Well inside the scale curve we already validated
  (10 k mean-dispatch = 13 ms).
- **≈100 gold queries,** hand-labeled.  Target distribution:
  - current_state: 20
  - causal_chain: 15
  - predecessor: 10
  - sequence: 10
  - interval: 8
  - ordinal_first / ordinal_last: 10
  - transitive_cause: 8
  - before_named: 5
  - concurrent: 5
  - range / retirement: 5
  - noise controls: 4
- **Balanced so no shape has fewer than 5 queries.**  Avoids
  single-shape variance dominating the aggregate.

---

## 3. Extraction pipeline

### 3.1 `mine.py` — raw thread → message tuples

Input: SWE-bench-Verified manifest (issue ID + resolving PR URL).

For each issue:
1. Fetch the issue thread (title, body, all comments) via
   GitHub API.  Cache locally.
2. Fetch the linked PR thread (title, body, review comments,
   commit messages).
3. Emit one tuple per message:
   ```jsonl
   {"issue": "django-5678",
    "message_id": "gh-comment-98765",
    "author": "carltongibson",
    "timestamp": "2023-07-15T14:32:00Z",
    "text": "...",
    "kind": "issue_body | issue_comment | pr_body | pr_review | commit_msg"}
   ```

### 3.2 `label.py` — tuples → memories + edges

Two-pass LLM labeler:

**Pass 1 (per-message classification).**  Prompt Nemotron Nano
with few-shot examples: `{declaration | retirement |
causal_link | ordinal_anchor | none}`.  Emit a candidate
memory per non-`none` message.

**Pass 2 (edge induction).**  For each issue thread, run the
retirement extractor from `src/ncms/domain/tlg/retirement.py`
against message pairs.  Emit `SUPERSEDES` / `REFINES` /
`CONFLICTS_WITH` edges with the retirement markers.

**Output schema** — identical to the ADR corpus schema so the
existing TLG dispatch works unchanged:
```jsonl
{"mid": "django-5678-m01",
 "subject": "django_5678",
 "content": "QuerySet.update() raises IntegrityError when ...",
 "observed_at": "2023-07-15T14:32:00Z",
 "entities": ["QuerySet.update", "IntegrityError"],
 "metadata": {
   "supersedes": [],
   "retires_entities": []
 },
 "kind": "declaration | retirement | causal_link | ordinal_anchor"}
```

**Calibration.**  Before we trust the labels on 500 issues,
hand-label 50 issues (≈600 messages) and compare.  Target
≥ 90 % message-level precision (we care about false positives
more than false negatives — a missed retirement marker is
annoying; a spurious one creates a phantom edge).

### 3.3 `run.py` — corpus + queries → table

Same signature as
`experiments/temporal_trajectory/run.py`.  Five strategies,
per-shape aggregation, overall top-5 and rank-1.

Extra: **latency distribution** per strategy (p50 / p95) since
we're now at a scale where BM25-only is ≈ 5 ms and the grammar
layer has cache effects.

---

## 4. Budget

**Two weeks, one engineer.**

| Week | Work |
|---|---|
| 1 | `mine.py`, `label.py`, calibration subset, full-corpus label run |
| 2 | Hand-write 100 gold queries, build `run.py`, produce write-up |

Slippage risks:
- **Labeler precision < 90 %.**  Mitigation: tighten the
  few-shot prompt, add a slot-validation pass, pay for one
  round of manual review on the worst-performing projects.
- **GitHub rate limiting.**  Mitigation: throttle to 1 req/s,
  cache aggressively, run mining overnight.

---

## 5. Evaluation matrix

The table we publish in `docs/tlg-swe-benchmark.md`:

|               | bm25 | bm25+date | entity_scope | path_rerank | **tlg** |
|---|---:|---:|---:|---:|---:|
| current_state | | | | | |
| causal_chain  | | | | | |
| predecessor   | | | | | |
| sequence      | | | | | |
| interval      | | | | | |
| ordinal_first | | | | | |
| ordinal_last  | | | | | |
| transitive_cause | | | | | |
| before_named  | | | | | |
| concurrent    | | | | | |
| range         | | | | | |
| noise         | | | | | |
| **overall top-5** | | | | | |
| **overall rank-1** | | | | | |
| **p50 latency (ms)** | | | | | |
| **p95 latency (ms)** | | | | | |

---

## 6. Why not just re-use LongMemEval?

Already answered in [tlg-validation-findings.md §3](tlg-validation-findings.md).
LongMemEval is conversational memory recall — the wrong axis.
The L1 inducer finds 0 subjects / 0 entity tokens on LME
content because there are no state declarations or retirement
markers.  TLG is inactive, not broken, on that corpus.  A
purpose-built state-evolution benchmark is the right axis.

---

## 7. Why SWE-bench rather than X?

- **Size.**  500 issues is the right scale (larger than we need
  for statistical significance, smaller than we need to spend
  months curating).
- **Structure.**  GitHub issue/PR threads have natural
  temporal + causal structure with real timestamps.
- **Reproducibility.**  SWE-bench Verified has fixed
  `instance_id`s and pinned repo SHAs.
- **Reusability.**  Other memory systems (Zep, Mem0, A-MEM,
  MemGPT) can consume the JSONL without special-casing.
- **Non-author curation.**  NCMS's authors don't control the
  GitHub history of astropy / django / sphinx.

The main alternatives considered:

| Candidate | Why rejected |
|---|---|
| Jira / Bugzilla archives | Fragmented, non-public, no standard format |
| Wikipedia revision history | Too drift-heavy, weak causal structure |
| Scientific paper citation networks | Wrong granularity (papers, not state changes) |
| ADRs scraped from public repos | Sparse, hard to find, already proven at small scale |

---

## 8. Out-of-scope for P3 (parked for later)

- **Cross-repo evolution.**  Some bugs span multiple repos (e.g.
  a numpy change causing a scipy regression).  Mining those
  chains is much harder and waits on a later iteration.
- **Non-Python SWE-bench.**  SWE-bench-Multilingual exists but
  it's smaller and less well-curated.  Start with the 500
  Python-verified set.
- **Human-factor labels.**  Who proposed the fix, whether the
  PR was contentious, review-round count — interesting
  signals but not on the TLG axis.

---

*This doc is a plan, not a shipped benchmark.  The shipped
benchmark produces `docs/tlg-swe-benchmark.md` plus the JSONL
artifacts listed in §1.*
