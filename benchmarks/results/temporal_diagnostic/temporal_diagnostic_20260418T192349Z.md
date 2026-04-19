# Temporal-Reasoning Diagnostic

**Questions analyzed:** 133 (LongMemEval temporal-reasoning category)
**Elapsed:** 1997.4 s
**Retrieval depth:** top-50
**Config:** features-on bundle (temporal_enabled=True)

## Pattern Distribution & Recall

| Pattern | # | R@5 | R@20 | R@50 | Upside (20\5) | Upside (50\5) | Arith ceiling |
|---|---:|---:|---:|---:|---:|---:|---:|
| COMPARE_FIRST | 29 | 0.379 | 0.448 | 0.448 | 2 | 2 | 16 |
| AGE_OF_EVENT | 19 | 0.158 | 0.210 | 0.210 | 1 | 1 | 15 |
| ARITH_BETWEEN | 17 | 0.000 | 0.000 | 0.000 | 0 | 0 | 17 |
| ARITH_ANCHORED | 15 | 0.267 | 0.400 | 0.400 | 2 | 2 | 9 |
| DURATION_SINCE | 13 | 0.000 | 0.000 | 0.000 | 0 | 0 | 13 |
| RANGE_FILTER | 13 | 0.538 | 0.538 | 0.538 | 0 | 0 | 6 |
| ORDER_OF_EVENTS | 7 | 0.000 | 0.000 | 0.000 | 0 | 0 | 7 |
| ORDINAL_FIRST | 6 | 0.333 | 0.333 | 0.333 | 0 | 0 | 4 |
| ORDINAL_LAST | 5 | 0.600 | 0.800 | 1.000 | 1 | 2 | 0 |
| OTHER | 5 | 0.600 | 0.600 | 0.600 | 0 | 0 | 2 |
| COMPARE_LAST | 3 | 1.000 | 1.000 | 1.000 | 0 | 0 | 0 |
| TIME_OF_EVENT | 1 | 0.000 | 0.000 | 0.000 | 0 | 0 | 1 |

**Reading the table:**

- **R@K** = fraction of questions where the answer text appears in the top-K retrieved memories.
- **Upside (20\5)** = questions where the answer is in top-20 but not top-5.  These are the ones a re-rank can recover.
- **Upside (50\5)** = same at depth 50.  Gap between the two upside columns measures how much a bigger candidate pool would help versus just better ranking.
- **Arith ceiling** = count of questions where the answer substring is present in *zero* memories in the haystack. Recall@K cannot score these at any depth; only RAG mode can.