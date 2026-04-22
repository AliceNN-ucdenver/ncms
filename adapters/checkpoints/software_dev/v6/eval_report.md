# Adapter eval report — software_dev

Generated: 2026-04-22T01:46:14.478427+00:00
Adapter:   `experiments/intent_slot_distillation/adapters/software_dev/v6`

**Gate verdict:** ❌ FAIL

## Failures

- slot_f1_macro 0.411 < threshold 0.750 on gold

## Thresholds

- intent_f1_min: **0.700**
- slot_f1_min: **0.750**
- confidently_wrong_max: **0.100**
- regression_tolerance: **0.020**
- latency_p95_soft_limit: **200.0 ms**

## Metrics

| Split | N | Intent F1 | Slot F1 | Joint | Topic F1 (N) | Admission F1 (N) | State F1 (N) | p95 ms | Conf-wrong % |
|:------|--:|---------:|--------:|------:|-------------:|-----------------:|-------------:|-------:|-------------:|
| gold | 223 | 1.000 | 0.411 | 0.879 | 0.786 (42) | 1.000 (223) | 1.000 (223) | 81.2 | 0.00% |
| adversarial | 4 | 0.333 | 0.286 | 0.250 | — | — | — | 39.4 | 25.00% |
