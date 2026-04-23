# CTLG Grammar Extension

**Status:** design
**Extends:** [`docs/temporal-linguistic-geometry.md`](../temporal-linguistic-geometry.md) (authoritative TLG base)
**Companion:** [`docs/research/ctlg-design.md`](./ctlg-design.md) (overall CTLG pivot)
**Owner:** NCMS core

---

## 0. Relationship to existing TLG

The existing TLG framework (shipped P1 2026-04-19, documented at `docs/temporal-linguistic-geometry.md`) already provides:

- A three-grammar architecture (trajectory `G_tr`, zone `G_z`, target `G_t`)
- A formal production system over typed edges `T = {introduces, refines, supersedes, retires}`
- Zone = connected component under `refines`, bounded by `supersedes`/`retires`
- 13 intent families with routing-as-parse semantics
- The zero-confidently-wrong composition invariant with BM25
- Three-layer induction (self-improving data / stable structural)

**CTLG does not replace this.** It extends it along three axes:

1. **Trajectory grammar gains a causal subgrammar `G_tr,c`.** Edges typed `caused_by` / `enables` become first-class productions. Existing `refines` / `supersedes` / `retires` trajectories remain as the state-evolution subgrammar `G_tr,s`.
2. **Target grammar gains causal + modal productions.** Causal queries ("why did we …", "what led to …") become productions rather than routing to a temporal walker by analogy. Modal queries ("what would have been …") introduce a **scenario** parameter carrying counterfactual skip-edges.
3. **Heuristics become typed and explicit.** The existing walkers use implicit structural heuristics (chronological sort, direct table lookup). CTLG lifts these to named, composable **causal heuristics** that rank candidate trajectories against the target grammar's intent.

The formal LG machinery — grammar-as-search-reduction, trajectory admissibility, zone-bounded walks, confident abstention — stays. What changes is **what each grammar generates** and **what ranks its output**.

---

## 1. Why this matters (Stilman → non-adversarial setting)

Classical Stilman LG reduces search in adversarial games via **min-max over trajectories**: the grammar generates admissible move sequences; min-max evaluates them assuming an optimal opponent; the search tree is pruned to trajectories that survive min-max.

NCMS is non-adversarial — there's no opponent. The current TLG code correctly **does not use min-max**: all 10 walkers are deterministic graph operations (chronological sort, direct lookup). But the field left open is: **what heuristic replaces min-max?**

Today: implicit per-walker logic. Each walker hand-codes its scoring.

CTLG proposes: **causal heuristics** — pure-function rankings over trajectories based on causal relevance, explanation coverage, parsimony, recency, and counterfactual distance. Made explicit, named, and composable.

This gives us Stilman's structural benefits (grammar-as-search-reduction, typed trajectories, zone algebra, confident abstention) without the game-theoretic baggage (min-max, zero-sum assumption, adversarial agent model) that doesn't apply to memory retrieval.

---

## 2. Extended trajectory grammar

### 2.1 State-evolution subgrammar `G_tr,s` (UNCHANGED from P1 TLG)

```
S              → introduces(M)
introduces(M)  → refines(M', M) | supersedes(M, M') | retires(M)
refines(M, M') → refines(M'', M') | supersedes(M', M'') | retires(M')
supersedes(M, M') → refines(M'', M') | supersedes(M', M'') | retires(M')
retires(M)     → ε
```

Generates refines-chain / supersedes-chain / retirement trajectories as today.

### 2.2 NEW: Causal subgrammar `G_tr,c`

```
C_start        → caused_by(M, M')
caused_by(M, M') → caused_by(M'', M') | enables(M'', M') | ε
enables(M, M')   → caused_by(M'', M') | enables(M'', M') | ε
```

Where:
- `caused_by(E, C)` — memory `E` is an effect whose cause is memory `C`.
- `enables(E, C)` — memory `C` is a necessary enabling condition for `E` (not a cause, but a prerequisite; e.g. "availability of pgvector **enabled** our Postgres decision").

Each `caused_by` or `enables` production corresponds to a typed edge inserted at ingest time by the cue-tagging pipeline (CTLG design §4.3).

### 2.3 NEW: Mixed subgrammar `G_tr,m`

Causal and state-evolution trajectories compose:

```
Mixed        → caused_by(M, M') ∘ supersedes(M', M'')
             | supersedes(M, M') ∘ caused_by(M', M'')
             | refines(M, M') ∘ caused_by(M', M'')
```

This captures queries like *"why did we supersede CRDB?"* — answer is a causal trajectory ending at the `supersedes` edge whose source is CRDB.

### 2.4 Trajectory typing

```python
@dataclass(frozen=True)
class Trajectory:
    """A typed walk through the zone graph.

    Replaces the implicit `Zone.memory_ids` chain with an explicit
    typed structure that names which subgrammar generated it and
    which heuristic ranked it.
    """

    kind: Literal[
        "refines_chain",     # G_tr,s — linear refinement
        "supersedes_chain",  # G_tr,s — linear supersession
        "retirement_arc",    # G_tr,s — terminates in retires(M)
        "causal_chain",      # G_tr,c — chain of caused_by edges
        "enables_arc",       # G_tr,c — terminates in enables(M)
        "mixed",             # G_tr,m — composed
    ]
    memory_ids: tuple[str, ...]     # ordered walk
    edge_types: tuple[str, ...]     # parallel to memory_ids, names each edge
    subject: str | None             # subject whose state the trajectory tracks
    length: int                     # # of edges (= len(edge_types))
    heuristic_scores: dict[str, float] = field(default_factory=dict)
    # ^ e.g. {"h_parsimony": 0.8, "h_recency": 0.6, "h_explanatory": 0.9}
```

The `heuristic_scores` dict is populated by the heuristic suite (§4), letting the dispatcher's ranker combine scores weighted by the target intent.

---

## 3. Extended target grammar

The existing 13 intent families stay. CTLG adds three new target productions and enriches four existing ones:

### 3.1 New productions

| Target intent | Production | Example query |
|--------------|-----------|---------------|
| `chain_cause_of(M)` | Walk `G_tr,c` backward from `M`, depth ≥ 2 | *"Explain the chain of reasons we moved to Yugabyte."* |
| `contributing_factor(M)` | Walk `G_tr,c` backward from `M`, depth = 1, collecting multiple parallel causes | *"What factors drove the Postgres decision?"* |
| `would_be_current_if(scenario)` | Walk `G_tr,s` forward from subject's earliest state, SKIPPING edges named in `scenario` | *"What would we be using if we hadn't moved off CRDB?"* |

### 3.2 Enriched productions (causal extension)

| Target | P1 TLG behavior | CTLG extension |
|--------|-----------------|----------------|
| `cause_of(M)` | Walked predecessor chain by timestamp | Prefers `G_tr,c` causal trajectory when available; falls to timestamp chain otherwise |
| `transitive_cause(M)` | Exhaustive predecessor DFS | Replaced by ranked `G_tr,c ∘ G_tr,s` mixed walk, `h_explanatory`-ranked |
| `retirement(M)` | Direct retires-edge lookup | Add causal-justification scoring via `h_explanatory` over any `caused_by` edges entering the retirement node |
| `origin(M)` | Root of earliest zone | Tie-break with `h_robustness` on SUPPORTS edges |

### 3.3 Target → trajectory subgrammar mapping

Each target production names which subgrammar generates its candidate trajectories. This is the **search-reduction mechanism**: the target dictates the trajectory type, eliminating the O(n_memories) walker scan documented in the audit.

| Target | Invokes | Heuristic (primary) |
|--------|---------|---------------------|
| `current` | zone terminal lookup (no walk) | — |
| `origin` | `G_tr,s` (refines chain to root) | `h_robustness` |
| `still` | zone terminal + retirement check | — |
| `sequence` | `G_tr,s` (refines chain forward) | `h_parsimony` |
| `predecessor` | `G_tr,s` (refines chain backward, depth=1) | `h_parsimony` |
| `before_named` | `G_tr,s` with timestamp constraint | `h_parsimony` |
| `interval` / `range` / `concurrent` | zone + temporal filter | `h_recency` |
| `cause_of` | `G_tr,c` then `G_tr,s` | `h_explanatory`, `h_parsimony` |
| `chain_cause_of` | `G_tr,c` depth-bounded | `h_explanatory`, `h_parsimony` |
| `contributing_factor` | `G_tr,c` depth=1, multi-return | `h_explanatory`, `h_robustness` |
| `transitive_cause` | `G_tr,m` | `h_explanatory` |
| `retirement` | retires-edge + `G_tr,c` justification | `h_explanatory` |
| `would_be_current_if` | `G_tr,s` with skip-edge filter | `h_parsimony` |

---

## 4. Causal heuristics (formal suite)

Each heuristic is a pure function `h: Trajectory × Context → [0, 1]`. They're explicit, named, composable, and unit-testable. Context carries the target intent + query parameters.

### 4.1 `h_explanatory(T, ctx)` — explanation coverage

> How much of the observed state does this trajectory explain?

Counts the subject's `(entity_id, state_key)` pairs whose `valid_from` is within `T`'s temporal span. Normalized by the subject's total state pairs. Higher = more explanatory.

**Rationale**: a causal chain that explains more of the current state is preferred for `cause_of` queries.

### 4.2 `h_parsimony(T, ctx)` — Occam razor

> Prefer shorter trajectories over longer ones.

```
h_parsimony(T) = 1 / (1 + α * (length(T) - min_length))
```

Where `min_length` is the minimum admissible trajectory length for the target intent (typically 1). `α = 0.2` by default.

**Rationale**: when multiple trajectories explain the same target, the shorter one is usually causally more direct. This replaces implicit "shortest predecessor chain" logic in `_dispatch_transitive_cause`.

### 4.3 `h_recency(T, ctx)` — temporal recency

> Prefer trajectories whose terminal node is more recent.

```
h_recency(T) = exp(-λ * days_since(T.terminal.observed_at))
```

With `λ` tuned per domain (software_dev: 0.01/day; clinical: slower decay).

**Rationale**: for interval/range queries, more recent state is usually more relevant. For historical queries (`origin`, `before_named`), recency is explicitly NOT used.

### 4.4 `h_robustness(T, ctx)` — supporting evidence

> How many SUPPORTS edges corroborate the trajectory's claims?

```
h_robustness(T) = sum(|supports_edges(m)| for m in T.memory_ids) / length(T)
```

**Rationale**: a trajectory backed by many confirming memories is more reliable than one based on a single observation. Replaces ad-hoc confidence tweaking in walker code.

### 4.5 `h_counterfactual_dist(T, ctx)` — counterfactual minimality

> For modal queries, minimize the number of edge-skips required to reach the specified scenario outcome.

```
h_counterfactual_dist(T, scenario) = 1 - (|skipped_edges(T, scenario)| / length(T))
```

**Rationale**: a counterfactual answer that requires skipping many edges is a weaker scenario than one requiring a single skip. Only active on `would_be_current_if` and related modal targets.

### 4.6 Composition

The dispatcher ranks candidate trajectories by a weighted sum of relevant heuristics:

```python
def rank_trajectories(
    candidates: list[Trajectory],
    intent: TLGQuery,
    weights: dict[str, float],
) -> list[Trajectory]:
    """Rank candidates by weighted heuristic sum."""
    def score(t: Trajectory) -> float:
        return sum(
            weights[h_name] * t.heuristic_scores[h_name]
            for h_name in weights
            if h_name in t.heuristic_scores
        )
    return sorted(candidates, key=score, reverse=True)
```

Weights are per-target-intent, specified in a config YAML. Initial defaults:

```yaml
# config/tlg_heuristic_weights.yaml
cause_of:
  h_explanatory: 0.5
  h_parsimony:   0.3
  h_robustness:  0.2
chain_cause_of:
  h_explanatory: 0.6
  h_parsimony:   0.4
would_be_current_if:
  h_counterfactual_dist: 0.7
  h_parsimony:           0.3
interval:
  h_recency: 0.8
  h_parsimony: 0.2
# ...
```

The config is LIVE-reloadable via `NCMS_TLG_HEURISTIC_WEIGHTS_PATH`. This lets operators tune without retraining.

---

## 5. Search reduction (the LG payoff)

### 5.1 Current state (audit finding)

Each walker in `application/tlg/dispatch.py` does an **exhaustive scan** within its scope:

- `_dispatch_interval` iterates every memory in the subject's range
- `_dispatch_transitive_cause` DFS's the entire predecessor tree
- `_dispatch_current_intent` scans all zones for ungrounded terminals

Branching factor is unbounded in subject size. This is valid today because corpora are small (hundreds of memories per subject), but doesn't scale.

### 5.2 Grammar-guided reduction (CTLG)

With the target → subgrammar mapping (§3.3), the dispatcher:

1. Parses the cue-tagged query into a `TLGQuery` (CTLG design §2.2)
2. Looks up the target intent's subgrammar (`G_tr,s` or `G_tr,c` or `G_tr,m`)
3. **Generates admissible trajectories via grammar productions**, bounded by depth limits from the target intent (e.g. `transitive_cause` caps at depth 6)
4. Each production expansion consults the zone index + edge index — NO exhaustive scans
5. Generated trajectories are scored by §4 heuristics, ranked
6. Top-1 (or top-k) returned with confidence = f(heuristic spread, trajectory length)

**Branching factor becomes**: `(avg edges per node under chosen subgrammar) × (max depth for target)`. For causal chains in realistic corpora: ~3 × 4 = 12, independent of corpus size.

This is the Stilman benefit: **polynomial search via grammar**, not exponential or linear-in-corpus.

### 5.3 Admissibility as a structural predicate (not a heuristic)

An edge is **admissible** for a trajectory expansion iff:

- Its type matches the subgrammar's allowed productions
- Its source subject equals the trajectory's subject (for subject-scoped targets)
- Its `valid_from` is within the temporal constraint (if any)
- The destination node is not already in the trajectory (cycle prevention)

Admissibility is **structural** — it doesn't depend on a heuristic score. A non-admissible trajectory is rejected at expansion time, never scored.

Heuristics rank only admissible candidates. This is the classical LG separation Stilman introduced and we inherit.

---

## 6. Confidence derivation (extended)

The existing TLG confidence enum (`HIGH / MEDIUM / LOW / ABSTAIN / NONE`) stays. CTLG extends the derivation with heuristic-aware calibration:

```python
def derive_confidence(
    ranked: list[Trajectory],
    intent: TLGQuery,
) -> Confidence:
    if not ranked:
        return Confidence.ABSTAIN
    top = ranked[0]
    margin = (ranked[0].combined_score - ranked[1].combined_score
              if len(ranked) > 1 else ranked[0].combined_score)
    if margin >= 0.3 and top.combined_score >= 0.7:
        return Confidence.HIGH
    if margin >= 0.15 and top.combined_score >= 0.5:
        return Confidence.MEDIUM
    if ranked[0].combined_score >= 0.3:
        return Confidence.LOW
    return Confidence.ABSTAIN
```

Rationale: a confident answer requires both a high absolute score AND a large margin over the runner-up. This preserves the zero-confidently-wrong invariant by abstaining when heuristics are close.

---

## 7. Integration with existing TLG

### 7.1 `LGTrace` is extended, not replaced

```python
@dataclass(frozen=True)
class LGTrace:
    intent: LGIntent                  # unchanged
    confidence: Confidence            # unchanged
    grammar_answer: str | None        # unchanged (memory id of top)
    zones: tuple[Zone, ...]           # unchanged
    # NEW:
    trajectory: Trajectory | None     # the winning typed trajectory
    ranked_trajectories: tuple[Trajectory, ...] = field(default_factory=tuple)
    heuristic_weights: dict[str, float] = field(default_factory=dict)
    production_trace: tuple[str, ...] = field(default_factory=tuple)
    # ^ e.g. ("target:chain_cause_of", "G_tr,c→caused_by", "G_tr,c→caused_by", "ε")
```

The `production_trace` is the **explainability primitive**: every answer has a formal derivation from the target grammar, visible to operators and auditors.

### 7.2 The compose_with_lg invariant holds

Proposition 1 from the existing TLG doc (zero-confidently-wrong composition with BM25) is preserved. The extended confidence derivation (§6) is stricter than the current rule (margin requirement added), so the invariant's contract is actually tightened, not loosened.

### 7.3 Zone index update

Zones today are connected components under `refines`. CTLG adds **causal zones**: connected components under `caused_by ∪ enables`. A memory can belong to one refines-zone and multiple causal-zones (causation is a many-to-many relation, unlike refines).

```python
# domain/tlg/zones.py additions
@dataclass(frozen=True)
class CausalZone:
    """Connected component under CAUSED_BY + ENABLES, bounded by
    subject boundaries or grammar-depth limits."""
    zone_id: int
    member_ids: frozenset[str]         # memories in this causal zone
    root_causes: tuple[str, ...]       # nodes with no incoming caused_by
    leaf_effects: tuple[str, ...]      # nodes with no outgoing caused_by
    subject_coverage: frozenset[str]   # subjects touched by this zone
```

A causal zone is the unit of traversal for causal-subgrammar walks.

---

## 8. What this gains us

### 8.1 Properties we inherit from LG

- **Grammar-as-search-reduction** — polynomial, not linear-in-corpus
- **Structural admissibility** — non-heuristic filtering, cycle prevention, depth bounds
- **Typed trajectories** — every answer is a formal derivation
- **Zone algebra** — walks are localized to relevant subgraphs
- **Confident abstention** — the composition invariant with BM25

### 8.2 Properties specifically from the causal reframe

- **Causal answers, not proxy-causal** — `_dispatch_transitive_cause` today uses predecessor timestamps as a proxy for causation. With `CAUSED_BY` edges it returns actual causes.
- **Counterfactual reasoning** — first-class via scenario parameter + skip-edge traversal
- **Tunable heuristic weights** — per-domain, hot-reloadable, explainable
- **Explainable derivations** — `production_trace` tells the operator exactly how an answer was derived
- **Self-improving** — novel causal cues / surfaces feed into catalog + training (CTLG design §3 + §2.5)

### 8.3 Properties from NOT being adversarial

- No min-max bookkeeping
- No adversarial-agent model
- No zero-sum assumption
- No move-counter-move alternation
- Heuristics are non-negative over a non-competitive state space

The classical Stilman game-theoretic layer is **inapplicable**; we acknowledge it and replace it with causal heuristics that match the problem shape.

---

## 9. Migration path (from current P1 TLG code)

Only three code locations change materially. Everything else stays byte-identical.

| File | Change | Effort |
|------|--------|--------|
| `domain/tlg/zones.py` | Add `CausalZone` type, add `Trajectory` type | 1h |
| `domain/tlg/heuristics.py` **NEW** | 5 pure functions in §4 + weighted composition | 2h |
| `application/tlg/dispatch.py` | Replace per-walker scoring with typed trajectory generation + heuristic ranking; walkers become production-rule parsers | 4-6h |
| `infrastructure/storage/*` | No schema change (edges are typed strings; `CAUSED_BY` is new value, not new column) | 0 |
| `config/tlg_heuristic_weights.yaml` **NEW** | Default weights per target intent | 30min |
| `CLAUDE.md` + `docs/temporal-linguistic-geometry.md` | Add CTLG extension note; cross-reference this doc | 30min |

Total: **one sprint of work** (7-10h) to land the grammar extension.

---

## 10. Open questions

1. **Zone binding for causal zones.** Causation can cross subject boundaries ("audit caused us to adopt Vault"). Do we: (a) allow causal zones to span subjects freely, (b) partition by subject with cross-subject edges explicit, (c) introduce a meta-subject? Lean toward (b).
2. **Heuristic weights calibration.** We pick defaults, but how do we validate them? Propose: a held-out benchmark subset where the heuristic choice matters ("if weights were uniform, answers would differ") — measure accuracy over that subset as heuristics are tuned.
3. **Depth bounds per target.** Hard-coded (e.g. `transitive_cause` max depth 6) or corpus-adaptive? Start hard-coded; revisit when we have production data.
4. **Trajectory ranking ties.** When two trajectories tie on combined score, what breaks the tie? Proposal: `h_recency`, then trajectory-id lexicographic (deterministic).
5. **Explainability UI.** `production_trace` is machine-readable. Do we need a human-readable formatter for operators? Proposal: yes, render the trace as a sentence ("Because **outage** (M3) caused **migration to Cockroach** (M4) which was superseded by **migration to Yugabyte** (M7) …") — small rendering module, 1h work.

---

## 11. TL;DR — the one-sentence pivot

**TLG's grammar/zone/trajectory/confidence machinery is correct and stays; classical Stilman's min-max heuristic is replaced by a formal causal heuristic suite (`h_explanatory`, `h_parsimony`, `h_recency`, `h_robustness`, `h_counterfactual_dist`) so the search reduction remains polynomial but the ranking matches the non-adversarial, causal-temporal structure of agent memory retrieval.**
