# Adapter eval report — conversational

Generated: 2026-04-20T00:37:33.292682+00:00
Adapter:   `experiments/intent_slot_distillation/adapters/conversational/v4`

**Gate verdict:** ✅ PASS

## Thresholds

- intent_f1_min: **0.700**
- slot_f1_min: **0.750**
- confidently_wrong_max: **0.100**
- regression_tolerance: **0.100**
- latency_p95_soft_limit: **200.0 ms**

## Metrics

| Split | N | Intent F1 | Slot F1 | Joint | Topic F1 (N) | Admission F1 (N) | State F1 (N) | p95 ms | Conf-wrong % |
|:------|--:|---------:|--------:|------:|-------------:|-----------------:|-------------:|-------:|-------------:|
| gold | 36 | 1.000 | 0.987 | 0.972 | 1.000 (36) | 1.000 (36) | 0.333 (36) | 43.5 | 0.00% |
| adversarial | 12 | 0.516 | 0.750 | 0.583 | — | — | — | 144.0 | 16.67% |

## Baseline comparison

| Split | Metric | Baseline | Current | Δ |
|:------|:-------|-------:|-------:|-------:|
| gold | intent_f1 | 1.000 | 1.000 | +0.000 ✅ |
| gold | slot_f1 | 0.987 | 0.987 | +0.000 ✅ |
| gold | joint_acc | 0.972 | 0.972 | +0.000 ✅ |
| gold | conf_wrong | 0.000 | 0.000 | +0.000 ✅ |
| adversarial | intent_f1 | 0.514 | 0.516 | +0.002 ✅ |
| adversarial | slot_f1 | 0.750 | 0.750 | +0.000 ✅ |
| adversarial | joint_acc | 0.500 | 0.583 | +0.083 ✅ |
| adversarial | conf_wrong | 0.167 | 0.167 | +0.000 ✅ |
