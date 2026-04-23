# Failed experiment: `shape_intent` classification head (v6 / v7.1 / v7.2)

**Duration:** 2026-01 → 2026-04
**Status:** RETIRED — superseded by CTLG cue-tagging architecture (see `docs/research/ctlg-design.md`)

## What we tried

A single classification head on the LoRA-adapted BERT SLM that takes a query-voice user question as input and outputs one of 12 TLG grammar shapes (`current_state`, `before_named`, `retirement`, `origin`, `sequence`, `predecessor`, `transitive_cause`, `causal_chain`, `concurrent`, `interval`, `ordinal_first`, `ordinal_last`) or `none`.

The head was added in v6 alongside the five content-classification heads (intent, topic, admission, state_change, slot/role) and trained on 181 labeled queries in v7 and 485 labeled queries in v7.2.

## Why we tried it

- Replace the hand-coded regex query parser in `ncms.domain.tlg.query_parser` (brittle, hard to maintain).
- Reuse the existing SLM encoder — one forward pass, six outputs, no new model to ship.
- Pattern-match the 5-head success: the content heads all hit F1 ≥ 0.90 with small training sets, so we assumed the 6th would too.

## What happened

### v7 / v7.1 / v7.2 metrics

| Adapter | Training gold | Held-out (hand-crafted natural queries) |
|---------|---------------|------------------------------------------|
| v7.1 | 100% | **25.6%** |
| v7.2 (projected, pre-gate) | 100% | ~50-65% (expected) |

The training gold accuracy was driven by memorization — the 181/485 training queries all shared only 12-61 distinct prefix templates (one per shape), so the head learned the prefix scaffold rather than the semantic intent.

20 of 29 held-out errors were **high-confidence wrong `none`** predictions — the worst failure mode for a query router.

### Representative failures (held-out, v7.1)

```
gold=current_state  pred=none  conf=0.94  "Which framework are we currently running in production?"
gold=before_named   pred=none  conf=0.97  "What did we use before we switched to Postgres?"
gold=before_named   pred=none  conf=1.00  "Which ORM predated Prisma in the codebase?"
gold=retirement     pred=none  conf=0.99  "Which technologies did we deprecate last quarter?"
gold=origin         pred=none  conf=1.00  "How did we end up using this database?"
```

None of these phrasings matched any training prefix, so the classifier — lacking any compositional generalization — defaulted to the most-frequent safe class (`none`).

## Root cause

**Task-shape mismatch.** The 6th head was asked to do **semantic parsing** (compose novel queries into one of 13 logical forms based on tense, reference structure, question focus, and modality) using the machinery of **surface-feature classification**.

BERT+LoRA with ~400 labeled examples cannot learn:
- Tense disambiguation ("What's our X?" vs "What was our X?")
- Reference structure ("What did we use before Postgres?" [named ref] vs "What did we use before?" [bare ref])
- Question focus (same words, different intent: ordinal-first vs frequency-first)
- Near-synonym class distinctions (`before_named` vs `predecessor`, `origin` vs `ordinal_first`, `transitive_cause` vs `causal_chain`)

The other 5 heads succeed because memory-content classification IS surface-feature classification — sentiment verbs, vocabulary clusters, trigger phrases. Surface features are exactly what BERT-base is good at. The 6th head's task is categorically different.

This is documented in the CTLG design (`docs/research/ctlg-design.md` §1.3).

## What we did that's still useful

Despite the head failing at its assigned task, adjacent work produced durable artifacts:

- **TLG dispatcher** — the 12-shape → 10-dispatcher-intent routing in `application/tlg/dispatch.py:605` + the 10 walker functions that traverse the zone graph. These are task-shape-agnostic and remain active.
- **Zone graph model** — L1 atomic, L2 entity_state, L3 episode, L4 abstract with typed edges (DERIVED_FROM, SUPERSEDES, REFINES, CONFLICTS, SUPPORTS). Structurally correct, only lacked CAUSED_BY (added in CTLG).
- **Bitemporal state nodes** (`observed_at`, `valid_to`, `is_current`) — correct design, stays.
- **181 → 485 shape_intent gold queries** — no longer directly useful as classification labels, BUT: the text content will be re-used as input seeds for the CTLG cue-tagging pipeline (LLM tags the existing queries with per-token cue labels, corpus gets a second life).

## What CTLG does differently

See `docs/research/ctlg-design.md`. The three structural changes:

1. **Sequence labeling replaces classification.** The 6th head tags tokens with 30-label BIO cue types (causal / temporal / ordinal / modal / referent / subject / scope). Many cues per query → effective training density is ~5-6× higher per labeled row.

2. **Compositional synthesizer replaces flat enum.** Rules in `src/ncms/domain/tlg/semantic_parser.py` compose cue spans into a structured `TLGQuery` logical form with axis / relation / referent / subject / scope / scenario fields. Explainable, testable, incrementally extensible.

3. **LLM fallback closes the loop.** Uncovered cue compositions kick to Spark, cache the result, feed back into training. The classifier's "confidently wrong → none → bypass" failure mode is replaced with "synthesizer abstains → LLM adjudicates → capture for next iteration".

## Files to archive

When CTLG lands in v8:

- `adapters/corpora/gold_shape_intent_*.jsonl` — keep as `adapters/_archive/pre_ctlg/gold_shape_intent_*.v7.2.jsonl`; text content survives into cue-tagging gold via LLM re-labeling
- `adapters/corpora/gold_shape_intent_software_dev.jsonl.pre_v7.2.bak` → archive
- `adapters/checkpoints/software_dev/v7_initial/` → archive (for historical comparison)
- `adapters/checkpoints/software_dev/v7.1/`, `v7.1_frozen/`, `v7.2/` → archive (adapters up through v7.2 use the failed head)

Code is **not** deleted — the sequence-labeling machinery in the SLM (v6 slot_head pattern) is the exact basis for the CTLG cue tagger. That code survives and gets repurposed.

## Lessons learned (applied to the CTLG design)

1. **Match task shape to model capability.** A classifier cannot compose; if the target task requires composition, use sequence labeling or parsing.
2. **Template-uniform training data is memorization bait.** LLM-generated diversity must be stress-tested against hand-crafted held-out queries BEFORE declaring a head healthy.
3. **High-confidence-wrong is worse than low-confidence-right.** When a router sends traffic to a bypass path with 0.97 confidence, the downstream system has no signal that anything is wrong. Forensics caught this only because we built a held-out test — otherwise v7.1 would have shipped.
4. **Hold-out sets must be hand-crafted.** LLM-generated held-out queries inherit the generator's writing habits. A model trained on LLM A's output, evaluated on LLM A's output, looks artificially strong.
5. **Invest in forensics before scale.** v7.2 with 8 epochs × ~25 min each cost ~3 hours of wall-clock. The forensics script that caught the failure took 30 minutes to write and reported it in seconds. Running forensics BEFORE v7.2 would have prevented the wasted training cycle.
