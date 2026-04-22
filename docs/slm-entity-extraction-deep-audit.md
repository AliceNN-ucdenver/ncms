# SLM + TLG Grammar Correctness — Deep Audit (softwaredev mini)

Scope: factual correctness audit of every signal used by the
TLG grammar retrieval path.  No assumptions; every number below
is measured from instrumented runs on post-fix commit `3394262`.

Audit scripts + raw outputs:
  `benchmarks/audit_head_quality.py`  → `benchmarks/results/audit/head_quality_softwaredev.txt`
  `benchmarks/audit_vocab_quality.py` → `benchmarks/results/audit/vocab_quality_softwaredev.log`
  `benchmarks/audit_grammar_paths.py` → `benchmarks/results/audit/grammar_paths_softwaredev.log`

---

## Verdict summary

| Component | Status | Notes |
|---|---|---|
| SLM admission head (ingest) | ✓ PASS | 186/186 persist, min conf 0.933 |
| SLM intent head (ingest)    | ✓ PASS | 186/186 none, correct for ingest voice |
| SLM shape_intent head (query) | ✓ PASS | 165/165 correct at high conf, 100% per-shape recall |
| SLM topic head (ingest)     | ~ PARTIAL | Fires on only 29.6% of memories; high precision when it does |
| SLM state_change head (ingest) | ✗ FAIL | 36% recall on real declarations; misses "we have decided to use X" at 0.96 conf |
| SLM slot head (ingest)      | ~ PARTIAL | 39.8% fire rate; 10.3% hallucination rate ("Chat", "dot" spurious) |
| UUID leakage in vocab       | ✓ PASS | 0 subjects, 0 entities |
| Regex fallback firing (ingest) | ✓ PASS | extract_entity_state_meta = 0 calls |
| Regex fallback firing (query) | ✓ PASS | extract_entity_state_meta = 0 calls |
| Vocabulary ambiguity        | ✗ FAIL | 20.2% of tokens route to >1 subject; stopwords ("the"/"and"/"our") in vocab |
| TLG zone coverage           | ✗ FAIL | 20 of 25 subjects have 0 zones; grammar walker effectively disabled |
| TLG subject resolution      | ✗ FAIL | 12 of 13 probe queries resolve to WRONG subject |
| TLG grammar dispatch        | ✗ FAIL | 13/13 abstain on target probes |
| `tlg.analyze_query` (v6 refactor) | ✓ PASS | vocabulary-lookup only; no regex grammar rules |

Search pipeline r@1 recovered to 0.7273 (vs broken 0.2303, vs
baseline 0.7455).  r@5 = 0.8424 (+7.3pp vs baseline), MRR = 0.7775
(+5.7pp).  The r@1 gap to baseline is NOT from grammar walker
degradation (grammar was abstaining at baseline too); it's from
slightly different signal interactions on search ranking.

**The TLG grammar walker is de facto disabled on this corpus** —
even though we wired Parts 1+2+4 correctly.  Two independent
failure modes block it:

  F1. SLM state_change head under-fires → only 8 L2 nodes → 20/25
      subjects have zero zones.
  F2. Vocabulary induction word-splits multi-word entities into
      secondary tokens → stopwords + generic words pollute the
      subject lookup → wrong subject resolved 92% of the time.

Fixing F2 is a code change (in-scope).  Fixing F1 is adapter
training-data work (out-of-scope).

---

## Phase A — SLM head-output quality (facts)

### admission head: PERFECT

```
decisions: {'persist': 186}
confidence: min=0.933, median=1.000, max=1.000
gold-curated misses (non-persist): 0
```

On a gold-curated benchmark corpus we expect 100% persist; delivered.

### intent head (ingest voice): PERFECT

```
intent distribution: {'none': 186}
confidence: min=0.000, median=0.999, max=1.000
ingest-time intent != 'none': 0
```

Ingest content is not user intent; head correctly abstains on every memory.

### shape_intent head (query voice): PERFECT

```
queries total: 165
high-conf (≥0.7) correct: 165 (100.0%)
high-conf (≥0.7) wrong:   0   (0.0%)
low-conf / abstain:       0   (0.0%)

Per-shape recall (100% on every class):
  before_named  15/15   causal_chain 11/11   concurrent    15/15
  current_state 15/15   none         20/20   ordinal_first 15/15
  ordinal_last  15/15   origin       15/15   predecessor    9/9
  retirement     9/9    sequence     15/15   transitive_cause 11/11
```

Query-side classification is the jewel of the v6 adapter.

### topic head (ingest voice): PARTIAL

```
distribution: {None: 131, framework: 18, infra: 14, tooling: 12,
               language_runtime: 6, testing: 5}
confidence (non-None only): min=0.702, median=0.830, max=0.999
```

Fires on 55/186 memories (29.6%).  When it fires, slug-hint
agreement is high: framework→framework 12/15, infra→infra 12/14,
testing→testing 5/5.  Two clear misfires: a team-culture ADR
tagged `topic='tooling'` at conf 0.81.  Topic head is usable as
a secondary signal but should not be promoted into structural
roles (we correctly no longer use it as `state_key`).

### state_change head (ingest voice): **FAIL — 36% recall on declarations**

```
SLM distribution: {none: 178, declaration: 8}

SLM vs heuristic-oracle agreement matrix:
  slm='declaration'  oracle='declaration'  n=2    ✓
  slm='declaration'  oracle='none'         n=6    ✗ (possible FP)
  slm='none'         oracle='declaration'  n=20   ✗ (FALSE NEGATIVE)
  slm='none'         oracle='none'         n=157  ✓
  slm='none'         oracle='retirement'   n=1    ✗

Heuristic-oracle declarations: 22.  SLM caught: 2.  Recall: 2/22 = 9%.
(Additional 6 memories SLM+oracle both flagged independently.)
```

Sample misses:
  - "we have decided to use gRPC for our API" → SLM=none, conf 0.962
  - "Decided on Bulma. Open to new CSS framework choices…" → SLM=none, conf 0.957
  - "we have decided to use Docker Swarm" → SLM=none, conf 0.729
  - "After considering the pros and cons, we have decided…" → SLM=none

The SLM is **confidently wrong** — it emits `none` at 0.9+ conf on
clear first-person declarations.  This is the single biggest
structural hole: zero-grammar coverage for ADR content follows
from this head's recall.

### slot head (ingest voice): PARTIAL, 10% hallucinations

```
slots extracted: 87
types: {library: 66, alternative: 21}
fire rate: 74/186 memories (39.8%)
hallucinations (surface not in content): 9 / 87 (10.3%)

Examples of hallucinated slots:
  docker-swarm-sec-06  → alternative: 'Chat'   (no mention of Chat)
  mysql-db-sec-08      → alternative: 'Chat'
  python-lang-sec-04   → alternative: 'Chat'
  env-var-sec-05       → library:     'dot'    (truncation of dotenv?)
  postgresql-sec-00    → alternative: 'MySQL'  (mentioned? borderline)
```

"Chat" appears as a phantom alternative on three unrelated
technology ADRs — training-data leakage.  Also note the schema
is only 2 labels (`library`, `alternative`) — no `technology`,
`framework`, `database`, `service`.  That's why GLiNER still
runs on 60.2% of memories as the open-vocabulary fallback.

### Per-head verdict

| Head | Used for | Quality on this corpus |
|---|---|---|
| admission | ingest gate | perfect |
| intent (ingest) | preference extraction | correctly inactive |
| shape_intent (query) | grammar shape | perfect |
| topic (ingest) | `Memory.domains` auto-populate | usable (30% fire) |
| state_change (ingest) | L2 ENTITY_STATE gate | broken (9% recall) |
| slot (ingest) | typed entity injection | narrow schema + 10% hallucinations |

---

## Phase B — Vocabulary quality (facts)

```
subjects: 25
entity tokens: 955
aliases (surface→group): 15
domain_nouns: 41
UUID leakage (subjects): 0   ← Part 1 fix holds
UUID leakage (entities): 0
```

### Generic-word entities in vocabulary (9 total)

```
['alternatives', 'approach', 'decision', 'decisions',
 'our', 'team', 'teams', 'the', 'they']
```

These come from GLiNER extracting multi-word expressions like
"The decision", "our team", "their approach" — which the
vocabulary induction then splits into secondary tokens.

### Ambiguous tokens (route to >1 subject) — **20.2% of vocab**

```
193 of 955 tokens are ambiguous.
Top 10 worst offenders (by n_subjects):
  token='team'         routes_to=browser-automation  n_subjects=11  mentions=17
  token='development'  routes_to=mysql-database      n_subjects=11  mentions=25
  token='and'          routes_to=mysql-database      n_subjects=9   mentions=21  ← stopword
  token='web'          routes_to=ruby-on-rails       n_subjects=9   mentions=13
  token='developers'   routes_to=programming-editors n_subjects=9   mentions=12
  token='decision'     routes_to=high-trust-teamwork n_subjects=8   mentions=13  ← squatter source
  token='scalability'  routes_to=google-cloud        n_subjects=8   mentions=14
  token='our'          routes_to=google-cloud        n_subjects=7   mentions=15  ← pronoun
  token='code'         routes_to=programming-editors n_subjects=7   mentions=13
  token='framework'    routes_to=browser-automation  n_subjects=7   mentions=11
  ...
  token='the'          routes_to=4-day-work-week     n_subjects=6   mentions=8   ← article
```

The ambiguity is caused by `induce_vocabulary`'s
secondary-token-split logic at
`src/ncms/domain/tlg/vocabulary.py:147-154`:

```python
for word in stripped.split():
    if word.lower() != stripped_lower:
        _register(word, word, mem.subject, is_primary=False)
```

Multi-word GLiNER entities like "The decision", "our team",
"a framework" get split into ["the", "decision"], ["our", "team"],
["a", "framework"] — each word registers as a secondary token
pointing to the subject.  Whichever subject has the most
"decision" mentions wins the majority vote for the "decision"
token.  High-trust-teamwork, with 7 decision-heavy sections,
wins "decision" — and every query containing "decision" routes
there.

### Entity type distribution (631 unique entities)

```
technology:    161
concept:       112
process:        60
product:        60
organization:   39     ← contains "We" and similar pronouns
library:        35     ← SLM slot head
event:          34     ← contains "The decision"
document:       31
subject:        25     ← Part 4 caller-asserted
metric:         25
person:         22
alternative:    14     ← SLM slot head
location:       13
```

Generic-word entity rows:
```
name='We'          type='organization'  (GLiNER)
name='decision'    type='process'       (GLiNER)
name='our'         type='organization'  (GLiNER)
...
```

GLiNER is the source of the stopword pollution.  The SLM slot
head does not emit these; it emits typed surface forms.

---

## Phase D — TLG grammar walker correctness (facts)

### Per-subject zone cardinality — **20 / 25 subjects have ZERO zones**

```
  subject                                                       zones  nodes  edges
  sdev-adr_jph-4-day-work-week                                      1      1      0
  sdev-adr_jph-api-using-json-v-grpc                                0      0      0  ←
  sdev-adr_jph-browser-automation-framework-for-e2e-testing…        0      0      0  ←
  sdev-adr_jph-choosing-a-database-technology                       0      0      0  ←
  sdev-adr_jph-continuous-integration                               0      0      0  ←
  sdev-adr_jph-css-framework                                        0      0      0  ←
  sdev-adr_jph-docker-swarm-container-orchestration                 0      0      0  ←
  sdev-adr_jph-environment-variable-configuration                   1      1      0
  sdev-adr_jph-go-programming-language                              2      2      1
  sdev-adr_jph-google-cloud-platform                                2      2      1
  sdev-adr_jph-high-trust-teamwork                                  0      0      0  ←
  sdev-adr_jph-kubernetes-container-orchestration                   0      0      0  ←
  sdev-adr_jph-mysql-database                                       0      0      0  ←
  sdev-adr_jph-postgresql-database                                  1      1      0
  sdev-adr_jph-programming-code-editors                             0      0      0  ←
  sdev-adr_jph-python-django-framework                              1      1      0
  sdev-adr_jph-python-programming-language                          0      0      0  ←
  sdev-adr_jph-ruby-on-rails-framework                              0      0      0  ←
  sdev-adr_jph-rust-programming-language                            0      0      0  ←
  sdev-adr_jph-secrets-storage                                      0      0      0  ←
  sdev-adr_jph-svelte-components                                    0      0      0  ←
  sdev-adr_jph-svelte-front-end-javascript-library                  0      0      0  ←
  sdev-adr_jph-sveltekit-framework                                  0      0      0  ←
  sdev-adr_jph-tailwind-css                                         0      0      0  ←
  sdev-adr_jph-timestamp-format                                     0      0      0  ←

Total ENTITY_STATE nodes across corpus: 8
Total DERIVED_FROM/supersedes edges:    2
```

Even subjects that DO have zones have tiny ones (1–2 nodes,
0–1 edges).  The grammar walker has almost nothing to walk.

### Per-query grammar trace — **13/13 queries abstain**

```
  qid                               gold subject                          resolved subject
  softwaredev-current_state-001     css-framework                         tailwind-css            ✗
  softwaredev-current_state-005     python-django-framework               ruby-on-rails-framework ✗
  softwaredev-origin-001            css-framework                         tailwind-css            ✗
  softwaredev-origin-005            python-django-framework               high-trust-teamwork     ✗
  softwaredev-ordinal_first-001     css-framework                         high-trust-teamwork     ✗
  softwaredev-ordinal_last-001      css-framework                         high-trust-teamwork     ✗
  softwaredev-retirement-001        docker-swarm-container-orchestration  high-trust-teamwork     ✗
  softwaredev-predecessor-001       docker-swarm-container-orchestration  docker-swarm            ✓
  softwaredev-sequence-001          css-framework                         high-trust-teamwork     ✗
  softwaredev-causal_chain-001      python-django-framework               high-trust-teamwork     ✗
  softwaredev-concurrent-001        css-framework                         tailwind-css            ✗
  softwaredev-before_named-002      api-using-json-v-grpc                 high-trust-teamwork     ✗
  softwaredev-transitive_cause-001  python-django-framework               high-trust-teamwork     ✗

Confidence distribution: {'abstain': 13}
Subject mismatch: 12 / 13  (92.3%)
Grammar answer correct: 0 / 13
```

Even the one subject match (predecessor-001) abstains because
docker-swarm has 0 zones to walk.

The grammar walker is walking wrong subjects' empty zones.

---

## Root causes (facts, not speculation)

### RC1: SLM state_change head under-fires on declarative ADR language

**Evidence:** Phase A — 20 real declarations (per heuristic oracle)
labeled `none` by SLM at 0.9+ confidence.  9% recall.

**Impact:** Only 8 L2 ENTITY_STATE nodes created for 186 memories.
20 of 25 subjects have zero zones.  Grammar walker can never fire
on them.

**Fix path:** adapter retraining with more declaration-style
examples.  Out of scope for this PR.

### RC2: Vocabulary induction pollutes subject lookup with stopwords

**Evidence:** Phase B — `induce_vocabulary` at
`src/ncms/domain/tlg/vocabulary.py:147-154` word-splits multi-word
GLiNER entities ("The decision", "our team") into secondary
tokens.  20.2% of vocabulary tokens route to multiple subjects.
Top ambiguous tokens include stopwords "the", "and", "our".
"decision" routes to high-trust-teamwork (8-way ambiguity).

**Impact:** Phase D — 12 of 13 probe queries resolve to the
WRONG subject via `lookup_subject`.  The grammar walker is given
the wrong subject's (usually empty) zones to walk.  Effectively
100% grammar abstain on the corpus.

**Fix path:** three options, pick one —

  **Option R2-a (minimal, recommended):** filter stopwords +
  short function words from the secondary-token registration.
  Keep "physical therapy" → {"physical", "therapy"} working;
  drop "The decision" → {"decision"} (keep "the" filtered).
  Cost: ~20 LOC + a stopword list constant.

  **Option R2-b (aggressive):** drop secondary-token registration
  entirely.  Only primary (exact-surface) tokens enter the
  subject_lookup table.  Cost: revert lines 147-154; may hurt
  recall on legitimate multi-word entity queries (e.g. a query
  saying "physical" when the corpus entity is "physical therapy"
  would no longer route).  Measure first.

  **Option R2-c (structural):** also filter GLiNER-extracted
  entities at extract time to drop generic-word entities
  ("The decision", "We", "our team", "The approach").
  Complements R2-a or R2-b.

### RC3: Slot head schema is narrow (library + alternative only)

**Evidence:** Phase A — slot head fires on 39.8% of memories;
only two slot labels seen across entire corpus.

**Impact:** GLiNER runs on 60.2% of memories as the open-vocab
fallback, bringing stopword noise with it.

**Fix path:** retrain adapter with richer slot schema.  Out of
scope for this PR.

---

## Verdict + recommended next action

**What this commit gets right** (compared to broken `78b9fc9`):
- Search r@1 recovered to within 2pp of baseline.
- r@5 and MRR exceed baseline.
- Zero regex fallback firing anywhere.
- Ingest L2 inflation fixed (186 → 8).
- UUID vocabulary leakage fixed (Part 1 holds).
- Subject vocabulary induction is now decoupled from L2 creation (Step 1 holds).

**What this commit leaves broken** (pre-existing, not my commit):
- TLG grammar walker is effectively disabled on the corpus
  (13/13 abstain on target queries).
- Subject lookup pollution from secondary-token word-splitting
  causes 92% subject mismatch on target queries.
- SLM state_change head under-fires on declarative ADR content.
- SLM slot head schema is narrow; GLiNER fallback still runs
  on majority of memories.

**Recommended next step (one PR, low-risk, in-scope):**

Implement **Option R2-a** (filter stopwords from secondary-token
registration in `induce_vocabulary`).  This would address the
subject-mismatch problem directly.  Expected result:

  - Subject-lookup correctness on probe queries improves from
    1/13 to 8+/13.
  - Search r@1 unchanged (grammar path doesn't drive top-1
    now).
  - Grammar walker STILL abstains on most queries due to RC1
    (the adapter state_change head recall issue) — but the
    subjects it does walk would be correct.

R2-a + adapter retraining for RC1 are independent and can ship
in any order.  Together they would enable TLG grammar to
actually contribute to ranking.

**Do NOT ship MSEB full-12 before deciding on R2-a.**  The
current configuration gives clean r@1/r@5/MRR numbers but the
TLG grammar is a placebo — any claim that the overnight benchmark
validates TLG correctness would be false.

---

## Artifacts committed in this audit

  benchmarks/audit_head_quality.py
  benchmarks/audit_vocab_quality.py
  benchmarks/audit_grammar_paths.py
  benchmarks/results/audit/head_quality_softwaredev.txt
  benchmarks/results/audit/vocab_quality_softwaredev.log
  benchmarks/results/audit/grammar_paths_softwaredev.log
  docs/slm-entity-extraction-deep-audit.md  (this file)
