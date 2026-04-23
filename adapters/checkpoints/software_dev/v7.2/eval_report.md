# Adapter eval report — software_dev

Generated: 2026-04-23T17:55:25.687074+00:00
Adapter:   `adapters/checkpoints/software_dev/v7.2`

**Gate verdict:** ✅ PASS

## Warnings

- eval split 'adversarial' is empty — skipped

## Thresholds

- intent_f1_min: **0.700**
- slot_f1_min: **0.650**
- confidently_wrong_max: **0.100**
- regression_tolerance: **0.020**
- latency_p95_soft_limit: **200.0 ms**

## Metrics

| Split | N | Intent F1 | Slot F1 | Joint | Topic F1 (N) | Admission F1 (N) | State F1 (N) | p95 ms | Conf-wrong % |
|:------|--:|---------:|--------:|------:|-------------:|-----------------:|-------------:|-------:|-------------:|
| gold | 671 | 1.000 | 0.813 | 0.937 | — | 1.000 (367) | 1.000 (367) | 104.2 | 0.00% |
