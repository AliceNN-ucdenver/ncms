# MSEB — Memory State-Evolution Benchmark

A reusable, multi-domain benchmark for systems that must answer
state-evolution queries over long message streams.  Every domain
follows the same schema (`schema.py`); a shared harness computes
per-shape retrieval metrics.  New domains plug in with a miner +
a labeler + an optional gold-query YAML.

> **Pre-paper §4.2 note.**  MSEB is the empirical evaluation for
> NCMS's TLG-grounded retrieval.  Results across MSEB-SWE and
> MSEB-Clinical exercise the same 14 intent shapes on two
> disjoint domains — the minimum bar to argue "TLG is
> domain-agnostic".

---

## 1. Why "state-evolution"?

Traditional RAG benchmarks (BEIR, LongMemEval, MemoryAgentBench)
answer **what** questions — "given this context, retrieve the
passage that contains X".  They do not probe **how a subject's
state changes over time**:

- What was the *initial* diagnosis / hypothesis / implementation?
- What came *before* the current state was adopted?
- Which finding *caused* the revision?
- Is the current state still the most recent, or has it been
  superseded?

Those are the queries that actually matter when an agent uses
memory to reason.  MSEB scores retrieval against exactly those
questions.  The taxonomy is TLG's (Temporal Linguistic Geometry,
`docs/temporal-linguistic-geometry.md`) — 14 intent shapes that
cover the observed question distribution on both clinical and
software-dev corpora.

## 2. Schema

Every domain produces two JSONL files:

- `corpus.jsonl` — typed `CorpusMemory` rows (see `schema.py`)
- `queries.jsonl` — typed `GoldQuery` rows

Both are ingested by the shared harness
(`benchmarks/mseb/harness.py`, forthcoming) via the standard
`store_memory` / `recall` API.  No bespoke adapters per domain.

| Field on `CorpusMemory` | Purpose |
| --- | --- |
| `mid` | stable ID (`<domain>-<subject>-m<NN>`) |
| `subject` | subject-chain ID — all memories with the same subject form one trajectory |
| `content` | natural-language message body (no paraphrase at mining time) |
| `observed_at` | ISO-8601 of the original event |
| `metadata.kind` | `MemoryKind` label: declaration / retirement / causal_link / ordinal_anchor / none |
| `metadata.supersedes` | list of `mid`s this memory supersedes |

| Field on `GoldQuery` | Purpose |
| --- | --- |
| `qid` | `<domain>-<shape>-<NNN>` |
| `shape` | one of 14 `IntentShape` values |
| `text` | the query in natural language |
| `subject` | which chain the answer lives in |
| `gold_mid` / `gold_alt` | accept set for rank-1 / top-5 grading |

Full rationale: `docs/p3-state-evolution-benchmark.md` §2.

## 3. Domains

MSEB instantiations live in sibling packages:

- `benchmarks/mseb_swe/` — SWE-bench Verified (500 real GitHub
  issues + resolving PRs across 12 Python projects).  Exercises
  state-evolution shapes on technical content.
- `benchmarks/mseb_clinical/` — PMC Open Access case reports
  filtered on MeSH `Diagnosis, Differential` / `Diagnostic
  Errors`.  Exercises state-evolution on diagnostic-revision
  narratives.
- `benchmarks/mseb_convo/` — LongMemEval conversational corpus
  + hand-authored preference queries.  Exercises the P2
  `intent_head`'s four preference sub-types (positive /
  avoidance / habitual / difficult) that SWE + Clinical don't
  naturally carry.
- `benchmarks/mseb_adr/` *(deferred)* — Architecture-Decision-
  Record corpora.  Slot kept for a 4th domain once the first
  three are landed.

Each sub-package follows the same three-phase mining pipeline:

1. **mine.py** — fetch & extract raw message tuples per subject
   (no labels).  Cacheable; hit the source once.
2. **label.py** *(forthcoming)* — apply the MemoryKind classifier
   (rule-based + LLM fallback).  Labels live in a separate file
   so the prompt can be iterated cheaply.
3. **gold.yaml** — hand-curated gold queries covering all 14
   intent shapes for each subject.

## 4. Intuition — why these two pilot domains

We chose three disjoint domains to argue the mechanism isn't
overfit to any one of them, and each exercises a different part
of the P2 multi-head SLM:

| Axis | MSEB-SWE | MSEB-Clinical | MSEB-Convo |
| --- | --- | --- | --- |
| **Trigger of change** | engineering decision (PR) | new evidence (lab / imaging) | user declaration (preference, status) |
| **Subject type** | software component / bug | patient / finding | conversational user |
| **Time granularity** | minutes–days (commit cadence) | hours–years (visit cadence) | seconds–weeks (session cadence) |
| **Ground truth** | `patch` + `test_patch` land | final diagnosis in narrative | authored gold queries citing specific turns |
| **License** | MIT / BSD (SWE-bench) | CC-BY only (open-access subset) | LMEval MIT-ish (verify before redistribution) |
| **SLM heads exercised** | admission, state_change, topic | admission, state_change, topic | **all five** (incl. intent preference sub-types + slot BIO) |
| **Headline shapes** | current_state, causal_chain, retirement | origin, predecessor, retirement | preference × {current_state, predecessor, retirement} |

Together the three give us:

- **Cross-domain stability of TLG on state-evolution shapes** —
  SWE + Clinical, two disjoint vocabularies, both state-heavy.
- **Preference sub-type coverage** — only Convo.  Needed to
  validate that the `intent_head`'s 4-way classification earns
  retrieval lift, not just a classifier-F1 number.
- **Ablation headroom** — `--tlg-off` vs default across all three
  produces the per-shape delta that proves each TLG mechanism
  (temporal / ordinal / retirement / causal) earns its keep.

## 5. Adding a new domain

1. Create `benchmarks/mseb_<domain>/`.
2. Write `mine.py` producing `raw/<subject>.jsonl` (message
   tuples with `text`, `timestamp`, `source`).
3. Write `label.py` mapping message tuples →
   `CorpusMemory` rows with `metadata.kind` filled.
4. Write `gold.yaml` with hand-labeled queries (aim for ~30
   queries covering all 14 shapes for the pilot).
5. Add a README snippet pointing at `docs/p3-state-evolution-benchmark.md`.

The shared harness will discover the new domain automatically.

## 6. Run logs

All pilots / full runs emit durable logs to
`benchmarks/mseb/run-logs/`:

- `<domain>-pilot-<ts>.log` — the full miner output
- `pilot-applicability-<ts>.md` — post-pilot analysis

These are the artefacts we cite in the pre-paper; don't rely on
transient terminal output.

## 7. Status

| Component | Status |
| --- | --- |
| `schema.py` — CorpusMemory + GoldQuery + PreferenceKind (v1 locked 2026-04-20) | ✅ done |
| `mseb_swe/mine.py` + `label.py` | ✅ done — SWE-bench Verified, 500 issues → 1,835 memories |
| `mseb_clinical/mine.py` + `label.py` | ✅ done — PMC OA, 200 PMCIDs → 153 kept → 1,565 memories |
| `mseb_convo/mine.py` + `label.py` | ✅ done — LongMemEval, 500 users → 458 subjects → 9,962 turns |
| `mseb/metrics.py` | ✅ done — per-shape / per-preference / per-head F1 + Wilson CI |
| `mseb/build.py` | ✅ done — labeled + gold.yaml → canonical JSONL with validation |
| `mseb/harness.py` | ✅ done — 8 ablation flags, `--backend {ncms,mem0}` |
| `mseb/backends/` — `MemoryBackend` protocol + `ncms` + `mem0` | ✅ done — mem0 uses Spark LLM + local MiniLM + in-memory Chroma |
| `mseb/gold_author.py` — templated gold candidate generator | ✅ done |
| `mseb_{swe,clinical,convo}/gold.yaml` — hand-authored / reviewed gold | pending (next sprint) |
| Full ablation matrix runs | pending |
| `docs/mseb-results.md` (pre-paper §4.2) | pending post-run |

## 8. Backend abstraction (`benchmarks/mseb/backends/`)

Every backend implements the ``MemoryBackend`` protocol
(`backends/base.py`): `setup()` + `ingest(memories)` + `search(query, limit)`
+ `shutdown()`.  The harness treats them interchangeably.
Adding a competitor is one file.

Currently registered:

| Backend | Heart | LLM | Embedder | Vector store |
| --- | --- | --- | --- | --- |
| `ncms` | `MemoryService` (full pipeline) | Spark for ingest-side SLM (optional) | SPLADE v3 sparse | Tantivy BM25 + NetworkX graph |
| `mem0` | `mem0.Memory` | Spark for fact-extraction / rerank (optional) | `sentence-transformers/all-MiniLM-L6-v2` local | Chroma (ephemeral tempdir) |

The mem0 default config is ``infer=False`` + ``rerank=False``
(stores content verbatim, pure dense retrieval at query time) so
the comparison against NCMS is apples-to-apples.  ``--mem0-infer
--mem0-rerank`` enable the full mem0 pipeline for a separate
"mem0-full" column in the results table.  Chroma runs against a
tempdir that ``shutdown()`` cleans up — no cross-run state leakage.
