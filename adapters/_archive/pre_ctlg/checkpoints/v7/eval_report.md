# Adapter eval report — software_dev

Generated: 2026-04-23T03:09:22.848915+00:00
Adapter:   `adapters/checkpoints/software_dev/v7`

**Gate verdict:** ✅ PASS

## Warnings

- eval split 'adversarial' is empty — skipped

## Thresholds

- intent_f1_min: **0.700**
- slot_f1_min: **0.500**
- confidently_wrong_max: **0.100**
- regression_tolerance: **0.020**
- latency_p95_soft_limit: **200.0 ms**

## Metrics

| Split | N | Intent F1 | Slot F1 | Joint | Topic F1 (N) | Admission F1 (N) | State F1 (N) | p95 ms | Conf-wrong % |
|:------|--:|---------:|--------:|------:|-------------:|-----------------:|-------------:|-------:|-------------:|
| gold | 367 | 1.000 | 0.662 | 0.826 | — | 1.000 (367) | 1.000 (367) | 111.2 | 0.00% |
