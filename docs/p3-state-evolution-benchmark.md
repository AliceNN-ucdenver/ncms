# P3 — Memory State-Evolution Benchmark (MSEB)

*Planning document · 2026-04-19 · reframed 2026-04-20 from an
SWE-only spec into a reusable multi-domain framework.  Schema
v1 locked 2026-04-20 after pilot mining of MSEB-SWE + MSEB-Clinical
confirmed state-evolution signal is present in both corpora; a
third domain (MSEB-Convo) was added to cover the preference
sub-type axis of the P2 multi-head classifier.*

---

## 0. Why this exists

Two benchmark runs in the record confirm that **conversational
memory recall is the wrong axis** for what NCMS's TLG grammar
layer + the intent-slot SLM were built to do:

| Run | Recall@5 | SLM delta |
|---|---:|---:|
| ADR state-evolution corpus (hand-curated, 32 queries, 11 intents) | **100 % rank-1 with TLG** | — |
| LongMemEval, 500 questions | 0.4680 | **0.0000** |

The ADR result proves the machinery works when the content has
**typed state transitions** (declarations, retirements, causal
edges).  The LongMemEval result proves the machinery doesn't
show up on conversational memory recall — because that benchmark
doesn't contain the state transitions TLG + the SLM are built to
detect.

What's missing is a benchmark family that:

1. **Is not curated by the paper's author.**  Derived
   mechanically from a public dataset.
2. **Is realistically sized.**  Thousands of memories, not
   fifty.
3. **Is publicly reproducible.**  Reusable JSONL artefact other
   memory systems can consume without knowing anything about
   NCMS.
4. **Is on-axis.**  State-change content with explicit
   supersedes / refines / causal structure.
5. **Is cross-domain.**  A single benchmark scoped to one
   domain (e.g. only software-dev) leaves the cross-adapter
   story unverified.  Our v4 adapters cover conversational,
   software_dev, and clinical — the benchmark should exercise
   at least two of those.

MSEB is the answer: a **pluggable benchmark framework** with a
shared harness + schema and three domain instantiations, so
dropping in a new domain (legal, scientific, ops incident
reports, ...) is a matter of adding one directory.

---

## 1. Deliverables

| Artifact | Location | Purpose |
|---|---|---|
| Framework doc (this) | `docs/p3-state-evolution-benchmark.md` | Design spec + addition playbook |
| Shared harness | `benchmarks/mseb/` | Schema, metrics, pytest-fixtured CLI, ablation matrix |
| Dataset #1: software-dev | `benchmarks/mseb_swe/` | Mined from SWE-bench Verified |
| Dataset #2: clinical | `benchmarks/mseb_clinical/` | Mined from PMC Open Access + synthetic augmentation |
| Dataset #3: conversational (preferences) | `benchmarks/mseb_convo/` | Wraps LongMemEval corpus + hand-authored preference queries (positive / avoidance / habitual / difficult) |
| Results | `benchmarks/results/mseb/<domain>/` | Timestamped runs with full tables |
| Write-up | `docs/mseb-results.md` | Headline numbers + per-domain breakdown + ablation tables |

All dataset JSONL files check in under CC-BY (matching both source
licenses), so the benchmark is itself reusable — other memory
systems consume the same JSONL, produce their own rank-1 / top-5
numbers, report back.

---

## 2. Shared schema

Every MSEB instantiation produces the same two JSONL files.  The
harness consumes them; nothing else needs to change.

### 2.1 `corpus.jsonl` — one memory per line

Same schema as NCMS ingest, so mining produces inputs the
retrieval path consumes directly.

```jsonl
{"mid": "swe-django-1234-m02",
 "subject": "django_1234",              // subject chain identifier
 "content": "QuerySet.update() raises IntegrityError when ...",
 "observed_at": "2023-07-15T14:32:00Z",
 "entities": ["QuerySet.update", "IntegrityError"],
 "metadata": {
   "kind": "declaration | retirement | causal_link | ordinal_anchor | none",
   "supersedes": [],
   "retires_entities": [],
   "source_msg_id": "gh-comment-98765"
 }}
```

**Required fields**: `mid`, `subject`, `content`, `observed_at`.

**Optional**: `entities`, `metadata.*`.  The `metadata.kind`
field tells TLG's retirement extractor what to look for; if
missing we infer via the structural extractor at ingest.

### 2.2 `queries.jsonl` — one gold query per line

```jsonl
{"qid": "swe-current-001",
 "shape": "current_state",              // one of TLG's 11 intent shapes
 "text": "What is the current status of django#1234?",
 "subject": "django_1234",
 "entity": null,                         // or a specific entity like "regression"
 "gold_mid": "swe-django-1234-m09",      // primary answer
 "gold_alt": [],                         // optional alternative acceptable answers
 "expected_proof_pattern": "terminal of zone 2"}
```

**Intent shapes** (TLG's 11, fixed across all MSEB domains):
`current_state`, `origin`, `ordinal_first`, `ordinal_last`,
`sequence`, `predecessor`, `interval`, `range`,
`transitive_cause`, `causal_chain`, `concurrent`, `before_named`,
`retirement`, plus `noise` (negative controls).

**Query distribution target** per instantiation:

| Shape | Count (per-domain) |
|---|---:|
| current_state | 20 |
| causal_chain | 15 |
| predecessor | 10 |
| sequence | 10 |
| ordinal_first | 6 |
| ordinal_last | 4 |
| interval | 8 |
| transitive_cause | 8 |
| before_named | 5 |
| concurrent | 5 |
| range | 5 |
| noise | 4 |
| **total per-domain** | **100** |

Balanced so no shape has fewer than 4 queries — avoids
single-shape variance dominating the aggregate.

---

## 3. Domain instantiations

Each instantiation lives in its own `benchmarks/mseb_<domain>/`
package and provides:

```
benchmarks/mseb_<domain>/
├── README.md              ← corpus provenance + licensing
├── mine.py                ← raw-source → messages JSONL
├── label.py               ← messages → labeled memory JSONL
├── taxonomy.yaml          ← topic + slot labels (matches the LoRA adapter)
├── queries.jsonl          ← hand-labeled gold queries
└── fixtures/              ← tiny sample used for CI + unit tests
```

The harness calls `mine.py` → `label.py` once at setup (cached)
and then drives the adapter A/B against the resulting JSONLs.

### 3a. MSEB-SWE (software_dev)

**Source.** SWE-bench Verified — 500 GitHub issues paired with
resolving PRs across 12 mature Python projects (astropy, django,
flask, matplotlib, pytest, requests, scikit-learn, sphinx, sympy,
xarray, pylint, pvlib).

**Mining.** For each issue, pull issue body + PR body + all
comments + review comments + commit messages via GitHub API.
Emit one message tuple per `{issue, message_id, author,
timestamp, text, kind}`.

**Labeling.** Two-pass LLM-assisted classifier:
1. Per-message: `{declaration | retirement | causal_link |
   ordinal_anchor | none}` via Nemotron Nano few-shot prompt.
2. Edge induction: run `domain/tlg/retirement_extractor.py`
   against message pairs to emit `SUPERSEDES` / `REFINES` /
   `CONFLICTS_WITH` edges.

**Calibration target.** Hand-label 50 issues (~600 messages),
compare against LLM labels.  Target ≥ 90 % message-level
precision.  If the labeler underperforms on specific projects
(e.g. pylint issues with heavy diffs), tighten the prompt.

**Scale.** 500 issues × mean 12 memories ≈ **6,000 memories**.

**Adapter used.** `~/.ncms/adapters/software_dev/v4/` — the v4
software_dev taxonomy maps library / language / pattern / tool
onto the SWE vocabulary (FastAPI, Django, pytest, Redis, etc.).

### 3b. MSEB-Clinical (clinical)

**Source.**  PubMed Central (PMC) Open Access case reports,
filtered by MeSH subject:

```
query: (Diagnosis, Differential[MH] OR Diagnostic Errors[MH])
       AND (Case Reports[PT])
       AND open access[filter]
       AND English[Lang]
```

NCBI eutils API is public; no auth required.  Results are CC-BY
under the PMC Open Access Subset license — redistributable as
JSONL.

**Why this source.**  Published case reports explicitly follow
the narrative arc we need: presentation → differential →
test → rule out → retest → revised diagnosis → treatment →
outcome.  Every paper is a state-evolution chain by
construction.  Contrast with raw clinical notes (MIMIC) where
state changes are buried in stream-of-consciousness
documentation.

**Mining.**  For each paper, extract:
- Title + abstract
- Structured narrative sections (Presentation, Workup,
  Differential Diagnosis, Final Diagnosis, Treatment,
  Outcome) via XML section parsing
- Timeline anchors from explicit date / hospital-day markers
  when present

**Filtering** — keep papers that match ≥ 2 of:
- `/initially (?:diagnosed|suspected|thought)/i`
- `/(?:was )?ruled out/i`
- `/(?:further testing|re-evaluation|retesting) revealed/i`
- `/final diagnosis/i` + narrative context
- `/discontinued .*(?:initiated|changed)/i`
- `/differential diagnosis/i` + subsequent narrowing

**Synthetic augmentation.**  Template-expand 100-200 additional
cases on the same state-change arc using the SDG tooling from
P2 (`experiments/intent_slot_distillation/sdg/`), keyed on the
clinical adapter's taxonomy (medication / imaging / surgery /
therapy / lab).  Gives us coverage on rare transition patterns
PMC papers don't hit.

**Scale.**  Target ~200 mined + ~100 synthetic = **~300 case
chains × mean 10 memories ≈ 3,000 memories**.

**Adapter used.**  `~/.ncms/adapters/clinical/v4/`.

### 3c. MSEB-Convo (conversational, preferences)

**Why this domain.**  The first two domains exercise the
`state_change_head` + `intent_head` (state-evolution shapes) but
they do not exercise the `intent_head`'s preference sub-types
(positive / avoidance / habitual / difficult).  Those four labels
are what the P2 LoRA adapter emits — without a benchmark that
contains them, the multi-head story is only two heads deep.
MSEB-Convo closes that axis.

**Source.**  LongMemEval (500 users, ~10K sessions, MIT-ish
licensed, already cached at
`benchmarks/results/.cache/longmemeval/`).  The LongMemEval
corpus supplies the substrate — multi-session conversations with
natural preference declarations — but its own labels are
**not sub-typed**: the 30 `single-session-preference` questions
have free-text answers that blend positive and negative
preferences into one paragraph.

**Mining.**  Explode each LongMemEval session into message tuples
(one per `haystack_sessions[k].turns[j]` pair) keyed on
`user_id`.  `observed_at` comes from `haystack_dates`.  Subject
chain = one user's full history.

**Labeling.**  Two passes:
1. Per-message `MemoryKind` via the same classifier used for
   SWE / Clinical — declarations (state reveals), retirements
   (preference changes), causal links (reason given).
2. Per-message `preference_kind` via a small rule-based
   classifier + LLM fallback, outputting one of
   `{positive, avoidance, habitual, difficult, none}`.

**Gold query authoring.**  LongMemEval's own question bank gives
us free coverage of `multi-session`, `temporal-reasoning`,
`knowledge-update` question-types; we re-annotate them into
MSEB's 14 shapes (many map directly).  For preferences we
hand-author **100 new queries** — 25 per sub-type — against
specific sessions that declare each preference kind.  Pattern:

```yaml
- qid: convo-pref-positive-007
  shape: current_state
  preference: positive
  text: "What kind of video-editing software does the user use?"
  subject: user-lm-0042
  gold_mid: convo-lm-0042-m0137   # turn where user said "I use Premiere Pro"
  gold_alt: [convo-lm-0042-m0219] # later turn confirming preference

- qid: convo-pref-avoidance-012
  shape: current_state
  preference: avoidance
  text: "What dietary restriction should the assistant remember?"
  subject: user-lm-0088
  gold_mid: convo-lm-0088-m0055   # "I can't eat shellfish"
```

**Scale.**

| Slice | Subjects | Memories (est.) | Gold queries |
|---|---:|---:|---:|
| Pilot | 50 users | ~1,000 | 50 (10 per preference sub-type + 10 state-evolution) |
| Full | 500 users | ~10,000 | 200 (25 per preference sub-type + 100 across shapes) |

**Adapter used.** `~/.ncms/adapters/conversational/v4/` — the
v4 conversational adapter is the **reason** this domain exists,
since its `intent_head` is the only head that routes on the
preference sub-type.

### 3d. MSEB-ADR (optional 4th — deferred)

Public ADR (Architecture Decision Record) repos on GitHub.
Deferred because MSEB-SWE already covers software_dev and ADRs
would be redundant without a new adapter.  Could become MSEB-Ops
(incident reports) or MSEB-Legal (case law state evolution) later.

---

## 4. Shared harness API (`benchmarks/mseb/`)

### 4.1 CLI

```bash
# Run one instantiation, one adapter, with TLG on
uv run python -m benchmarks.mseb \
    --corpus mseb_swe \
    --adapter software_dev

# TLG fully off — baseline (BM25 + SPLADE + graph only)
uv run python -m benchmarks.mseb --corpus mseb_swe --tlg-off

# Full ablation matrix: every domain × every feature-flag cell
uv run python -m benchmarks.mseb --all --ablate
```

### 4.1.1 Ablation flag matrix

Every flag toggles exactly one mechanism; they compose so we can
run each flag alone or `--tlg-off` for the cumulative baseline.

| Flag | Disables | Expected shapes affected |
|---|---|---|
| `--no-temporal` | `observed_at` scoring, recency half-life decay | `interval`, `range`, `ordinal_*` |
| `--no-ordinal` | ordinal-anchor intent supplement, section-order tiebreak | `ordinal_first`, `ordinal_last`, `sequence` |
| `--no-retirement` | supersession penalties (state conflicts), retirement intent supplement | `retirement`, `predecessor` |
| `--no-causal` | causal-link edge traversal, transitive-cause intent supplement | `transitive_cause`, `causal_chain`, `before_named` |
| `--no-preference` | `intent_head` preference routing | per-preference metrics (Convo only) |
| `--slm-off` | Bypass entire P2 adapter → regex/heuristic fallback only | all (baseline for multi-head value) |
| `--head <name>` | Evaluate only one head (`admission` / `state_change` / `topic` / `intent` / `slot`) | diagnostic isolation |
| `--tlg-off` | `--no-temporal` + `--no-ordinal` + `--no-retirement` + `--no-causal` | the full-off baseline |

The headline "look how cool TLG is" figure is the diff between
the default run and `--tlg-off` per shape.

### 4.1.2 Evaluation axes

Each run produces three reports from one pass over the corpus:

1. **Ingest classifier accuracy** — per-head F1 against gold
   `MemoryKind` (state_change head) + `PreferenceKind` (intent
   head) + gold slot spans (slot head) + admission decision
   (admission head) + topic label (topic head).  Answers: which
   heads earn their keep.
2. **Retrieval accuracy** — rank-1 / top-5 per shape (all
   domains) + per preference sub-type (Convo only).  Answers:
   does the classifier's output improve retrieval.
3. **Ablation matrix** — the full × each flag grid.  Answers:
   which mechanism does the lifting, per shape.

### 4.2 Output

Results land in:
```
benchmarks/results/mseb/<domain>/<ts>.{md,json}
```

Markdown tables in the same style as `experiments/temporal_trajectory/run.py`:

```
Aggregate top-5 accuracy by query shape
═══════════════════════════════════════════════════════════════════
Shape             N   bm25   bm25_date  entity  path  lg_grammar   slm  slm+lg
─────────────────────────────────────────────────────────────────────────────
current_state    20   22%       47%      33%    37%     95%      38%   98%
causal_chain     15   33%       33%      57%    60%    100%      37%   100%
predecessor      10    5%        5%       5%    10%     95%      15%   95%
…
```

### 4.3 Metrics

Shared module `benchmarks/mseb/metrics.py` computes:

- **top-5 accuracy** (any of top-5 = gold_mid OR in gold_alt)
- **rank-1 accuracy** (top-1 = gold)
- **latency p50 / p95** (per-query wall time)
- **confidently-wrong rate** (classifier `intent_confidence ≥
  0.7` AND wrong; only applicable to runs with the SLM)

Reported at three grains:

- `per_shape`       — 14 rows × one value per metric
- `per_preference`  — 5 rows (Convo only)
- `per_head`        — 5 rows of classifier F1

Each run writes a single `results.json` of shape:

```json
{
  "run_id": "mseb_convo_full_tlg-off_2026-04-20T12:00:00Z",
  "domain": "mseb_convo",
  "feature_set": {"temporal": false, "ordinal": false, ...},
  "total_queries": 200,
  "per_shape":      {"current_state": {"r@1": ..., "r@5": ..., "n": ...}, ...},
  "per_preference": {"positive": {...}, "avoidance": {...}, ...},
  "per_head":       {"admission": {"f1": ...}, "state_change": {...}, ...},
  "latency_p50_ms": 47.2,
  "latency_p95_ms": 183.1,
  "confidently_wrong": 0.0
}
```

plus a markdown summary ready to paste into the pre-paper.

### 4.4 pytest integration

The harness is written as a pytest plugin so `pytest
benchmarks/mseb/` runs the smoke suite (100-memory fixture per
domain) in CI, while the CLI runs the full matrix.

---

## 5. Add-a-new-domain playbook

To add MSEB-Legal (or any 4th+ domain):

1. `mkdir benchmarks/mseb_legal/` — scaffold from
   `benchmarks/mseb_swe/` (copy 5 files, 1-line path edits).
2. **`mine.py`**: fetch your raw source (e.g. SCOTUS Blog / Oyez
   opinions for legal).
3. **`label.py`**: message-level classifier — can reuse the
   MSEB-SWE labeler's prompts with domain-specific few-shot
   examples.
4. **`taxonomy.yaml`**: topic + slot vocab matching a trained
   adapter (or train a new adapter via P2's `train_adapter.py`).
5. **`queries.jsonl`**: 30-100 hand-labeled gold queries across
   the 11 intent shapes.
6. **`README.md`**: corpus provenance, licensing, caveats.

Run:
```bash
uv run python -m benchmarks.mseb --corpus mseb_legal --adapter legal
```

Results drop into `benchmarks/results/mseb/legal/`, identical
format to SWE and clinical.

---

## 6. Timeline

Revised from the 2-week SWE-only budget to **~4 weeks** for the
framework + both pilots + both at-scale runs:

| Week | Work | Deliverable |
|---|---|---|
| 1 | **Shared harness** (`benchmarks/mseb/`) — schema, metrics, CLI, pytest fixtures.  Test-driven against ADR corpus as a smoke dataset. | Harness + 100 % TLG / BM25 / slm baselines reproduced on the ADR fixture. |
| 2 | **MSEB-SWE pilot** — 50 issues mined + labeled, 30 gold queries, A/B results with software_dev adapter + TLG on/off. | First published MSEB result table. |
| 2-3 | **MSEB-Clinical pilot** — 30 PMC papers mined + filtered + labeled, 30 gold queries, A/B with clinical adapter + TLG. | Second MSEB result table.  Proves the pattern reuses across corpora. |
| 3-4 | **Full scale** — MSEB-SWE 500 issues / 100 queries + MSEB-Clinical 200-300 cases / 100 queries. | Headline write-up. |

Pilots can parallelize — mining + labeling are independent per-domain.  Shared harness is the gate for both.

---

## 7. Evaluation matrix per domain

Each MSEB instantiation publishes three tables.

### 7.1 Per-shape × per-strategy (all domains)

| | BM25 | BM25+SPLADE | +graph | **+TLG** | **SLM+TLG** |
|---|---:|---:|---:|---:|---:|
| current_state | | | | | |
| origin | | | | | |
| ordinal_first / ordinal_last | | | | | |
| sequence | | | | | |
| predecessor | | | | | |
| interval / range | | | | | |
| transitive_cause | | | | | |
| causal_chain | | | | | |
| concurrent | | | | | |
| before_named | | | | | |
| retirement | | | | | |
| noise (rejection) | | | | | |
| **overall rank-1** | | | | | |

`SLM+TLG` is the headline strategy: the SLM's heads emit
ingest-side labels (declaration / retirement / preference) that
TLG consumes when dispatching query-side.

### 7.2 Per-preference × per-strategy (MSEB-Convo only)

| | BM25 | BM25+SPLADE | **SLM only** | **SLM+TLG** |
|---|---:|---:|---:|---:|
| positive | | | | |
| avoidance | | | | |
| habitual | | | | |
| difficult | | | | |
| **overall** | | | | |

### 7.3 Per-head classifier F1 (all domains, when SLM on)

| Head | Labels | SWE F1 | Clinical F1 | Convo F1 |
|---|---|---:|---:|---:|
| `admission_head` | persist / ephemeral / discard | | | |
| `state_change_head` | declaration / retirement / none | | | |
| `topic_head` | domain taxonomy | | | |
| `intent_head` (preference) | positive / avoidance / habitual / difficult / none | n/a | n/a | |
| `slot_head` | BIO spans | | | |

---

## 8. Success criteria

A strong MSEB result has these six properties:

1. **TLG ≥ 80 % rank-1 on state-evolution shapes** (current_state,
   causal_chain, predecessor, sequence, transitive_cause,
   interval, retirement) across all three domains.  Noise shapes
   should be rejected (abstain, not confidently-wrong).
2. **BM25 ≤ 30 % rank-1 on the same shapes** — proves the
   grammar is adding something, not just rephrasing lexical
   overlap.
3. **TLG-on vs TLG-off delta ≥ +0.40 absolute rank-1** on at
   least 4 of the 14 shapes (predecessor, ordinal_first,
   retirement, causal_chain are the strongest candidates).
   This is the visible "TLG earns its keep" number.
4. **Zero confidently-wrong** (`SLM intent_conf ≥ 0.7` AND
   wrong) across all three domains.
5. **Cross-domain stability** — SWE, Clinical, and Convo
   within ±10 % on state-evolution shapes, proving the
   architecture is domain-portable.
6. **Per-preference rank-1 ≥ 0.70** on all four preference
   sub-types (Convo) with SLM on, and a clear drop (≥ 0.20)
   on all four when SLM is off.  This is the multi-head
   classifier's headline number.

---

## 9. Risk register

| Risk | Severity | Mitigation |
|---|---|---|
| PMC MeSH filter yields too few high-state-change papers | Medium | Synthetic augmentation bridge (§3b); broaden MeSH to include `Clinical Decision-Making` + `Uncertainty` |
| LLM labeler precision < 90 % on SWE | Medium | Hand-curate labels for the 50-issue pilot before scaling; tighten prompt with pilot examples |
| GitHub rate-limiting blocks SWE mining | Low | Cached raw messages checked in as fixtures; full mine runs off-peak with throttle |
| Clinical corpus redistribution license ambiguity | High | Only ship CC-BY PMC subset; verify license per paper at mine time; flag non-CC-BY in metadata |
| Adapter performance drift on real-world vocabulary | Medium | Each mining pass reports "unknown topic" rate; > 10 % triggers a retrain with the new vocab |
| Confidently-wrong on edge-case queries (quoted speech, negation) | Medium | Queries JSONL includes ≥ 5 noise / adversarial per domain; gate checks these specifically |

---

## 10. What this benchmark lets others do

Because the schema is a simple two-file JSONL contract (`corpus` +
`queries`), any memory system can consume MSEB without knowing
anything about NCMS.  Mem0, Letta, Zep, MemoryAgent — all can
run their own retrievers against the same corpus and publish
their own per-shape rank-1 / top-5 tables.

That's the bigger win this framework enables: **MSEB as a
reusable measurement stick for state-evolution retrieval**, the
way BEIR is for lexical retrieval and LongMemEval is for
conversational recall.

The paper section becomes:

> *We introduce **MSEB** (Memory State-Evolution Benchmark), a
> pluggable multi-domain benchmark targeting typed state-change
> retrieval.  MSEB provides a shared harness + schema + metrics,
> with reference instantiations on software-dev (SWE-bench
> Verified-derived) and clinical (PMC Open Access-derived)
> corpora.  NCMS TLG achieves X rank-1 on state-evolution shapes
> where BM25 baselines fail at Y.*

---

## Status

* **Plan authored:** 2026-04-19 (original SWE-only scope)
* **Reframed:** 2026-04-20 — multi-domain framework + two
  pilots, per the feedback that a single-domain benchmark
  doesn't exercise cross-adapter portability.
* **Status:** Ready for kickoff on approval.
* **Owner:** TBD
* **Dependencies:** Requires `experiments/intent_slot_distillation/`
  Sprint 4 shipped (done 2026-04-20) — uses the v4 software_dev
  and clinical adapters as the SLM backends.
* **Next action:** Approve this framework; kick off Week 1
  (shared harness).
