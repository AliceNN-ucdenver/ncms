# NCMS Research Docs

Design documents for in-progress and proposed NCMS research projects. Differs from `docs/completed/` (shipped work) and `docs/` root (steady-state design / specs).

## Active research projects

### CTLG — Causal-Temporal Linguistic Geometry

Rework of (a) the 6th SLM head from classification to compositional semantic parsing (cue-tagging + rule-synthesizer), and (b) the TLG grammar/heuristic layer from implicit per-walker structural scoring to an explicit causal-heuristic suite over typed trajectories. Inspired by PDTB 3.0 discourse parsing, AltLex, MAVEN-ERE event relation extraction, and classical Stilman Linguistic Geometry (with the game-theoretic min-max swapped for causal heuristics to fit the non-adversarial memory-retrieval setting).

Reading order:

1. [`../temporal-linguistic-geometry.md`](../temporal-linguistic-geometry.md) — **READ FIRST.** Authoritative TLG base (P1-shipped, grammar/zone/trajectory framework).
2. [`ctlg-design.md`](./ctlg-design.md) — overall CTLG pivot: 6th head reshape, zone graph evolution (CAUSED_BY edges + counterfactual walker), self-evolving catalog, training data plan, 8-phase roadmap.
3. [`ctlg-grammar.md`](./ctlg-grammar.md) — formal grammar extension: new trajectory subgrammars (`G_tr,c`, `G_tr,m`), typed `Trajectory` class, 5 causal heuristics (`h_explanatory`, `h_parsimony`, `h_recency`, `h_robustness`, `h_counterfactual_dist`), grammar-guided search reduction.
4. [`ctlg-cue-guidelines.md`](./ctlg-cue-guidelines.md) — annotator contract for cue-tagging training data (14 cue families, PDTB/AltLex/TempEval anchored, worked examples, κ ≥ 0.8 target).
5. [`ctlg-migration-audit.md`](./ctlg-migration-audit.md) — what stays / extends / reframes / archives / retires across code, docs, corpora, checkpoints. Phase 0 cleanup checklist.

Source artifacts:

- **Retrospective of the failure it replaces**: [`../completed/failed-experiments/shape-intent-classification.md`](../completed/failed-experiments/shape-intent-classification.md)
- **Forensics that motivated the pivot**: [`../forensics/v7.1-tlg-forensics.md`](../forensics/v7.1-tlg-forensics.md)

## Conventions

- **design docs** go here while they're in-progress. Moved to `docs/completed/` when shipped.
- **failed experiments** get a retrospective under `docs/completed/failed-experiments/` summarising what was tried, what failed, why, and what we carry forward.
- **forensics reports** go to `docs/forensics/<version>-<topic>.md` — point-in-time diagnostics of a running system.
- **benchmarks** document their methodology under `benchmarks/<name>/README.md`; results go to `docs/<name>-results.md`.

## Quick links

- `CLAUDE.md` (project root) — authoritative overview + design decisions list
- `docs/ncms-design-spec.md` — steady-state architecture
- `docs/completed/` — shipped research
- `docs/forensics/` — running-system diagnostics
- `docs/research/` — this directory, proposed work
