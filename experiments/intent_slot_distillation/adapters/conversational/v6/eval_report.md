# Adapter eval report — conversational

Generated: 2026-04-22T01:28:19.068498+00:00
Adapter:   `experiments/intent_slot_distillation/adapters/conversational/v6`

**Gate verdict:** ❌ FAIL

## Failures

- slot_f1_macro 0.505 < threshold 0.750 on gold

## Thresholds

- intent_f1_min: **0.700**
- slot_f1_min: **0.750**
- confidently_wrong_max: **0.100**
- regression_tolerance: **0.020**
- latency_p95_soft_limit: **200.0 ms**

## Metrics

| Split | N | Intent F1 | Slot F1 | Joint | Topic F1 (N) | Admission F1 (N) | State F1 (N) | p95 ms | Conf-wrong % |
|:------|--:|---------:|--------:|------:|-------------:|-----------------:|-------------:|-------:|-------------:|
| gold | 305 | 0.933 | 0.505 | 0.895 | 0.968 (36) | 1.000 (305) | 0.333 (305) | 64.1 | 0.33% |
| adversarial | 12 | 0.587 | 0.706 | 0.750 | — | — | — | 38.6 | 16.67% |
