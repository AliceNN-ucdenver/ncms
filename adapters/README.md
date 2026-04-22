# NCMS adapters — training artifacts

First-class NCMS artifact directory.  Holds everything needed to
(re)train and deploy the 6-head SLM LoRA adapters without reaching
into `experiments/`.

```
adapters/
├── corpora/                            ← training data
│   ├── gold_<domain>.jsonl             hand-labeled gold
│   ├── gold_shape_intent_<domain>.jsonl  query-voice 6th-head gold
│   ├── sdg_<domain>.jsonl              typed-slot synthetic data
│   ├── adversarial_train_<domain>.jsonl  hard negatives
│   └── mixed_<domain>.jsonl            legacy joint-bert splits
├── taxonomies/
│   └── <domain>.yaml                   topic / admission / state_change label sets
├── checkpoints/
│   └── <domain>/
│       └── <version>/                  trained LoRA weights + manifest.json
└── README.md                           (this file)
```

Runtime deployment path is separate:
`~/.ncms/adapters/<domain>/<version>/` — what the extractor chain
loads at service boot.  `ncms adapters deploy` copies from
`adapters/checkpoints/` → `~/.ncms/adapters/`.

## Built-in domains

| domain | slots | what it handles |
|---|---|---|
| `conversational` | object / frequency / alternative | Persona + preference dialog (LongMemEval-shaped) |
| `software_dev` | library / language / pattern / tool / alternative / frequency | ADRs, RFCs, design docs, post-mortems |
| `clinical` | medication / procedure / symptom / severity / alternative / frequency | Case-report prose (PMC Open Access) |
| `swe_diff` | file_path / function / symbol / test_path / issue_ref / alternative | GitHub issue/PR artefacts with diff headers |

Current deployed versions: conversational/v6, clinical/v6,
software_dev/v6, swe_diff/v1.  Next planned version is **v7** with
the rewritten typed-slot SDG (see `ncms adapters list`).

## Operator workflows

```bash
ncms adapters list                             # what exists + what's deployed
ncms adapters status                           # manifest details per domain
ncms adapters generate-sdg --domain software_dev --target 3000
ncms adapters train --domain software_dev --version v7
ncms adapters deploy --domain software_dev --version v7
```

Training writes under `adapters/checkpoints/<domain>/<version>/`
so the checkpoint persists in-repo and can be baked into the hub
Docker image (see `deployment/nemoclaw-blueprint/hub.Dockerfile`).

## Registering a new domain

See `src/ncms/application/adapters/sdg/templates.py` for the
`SlotPool` / `SlotTemplate` primitives.  A new domain needs:

1. A new member of the `Domain` literal in `schemas.py`.
2. A `SLOT_TAXONOMY` entry (the slot label list).
3. A new `<Domain>_TEMPLATES = DomainTemplates(...)` with typed
   pools covering every slot + `SlotTemplate` instances for each
   intent × slot combination.
4. Registration in `TEMPLATE_REGISTRY`.
5. A `DomainManifest` wiring in `DOMAIN_MANIFESTS`.
6. A new `<domain>.yaml` under `taxonomies/` with
   `topic_labels` / `admission_labels` / `state_change_labels`.
7. Gold JSONL at `corpora/gold_<domain>.jsonl` (hand-labeled or
   LLM-labeled via `experiments/intent_slot_distillation/sdg/llm_labeler.py`).

Then: `ncms adapters generate-sdg --domain <X>` + `ncms adapters train`.

## Research artifacts

`experiments/intent_slot_distillation/` keeps research-specific
tooling:

- Baseline methods (E5 zero-shot, GLiNER+E5, joint_bert without LoRA)
- `evaluate.py` — method comparison harness
- `corpus/autolabel_multihead.py` — LLM labeler
- `corpus/build_shape_intent_gold.py` — MSEB curation
- `train_joint_bert.py` / `train_lora_adapter.py` — research variants
- Historical adapter versions (v1, v2, v3, v5, v*_multihead, v*_final)

The production training path — SDG, trainer, gate, adversarial
augmentation — lives at `src/ncms/application/adapters/`.
