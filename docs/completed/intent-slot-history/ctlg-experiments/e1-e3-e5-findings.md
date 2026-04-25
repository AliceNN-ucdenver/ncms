# CTLG Experiments E1 / E3 / E5 — Phase 2a findings

Ran while the 485-row Spark cue relabel completed.  Results on the
final corpus.

## E1 — Cue-label distribution (485 rows)

- Total tokens: 6,124.  Non-O: 695 (11.3%) — healthy density.
- Per-row non-O: mean 1.43, median 1, max 8.
- Rows with zero cues: 148 (30.5%) — mostly template-scaffold queries.

Per-family coverage (B + I combined):

| family             | count | share |
|--------------------|------:|------:|
| REFERENT           | 181   | 26.0% |
| TEMPORAL           | 147   | 21.2% |
| ORDINAL            | 136   | 19.6% |
| SCOPE              | 104   | 15.0% |
| CAUSAL             | 88    | 12.7% |
| ASK_CURRENT        | 23    | 3.3%  |
| TEMPORAL_ANCHOR    | 12    | 1.7%  |
| SUBJECT            | 4     | 0.6%  |
| MODAL_HYPOTHETICAL | **0** | 0%    |
| ASK_CHANGE         | **0** | 0%    |
| ORDINAL_NTH        | **0** | 0%    |
| TEMPORAL_SINCE     | **0** | 0%    |

Zero-coverage families confirm Phase 2b is required.

## E3 — Synthesizer hit rate (485 rows)

- Overall: 48.7% (236/485 rows produce a non-None TLGQuery).
- Semantic mappings — when a rule fires, the TLGQuery is coherent.

Per-rule frequency:

| rule                     | count |
|--------------------------|------:|
| ordinal_first            | 52    |
| causal_direct            | 46    |
| temporal_before_named    | 42    |
| state_current            | 30    |
| ordinal_last             | 24    |
| causal_chain             | 19    |
| temporal_during          | 15    |
| state_bare_referent      | 7     |
| temporal_after_named     | 1     |

Per legacy shape_intent hit rate:

| shape             | hit | miss | rate   |
|-------------------|----:|-----:|-------:|
| ordinal_first     | 38  | 2    | 95.0%  |
| origin            | 33  | 11   | 75.0%  |
| causal_chain      | 30  | 10   | 75.0%  |
| ordinal_last      | 33  | 11   | 75.0%  |
| predecessor       | 19  | 15   | 55.9%  |
| transitive_cause  | 20  | 17   | 54.1%  |
| interval          | 13  | 12   | 52.0%  |
| before_named      | 13  | 26   | 33.3%  |
| current_state     | 12  | 29   | 29.3%  |
| sequence          | 11  | 30   | 26.8%  |
| retirement        | 8   | 29   | 21.6%  |
| concurrent        | 6   | 37   | 14.0%  |
| none              | 0   | 20   | 0%     |

Weakness clusters match E1's gaps — queries whose templates carry
few or no cues (retirement / concurrent / sequence / current_state).
Phase 2b natural queries will lift these specifically.

## E5 — End-to-end pipeline smoke test

Stored two hand-authored memories with simulated SLM payload
(role_spans + cue_tags) in an in-memory NCMS, then queried via the
dispatcher:

- M1: `"The audit recommended encryption-at-rest..."` → L2(entity=compliance, state=audit)
- M2: `"The auth-service uses postgres because of the audit..."` → L2(entity=auth-service, state=postgres) + causal cue triple (postgres + because + audit)

Ingest path produced:

- 2 L1 atomic nodes ✓
- 2 L2 entity_state nodes ✓
- **1 CAUSED_BY graph edge** (src=M2, dst=M1, cue_type=CAUSAL_EXPLICIT) ✓

Query `"what caused postgres for auth-service?"` with
`slm_shape_intent=transitive_cause`:

- Dispatcher loaded causal graph via `ctx.get_causal_edges()`
- `_walk_causal_chain` traversed M2 → M1 (depth 1)
- Returned `HIGH` confidence with proof naming `"CTLG causal chain"`
- `grammar_answer` = M1 (the audit memory — correct cause)

### Bug caught

During E5, discovered that `_load_causal_graph` in
`application/tlg/dispatch.py` passed a **single string** to
`SQLiteStore.list_graph_edges_by_type`, which takes `list[str]`.  Python
iterated the string as chars into the SQL IN clause, matching zero
edges.  Symptom: ingest would persist CAUSED_BY edges but the
dispatcher's causal walker would never see them — silent production
failure.  Fix: pass `list(_CTLG_CAUSAL_EDGE_TYPES)` in one query.

Committed as part of `c276ab0` alongside the E5 script.

## Takeaways for Phase 2b

Generate ~75 queries per gap bucket covering:

1. MODAL_HYPOTHETICAL — counterfactuals
2. ASK_CHANGE — "what changed / what happened to"
3. TEMPORAL_SINCE — "since / as of / ever since"
4. TEMPORAL_ANCHOR — concrete dates / named periods
5. SUBJECT — subject-voice queries naming specific services
6. ORDINAL_NTH — "2nd / third / fourth"

Total target: ~450 new rows.  Combined with the 485 existing rows,
corpus grows to ~935 for v8 training.

Post-Phase-2b, expected synthesizer hit rate: 65-75% (the zero-
coverage families go from 0 → meaningful coverage, lifting the
retirement/concurrent/sequence/before_named/current_state groups
that currently fail).

Generator: `scripts/ctlg/gen_gap_queries.py`
