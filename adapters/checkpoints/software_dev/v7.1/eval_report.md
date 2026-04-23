# Adapter eval report — software_dev

Generated: 2026-04-23T05:07:35.963831+00:00
Adapter:   `adapters/checkpoints/software_dev/v7.1`

**Gate verdict:** ✅ PASS

## Warnings

- eval split 'adversarial' is empty — skipped

## Thresholds

- intent_f1_min: **0.700**
- slot_f1_min: **0.600**
- confidently_wrong_max: **0.100**
- regression_tolerance: **0.020**
- latency_p95_soft_limit: **200.0 ms**

## Metrics

| Split | N | Intent F1 | Slot F1 | Joint | Topic F1 (N) | Admission F1 (N) | State F1 (N) | p95 ms | Conf-wrong % |
|:------|--:|---------:|--------:|------:|-------------:|-----------------:|-------------:|-------:|-------------:|
| gold | 367 | 1.000 | 0.807 | 0.888 | — | 1.000 (367) | 1.000 (367) | 118.7 | 0.00% |
