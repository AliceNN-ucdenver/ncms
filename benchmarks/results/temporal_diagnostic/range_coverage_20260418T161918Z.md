# P1-Temporal-Experiment — Phase A Range Coverage

**Questions analyzed:** 10 (LongMemEval temporal-reasoning subset)
**Elapsed:** 27.0 s

## Aggregate

- **Query extraction rate:** 0.0% (Phase B gate: ≥ 80%)
- **GLiNER per-query (avg):** 162 ms
- **Memory coverage (sample of 50):** 2.0% had ≥1 resolvable span  (avg 5.0 intervals/hit)

## Query extraction rate by pattern

| Pattern | # Qs | Had range | Rate |
|---|---:|---:|---:|
| AGE_OF_EVENT | 4 | 0 | 0.0% |
| ARITH_BETWEEN | 2 | 0 | 0.0% |
| COMPARE_FIRST | 2 | 0 | 0.0% |
| ARITH_ANCHORED | 2 | 0 | 0.0% |

## GLiNER latency (with temporal labels)

- **p50:** 185 ms
- **p95:** 268 ms
- **n:** 10

## Notes

- A question is *extracted* when the normalizer resolves at least one temporal span to a calendar interval.
- Gate for Phase B: ≥ 80% query extraction rate.  Below that, iterate on labels/normalizer before enabling the filter.