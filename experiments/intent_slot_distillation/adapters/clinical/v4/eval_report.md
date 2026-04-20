# Adapter eval report — clinical

Generated: 2026-04-20T01:08:32.482021+00:00
Adapter:   `experiments/intent_slot_distillation/adapters/clinical/v4`

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
| gold | 27 | 1.000 | 0.966 | 0.926 | 1.000 (27) | 1.000 (27) | 1.000 (27) | 42.0 | 0.00% |
| adversarial | 4 | 0.333 | 0.545 | 0.500 | — | — | — | 47.5 | 0.00% |

## Baseline comparison

| Split | Metric | Baseline | Current | Δ |
|:------|:-------|-------:|-------:|-------:|
| gold | intent_f1 | 1.000 | 1.000 | +0.000 ✅ |
| gold | slot_f1 | 0.931 | 0.966 | +0.034 ✅ |
| gold | joint_acc | 0.889 | 0.926 | +0.037 ✅ |
| gold | conf_wrong | 0.000 | 0.000 | +0.000 ✅ |
| adversarial | intent_f1 | 0.000 | 0.333 | +0.333 ✅ |
| adversarial | slot_f1 | 0.400 | 0.545 | +0.145 ✅ |
| adversarial | joint_acc | 0.000 | 0.500 | +0.500 ✅ |
| adversarial | conf_wrong | 0.000 | 0.000 | +0.000 ✅ |
