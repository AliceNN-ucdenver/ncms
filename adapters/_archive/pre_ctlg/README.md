# Pre-CTLG archive

This directory contains adapter artifacts and code snapshots from the pre-CTLG era (through v7.2, 2026-04-23). Retained for:

1. **Historical comparison** — MSEB baselines, forensics results, and published metrics were measured with these adapters. Reproducing pre-pivot numbers needs old code + old adapters.
2. **Compatibility** — the loader still supports these manifests, and operators running old deployments may upgrade incrementally.
3. **Research record** — three SLM experiments lived in this era:
   - **v4 / v6** — early BIO slot tagger
   - **v7** (and `v7_initial`) — span-role classifier, pre-fix
   - **v7.1** (frozen copy at `v7.1_frozen`) — span-role classifier with max-confidence slot reconstruction + gold repair; slot F1 = 0.807 on 367-row eval
   - **v7.2** *(lives outside this archive at `adapters/checkpoints/software_dev/v7.2/`)* — migration-direction-corrected SDG templates + 304 natural shape_intent queries; slot F1 = 0.813 on 671-row eval; **this is the pre-pivot baseline**

The v6/v7/v7.1 artifacts here carry the failed `shape_intent` classification head. Retrospective at [`docs/completed/failed-experiments/shape-intent-classification.md`](../../../docs/completed/failed-experiments/shape-intent-classification.md) explains the failure mode (100% train / 25.6% held-out on natural queries — template-scaffold overfit).

## Contents

### `checkpoints/`
Trained adapter artifacts — `lora_adapter/`, `heads.safetensors`, `manifest.json`, `eval_report.md`.

| dir | role_head vs slot_head | gate slot_f1 | notes |
|-----|------------------------|--------------|-------|
| `v4/` | BIO slot_head | low | Early experiment |
| `v6/` | BIO slot_head (refined) | ~0.46 | v6 baseline |
| `v7/` | role_head (span classifier) | 0.662 | Initial v7; has first-wins reconstruction bug |
| `v7_initial/` | role_head | 0.662 | Same artifact, preserved |
| `v7.1_frozen/` | role_head + gold repair + max-conf reconstruction | 0.807 | Preserved before v7.2 retrain |

### `corpora/`
Intermediate backup files from the v7/v7.1/v7.2 pipeline:
- `*.pre_v7role.bak` — gold/sdg/adversarial files snapshotted before the role-head migration
- `*.pre_v7.2.bak` — shape_intent gold snapshot before the 304-row natural-query expansion

### `snapshots/`
Pure source-code snapshots at the pre-CTLG boundary (2026-04-23), kept for A/B / diff purposes:
- `templates_pre_ctlg.py` — the SDG templates with the backwards-direction migration patterns (primary on LHS of "from X to Y" — the bug that caused role-head migration-direction confusion on v7.1)
- `dispatch_pre_ctlg.py` — the TLG dispatcher using implicit per-walker scoring (no typed causal heuristics)
- `joint_bert_lora_pre_ctlg.py` — the LoRA joint model with shape_intent classification head (argmax over 13 labels on [CLS] pool)

## Policy

Do **NOT** delete without a quorum decision. If space is needed, compress checkpoint directories rather than removing them:

```bash
cd adapters/_archive/pre_ctlg/checkpoints
for d in */; do tar czf "${d%/}.tar.gz" "$d" && rm -rf "$d"; done
```

## What's NOT here

- **v7.2** — lives in `adapters/checkpoints/software_dev/v7.2/` and `~/.ncms/adapters/software_dev/v7.2/` as the active **pre-pivot production baseline**. Stays there until v8 ships.
- **v7.1** — same, stays at `adapters/checkpoints/software_dev/v7.1/` as the next-most-recent deployed baseline.
- **Live catalog / gold corpora** — stay at `adapters/corpora/` and `src/ncms/application/adapters/sdg/catalog/`. The 5 content heads remain correct; only the 6th head is being reshaped for CTLG.

## Pointers

- Design of the replacement: [`docs/research/ctlg-design.md`](../../../docs/research/ctlg-design.md)
- Grammar extension: [`docs/research/ctlg-grammar.md`](../../../docs/research/ctlg-grammar.md)
- Migration audit (what stays/reframes/retires): [`docs/research/ctlg-migration-audit.md`](../../../docs/research/ctlg-migration-audit.md)
- Forensics that motivated the pivot: [`docs/forensics/v7.1-tlg-forensics.md`](../../../docs/forensics/v7.1-tlg-forensics.md)
