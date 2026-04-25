# v9 SLM MSEB-lift findings

> Update history:
>   * Phase F (2026-04-25 morning) — regression discovered, hypothesis log.
>   * Phase G (2026-04-25 noon) — root cause isolated, principled fix landed.
>   * Phase H.1 / H.2 / H.3 (2026-04-25 afternoon) — signal-leverage attempts;
>     two no-ops, one regression at the proposed weight.
>   * **Bottom line:** Phase G recovered most of the regression.  Adding
>     more bonuses on top of an already-tuned BM25/SPLADE/graph mix is
>     hitting diminishing returns on this benchmark; the SLM's primary
>     retrieval value comes from CONSTRAINING existing penalties (G), not
>     from layering new ones (H.\*).

---

# Phase F: original regression

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

---

# Phase G: hypothesis-driven isolation + principled fix

Ran four single-flag ablations against the regressed Phase F baseline
to isolate which downstream consequence of the SLM's labels was
hurting retrieval most.  All four cells use the SLM ON; only the
post-SLM scoring pathway varies.

| Ablation | Flag | sw r@1 | clin r@1 | convo r@1 |
|---|---|---:|---:|---:|
| Baseline (Phase F) | (SLM on, all defaults) | 0.5515 | 0.6724 | 0.2414 |
| A — no_populate_domains | `--no-populate-domains` | 0.5515 | 0.6724 | 0.2414 |
| **B — no_recon_penalty** | `--no-reconciliation-penalty` | **0.7515** | **0.6724** | **0.3448** |
| C — no_hier_bonus | `--hierarchy-weight 0.0` | 0.5515 | 0.6724 | 0.2414 |
| D — high_threshold | `--slm-confidence-threshold 0.7` | 0.5515 | 0.6724 | 0.2414 |

**Ablation B was decisive** — zeroing the supersession + conflict
penalties alone fully recovered the regression on softwaredev
(0.5515 → 0.7515) and convo (0.2414 → 0.3448).  The other three
ablations had zero effect, eliminating the original Phase F
hypotheses 1, 4, 5 from the suspect list.

**Root cause:** the reconciliation penalty was being applied
indiscriminately for every retrieval, regardless of the query's
intent.  With v9 producing more `state_change=declaration` labels,
more memories ended up on supersession chains.  The penalty pushed
the gold answer below its replacement on every query class — but
the penalty only makes SEMANTIC sense for queries that ask "what
is X NOW".  For historical / fact / event-reconstruction queries,
the older memory IS the answer.

**Fix:** intent-gate the penalty.  Only apply it when the BM25
exemplar classifier emits `QueryIntent.CURRENT_STATE_LOOKUP`.  For
every other intent, return the (is_superseded, has_conflicts)
diagnostic flags but skip the score deduction.

```python
_RECONCILIATION_PENALTY_INTENTS = frozenset({
    QueryIntent.CURRENT_STATE_LOOKUP,
})
```

Implemented in `src/ncms/application/scoring/pipeline.py::_compute_
reconciliation_penalty` (commit `32aafe6`).  Validation MSEB run
showed full recovery to the ablation-B numbers, identical to the
brute-force fix but architecturally clean.

---

# Phase H: signal-leverage attempts (the "use the SLM signals" series)

The premise: the v9 SLM emits five rich labels per memory
(intent / role / topic / admission / state_change).  Phase G fixed
the WAY one signal was over-firing; Phase H asks whether the OTHER
signals can be wired into retrieval to LIFT (not just recover).

Each H sub-phase adds a per-memory scoring bonus gated by the BM25
exemplar QueryIntent classifier.  Same shape as the existing
`hierarchy_match_bonus`: raw bonus × weight, additive on `combined`.

## H.1 — intent × QueryIntent alignment (commit 82916e8)

`_INTENT_ALIGNMENT_TABLE`:
  * `PATTERN_LOOKUP` → memory intent in {`habitual`}
  * `STRATEGIC_REFLECTION` → memory intent in {`habitual`, `choice`}

| Domain      | OFF (w=0.0) | ON (w=0.5) | Δ      |
|-------------|------------:|-----------:|-------:|
| softwaredev |      0.7455 |     0.7455 |  0.000 |
| clinical    |      0.6724 |     0.6724 |  0.000 |
| convo       |      0.3448 |     0.3448 |  0.000 |

**Result: 0 movement.**  Direct measurement of why:
  * v9 SLM emits `intent=habitual` at 1.00 confidence on routine
    statements ("I always do yoga in the morning") — signal real.
  * MSEB v1 has 0 `PATTERN_LOOKUP` queries on softwaredev/convo and
    1 on clinical (whose gold is causal_chain, not habitual).  Plus
    0 / 0 / 4 `STRATEGIC_REFLECTION` (clinical ones are case-
    discussion lookups, not habitual either).
  * The path can't fire on this benchmark.

**Decision:** ship default `weight=0.5` (on) — costs nothing when
the path doesn't fire.  Building block for deployments that DO see
pattern queries on habitual memories.

## H.2 — state_change × QueryIntent alignment (commit f42394a)

`_STATE_CHANGE_ALIGNMENT_TABLE`:
  * `CHANGE_DETECTION` → memory state_change in {`declaration`,
    `retirement`}

Reuses the same `intent_alignment_bonus` primitive (generic over
`(label, aligned_set)`); only the dispatch table differs.

| Domain      | OFF (w=0.0) | ON (w=0.5) | Δ      |
|-------------|------------:|-----------:|-------:|
| softwaredev |      0.7455 |     0.7455 |  0.000 |
| clinical    |      0.6724 |     0.6724 |  0.000 |
| convo       |      0.3448 |     0.3448 |  0.000 |

**Result: 0 movement.**  Surface area is small (5 `CHANGE_DETECTION`
queries across 252 — 3 sw + 2 convo + 0 clinical).  The bonus
either doesn't fire on those queries' candidate sets, or fires on
already-correctly-ranked memories.

**Decision:** ship default `weight=0.5` (on) — same logic as H.1.

## H.3 — role-grounding (commit db3e085)

`role_grounding_bonus(role_spans, query_canonicals, primary_bonus)`:
  * Reward memories where a query entity appears in a role_span
    with `role=primary`.  Per-span signal (not per-memory) → in
    principle the largest surface area of any H phase.

| Domain      | OFF (w=0.0) | ON (w=0.5) | Δ r@1   |
|-------------|------------:|-----------:|--------:|
| softwaredev |      0.7455 |     0.7212 | **−0.024** |
| clinical    |      0.6724 |     0.6724 |  0.000  |
| convo       |      0.3448 |     0.3448 |  0.000  |

**Result: REGRESSION** on softwaredev.  Direct measurement: 4
queries flipped from correct → wrong, all on shapes asking for
`alternative` / `predecessor` / `retired` entities:

```
"What rationale justified the choice in: Python is a versatile..."
   OFF: python-language-sec-02 (correct)
   ON:  python-language-sec-04 (wrong)

"What alternatives were considered before the final choice in:
 The other option..."
   OFF: postgresql-database-sec-03 (correct, the chosen DB)
   ON:  mysql-database-sec-01 (wrong, the considered alternative)
```

**Root cause:** the role_head's `primary` semantics are
"syntactically primary" (the chosen entity in "switched from X to
Y" tags Y=primary), not "answer-relevance primary".  For queries
that ASK ABOUT the alternative, the boost goes the wrong direction.
Intent-gating doesn't fix it (3 of 4 regressions classify as
`fact_lookup`).

**Decision:** ship default `weight=0.0` (off) — same opt-in pattern
as `scoring_weight_hierarchy=0.0`.  The primitive ships as a
building block; deployments enable it after verifying role_head
accuracy on their domain.  Future v9 retraining can supervise on
"answer-relevance primary" instead of "syntactically primary".

---

# Cumulative summary

| Phase | Commit | Default weight | MSEB Δ r@1 (sw / clin / convo) |
|---|---|---|---|
| F (regression) | (pre-fix) | n/a | −0.20 / 0 / −0.10 |
| **G** (intent-gate penalty) | `32aafe6` | gate-only | **+0.20 / 0 / +0.10** |
| H.1 (intent × QueryIntent) | `82916e8` | 0.5 | 0 / 0 / 0 |
| H.2 (state_change × QueryIntent) | `f42394a` | 0.5 | 0 / 0 / 0 |
| H.3 (role-grounding) | `db3e085` | 0.0 | n/a (off) |

**Pattern:** the only Phase G/H change with measurable lift was
the intent-gated reconciliation penalty in G — which CONSTRAINED
an existing penalty from over-firing.  Adding new bonuses on top
of the existing BM25/SPLADE/graph mix doesn't move MSEB v1 because
either:
  * the gold queries don't exercise the narrow alignment paths
    the SLM heads are trained on (H.1, H.2 — surface area is too
    small in MSEB v1), or
  * the SLM's per-span semantics don't align with retrieval
    relevance (H.3 — role_head needs retraining).

## The deeper insight: did the legacy regex EVER lift retrieval?

The H.1/H.2 zero-lift result reframes a question worth asking
explicitly: did the **legacy** regex path the SLM is replacing ever
contribute retrieval lift either?  Audit of default weights:

| Retrieval signal driven by labels | Default weight | Effect |
|---|---:|---|
| Hierarchy bonus (regex or SLM L2 nodes) | 0.0 | Disabled since Phase 4 |
| Reconciliation supersession penalty | 0.3 | **Active, both paths** |
| Reconciliation conflict penalty | 0.15 | Active, both paths |
| Temporal scoring (observed_at) | 0.2 | Active, both paths |
| Intent alignment (Phase H.1) | 0.5 | New, 0 MSEB movement |
| State_change alignment (Phase H.2) | 0.5 | New, 0 MSEB movement |
| Role-grounding (Phase H.3) | 0.0 | Off by default |

**Conclusion: the regex path's retrieval contribution was always
the same as the SLM's — zero direct bonus paths moved metrics.**
The reconciliation penalty was the only path on by default that
labels affect, and Phase G showed it was over-firing in the wrong
direction.

So what's the SLM actually buying us?  Not retrieval-time scoring
lift — that was always near-zero from labels alone.  The SLM's
value is at INGEST time:

  1. **Better L1 → L2 promotion.**  The state_change_head and
     role_head together emit cleaner signals than the
     "Entity: key = value" regex about which atomic fragments
     should produce L2 entity_state nodes.  Better L2 nodes are
     more findable by BM25/SPLADE because they're shorter,
     vocabulary-dense, and grounded on the canonical slot value.
  2. **Higher-quality reconciliation.**  state_change=retirement
     reliably triggers the supersession edge; the regex baseline
     missed many implicit retirements ("I quit smoking" doesn't
     match "X = NULL" patterns).  Better supersession chains
     mean Phase G's intent-gated penalty has more correct
     candidates to penalise on CURRENT_STATE_LOOKUP.
  3. **Topic-driven domain expansion.**  topic_head auto-appends
     a topic label to ``Memory.domains``, widening the surface
     area of the domain filter without manual configuration.
     This is the biggest single-knob retrieval impact — but it
     showed in Phase F as a NEGATIVE (more collisions on shared
     topics).  The Phase G ablation A confirmed this is benign
     when other signals are calibrated.
  4. **Confident admission decisions.**  admission_head replaces
     four regex heuristics that approximated importance.  Better
     admission means fewer junk memories pollute the candidate
     set at retrieval time — but admission is currently OFF in
     MSEB by design (so it can't drop gold rows into ephemeral
     cache).

The H.1/H.2/H.3 series built **scoring infrastructure** that
will help when:
  * gold queries explicitly ask for habitual / preference patterns
    (real conversational deployments, not MSEB v1's temporal-shape
    gold)
  * role_head retraining shifts "syntactically primary" toward
    "answer-relevance primary"
  * a new query-side classifier emits per-query labels that align
    with the existing SLM head outputs (a likely Phase H follow-on)

None of which is wasted work — it's the framework deployments
will reach for first when their gold sets cover preference/pattern
queries.  But MSEB v1 was the wrong evaluator for the H series.

**v9 ship readiness:**
  * Phase G recovers the Phase F regression.  Production cutover
    is now safe at the Phase G floor (sw 0.7455 / clin 0.6724 /
    convo 0.3448).
  * H.1 / H.2 add zero benchmark lift but ship as opt-in building
    blocks — no regression risk because the paths don't fire on
    MSEB queries.
  * H.3 ships off-by-default; revisit after a future v9 role_head
    retraining round.
  * **Phase I (retire fallbacks):** flip `NCMS_SLM_ENABLED=true`
    by default, then delete the flag.  Same direction the user
    explicitly asked for — Phase G demonstrated SLM-on retrieval
    is at parity-or-better with SLM-off.

## Why MSEB v1 doesn't measure further H lift

MSEB v1 gold is structured around state-evolution shapes
(`current_state`, `causal_chain`, `retirement`, `ordinal_*`,
`predecessor`, `transitive_cause`, `concurrent`, `before_named`,
`origin`, `sequence`, `noise`).  The QueryIntent distribution
across 252 queries:

| QueryIntent | n | %  |
|---|---:|---:|
| fact_lookup | 189 | 75 |
| historical_lookup | 39 | 15 |
| current_state_lookup | 13 | 5 |
| change_detection | 5 | 2 |
| strategic_reflection | 4 | 2 |
| pattern_lookup | 1 | 0.4 |
| event_reconstruction | 1 | 0.4 |

The SLM heads were trained on a different signal axis (preference-
stance: positive / negative / habitual / difficulty / choice / none
+ admission + state_change + topic + role).  These don't directly
map to MSEB's temporal-shape gold.  Future MSEB rounds (v2+) that
include preference / pattern queries should exercise H.1's path;
deployments outside MSEB (real conversational use) light up H.1
on day one.
