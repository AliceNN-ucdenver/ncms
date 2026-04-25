# v9 Corpus Generation Design

> Status: **design**, not implemented.  Writeup of Phase B' of the v9
> SLM rebuild.  The v9 architectural retrospective lives at
> `docs/completed/failed-experiments/ctlg-joint-cue-head.md` (to be
> written after v9 ships).

## Goals

1. **Produce a labeled corpus per domain** sufficient to train a
   v7-style joint 5-head adapter that passes per-head gates:
   intent_f1_macro ≥ 0.70, slot_f1_macro ≥ 0.75,
   topic_f1_macro ≥ 0.65, admission_f1_macro ≥ 0.85,
   state_change_f1_macro ≥ 0.70.
2. **SLM-only at runtime.**  The 5-head joint adapter is the sole
   ingest-time classifier.  No foundation-model dependency.
3. **Spark Nemotron is the offline generator.**  Local LLM, no API
   cost, no external dependency at generation time either.
4. **Per-head class balance is a corpus-level invariant.**  If the
   corpus doesn't have ≥50 rows per class per head, training is
   blocked.  Fixed at generation, not at loss weighting.
5. **Real-catalog grounding where the catalog exists.**  For
   software_dev, every `primary` / `alternative` role span must
   resolve to a real catalog entry.  For domains without a
   catalog, role spans come from LLM-generated entities marked
   in the text; see per-domain strategy below.
6. **Strict validation at load time.**  Rows that fail schema
   validation are dropped and counted.  No silent schema drift.
7. **Cross-model audit.**  A second Spark call re-labels a 10%
   sample of generated rows; archetypes with <90% agreement are
   flagged for prompt refinement.

## Non-goals

* **CTLG / cue_tags.**  v9 is 5-head only.  The cue head is a
  separate future adapter (Phase E, post-v9).  The corpus
  generator does not emit cue_tags.
* **swe_diff in this phase.**  swe_diff operates on diff content
  (file paths, symbols, test paths) which is fundamentally
  open-vocabulary.  We ship v9 for the three prose domains first;
  swe_diff follows as a dedicated track.
* **Training the adapter.**  That's Phase C'.  This doc is only
  about generating the corpus.

## Architecture: stratified archetype generation

### Why archetypes, not one big prompt

Ad-hoc "ask Spark for 500 preference utterances" produces a corpus
skewed toward whatever classes Spark finds easiest (usually
`intent=positive`, `admission=persist`, `state_change=none`).  The
pre-v9 gold audit
(`docs/forensics/gold-audit-pre-v9.md`) documents the concrete
failure: `conversational/gold` has 410 rows with
`admission=persist` across every single one, zero `ephemeral` or
`discard`, and `negative` intent shows up 2 times out of 410.

An **archetype** is a structured prompt targeting exactly one
joint label combination:
- fixed `(intent, admission, state_change)` triple
- fixed `role_span` composition (how many `primary`, `alternative`,
  `casual`, `not_relevant` spans)
- fixed target row count
- per-archetype phrasing bank + few-shot examples

The generator iterates archetypes.  Because each archetype fixes
its labels up front, class balance is guaranteed by construction,
not by hoping the LLM distributes evenly.

### Coverage strategy

Per domain, ~16 archetypes cover the full joint label space
without sparsity.  Design rules:

* **Every intent class** appears in ≥2 archetypes (intent floor).
* **Every admission class** appears in ≥1 archetype — `persist`
  dominates, `ephemeral` has 2-3 archetypes, `discard` has 1
  (spam-like rows).
* **Every state_change class** appears in ≥2 archetypes.
* **Role-span combinations** span the 4 roles × typical counts:
  "primary only", "primary + alternative" (for choice rows),
  "primary + not_relevant" (for mixed query-voice rows),
  "multiple casual" (for factual narrative).
* **Content length** varies per archetype (`target_min_chars` /
  `target_max_chars`): short user utterances (15-60), medium
  (60-150), occasional longer (150-400).

### Three corpus files per domain

| File | Rows | Archetypes | Generator | Use |
|---|---|---|---|---|
| `gold.jsonl` | ~500 | 16 with `n_gold` targets | Spark Nemotron | Per-head gate + held-out eval |
| `sdg.jsonl` | ~2500 | same 16 with `n_sdg` targets | Spark Nemotron | Training bulk |
| `adv.jsonl` | ~300 | template-based mutations | Python (no LLM) | Adversarial training |

Gold is the highest-quality pass, used for gate evaluation.  SDG
re-runs the same archetypes at 5× volume for training bulk.
Adversarial uses the existing `phase3_adversarial` helper that
mutates gold rows via hardcoded patterns (negation, sarcasm,
quoted-speech) — no LLM, free, already works.

## The archetype schema

```python
@dataclass(frozen=True)
class ArchetypeSpec:
    """One generation archetype: a joint label combination + prompt."""

    name: str                      # "positive_adoption_with_alternative"
    domain: Domain                 # "conversational" | "clinical" | "software_dev"

    # ── Joint label fixed by this archetype ──
    intent: Intent                 # every row gets this intent
    admission: AdmissionDecision   # every row gets this admission
    state_change: StateChange      # every row gets this state_change
    topic: str | None = None       # when this archetype has a fixed topic
                                   # (else topic is sampled from the domain
                                   # taxonomy per-row)

    # ── Role-span composition ──
    role_spans: tuple[RoleSpec, ...] = ()
    # RoleSpec = (role, slot, count).  Example: ("primary", "framework", 1)
    # means every row has exactly 1 primary-role span in the framework slot.

    # ── Generation params ──
    n_gold: int = 30               # target rows for gold
    n_sdg: int = 150               # target rows for sdg
    target_min_chars: int = 20
    target_max_chars: int = 200
    batch_size: int = 10           # rows per Spark call

    # ── Prompt surface ──
    description: str = ""          # one-liner for the prompt + audit
    example_utterances: tuple[str, ...] = ()  # 3-5 few-shot examples
    phrasings: tuple[str, ...] = ()   # surface templates w/ {primary}, {alt}


@dataclass(frozen=True)
class RoleSpec:
    role: Role          # "primary" | "alternative" | "casual" | "not_relevant"
    slot: str           # "framework" | "object" | "medication" | ...
    count: int = 1      # how many spans of this role
```

## Per-domain generation strategies

### software_dev (catalog-grounded)

* **Catalog**: 618 entries across 9 slots, already shipped at
  `ncms.application.adapters.sdg.catalog.software_dev`.
* **Entity picking**: before generating a batch, pre-sample real
  catalog entries matching each archetype's `role_spans` slots.
  Rotate through the catalog without replacement within the batch
  so we don't mention `postgres` 40× and `snowflake` 0×.
* **Prompt structure**: "Write a natural engineering statement
  that uses `<primary_canonical>` as the primary framework and
  `<alternative_canonical>` as the rejected alternative.
  Intent = `choice`, admission = `persist`."
* **Role span extraction**: after Spark returns text, run
  `catalog.detect_spans(text, domain='software_dev')` to find the
  canonical surfaces.  If the canonical we requested isn't
  present, drop the row.
* **Guarantees**: 100% gazetteer hit rate on gold + sdg.  Role
  head gets real surfaces to train on.  Slot distribution matches
  the catalog taxonomy.

### conversational (LLM-generated entities, no catalog)

* **Why no catalog**: the `object` slot is open-vocabulary — any
  noun could be an object of preference (food, place, activity,
  movie, ...).  A catalog would be hopelessly incomplete and
  would bias training toward only the catalog's entries.
* **Entity picking**: Spark generates the primary + alternative
  entities directly as part of the utterance.  The prompt asks
  for "a short natural preference statement" with `slot=object`
  on the primary and `slot=alternative` or `slot=object` on the
  contrast partner.
* **Role span extraction**: Spark returns text *and* explicit
  character-offset annotations for the primary + alternative
  spans, validated to satisfy `text[char_start:char_end] ==
  surface`.  Rows that fail this check are dropped.
* **Inference-time compatibility**: GLiNER (zero-shot NER)
  handles open-vocabulary entity extraction at inference.  The
  role head learns to classify `primary` vs `alternative` from
  linguistic context, not catalog membership.
* **Trade-off**: slightly higher validation drop rate (~5-10% vs
  ~1% for catalog-grounded) because Spark sometimes produces
  off-by-one offsets.  Acceptable.

### clinical (seed catalog + LLM expansion)

* **Catalog status**: none exists today.  The generator seeds a
  starter catalog inline (~50 medications, ~30 procedures, ~30
  symptoms) drawn from common clinical references, cited in
  comments.  Not exhaustive, but enough to ground ~40% of gold
  rows.
* **Hybrid picking**: archetypes alternate between
  catalog-grounded and LLM-generated modes.  Catalog-grounded
  mode works like software_dev.  LLM-generated works like
  conversational.
* **Medical accuracy**: Spark outputs are sanity-checked against
  a simple allowlist of clinical nouns; rows with obvious
  non-medical `medication` slots get flagged for human review.
* **Follow-up**: if v9 clinical passes the gate, Phase D' builds
  out a full clinical catalog so v10 can be fully
  catalog-grounded.

## Generation pipeline

```
For each (domain, split) in {software_dev, conversational, clinical} x {gold, sdg}:

  1. Load archetype specs for domain
  2. For each archetype a:
     2a. Pre-sample entities from catalog OR prepare open-vocab slots
     2b. For batch in batches(a.n_target, size=a.batch_size):
         - Build prompt (archetype description + phrasings +
           few-shot examples + entity hints + JSON schema)
         - Call Spark Nemotron via call_llm_json
         - Parse response (list of rows with text + offsets)
         - For each returned row:
             * Validate through GoldExample loader (schema)
             * Extract role_spans from offsets or gazetteer
             * Assert labels match archetype (intent / admission / state_change)
             * Assert text length in [min, max]
             * Append to output JSONL (flush per batch)
     2c. Count rows actually produced; if < a.n_target * 0.9,
         queue top-up batch
  3. Final audit pass:
     - 10% sample re-labeled by Spark with different prompt
     - Per-archetype agreement % logged
     - Per-head class distribution dumped to class_distribution.json
     - Any class with < floor(30) rows triggers a top-up loop
```

Output:

```
adapters/corpora/v9/
  software_dev/
    gold.jsonl              # ~500 rows
    sdg.jsonl               # ~2500 rows
    adv.jsonl               # ~300 rows (from adversarial templates)
    archetypes.yaml         # snapshot of archetype specs used
    generation_log.json     # per-archetype success / drop / agreement rates
    class_distribution.json # final histogram per head
  conversational/ ...
  clinical/ ...
```

## Validation + audit

### Load-time validation (hard)

Every generated row parses through
`ncms.application.adapters.corpus.loader.load_jsonl`.  Invalid
rows are:
- Dropped (not silently kept)
- Counted per archetype
- Logged with reason (schema violation, missing label, bad offset)

If an archetype's drop rate exceeds 30%, the archetype is flagged
for prompt refinement and the generator halts on that archetype.

### Cross-model audit (soft)

After each split completes, a 10% random sample is re-labeled:

1. Take sample row's `text`
2. Issue a fresh Spark call with a neutral "classify this row"
   prompt (no archetype context, no few-shot examples that match
   the original archetype)
3. Compare Spark's labels to the row's archetype-assigned labels
4. Compute per-archetype agreement % on each head

Per-archetype thresholds:
* Agreement ≥ 90% → archetype is healthy
* 75-90% → warn but accept (label schema is borderline)
* <75% → archetype is unreliable, flag for human review of prompt

Cross-model audit uses ~300 rows/domain × 3 domains = ~900 Spark
calls, takes ~15 min, no cost.

### Human spot-check (process gate)

Before the corpus is committed, a human (the builder) reviews 30
random rows per domain and a 10-row sample from each archetype's
edge cases (shortest rows, longest rows, highest-confidence
`discard`, rows where cross-model audit disagreed).  If obvious
labeling errors exceed 5%, Phase B' doesn't ship — iterate on
the archetype that's producing bad rows.

## Budget

| Resource | Cost |
|---|---|
| Spark Nemotron (local, already running) | $0 |
| OpenAI (optional audit sanity-check, not required) | $0 (not used by default) |
| Wall-clock generation time | ~2-4 hours per domain |
| Human spot-check | ~1-2 hours per domain |

Total v9 generation budget: **$0 in API costs**.  The earlier
$25 OpenAI budget is unused — shifted to Spark because local SLM
meets the quality bar and matches the "no foundation model
dependency" goal.

## File layout

```
src/ncms/application/adapters/sdg/v9/
  __init__.py
  archetypes.py               # ArchetypeSpec + RoleSpec dataclasses
  runner.py                   # generation pipeline
  validator.py                # per-row validation + archetype drop tracking
  audit.py                    # cross-model re-label pass
  domains/
    __init__.py
    software_dev.py           # 16 archetypes
    conversational.py         # 16 archetypes
    clinical.py               # 16 archetypes + starter catalog
  phrasings/
    software_dev/
      positive_adoption.txt
      choice_with_alternative.txt
      ...
    conversational/ ...
    clinical/ ...

scripts/v9/
  generate_corpus.py          # CLI wrapper around runner.py

adapters/corpora/v9/          # output (gitignored large blobs,
                              # committed metadata)
  software_dev/
    gold.jsonl
    sdg.jsonl
    adv.jsonl
    archetypes.yaml
    generation_log.json
    class_distribution.json
  ...
```

## Deliverables

Phase B' ships when:

1. All 3 domains have gold + sdg + adv committed
2. `class_distribution.json` shows every head class ≥ 50 rows
3. Cross-model audit shows ≥ 90% agreement per archetype
4. Human spot-check approval recorded (commit message cites the
   reviewer + N rows reviewed)
5. Unit tests in `tests/unit/adapters/test_v9_corpus.py` verify
   schema + class balance invariants

Only then does Phase C' start (training the v9 adapter on this
corpus).

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Spark Nemotron produces off-by-one character offsets for role_spans | Drop rows that fail `text[s:e] == surface` check.  If drop rate >10%, switch to gazetteer-only (software_dev) or alternate prompt style (conversational). |
| Archetype prompts generate homogeneous text ("I switched to X" × 50 rows) | Phrasing banks rotate 10-15 templates per archetype.  Entity picking rotates catalog without replacement.  Length variance per archetype. |
| A single class stays below floor after top-up | Halt generation.  Surface the issue (which archetype, which class) for prompt redesign.  Never ship an under-floored corpus. |
| Clinical generation produces medically wrong content (e.g. wrong dose) | Not our problem for training — we label text, we don't deploy clinical advice.  Note in docs that v9 clinical adapter is a text-classification artifact, not a clinical recommender. |
| Cross-model audit reveals an archetype has subtle label confusion | Flag, refine prompt, regenerate that archetype only.  Per-archetype regeneration is supported (`--archetype positive_adoption`). |

## Open questions

1. **Few-shot example count per prompt.**  3 examples vs 8?  More
   = better match to archetype, more tokens.  Default: 5.
2. **Should archetype specs be YAML or Python?**  Python for type
   safety + refactor support; YAML for non-engineer editing.
   Default: Python modules importing validated dataclasses.
3. **Conversational `object` slot drift.**  Spark might generate
   "Formula 1" (legitimate preference object) but our gazetteer
   won't detect it.  OK for conversational (LLM-generated
   entities don't need gazetteer hit at inference — GLiNER
   handles it).  Worth stating explicitly.

---

## Results (Phase B' + C', 2026-04-24)

The full pipeline (Phase B'.0 plugin foundation → B'.4 corpus
generation → B'.5 sanity + judge gates → C' training + deploy)
shipped clean v9 adapters for all three domains.  This section
captures the actual numbers so they live next to the design that
predicted them.

### Corpus (Phase B'.4)

Generated by Spark Nemotron 30B Nano (`openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16`) at temperature 0.8, per-row entity
sampling, content-fidelity prompts:

| Domain          | Rows | Generation yield | Sanity   |
| --------------- | ----:| ----------------:| -------- |
| clinical        |  940 |            96.1% | OK       |
| conversational  |  880 |            93.3% | OK       |
| software_dev    |  840 |            94.2% | OK       |

Total: 2660 rows across 18 archetypes (6 per domain).

### Judge (Phase B'.5, content-fidelity prompt)

60 stratified samples per domain, judged by the same Spark
Nemotron model that generated the corpus (acceptable for a
first-pass gate; B'.7 will rerun with a different judge for
publication-quality numbers):

| Domain          | pct_faithful | partial-or-faithful |
| --------------- | -----------:| -------------------:|
| clinical        |        83.3% |               96.7% |
| conversational  |        50.0% |               91.7% |
| software_dev    |        60.0% |              100.0% |

Conversational's lower pct_faithful reflects LLM-prose-quality
variance at temp=0.8 — the dominant failures are ungrammatical
compositions like "every in the morning" when the LLM has to
insert a specific frequency phrase.  Sanity invariants (label
correctness + entity placement) all pass; partials are
training-usable.

A first draft of this judge graded *labels* against text and
returned 0% / 8% / 17% pct_correct — false positives caused by
a prompt-design mismatch (the judge re-derived intent from the
prose, not noticing the labels were archetype-determined by
construction).  The content-fidelity rewrite is the right
question to ask of an SDG corpus.

### Adapter F1 (Phase C', held-out 80/20 eval, 10-epoch training)

LoRA r=16 / alpha=32, BERT-base-uncased, MPS device.  All gates
PASS.

| Domain          | Intent F1 | Topic F1 | Admission F1 | State F1 |
| --------------- | ---------:| --------:| ------------:| --------:|
| clinical        |    0.990  |   0.810  |       1.000  |   0.978  |
| conversational  |    0.965  |   0.815  |       0.946  |   0.985  |
| software_dev    |    0.969  |   0.993  |       0.988  |   0.990  |

11/12 head-domain pairs land at ≥ 0.94 F1.  Loss curves are
monotonic across all three runs (no v8-style oscillation —
caught directly by the per-epoch loss log).

The B'.6 ablation between 6-epoch and 10-epoch training on
clinical caught a real signal: clinical's topic head lifts
from F1 0.556 → 0.810 between epochs 6 and 10 while the other
three heads stay stable.  CLI default raised to 10 epochs.

### What didn't make it (B'.7+ work)

* **Adversarial generator** — the v6/v7 generator's slot
  vocabulary doesn't include `framework` (software_dev) or the
  v9 clinical/conversational slot taxonomy.  Skipped for first
  training; refresh against archetype schema later.
* **Hand-curated gold corpus** — Phase 1 of training currently
  falls back to SDG-as-gold when no curated gold exists.  A
  small (~50 rows / domain) hand-authored gold set would give
  honest train/eval separation and cover the few prose-quality
  failures the judge flagged.
* **Conversational prose polish** — the 5 unfaithful rows in
  conversational (8.3% of judged sample) are temperature-driven
  ungrammatical compositions.  Lower temp + a "verify grammar"
  re-pass would fix them.
* **Per-archetype topic locking** — clinical's topic head learns
  a soft mapping from gazetteer-entry topic; the corpus has 4
  archetypes whose rows mix {chronic_care, acute_care,
  medication_mgmt, other} based on the medication's gazetteer
  topic, with prose only weakly distinguishing.  Fixable by
  forcing one topic per archetype, but loses the gazetteer-
  grounded variety.


### Judge-bias addendum (B'.7, 2026-04-24)

The B'.5 judge ran against Spark Nemotron — the same model that
generated the corpus.  B'.7 re-ran the judge against OpenAI
gpt-4o (different model family) on the same 60-sample stratified
draws.  Results disagree significantly:

| Domain | Nemotron pct_faithful | gpt-4o pct_faithful (adjusted) | Δ |
|---|---|---|---|
| clinical       | 83.3% | 51.2% | −32 pts |
| conversational | 50.0% | 58.5% |  +9 pts |
| software_dev   | 60.0% | 70.0% | +10 pts |

(gpt-4o numbers exclude rows where the LLM call returned
malformed output — about 17–20 per domain — which counts as
``failed`` separately from genuine ``unfaithful`` verdicts.)

**Same-model-family judge significantly overrates clinical.**
Cross-domain the pattern is mixed; conversational + software_dev
slightly UNDER-rated by Nemotron, clinical heavily over-rated.
The clinical gap is consistent with Nemotron sharing training
data with the kind of medical prose it generates — it accepts
its own register as correct where gpt-4o flags it as registrically
ambiguous.

**The unfaithful complaints from gpt-4o are mostly real
archetype-design issues, not pedantry:**

* Clinical: medication ✕ frequency mismatches ("Began
  clobetasol *yearly*" — clobetasol is a daily topical steroid;
  the gazetteer permits any (medication, frequency) pair without
  medical-compatibility constraints).
* Conversational: ``choice_object_vs_alternative`` pairs cross-
  category alternatives ("hot chocolate over Kyoto") because
  the archetype doesn't constrain ``alternative`` to share the
  primary's ``topic_hint``.
* Software_dev: nonsensical subjects ("zoroastrianism adopted
  keras over huggingface") because the prompt allows the LLM
  to invent a subject and Nemotron sometimes picks pathological
  ones.

These are archetype-design fixes (B'.8+):

1. Same-topic constraint on ``alternative`` slot in choice
   archetypes.
2. Per-medication frequency-compatibility table for the clinical
   gazetteer.
3. Subject vocabulary constraint in the SDG prompt
   ("speaker is a clinician / engineer / individual user;
   not a religion / company name / abstract concept").

**Why all rows still train fine:** labels remain correct by
archetype construction.  An "unfaithful" row just has weaker
prose-to-archetype alignment; the trainer still sees the right
label and the right entity placement.  Gate metrics continue
to land at ≥0.94 F1 on three of four heads (clinical topic
the lone exception, B'.6 fix already applied).

**Lesson logged:** any future SDG corpus quality claim must
include a different-judge cross-check.  Same-model-family
judges produce systematically biased verdicts.

### Conversational temp=0.5 regen (B'.7, 2026-04-24)

After observing that conversational's main failure mode at
temp=0.8 was ungrammatical compositions ("every in the morning"),
re-generated the conversational corpus at temp=0.5 and re-trained.
Per-head F1 lifted across the board:

| Head | temp=0.8 v9 | temp=0.5 v9 | Δ |
|---|---|---|---|
| intent       | 0.965 | 0.995 | +0.030 |
| topic        | 0.815 | 0.856 | +0.041 |
| admission    | 0.946 | 1.000 | +0.054 |
| state_change | 0.985 | 0.996 | +0.011 |

Loss curve also cleaner (4.94 → 0.40 at temp=0.8 vs 4.98 → 0.11
at temp=0.5).  The lower temperature produces less surface
diversity but cleaner grammar, which is the better trade for
training data on small archetype taxonomies.  Generation yield
slightly down (93.3% → 92.3%) — fewer rows passed entity-
presence validation, suggesting Nemotron at lower temp adheres
more strictly to entity placement.

