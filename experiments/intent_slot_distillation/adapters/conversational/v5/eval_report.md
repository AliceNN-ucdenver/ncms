# Adapter eval report — conversational

Generated: 2026-04-21T23:09:41.666564+00:00
Adapter:   `experiments/intent_slot_distillation/adapters/conversational/v5`

**Gate verdict:** ✅ PASS

## Thresholds

- intent_f1_min: **0.700**
- slot_f1_min: **0.750**
- confidently_wrong_max: **0.100**
- regression_tolerance: **0.020**
- latency_p95_soft_limit: **200.0 ms**

## Metrics

| Split | N | Intent F1 | Slot F1 | Joint | Topic F1 (N) | Admission F1 (N) | State F1 (N) | p95 ms | Conf-wrong % |
|:------|--:|---------:|--------:|------:|-------------:|-----------------:|-------------:|-------:|-------------:|
| gold | 36 | 1.000 | 0.816 | 0.778 | 0.968 (36) | 1.000 (36) | 0.333 (36) | 20.8 | 0.00% |
| adversarial | 12 | 0.403 | 0.750 | 0.750 | — | — | — | 22.2 | 8.33% |
