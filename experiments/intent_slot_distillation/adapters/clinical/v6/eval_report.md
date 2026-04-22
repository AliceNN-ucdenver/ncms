# Adapter eval report — clinical

Generated: 2026-04-22T02:02:45.235570+00:00
Adapter:   `experiments/intent_slot_distillation/adapters/clinical/v6`

**Gate verdict:** ❌ FAIL

## Failures

- slot_f1_macro 0.483 < threshold 0.750 on gold

## Thresholds

- intent_f1_min: **0.700**
- slot_f1_min: **0.750**
- confidently_wrong_max: **0.100**
- regression_tolerance: **0.020**
- latency_p95_soft_limit: **200.0 ms**

## Metrics

| Split | N | Intent F1 | Slot F1 | Joint | Topic F1 (N) | Admission F1 (N) | State F1 (N) | p95 ms | Conf-wrong % |
|:------|--:|---------:|--------:|------:|-------------:|-----------------:|-------------:|-------:|-------------:|
| gold | 199 | 1.000 | 0.483 | 0.920 | 1.000 (27) | 1.000 (199) | 1.000 (199) | 88.1 | 0.00% |
| adversarial | 4 | 0.194 | 0.364 | 0.250 | — | — | — | 49.6 | 25.00% |
