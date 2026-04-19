# Intent & Slot Distillation: Replacing Regex Preference with a Learned Classifier

> **Status.** Pre-paper (experiment design, pre-implementation).
> **Date.** 2026-04-19.
> **Relates to.** `docs/p2-plan.md` (preference extraction), `docs/temporal-linguistic-geometry.md` (query-time retrieval intent, separate axis).
> **Experiment code.** `experiments/intent_slot_distillation/` (to be populated per §3).
> **Prerequisite reads.** `docs/p2-plan.md` §4.2 ("the regex the pre-paper is replacing").

---

## Abstract

`p2-plan.md` §4.2 proposes a regex-family extractor for user-preference statements at ingest time — five intent categories (positive / negative / habitual / difficulty / choice) detected via first-person pattern matching.  Regex preference classifiers are brittle by construction: they over-trigger on quoted speech, miss every phrasing not in the pattern list, and require human maintenance whenever a new domain surfaces new preference vocabulary (a recurring problem we already solved for the temporal side with TLG's corpus-driven induction).

This pre-paper proposes three candidate replacements and a head-to-head experiment to pick one before we ship preference extraction into NCMS production.  Contributions:

1. **C1** — A **three-tier shipping hypothesis**: zero-shot default, pre-trained checkpoints for common domains, user-trained domain SLMs — so NCMS keeps its "works out of the box" property while supporting high-precision custom deployments.
2. **C2** — An **evaluation harness** (gold-labeled + LLM-labeled + SDG-expanded) structured so the three candidates are measured on identical data with identical metrics.
3. **C3** — **Decision criteria** tied to concrete F1 / latency / transfer thresholds: every outcome branch maps to a specific shipping decision (no experiment-that-can't-conclude).
4. **C4** — **Axis separation** — preference intent (ingest-side metadata) is explicitly separated from TLG's retrieval intent (query-side dispatch).  We won't conflate them into one model.

---

## 1. What failed, and why a new extractor

### 1.1 Regex preference matching fails at ingest scale

The `p2-plan.md` §4.2 proposal bakes English preference vocabulary into five regex families:

```python
_POSITIVE = [
    re.compile(r"\bI\s+(?:really\s+)?(?:like|love|enjoy|prefer|adore)\s+…", re.I),
    re.compile(r"\bmy\s+(?:favorite|favourite)\s+(?:\w+\s+)?is\s+…", re.I),
    …
]
```

This is exactly the brittleness mode we documented in the TLG pre-paper §1.3 (regex query classifiers).  Concrete failure modes:

* **Phrasing drift.** "Couldn't live without my standing desk" expresses a preference without any of the seeded verbs.
* **Quoted speech false positives.** "The manager said 'I love working weekends'" is a statement *about* someone else's preference, not the user's.  The pattern fires.
* **Negation scoping.** "I don't usually prefer vanilla" — regex picks up "prefer" from `_POSITIVE`, misses the negation that flips it.
* **Domain drift.** A clinical corpus has preferences like "patient reports good tolerance to metformin."  Software-dev has "my go-to stack is FastAPI + Pydantic."  No single regex family captures both.
* **Maintenance burden.** Every new domain = adding patterns = risk of over-fitting to the seed examples.

### 1.2 Confidently-wrong risk

Regex preference extraction emits *synthetic preference memories* into the store (see `p2-plan.md` §4.3).  A misfire creates a fabricated preference the agent then surfaces during retrieval.  Unlike TLG's dispatch layer, **there is no `has_confident_answer()` gate on regex preference extraction** — a false positive at ingest time becomes a permanent synthetic memory.  The composition invariant that protected TLG doesn't apply here.

### 1.3 The axis the current plan conflates

`p2-plan.md` treats "preference" as a single bucket.  In practice the five categories span two different semantic axes:

| Category | Axis | Example |
|---|---|---|
| positive / negative | **Valence** | "I love sushi" vs "I can't stand sushi" |
| habitual | **Frequency** | "I take the subway every morning" |
| difficulty | **Evaluation** | "This test is hard" |
| choice | **Decision** | "I went with the vegetarian option" |

A learned classifier handles multi-axis labelling naturally; a regex family stack does not.  (This is why the p2-plan's `_CHOICE` and `_DIFFICULTY` families would inevitably grow a long "both fire simultaneously" tail.)

---

## 2. Three-tier hypothesis

The right replacement depends on a property the experiment must measure: *does fine-tuning generalize across domains, or does every new domain require its own training pass?*  Without knowing the answer, the safest shipping strategy hedges by offering three tiers:

### Tier 1 — Zero-shot default (no training)

**Stack.** GLiNER for slots (already in NCMS) + E5-small-v2 for intent via descriptive semantic-label matching.  No training, no per-domain artefact.

**Path the query takes:**
1. GLiNER extracts entity slots with pluggable domain labels.
2. E5 encodes the sentence and a fixed set of intent-description embeddings (one per class), picks argmax of cosine similarity.

**Pros.** Zero training cost.  Works on day 1 for every domain NCMS already supports.  Drop-in upgrade over `_POSITIVE` / `_NEGATIVE` regex.

**Cons.** Intent accuracy capped by E5's zero-shot precision — likely strong on `positive` / `negative` (clearly different valence embeddings), weaker on `difficulty` vs `choice` where the descriptive labels are semantically close.

### Tier 2 — Pre-trained NeMo Joint Intent+Slot checkpoints

**Stack.** NeMo's Joint Intent & Slot Classification (`bert-base-uncased` encoder, ~110 M params — the tutorial at `NVIDIA-NeMo/NeMo/tutorials/nlp/Joint_Intent_and_Slot_Classification.ipynb`).  **Smaller** and **single-forward-pass faster** than GLiNER + E5 combined.

**Path.** One model produces intent logits (5 classes) + BIO slot tags per token.  Joint supervision lets intent inform slot tagging and vice versa.

**Pros.** ~3× faster than the two-model zero-shot path (single BERT forward pass vs. two).  Intent + slots in one call.  Ships as a pre-trained checkpoint per common domain: `conversational`, `software-dev`, `clinical`.

**Cons.** Slot taxonomy is fixed at training time (no runtime label swap like GLiNER).  Requires us to publish and maintain checkpoints.  Users in unlisted domains get worse performance than Tier 1 until they train their own.

### Tier 3 — User-trained domain SLM

**Stack.** Same NeMo Joint Intent+Slot architecture, but the user runs a CLI that fine-tunes on their corpus (`ncms tlg train-intent-slot --domain custom --data /path/to/labels.jsonl`).

**Pros.** Best accuracy on the target domain.  Accepts custom slot vocabularies and custom intent categories.

**Cons.** Requires labeled data.  Users without label budgets need an SDG pathway (see §3.2).  GPU required for training (CPU inference is fine).

### Why tier-based?

* Day 1 every NCMS deployment works zero-shot (Tier 1).
* Common domains upgrade by downloading a Tier 2 checkpoint — no training.
* Power users train for their specific corpus (Tier 3) and get best-in-class precision.

**Selection at runtime** is already supported by NCMS's protocol-based DI.  We introduce one new protocol — `IntentSlotExtractor` — and ship three implementations.  The user picks via `NCMS_INTENT_SLOT_BACKEND={zero_shot, pretrained, custom}`.

---

## 3. Experimental methodology

### 3.1 Evaluation corpus

Three domains, three data tiers.  Volume targets informed by PapersWithCode Joint Intent+Slot leaderboards (SNIPS = 13 K, ATIS = 5 K; our 10 K synthetic per domain sits in the same order of magnitude).

| Domain | Source | Gold (hand-labeled) | LLM-labeled (real data) | SDG-expanded (synthetic) |
|---|---|---:|---:|---:|
| conversational | LongMemEval sessions | 200 | 2 000 | 10 000 |
| software-dev | SWE-bench Django + Stack Overflow | 200 | 2 000 | 10 000 |
| clinical | MIMIC-III notes (subset, de-identified) | 100 | — | 5 000 |

**Gold labels** are hand-created by us (calibration anchor).  **LLM labels** come from Qwen-3.5 via Ollama on the existing endpoint — different model family from whatever SDG uses, avoiding train/test contamination.  **SDG expansion** uses either NeMo Curator's synthetic pipeline or Nemotron-3-Nano on `spark-ee7d.local:8000/v1` (already wired in NCMS) with templated prompts.

### 3.2 Synthetic data generation

Template-based SDG:

```yaml
- intent: positive
  template: "I {verb} {object}"
  verbs: [love, enjoy, adore, really like, am a fan of, can't get enough of]
  objects: [sampled from domain entity list]

- intent: negative
  template: "I {verb} {object}"
  verbs: [hate, can't stand, despise, dislike, am not a fan of]

- intent: habitual
  template: "I {freq} {verb} {object}"
  freq: [always, usually, every morning, on weekends, routinely]

…
```

The `object` slots come from the domain's own entity list (LongMemEval entities, Django class names, MIMIC-III medication vocabulary).  This grounds synthetic data in realistic vocabulary.  The template structure is what the classifier is learning to generalize past.

**Hold-out rule.** Gold-labeled examples never enter SDG or training.  LLM-labeled examples are split 70/30 train/validation.  SDG is training-only.  Gold is test-only.  Domain transfer tests hold out an entire domain from training.

### 3.3 Three methods, one harness

```
experiments/intent_slot_distillation/
├── README.md                  # quickstart
├── corpus/
│   ├── lme_labels.py          # hand-labeled gold for LongMemEval
│   ├── swe_labels.py
│   └── mimic_labels.py
├── sdg/
│   ├── template_expander.py
│   ├── nemotron_generator.py  # LLM-side templating
│   └── llm_labeler.py         # label real data via Qwen
├── methods/
│   ├── e5_zero_shot.py        # Tier 1 candidate
│   ├── nemo_joint.py          # Tier 2 / 3 candidate (same code, diff checkpoint)
│   └── gliner_plus_e5.py      # GLiNER slots + E5 intent (current baseline)
├── evaluate.py                # intent F1 + slot F1 + joint acc + latency
└── results/
    ├── matrix_<timestamp>.md  # every method × every domain × every data tier
    └── plots/
```

Each method implements a minimal `IntentSlotExtractor` protocol:

```python
class IntentSlotExtractor(Protocol):
    def extract(self, text: str) -> ExtractedLabel: ...

@dataclass
class ExtractedLabel:
    intent: str
    intent_confidence: float
    slots: dict[str, str]          # slot_name -> surface form
    slot_confidences: dict[str, float]
```

So the harness swaps implementations without branch logic.  When the experiment concludes, the same protocol becomes the NCMS-side entry point (exactly how `IndexEngine` + `GraphEngine` work today).

### 3.4 Metrics

| Metric | How measured | Threshold for "pass" |
|---|---|---|
| **Intent F1** (macro) | Standard seqeval macro F1 over 5 classes | ≥ 0.85 on held-out domain |
| **Slot F1** | seqeval entity-level F1 (BIO tags) | ≥ 0.80 on held-out domain |
| **Joint accuracy** | Example counts as correct iff intent AND all slots match | ≥ 0.75 |
| **Latency p95** | Single-sentence extract, CPU, cold start excluded | < 50 ms |
| **Train cost** | Wall-clock GPU hours to fine-tune on 10 K examples | Informative only |
| **Cross-domain transfer** | Train on domain A, test on domain B's gold | ≥ 0.85 × trained-domain F1 |

We compute each metric for each method on each domain.  Result is a 3-method × 3-domain × 2-split (trained, held-out) matrix plus a latency/cost annotation.

### 3.5 Guardrails (lessons from TLG)

From the TLG experiment we know three things to pre-build:

1. **Determinism regression** — same input → same output, every run (hash-level).  Add to the harness on day one.
2. **Confidently-wrong rate** — count cases where the classifier is high-confidence on an example it got wrong.  Must be ≤ 1% on held-out.
3. **Adversarial set** — construct 20–50 examples designed to trip each method's likely failure mode (quoted speech, negation, double-preference sentences).  Report separately from main F1.

---

## 4. Decision criteria

After the matrix is populated, we branch:

### Branch A — E5 zero-shot is competitive (within 5 F1 of NeMo Joint)

Ship Tier 1 only.  Skip the pre-trained checkpoint pipeline.  Update `p2-plan.md` §4.2 to replace the regex families with E5 semantic matching.  Total code deletion vs. current p2-plan: regex families removed, ~60 lines of `preference_extractor.py` replaced by a 20-line E5 wrapper.

### Branch B — NeMo Joint wins trained-domain by ≥ 5 F1 AND transfers at ≥ 0.85

Ship all three tiers.  Publish NeMo Joint checkpoints for conversational / software-dev / clinical on Hugging Face Hub (or NGC).  CLI command for Tier 3 training.  Update `p2-plan.md` with tier-selection guidance.

### Branch C — NeMo Joint wins trained-domain but transfers poorly (< 0.7)

Ship Tier 1 as the default + Tier 3 as opt-in for domain-specific deployments.  Skip Tier 2 — we can't publish a generic checkpoint users can trust out of the box.  Document the narrow use case.

### Branch D — Neither beats the baseline

Back to the drawing board.  Log what went wrong (likely: data quality, SDG distribution mismatch, or the label taxonomy itself is too fuzzy for automated classification).

**Every branch has a concrete shipping decision.  We won't end up with "interesting but inconclusive."**

---

## 5. Experimental plan

Following the TLG milestone pattern:

| # | Milestone | Effort | Artifact |
|---|---|---:|---|
| IS-M1 | Hand-label 500 gold examples across 3 domains | 2–3 days | `experiments/intent_slot_distillation/corpus/*.py` |
| IS-M2 | E5 zero-shot baseline on gold | 1 day | `e5_zero_shot.py` + baseline F1 |
| IS-M3 | GLiNER + E5 two-pass baseline | 1 day | `gliner_plus_e5.py` |
| IS-M4 | Template SDG expander producing 10 K synthetic examples | 2 days | `sdg/template_expander.py` + dataset |
| IS-M5 | LLM labeler for real corpus data (Qwen via Ollama) | 1 day | `sdg/llm_labeler.py` |
| IS-M6 | Fine-tune NeMo Joint Intent+Slot on conversational domain | 2 days | `methods/nemo_joint.py` + checkpoint |
| IS-M7 | Repeat training on software-dev + clinical | 3 days | Two more checkpoints |
| IS-M8 | Cross-domain transfer evaluation | 1 day | Full 3×3×2 matrix |
| IS-M9 | Adversarial + confidently-wrong audit | 1 day | `adversarial.py`, audit report |
| IS-M10 | Decision + p2-plan revision | 0.5 day | Updated `docs/p2-plan.md` §4 |

Total effort ~14 working days (3 calendar weeks with background time).

## 6. What this does NOT touch

* **TLG.**  The structural query parser in `ncms.domain.tlg.query_parser` is a separate axis (retrieval intent, not preference intent) and stays as shipped.
* **Memory storage format.**  Preference extraction still emits synthetic memories the same way `p2-plan.md` §4.3 described; only the extraction method changes.
* **Reconciliation.**  Preference memories don't participate in the HTMG supersedence layer.  A later extension might ("I used to love vanilla, now I prefer chocolate") but that's out of scope.
* **P1/TLG feature flags.**  `NCMS_TLG_ENABLED` stays independent.  New flag: `NCMS_INTENT_SLOT_BACKEND={zero_shot, pretrained, custom}`.

---

## 7. Why this is the right shape

We learned from TLG that the successful experiment shape has three properties:

1. **Experiment-first in a dedicated folder.**  Don't touch NCMS production code until the experiment converges.
2. **Concrete decision criteria baked in.**  Every outcome maps to a shipping choice before we start.
3. **Multiple candidate methods, one test harness.**  Swap implementations behind a shared protocol so the winner ports cleanly.

This pre-paper follows that shape.  If the experiment converges, the winning implementation drops into `src/ncms/infrastructure/extraction/intent_slot_extractor.py` behind the `IntentSlotExtractor` protocol and the p2-plan's §4.2 regex section gets deleted.

---

## Appendix A — Why not a bigger LLM?

The user could ask Nemotron-Nano-2 (9 B) to classify every incoming memory.  Reasons we don't:

* **Latency.**  9 B params is 30-50× slower than BERT-base on CPU; unacceptable on the ingest path.
* **Size mismatch for the task.**  Intent + slot classification is exactly the supervised-learning problem BERT was designed for.  Generative models are the wrong tool shape.
* **Determinism.**  A learned classifier is deterministic; a generative model sampling from a distribution is not.  NCMS ingest needs the former.
* **Cost.**  A local 9B model pins a GPU; ingest should run on any laptop.

Nemotron-Nano-2 stays a useful tool for one-off LLM labeling (§3.1) but never for the hot-path classifier.
