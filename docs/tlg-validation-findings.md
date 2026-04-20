# TLG validation findings (P1 Phase 6)

*Status: directional signal complete · 2026-04-19 · companion to
[p1-plan.md](p1-plan.md) §Phase 6, [temporal-linguistic-geometry.md](temporal-linguistic-geometry.md) §7.6.*

---

## 1. Executive summary

TLG's structural grammar delivers **100% top-5 and rank-1 accuracy
(32/32)** on the hand-curated ADR/project/medical corpus that
spans every intent shape we ship, against a BM25 baseline at
**41%/16%**.  That is the axis TLG was designed to
operate on.

On LongMemEval — conversational memory recall — TLG neither helps
nor hurts (3/9 both ways in smoke).  **Not a regression;
framing mismatch.**  LongMemEval queries ask "what did we talk
about", not "what is the current state / what caused what / what
came before X."  The zero L2/ENTITY_STATE node count from the LME
ingest log is the mechanical confirmation: there is no state-
change content for TLG to index.

**Decision:** ship Phase 6 on the ADR signal.  LongMemEval stays
as a non-regression check, not a headline benchmark.  The
headline benchmark is a **SWE state-evolution corpus** (§4
below), curated and labeled, reusable.

---

## 2. ADR validation — 32/32

**Corpus.** `experiments/temporal_trajectory/corpus.py` — 552 lines,
three subject chains (authentication ADR history, payments
project lifecycle, knee-injury medical record) with explicit
`SUPERSEDES` / `REFINES` / causal edges and bitemporal
`observed_at` values.

**Query set.** `experiments/temporal_trajectory/queries.py` —
32 queries covering every intent shape TLG ships:

| Shape | N | Example |
|---|---:|---|
| current_state | 13 | "What is the current authentication scheme?" |
| ordinal_first | 4 | "When did the payments project start?" |
| ordinal_last | 1 | "What was the last authentication decision?" |
| causal_chain | 3 | "What caused the delay on payments?" |
| sequence | 2 | "What came right after OAuth in authentication?" |
| predecessor | 2 | "What came before MFA?" |
| interval | 2 | "What happened between kickoff and the blocker?" |
| transitive_cause | 2 | "What eventually led to passkeys?" |
| before_named | 1 | "Did OAuth come before JWT?" |
| concurrent | 1 | "What else was happening during the Stripe blocker?" |
| noise (negative control) | 1 | query with no matching subject |

**Runner.** `uv run python -m experiments.temporal_trajectory.run`
— log at `experiments/temporal_trajectory/results/adr_validation_20260419_142727.log`.

**Strategies compared.**

| Strategy | What it does |
|---|---|
| `A_bm25` | Plain BM25 on all 50 corpus memories |
| `B_bm25_date` | BM25 + `observed_at DESC` tiebreak |
| `C_entity_scoped` | BM25 restricted to the query subject's chain |
| `D_path_rerank` | Entity-scoped + SUPERSEDES-chain walk |
| `E_lg_grammar` | TLG: query parser → intent → structural retrieval |

**Results (top-5 accuracy).**

| Shape | N | bm25 | bm25+date | entity_scope | path_rerank | **lg_grammar** |
|---|---:|---:|---:|---:|---:|---:|
| current_state    | 13 | 31% | 54% | 38% | 38% | **100%** |
| ordinal_last     |  1 |  0% |100% |100% |100% | **100%** |
| ordinal_first    |  4 | 75% | 25% | 50% | 75% | **100%** |
| causal_chain     |  3 | 33% | 33% | 67% | 67% | **100%** |
| sequence         |  2 | 50% |  0% |  0% | 50% | **100%** |
| predecessor      |  2 |  0% |  0% |  0% |  0% | **100%** |
| interval         |  2 | 50% |  0% |  0% |  0% | **100%** |
| before_named     |  1 |100% |100% |100% |100% | **100%** |
| transitive_cause |  2 | 50% | 50% |  0% |  0% | **100%** |
| concurrent       |  1 |  0% |  0% |  0% |  0% | **100%** |
| noise            |  1 |100% |100% |100% |100% | **100%** |
| **Overall top-5**  | 32 | 41% | 41% | 38% | 44% | **100%** |
| **Overall rank-1** | 32 | 16% |  0% |  6% | 19% | **100%** |

**Why BM25+date scored rank-1 = 0%.**  Sorting by `observed_at`
DESC guarantees the most recent memory is rank 1.  For half the
intent shapes the gold is *not* the most recent memory (e.g.
`ordinal_first` gold is the oldest; `predecessor` gold is two
steps back).  This is exactly the failure mode TLG is designed
to eliminate: temporal metadata is necessary but not sufficient —
you need the intent to know which temporal ordering is right.

**Proofs are readable.**  Every LG answer comes with a syntactic
proof.  Examples from the run:

```
[sequence] What came right after OAuth in authentication?
  intent: sequence(subject=authentication, entity=OAuth)
  grammar_answer: ADR-010 ✓  (gold=ADR-010)
  proof: sequence(subject=authentication, after=OAuth@ADR-007):
         successor = ADR-010 (refines)

[transitive_cause] What eventually led to passkeys?
  intent: transitive_cause(subject=authentication, entity=passkeys)
  grammar_answer: ADR-001 ✓  (gold=ADR-001)
  proof: transitive_cause(subject=authentication, to=passkeys@ADR-029):
         walked 6 predecessors; root = ADR-001
```

These proofs are what separate TLG from a confidently-wrong black
box: if the answer is wrong, the proof tells you which edge or
which zone computation is wrong — you can debug the grammar, not
just re-weight a ranker.

---

## 3. LongMemEval — axis mismatch, not a regression

**Smoke result.**  3/9 recall both with and without `--tlg` on
the LME test subset.  The ingest log confirms the root cause:

```
TLG L1 induction: 0 subjects, 0 entity tokens
```

LongMemEval messages are conversational — "I was thinking about
my trip", "tell me about your sister" — with neither explicit
state declarations (`auth-svc: auth_method = OAuth`) nor
retirement markers (`deprecated`, `superseded by`, `moved to`)
that the L1 inducer keys off of.  **Zero state-change content ⇒
zero ENTITY_STATE nodes ⇒ TLG has nothing to score against.**
The retrieval path falls through to BM25+SPLADE+graph exactly as
before the flag.

**Implication.**  LongMemEval is the wrong axis for a headline
TLG benchmark.  It remains useful as a **non-regression check**
(flag on shouldn't hurt conversational retrieval; 3/9 == 3/9
confirms that at smoke scale), but we should not ship TLG
claiming LME wins.  The published axis is state-evolution.

---

## 4. SWE state-evolution benchmark (proposed, reusable)

LME is the wrong axis; the ADR corpus is the right axis but
small-scale (50 memories, hand-curated).  We need a
**reusable, realistically-sized, publicly-reproducible** state-
evolution corpus.  Proposal:

### 4.1 Source

- **SWE-bench Verified** — 500 GitHub issues paired with the
  actual resolving PRs from real Python projects (astropy,
  django, flask, pytest, matplotlib, requests, scikit-learn,
  sphinx, sympy, xarray, pylint, pvlib).  Each issue is a
  multi-turn evolution: the bug is reported, diagnoses land,
  fixes are attempted, superseded, and eventually merged.
- **Mining target:** extract per-issue state-change memories
  from the linked issue/PR thread:
  - **declarations**: "`module.foo` returns `None` when `X=True`"
  - **retirements**: "this was fixed in PR #N" / "superseded by"
  - **causal edges**: "caused by" / "regression from #M"
  - **ordinal anchors**: first reproduction, first diagnosis,
    first fix attempt, merge
- Per issue, we get a chain of 5–30 memories with explicit
  timestamps (GitHub's `created_at`) and explicit
  supersedes/refines edges derived from reply structure.

### 4.2 Scale target

- **500 issues × mean 12 memories ≈ 6,000 memory corpus.**
  Large enough to regress scale-curve findings (we already
  validated 10 k on synthetic).
- **100 gold queries** hand-labeled across the same 11 intent
  shapes we tested on ADRs: `current_state` ("what is the
  current status of astropy#1234"), `causal_chain` ("what
  regression caused django#5678"), `predecessor`, `sequence`,
  `interval`, `transitive_cause`, etc.
- Balanced so no shape has fewer than 5 queries.

### 4.3 Curation plan (2 weeks)

1. **Week 1 — extraction pipeline.**
   - `benchmarks/swe_state/mine.py`: pull SWE-bench Verified
     manifest, for each issue fetch the resolved PR thread,
     extract message + author + timestamp tuples.
   - `benchmarks/swe_state/label.py`: LLM-assisted labeler
     that emits per-message {declaration | retirement |
     causal_link | ordinal_anchor | none} and writes a JSONL
     with the NCMS ingest schema.
   - Human spot-check on a 50-issue subset to calibrate
     labeler precision (target ≥ 90%).
2. **Week 2 — query set + harness.**
   - Hand-write ~10 queries per intent shape over the labeled
     corpus.  Gold is the memory ID whose proof terminal
     matches the query.
   - `benchmarks/swe_state/run.py`: ingest corpus into NCMS
     once, then loop over the query set with `--tlg` and
     without.
   - Deliverable: the same table format as the ADR run
     (per-shape + overall top-5 and rank-1), published as
     `docs/tlg-swe-benchmark.md` + `benchmarks/results/swe_state/`.
3. **Reusability.**
   - Corpus + gold set checked in as JSONL.
   - Deterministic labels — if the labeler re-runs we get the
     same gold.
   - Other memory systems can consume it without knowing
     anything about NCMS (just the JSONL schema).

### 4.4 What it proves

- TLG wins on state-evolution at a **scale that isn't hand-
  curated by its author**.
- The 11 intent shapes generalize off the ADR/project/medical
  triplet to real software-engineering conversations.
- Regression against BM25/SPLADE/graph fusion is measurable and
  publishable.

---

## 5. Other open items from Phase 6

- **SciFact / NFCorpus / ArguAna ablation with `--tlg`.**  Not
  expected to move — these are lexical/semantic retrieval
  benchmarks with no temporal axis — but we should run them once
  as a regression guard.  Budget: one afternoon, already-
  parallelised runner in `benchmarks/run_parallel.sh`.
- **MemoryAgentBench.**  AR/TTL/LRU axes.  AR is the closest to
  TLG's axis (it exercises state updates); TTL and LRU are
  orthogonal.  Same treatment: run on/off, check for regression,
  not expecting large deltas.

These are routine regression checks rather than the headline
result.

---

## 6. Paper impact

- **M1 (structural-proof retrieval).**  ✅ validated — 32/32 on
  ADRs with readable proofs.  Write up the table in §7.6 M1 and
  close the milestone.
- **M2 (production integration).**  ✅ shipped — scale curve
  ≤50 ms through 10 k memories, dashboard events emit on every
  dispatch, `--tlg` benchmark flag wired.  Close with reference
  to `tlg-scale-validation.md`.
- **M3 (confidently-wrong = 0).**  Partially supported — 32/32 on
  ADRs shows no wrong answers with high confidence.  Full
  confirmation blocks on SWE corpus run (§4).  Mark "in
  progress, blocked on SWE benchmark."

Milestones **M4–M8** stay open — those cover scaling past 100 k,
cross-subject join paths, the formal proof of the zone lattice,
and the P2 slot-induction line.

---

## 7. Next actions

1. **Close Phase 6** — update `p1-plan.md` Phase 6 status to
   ✅ with pointer to this doc.  Done.
2. **Paper revision** — rewrite §7.6 M1 and M2 with the shipped
   numbers; mark M3 in-progress.  Done in same commit.
3. **Queue SWE benchmark curation** — 2-week budget, parked
   under a new `docs/p3-state-evolution-benchmark.md` planning doc.
   Not started; tracked as follow-up.
4. **Regression runs** — SciFact / NFCorpus / ArguAna /
   MemoryAgentBench with `--tlg`.  Low priority; single-afternoon.

---

*Companion artifacts*:
- `experiments/temporal_trajectory/results/adr_validation_20260419_142727.log`
  — full 454-line run log with per-query traces + LG proofs.
- `experiments/temporal_trajectory/corpus.py`, `queries.py` —
  corpus + gold queries, checked in.
- `benchmarks/results/longmemeval/tlg/` — LME smoke results
  (3/9 with and without `--tlg`).
