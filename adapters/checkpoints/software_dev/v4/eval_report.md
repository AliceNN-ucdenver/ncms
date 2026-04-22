# Adapter eval report — software_dev

Generated: 2026-04-20T00:54:43.492509+00:00
Adapter:   `experiments/intent_slot_distillation/adapters/software_dev/v4`

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
| gold | 42 | 1.000 | 0.983 | 0.952 | 1.000 (42) | 1.000 (42) | 1.000 (42) | 72.6 | 0.00% |
| adversarial | 4 | 0.111 | 0.286 | 0.000 | — | — | — | 270.5 | 0.00% |

## Baseline comparison

| Split | Metric | Baseline | Current | Δ |
|:------|:-------|-------:|-------:|-------:|
| gold | intent_f1 | 1.000 | 1.000 | +0.000 ✅ |
| gold | slot_f1 | 0.939 | 0.983 | +0.044 ✅ |
| gold | joint_acc | 0.881 | 0.952 | +0.071 ✅ |
| gold | conf_wrong | 0.000 | 0.000 | +0.000 ✅ |
| adversarial | intent_f1 | 0.000 | 0.111 | +0.111 ✅ |
| adversarial | slot_f1 | 0.286 | 0.286 | +0.000 ✅ |
| adversarial | joint_acc | 0.000 | 0.000 | +0.000 ✅ |
| adversarial | conf_wrong | 0.500 | 0.000 | -0.500 ❌ |
