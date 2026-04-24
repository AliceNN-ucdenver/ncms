# Failed experiment: v8 / v8.1 joint six-head training saturation

**Duration:** 2026-03 → 2026-04
**Status:** RETIRED — superseded by v9 YAML-plugin 5-head architecture

## Summary

v8 extended the v6 five-head classifier with a sixth head: a BIO-
tagged sequence labeler for CTLG causal / temporal / ordinal /
modal / referent / subject / scope cues, intended to replace the
hand-coded TLG query parser with a compositional semantic
parser.  Under joint training on one shared BERT+LoRA encoder,
the sequence-labeling head and the five classification heads
competed for encoder capacity and failed to converge cleanly.
Training loss bounced, held-out metrics stayed flat, and several
heads that had been healthy in v6 regressed.  We killed the run,
retired the sixth head, and moved the remaining five heads to
the v9 YAML-plugin architecture.

## What we tried

**v8 design:** six heads on one shared rank-16 LoRA-adapted
BERT-base encoder, trained end-to-end with a weighted multi-task
loss:

| Head | Shape | Labels | Weight |
|---|---|---|---|
| `intent_head` | CLS-token classification | ~10 intent categories | 1.0 |
| `admission_head` | CLS-token classification | persist / ephemeral / discard | 1.0 |
| `state_change_head` | CLS-token classification | declaration / retirement / none | 1.0 |
| `topic_head` | CLS-token classification | per-domain topic vocab (~7 labels) | 1.0 |
| `role_head` | per-span classification over gazetteer spans | primary / alternative / casual / not_relevant | 1.0 |
| `cue_tagger_head` (NEW) | per-token BIO sequence labeling | ~30 labels (PDTB + AltLex + TempEval) | 0.5 then 1.0 |

The cue tagger used standard BIO encoding — `B-CAUSAL_CAUSE`,
`I-CAUSAL_CAUSE`, `B-TEMPORAL_BEFORE`, `O`, etc. — emitted from
the same encoder's per-token hidden states with a thin linear
head.

**v8.1 variant:** rebalanced loss weights (0.3 on the four
content heads, 0.7 on the cue tagger) to try to give the new
head more gradient signal without starving the others.  Also
tried curriculum learning — train the cue tagger alone for 2
epochs, then unfreeze the rest.

## What broke

### Training-loss instability

The joint loss oscillated throughout the v8 run, never settling
into the monotone decline we saw from v6 (five classification
heads only).  The oscillation pattern was visible on every
training iteration, not just across epochs.  We attribute it
to competing gradient directions:

- Classification heads pull the encoder toward a discriminative
  CLS-summary representation.
- The cue tagger pulls the encoder toward preserved per-token
  discrimination for sequence labeling.

Neither objective dominated cleanly with a rank-16 LoRA adapter
as the only shared-encoder capacity, so each batch's update
partially undid the previous one's.  The run was terminated
before converging.

### Qualitative regression signal

We do not have precise held-out F1 numbers for the v8.1
attempt — training was killed before the scheduled eval pass
landed, specifically because the loss curve showed no sign
of stabilizing.  What we *did* observe from the live training
telemetry:

- The four classification heads (admission / state_change /
  intent / topic / role) had all been stable at v6/v7 under
  solo training; mid-v8.1 per-batch accuracy on those heads
  degraded visibly compared to the v7 baseline snapshots.
- The cue tagger head was the newcomer and never hit a
  regime where per-token accuracy approached its v6 classifier
  peers.

This is narrative evidence, not a metrics table.  The v9
training harness re-introduces an explicit per-head held-out
eval at epoch 1 + 2 so future failure modes surface with
numbers, not vibes.

### User signal

Mid-training: **"it's bouncing like that it's a failed run —
kill it."**  Correct call — the call was made on the loss-curve
oscillation alone, not on held-out metrics (which is why we
can't cite precise held-out deltas above).  The v9 harness
keeps this human-in-the-loop check as a first-class gate.

### Corpus-quality compounding factor

Separately from the architectural issue, the SDG v7 corpus was
too narrow for joint training.  Held-out validation surfaced
that generated utterances shared too much prefix structure
across archetypes — the classifiers could crib on template
scaffolds, which hid the encoder regression on in-template
queries.  The v8 failure mode was only obvious on held-out
hand-crafted probes.  This is the same diagnostic gap that
masked the v7.x `shape_intent` failure; in v9 we invest
upfront in hand-crafted held-out sets per head.

## Root cause

Two orthogonal problems that multiplied:

**1. Task-shape mismatch within a shared encoder.**  Classification
heads extract a global-summary representation; sequence-labeling
heads require preserved per-token discrimination.  Rank-16 LoRA
on BERT-base does not have enough adapter capacity to serve
both objectives simultaneously.  The literature warns about
this (Liu et al 2019 multi-task BERT, Conneau & Lample 2019 on
encoder sharing tradeoffs), but we deferred the decision under
"maybe LoRA's decomposition handles it" optimism.  It didn't.

**2. The cue tagger was the wrong abstraction anyway.**  See the
v7 `shape_intent` retrospective in
`docs/completed/failed-experiments/shape-intent-classification.md`
— the underlying task (compose novel queries into a structured
logical form based on tense / reference / focus / modality)
isn't a classification problem.  The v8 cue tagger tried to
reframe it as sequence labeling, which is closer to the right
shape, but jointly training it with four classification heads
gave it neither the data density nor the encoder capacity it
needed.

## Evidence

The v8/v8.1 runs were terminated before the scheduled held-out
eval pass executed, so we do not have archived per-epoch F1
artifacts.  What we do have:

- Pre-v9 checkpoint + corpus artifacts under
  `adapters/_archive/pre_ctlg/` (checkpoints, legacy corpora,
  snapshots) — the state of the world immediately before the
  v9 refactor began.
- The sibling post-mortem documenting the same underlying
  failure mode on the v7.x classification variant:
  `docs/completed/failed-experiments/shape-intent-classification.md`
  (100% train / 25.6% held-out on the `shape_intent` head,
  which v8.1's cue tagger was meant to replace but did not fix).
- The user's live kill decision, made on loss-curve
  oscillation rather than held-out metrics.

In v9 the eval schedule is inverted: a short held-out pass
runs at epoch 1 + 2 *before* the full training completes,
precisely so a future v8-style failure surfaces as numbers
and not just vibes.

## What we changed (v9)

**Dropped the sequence-labeling head.**  The CTLG cue tagger is
retired from the 5-head trainer.  CTLG research continues as a
design-track-only effort (`docs/research/ctlg-design.md`),
targeting a separately-trained encoder with its own data
regime — NOT co-trained with the classification heads.

**Moved slot detection into role_head (kept from v6).**  The
gazetteer's `detect_spans()` finds candidate spans via
longest-match lookup.  GLiNER handles the open-vocab fallback.
`role_head` classifies each span as primary / alternative /
casual / not_relevant.  No BIO tagging inside the SLM.

**YAML plugin architecture for domains.**  The v6/v7/v8 code
had per-domain Python modules under
`src/ncms/application/adapters/sdg/catalog/`.  v9 moves that to
`adapters/domains/<name>/{domain,gazetteer,diversity,archetypes}.yaml`
with one validated loader (`domain_loader.py::load_domain` →
`DomainSpec`) that every downstream component reads through.
Adding a domain is now a pure-YAML contribution.  See
`docs/add-a-domain.md` for the end-to-end walkthrough.

**Regenerated corpus with better diversity.**  Phase B'.2-5
rewrites the SDG pipeline around archetype + diversity YAML,
with per-archetype Spark Nemotron prompts and openai-backed
quality gates.  Hand-crafted held-out sets land before training
so we surface task-shape failures *before* burning compute.

## What we kept

- **5-head design** (intent / role / topic / admission /
  state_change) — retained unchanged from v6.  These five
  heads trained cleanly under the v6 single-task regime; only
  the addition of the sixth sequence-labeling head in v8
  destabilised the shared encoder.
- **LoRA multi-head architecture** — still sound for
  classification-only heads.  The rank-16 capacity is adequate
  when all heads share the same task shape.
- **Gazetteer-backed slot detection** (`detect_spans()`) —
  deterministic longest-match lookup still beats GLiNER on
  recall for known surfaces.  The v9 YAML-plugin system is
  built around this same detection primitive.
- **Bitemporal state nodes** (`observed_at`, `valid_to`,
  `is_current`) — unchanged.
- **Zone graph model** (L1 atomic, L2 entity_state, L3 episode,
  L4 abstract) — unchanged.  The `CAUSED_BY` edge type that CTLG
  proposed stays as a pending design-track item; not wired in
  v9.

## Lessons learned

1. **Do not co-train classification heads and sequence-labeling
   heads on a shared small encoder.**  Their gradient objectives
   compete and rank-16 LoRA does not have enough capacity to
   serve both.  If you need both capabilities, train separate
   encoders.
2. **"Classifier saturation" looks like loss oscillation in the
   first 3 epochs.**  If the joint loss is bouncing rather than
   monotonically decreasing by epoch 3, kill the run — more
   epochs will not fix task-shape-mismatched gradients.
3. **Regression on previously healthy heads is the earliest
   reliable signal.**  Live per-batch accuracy on the four v6
   classification heads dipped below their solo-training
   baseline within the first few v8 epochs, well before the
   cue tagger produced usable outputs.  Set an explicit guard
   (e.g. "if any head's per-batch accuracy drops materially
   from the pre-joint baseline at the first eval window, halt").
4. **Hand-crafted held-out sets are non-negotiable.**  The
   lesson from the v7.1 `shape_intent` retrospective (100%
   train, 25.6% held-out — see the sibling doc) is that
   in-template metrics are systematically misleading when
   the SDG corpus shares surface structure across archetypes.
   Any future head must ship alongside a hand-crafted
   paraphrased held-out probe set, authored before training
   starts and held back from the generator pipeline.
5. **Carry the retrospective forward into the next architecture's
   design doc.**  The v9 plugin architecture explicitly excludes
   sequence-labeling heads from the 5-head trainer, and the
   CTLG design doc now carries the v8 failure note alongside
   the v7.1 `shape_intent` note as a "why we're not doing this
   in-band" pointer.

## Status

v9 Phase B'.0 is done: catalog consolidation (712 software_dev
entries, 536 clinical), YAML plugin foundation, three domain
directories (software_dev / conversational / clinical),
`schemas.py` hydration from the YAML registry,
`NCMS_V9_DOMAIN_LOADER=1` default flag.

v9 Phase B'.2-5 is in flight: corpus regeneration pipeline
(Spark Nemotron + openai quality gates, archetype-stratified
batching, hand-crafted held-out sets per head).

v9 Phase C' is pending: 5-head training on the regenerated v9
corpus.  Trained adapters will land at
`~/.ncms/adapters/<domain>/v9/` (~2.4 MB each).  No v8
checkpoint is kept as a baseline — we're restarting from the
v6 training regime on the cleaner v9 corpus.

Related docs:
- `docs/completed/failed-experiments/shape-intent-classification.md`
  (v7.1 predecessor failure)
- `docs/research/v9-domain-plugin-architecture.md` (v9 design)
- `docs/add-a-domain.md` (walkthrough for adding a domain)
- `docs/research/ctlg-design.md` (CTLG research track —
  design-only, no v9 training)
- CLAUDE.md §27 (intent-slot SLM, current state)
