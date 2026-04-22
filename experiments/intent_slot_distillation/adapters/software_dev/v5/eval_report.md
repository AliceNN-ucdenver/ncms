# Adapter eval report — software_dev

Generated: 2026-04-22T00:16:29.912301+00:00
Adapter:   `experiments/intent_slot_distillation/adapters/software_dev/v5`

**Gate verdict:** ❌ FAIL

## Failures

- slot_f1_macro 0.432 < threshold 0.750 on gold

## Thresholds

- intent_f1_min: **0.700**
- slot_f1_min: **0.750**
- confidently_wrong_max: **0.100**
- regression_tolerance: **0.020**
- latency_p95_soft_limit: **200.0 ms**

## Metrics

| Split | N | Intent F1 | Slot F1 | Joint | Topic F1 (N) | Admission F1 (N) | State F1 (N) | p95 ms | Conf-wrong % |
|:------|--:|---------:|--------:|------:|-------------:|-----------------:|-------------:|-------:|-------------:|
| gold | 42 | 1.000 | 0.432 | 0.381 | 1.000 (42) | 1.000 (42) | 1.000 (42) | 34.0 | 0.00% |
| adversarial | 4 | 0.278 | 0.571 | 0.250 | — | — | — | 55.1 | 0.00% |
