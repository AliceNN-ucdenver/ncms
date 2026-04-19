# P1-Temporal-Experiment — Phase A Range Coverage

**Questions analyzed:** 30 (LongMemEval temporal-reasoning subset)
**Elapsed:** 387.9 s

## Variant comparison

| Label preset | Strategy | Labels/call | Query R | Memory R | p50 ms | p95 ms |
|---|---|---:|---:|---:|---:|---:|
| full | combined | 17 | 3.3% | 5.0% | 289 | 3589 |
| full | split | 17 | 3.3% | 5.0% | 342 | 1280 |
| slim | combined | 4 | 0.0% | 0.0% | 193 | 634 |
| slim | split | 4 | 0.0% | 0.0% | 414 | 1698 |

**Reading:** *Query R* and *Memory R* are the share of inputs where the normalizer resolved ≥1 calendar interval.  Latency columns are per-question (sum of sub-call times for `split`).

## Query rate by pattern (baseline: full · combined)

| Pattern | # Qs | Had range | Rate |
|---|---:|---:|---:|
| ARITH_BETWEEN | 8 | 0 | 0.0% |
| AGE_OF_EVENT | 8 | 0 | 0.0% |
| ORDER_OF_EVENTS | 5 | 1 | 20.0% |
| ARITH_ANCHORED | 4 | 0 | 0.0% |
| DURATION_SINCE | 3 | 0 | 0.0% |
| COMPARE_FIRST | 2 | 0 | 0.0% |

## Notes

- Phase B gate: ≥ 80% query extraction rate.  Below that, the filter can't meaningfully help — revisit labels or fall back to the subject-scoped / metadata-anchored path in §14 of the design.
- If `slim` holds query coverage within ~5% of `full` but cuts latency meaningfully, ship slim as the default.
- If `split` latency is ≥ 1.5× `combined` without a coverage gain, keep the combined single call.