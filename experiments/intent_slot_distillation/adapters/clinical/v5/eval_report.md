# Adapter eval report — clinical

Generated: 2026-04-21T23:58:18.592736+00:00
Adapter:   `experiments/intent_slot_distillation/adapters/clinical/v5`

**Gate verdict:** ❌ FAIL

## Failures

- slot_f1_macro 0.517 < threshold 0.750 on gold

## Thresholds

- intent_f1_min: **0.700**
- slot_f1_min: **0.750**
- confidently_wrong_max: **0.100**
- regression_tolerance: **0.020**
- latency_p95_soft_limit: **200.0 ms**

## Metrics

| Split | N | Intent F1 | Slot F1 | Joint | Topic F1 (N) | Admission F1 (N) | State F1 (N) | p95 ms | Conf-wrong % |
|:------|--:|---------:|--------:|------:|-------------:|-----------------:|-------------:|-------:|-------------:|
| gold | 27 | 1.000 | 0.517 | 0.444 | 0.975 (27) | 1.000 (27) | 1.000 (27) | 25.7 | 0.00% |
| adversarial | 4 | 0.222 | 0.364 | 0.250 | — | — | — | 29.3 | 50.00% |
