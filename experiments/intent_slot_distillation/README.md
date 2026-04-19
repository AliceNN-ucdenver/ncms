# Intent & Slot Distillation — Experiment Folder

Companion to `docs/intent-slot-distillation.md`.  This folder holds
the experiment code (corpora, generators, methods, evaluation
harness) that feeds the decision branches in the pre-paper §4.

**Not in production.**  Like `experiments/temporal_trajectory/`
was for TLG, nothing here should be imported from
`src/ncms/…`.  When the experiment converges, the winning method
is ported — not imported — into NCMS under the
`IntentSlotExtractor` protocol.

## Layout

```
experiments/intent_slot_distillation/
├── README.md             ← this file
├── corpus/               # hand-labeled gold per domain
├── sdg/                  # synthetic + LLM-labeled data generators
├── methods/              # three candidate classifiers
└── evaluate.py           # shared harness; produces the 3×3×2 matrix
```

## Running the experiment

Each milestone (IS-M1 through IS-M10 in the pre-paper) corresponds
to a script in this folder.  Run in order:

```bash
# IS-M1: hand-label gold (manual; produces corpus/*.jsonl)
uv run python -m experiments.intent_slot_distillation.corpus.lme_labels --edit

# IS-M2 / IS-M3: zero-shot + two-pass baselines
uv run python -m experiments.intent_slot_distillation.evaluate \
    --methods e5_zero_shot,gliner_plus_e5 \
    --domain conversational

# IS-M4: SDG expansion
uv run python -m experiments.intent_slot_distillation.sdg.template_expander \
    --domain conversational --target 10000

# IS-M6: fine-tune NeMo Joint
uv run python -m experiments.intent_slot_distillation.methods.nemo_joint train \
    --data corpus/conversational_train.jsonl \
    --epochs 5

# IS-M8: full matrix
uv run python -m experiments.intent_slot_distillation.evaluate \
    --methods all --domain all --splits all \
    --output results/matrix_$(date -u +%Y%m%dT%H%M%SZ).md
```

## Decision criteria (copy from pre-paper §4)

| Branch | Condition | Ships as |
|---|---|---|
| A | E5 zero-shot within 5 F1 of NeMo Joint | Tier 1 only |
| B | NeMo Joint > 5 F1 AND transfers ≥ 0.85 | All three tiers |
| C | NeMo Joint > 5 F1 but poor transfer | Tier 1 + Tier 3 opt-in |
| D | Nothing beats baseline | Revisit data quality + taxonomy |

## Porting to NCMS (post-experiment)

1. Move the winning method behind
   `src/ncms/infrastructure/extraction/intent_slot_extractor.py`
   implementing the `IntentSlotExtractor` protocol from
   `src/ncms/domain/protocols.py`.
2. Delete the regex preference extractor proposed in
   `docs/p2-plan.md` §4.2.
3. Add `NCMS_INTENT_SLOT_BACKEND={zero_shot,pretrained,custom}` to
   `src/ncms/config.py`.
4. Wire through `IngestionPipeline.run_inline_indexing` the same
   way GLiNER is wired today (parallel with entity extraction,
   feature-flagged).
5. Dashboard events: `intent_slot.extracted` per memory.
