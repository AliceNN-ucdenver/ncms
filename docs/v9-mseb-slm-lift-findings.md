# Phase F: v9 SLM MSEB-lift findings

**TL;DR:** the v9 5-head SLM does NOT improve MSEB retrieval out of the
box.  On software_dev it's a **−20 pt r@1 regression**; on conversational
−10 pts; clinical unchanged.  The gold answers are still being retrieved
(usually within top-3) but the SLM-derived metadata reshuffles ranking
in ways that displace the right answer from top-1.

This is a real finding, not a bug — it's the SLM and the retrieval
stack disagreeing about which signals matter.  The SLM enriches
*ingest-time* metadata; the retrieval stack scores at *query* time
using those signals as inputs.  Without joint tuning, enrichment can
hurt.

## Numbers (MSEB main12 mini, 6 cells, 2026-04-25)

| Domain | Metric | slm-off | slm-on | Δ |
|---|---|---:|---:|---:|
| softwaredev | r@1 | 0.7515 | 0.5515 | **−0.2000** |
| softwaredev | mrr | 0.7875 | 0.6616 | −0.1259 |
| softwaredev | r@5 | 0.8424 | 0.8424 | 0.0000 |
| clinical | r@1 | 0.6724 | 0.6724 | 0.0000 |
| clinical | mrr | 0.6897 | 0.6897 | 0.0000 |
| clinical | r@5 | 0.7069 | 0.7069 | 0.0000 |
| convo | r@1 | 0.3448 | 0.2414 | −0.1034 |
| convo | mrr | 0.4335 | 0.3367 | −0.0967 |
| convo | r@5 | 0.5862 | 0.4138 | −0.1724 |

r@5 unchanged on softwaredev confirms the gold IS still retrieved within
the top-5 — the SLM only perturbs the order.

## Per-class breakdown (softwaredev)

| Class | slm-off r@1 | slm-on r@1 | Δ |
|---|---:|---:|---:|
| general (n=76) | 0.974 | 0.632 | **−0.342** |
| temporal (n=69) | 0.725 | 0.623 | −0.102 |
| noise (n=20) | 0.000 | 0.000 | (expected — should not match) |

The biggest hit is on **general queries** — the easiest class.  This is
diagnostic: hard queries (temporal) were already sub-optimal so SLM
disturbance has less to lose; easy queries (general) had the gold
locked at top-1 and the SLM dislodged it.

## Sample regressions (gold present in top-3, displaced from top-1)

```
[softwaredev-current_state-002] class=general
  query: "What decision was adopted in: After considering the pros
          and cons of both options, we have decided to use?"
  gold:  sdev-adr_jph-api-using-json-v-grpc-sec-01
  slm-off top-3: [api-using-json-v-grpc-sec-01, python-programming-sec-04, go-programming-sec-05]
  slm-on  top-3: [python-programming-sec-04, api-using-json-v-grpc-sec-01, high-trust-teamwork-sec-04]

[convo-pref-positive-005] class=preference
  query: "What does the user prefer when it comes to is Kansas City
          Masterpiece BBQ sauce?"
  gold:  convo-user-18bc8abd-m0023
  slm-off top-3: [m0023, m0024, m0013]
  slm-on  top-3: [m0024, m0023, m0008]
```

## Why this happens (hypotheses, ranked by likelihood)

1. **Topic auto-expansion of `Memory.domains`** — when SLM is on,
   `slm_populate_domains=True` auto-appends the predicted topic to
   `Memory.domains`.  A memory tagged `["software_dev"]` becomes
   `["software_dev", "framework"]`.  This widens the surface area
   of memories that match a domain-filtered query, increasing
   collisions on shared topics.

2. **L2 ENTITY_STATE nodes shadow the original memory** — when
   `state_change_head` predicts `declaration` or `retirement` AND
   `role_head` produces a primary span, the ingestion pipeline
   creates an L2 ENTITY_STATE node alongside the original L1
   atomic memory.  Both are retrievable.  The L2 abstracted
   form may rank higher on some queries (it's shorter, denser
   on the slot value) and displace the L1.

3. **State reconciliation penalties** — the SLM's state_change
   labels feed reconciliation; superseded memories get an ACT-R
   mismatch penalty applied to combined retrieval score.  If
   the SLM's state predictions create more supersession edges
   than the regex baseline, more memories get penalised.

4. **Hierarchy bonus** — `INTENT_HIERARCHY_BONUS` adds a score
   bump to memories whose node type matches the classified
   intent.  More confident intent labels from the SLM →
   different bonus distribution.

5. **Confidence threshold change (0.7 → 0.3)** — Phase E.1
   lowered the SLM confidence floor.  More predictions reach
   the merged label; the chain may now accept SLM labels that
   the v6/v7 calibration would have dropped to heuristic.

## Likely fix path (B'.10 / G work)

* **Disable `slm_populate_domains` by default** — the auto-domain
  expansion is the most direct retrieval perturbation, easy to
  isolate.  Run this same 6-cell ablation with it off and check
  whether r@1 recovers.
* **Audit L2 ENTITY_STATE node visibility in retrieval** — should
  L2 nodes be filtered from the primary retrieval surface for
  `general`-class queries?
* **Re-tune retrieval weights** for the SLM-on regime — the
  current weights (BM25 0.6, SPLADE 0.3, graph 0.3) were grid-
  searched on SciFact without an SLM in the loop.  A small grid
  sweep with SLM on may recover the lost ranking quality.
* **Rerun MSEB after each fix** to isolate the contribution.

## What this means for v9 ship readiness

* Adapter F1 stays excellent (≥0.85 on 11/12 head-domain pairs).
  The HEADS work; the model is correctly classifying what the
  archetype taxonomy describes.
* Ingest-time labelling is reliable (Phase E.2 confirmed 9/9
  inputs got correct labels under SLM, vs all-None under
  heuristic).
* Retrieval is REGRESSED with the current default settings.
  Production cutover (`NCMS_SLM_ENABLED=true` as a default)
  should NOT happen until the regression is understood and
  fixed.

The SLM is a labelling success and a retrieval surprise.  v9 ship-
readiness for ingest is real; for end-to-end retrieval it needs
B'.10 / G work first.
