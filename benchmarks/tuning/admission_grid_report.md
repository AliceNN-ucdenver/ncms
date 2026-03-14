# Admission Weight Tuning Report

**Date**: 2026-03-14T00:36:40.857044+00:00
**Git SHA**: `9e3a6dd`
**Examples**: 44
**Configs tested**: 486

## Best Configuration

**Accuracy**: 65.9% (29/44)

### Routing Thresholds

| Parameter | Default | Tuned |
|-----------|---------|-------|
| Discard threshold | 0.25 | 0.25 |
| Ephemeral upper | 0.45 | 0.35 |
| State change threshold | 0.50 | 0.35 |
| Episode affinity threshold | 0.55 | 0.4 |

### Feature Weights

| Feature | Default | Tuned |
|---------|---------|-------|
| novelty | 0.2 | 0.15 * |
| utility | 0.18 | 0.22 * |
| reliability | 0.12 | 0.12 |
| temporal_salience | 0.12 | 0.12 |
| persistence | 0.15 | 0.15 |
| redundancy | -0.15 | -0.15 |
| episode_affinity | 0.04 | 0.04 |
| state_change_signal | 0.14 | 0.14 |

### Per-Category Accuracy

| Route | Accuracy |
|-------|----------|
| atomic_memory | 41.7% |
| discard | 90.0% |
| entity_state_update | 87.5% |
| ephemeral_cache | 62.5% |
| episode_fragment | 50.0% |

## Top 10 Configurations

| # | Accuracy | Discard | Ephemeral | StateChg | EpisodeAff |
|---|----------|---------|-----------|----------|------------|
| 1 | 65.9% | 0.25 | 0.35 | 0.35 | 0.4 |
| 2 | 65.9% | 0.25 | 0.35 | 0.4 | 0.4 |
| 3 | 65.9% | 0.25 | 0.4 | 0.35 | 0.4 |
| 4 | 65.9% | 0.25 | 0.4 | 0.4 | 0.4 |
| 5 | 65.9% | 0.25 | 0.45 | 0.35 | 0.4 |
| 6 | 65.9% | 0.25 | 0.45 | 0.4 | 0.4 |
| 7 | 65.9% | 0.25 | 0.35 | 0.35 | 0.4 |
| 8 | 65.9% | 0.25 | 0.35 | 0.4 | 0.4 |
| 9 | 63.6% | 0.25 | 0.35 | 0.35 | 0.4 |
| 10 | 63.6% | 0.25 | 0.35 | 0.4 | 0.4 |
