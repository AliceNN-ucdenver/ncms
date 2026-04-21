# swe_diff/v1 — Adapter Datasheet

A ``DATASHEET`` in the sense of Gebru et al. (2018): everything
another researcher needs to reproduce or audit this adapter.

## What it is

A LoRA-fine-tuned BERT-base-uncased classifier with five heads
(intent / slot-BIO / topic / admission / state_change) tuned for
**software-engineering diff-shaped content**: raw git diff headers,
issue bodies with code snippets, PR-review discussion prose.

Distinct from the existing ``software_dev/v4`` adapter which was
trained on *prose* state-change content (ADRs, preferences,
opinions — "I prefer async over threads").  See
``experiments/intent_slot_distillation/schemas.py:DOMAIN_MANIFESTS``
for the registry wiring.

## Why a separate adapter was needed

Initial MSEB-SWE runs with the ``software_dev/v4`` adapter on
SWE-bench Verified scored ``rank-1 = 0.31`` — far below clinical
(0.74).  Forensic analysis
(``benchmarks/mseb/run-logs/forensic-swe-*.json``) showed:

- ``state_change_head`` classified **100 % of resolving patches as
  ``declaration``** instead of ``retirement`` → NCMS's
  reconciliation mechanism never built supersession edges for SWE
  content.
- The intent classifier (BM25 exemplar) routed retirement queries
  like *"Which diff removed the faulty implementation of X?"* to
  ``fact_lookup`` with confidence 1.00 — the exemplars were
  conversational and had no overlap with diff vocabulary.

The mismatch was structural: the adapter was trained on prose; the
benchmark corpus was raw diffs.  This adapter addresses the
distribution gap for the specific case where retrieval operates
over version-control artefacts.

## Training data — SWE-Gym, zero-overlap with benchmark

### Source

- **Dataset**: ``SWE-Gym/SWE-Gym`` on HuggingFace
  ([link](https://huggingface.co/datasets/SWE-Gym/SWE-Gym))
- **License**: MIT (same upstream project licenses — pandas BSD,
  pydantic MIT, etc.).
- **Schema**: identical to SWE-bench Verified (instance_id,
  problem_statement, hints_text, patch, test_patch, …).
- **Size**: 2,438 training issues.

### Disjoint from the MSEB-SWE benchmark

MSEB-SWE benchmarks on SWE-bench Verified (500 issues across 12
projects: django, astropy, matplotlib, scikit-learn, sympy, xarray,
sphinx, pytest, pylint, pvlib, requests, flask, seaborn).

SWE-Gym training covers 11 **completely different** projects:

| Project | Train issues |
|---|---:|
| pandas-dev/pandas       | 737 |
| Project-MONAI/MONAI     | 374 |
| getmoto/moto            | 343 |
| python/mypy             | 257 |
| iterative/dvc           | 225 |
| dask/dask               | 145 |
| modin-project/modin     | 107 |
| pydantic/pydantic       |  83 |
| conan-io/conan          |  75 |
| facebookresearch/hydra  |  66 |
| bokeh/bokeh             |  26 |
| **Total**               | **2,438** |

**Zero repo overlap** with MSEB-SWE benchmark.  This is an
out-of-distribution generalization test — the adapter must learn
diff structure, not memorize Verified issues.

### Pipeline to produce ``gold_swe_diff.jsonl``

```bash
# 1. Mine — shuffle for balanced project coverage
uv run python -m benchmarks.mseb_swe.mine \
    --dataset swe_gym --limit 2500 --shuffle-seed 42 \
    --out-dir benchmarks/mseb_swe/raw_gym

# 2. Label — same source→MemoryKind rules as MSEB-SWE labeler
#    issue_body → declaration / ordinal_anchor
#    pr_discussion → causal_link
#    resolving_patch → retirement
#    test_patch → declaration
uv run python -m benchmarks.mseb_swe.label \
    --raw-dir benchmarks/mseb_swe/raw_gym \
    --out-dir benchmarks/mseb_swe/raw_gym_labeled

# 3. Transform to training GoldExample schema
uv run python -m benchmarks.mseb.build_swe_diff_gold \
    --labeled-dir benchmarks/mseb_swe/raw_gym_labeled \
    --out experiments/intent_slot_distillation/corpus/gold_swe_diff.jsonl
```

### Label distribution in ``gold_swe_diff.jsonl``

- **Total examples**: 8,842
- **state_change**: declaration 4,876 / retirement 2,438 / none 1,528
- **topic**: core_module 7,077 / test_module 1,118 / docs 638 /
  build 6 / config 3
- **intent**: all ``none`` — diff content carries no preference
  signal.  The intent head trains as a constant predictor and is
  masked out of the eval gate (``--intent-f1-min 0.0``).
- **admission**: all ``persist`` — same rationale (the MSEB corpus
  only contains persist-grade messages by construction).

### Shuffle fingerprint

- Upstream HF dataset ``SWE-Gym/SWE-Gym`` split=``train``
- Shuffle seed: ``42`` (via ``datasets.Dataset.shuffle(seed=42)``)
- Limit: ``2500`` (captures all 2,438 rows)

Re-running the pipeline with the same seed yields byte-identical
``gold_swe_diff.jsonl``.

## Training configuration

```
encoder        = bert-base-uncased
LoRA r         = 16
LoRA alpha     = 32
LoRA dropout   = 0.05
LoRA targets   = query, value
batch size     = 16
epochs         = 4
learning rate  = 5e-4
max length     = 128
seed           = 42
device         = MPS (Apple Silicon) or auto
```

Invoked via:

```bash
uv run python -m experiments.intent_slot_distillation.train_adapter \
    --domain swe_diff \
    --adapter-dir experiments/intent_slot_distillation/adapters/swe_diff \
    --taxonomy experiments/intent_slot_distillation/taxonomies/swe_diff.yaml \
    --version v1 \
    --skip-sdg --skip-adversarial \
    --epochs 4 --batch-size 16 \
    --intent-f1-min 0.0 --slot-f1-min 0.0 --conf-wrong-max 1.0 \
    --eval-splits gold
```

The gate thresholds are set permissively because intent + slot heads
have weak signal on diff content (single-class intent; slots are
path/symbol strings rather than preference objects).  The gate is
not intended to block this adapter — we audit state_change + topic
head F1 directly in the eval report.

## Deployment

- Training output: ``experiments/intent_slot_distillation/adapters/swe_diff/v1/``
- Runtime path  : ``~/.ncms/adapters/swe_diff/v1/``
- Loaded by NCMS when ``NCMS_INTENT_SLOT_CHECKPOINT_DIR`` or
  ``--adapter-domain swe_diff`` on the benchmark harness resolves
  to this path.

## Intended evaluation

- **MSEB-SWE** (SWE-bench Verified) with ``--adapter-domain swe_diff``
- Compared against the same benchmark with ``--adapter-domain
  software_dev`` (prose adapter, mismatched) and ``--slm-off`` (no
  adapter at all).  The three-way comparison isolates the value of
  a distribution-matched adapter.

## Limitations, risks, what NOT to claim

- **Intent head has no training signal.**  Do not report per-intent
  F1 from this adapter.  If the paper needs a ``per_intent_f1`` cell,
  mark it "N/A — adapter not trained on intent labels".
- **Admission head is constant.**  Same caveat.
- **slot_head labels are PATHS and SYMBOLS**, not preference objects.
  The slot universe (``file_path`` / ``test_path`` / ``symbol`` /
  ``function`` / ``issue_ref``) is domain-specific and cannot be
  directly compared to ``software_dev``'s slot F1 numbers.
- **Out-of-distribution for non-Python projects**: all SWE-Gym repos
  are Python.  Performance on Go / TypeScript / Rust diffs is
  extrapolation.
- **Pretraining leakage concern**: BERT-base-uncased was trained on
  Wikipedia + BooksCorpus, which may contain incidental overlap with
  public GitHub issue text.  This is the same pretraining-leakage
  caveat that applies to every transformer-based system in the
  comparison, including mem0's MiniLM.
