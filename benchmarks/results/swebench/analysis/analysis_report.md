# SWE-bench Django Structural Analysis

**Total Django issues**: 850
**Date range**: 2015-08-19 to 2023-07-17

## Subsystem Distribution

| Subsystem | Count |
|-----------|-------|
| orm | 382 |
| management | 67 |
| utils | 63 |
| admin | 57 |
| other | 56 |
| forms | 48 |
| auth | 38 |
| templates | 26 |
| views | 24 |
| urls | 15 |
| http | 13 |
| middleware | 13 |
| cache | 13 |
| staticfiles | 11 |
| contenttypes | 8 |
| sessions | 7 |
| mail | 4 |
| serializers | 4 |
| signals | 1 |

## File Overlap (AR Ground Truth Signal)

- Total issue pairs: 360,825
- Pairs with file overlap: 7,193 (2.0%)
- Jaccard mean: 0.5491
- Jaccard median: 0.5000
- Jaccard max: 1.0000

## File Distribution

- Unique files: 313
- Files in 2+ issues: 208
- Files in 5+ issues: 85
- Files in 10+ issues: 25

### Top 20 Most-Modified Files

| File | Issues |
|------|--------|
| `django/db/models/sql/query.py` | 50 |
| `django/db/models/expressions.py` | 44 |
| `django/db/models/query.py` | 38 |
| `django/db/models/base.py` | 37 |
| `django/db/models/sql/compiler.py` | 32 |
| `django/db/models/fields/__init__.py` | 31 |
| `django/contrib/admin/options.py` | 18 |
| `django/db/migrations/autodetector.py` | 18 |
| `django/db/models/fields/related.py` | 17 |
| `django/forms/models.py` | 16 |
| `django/urls/resolvers.py` | 15 |
| `django/views/debug.py` | 14 |
| `django/utils/autoreload.py` | 14 |
| `django/db/backends/sqlite3/schema.py` | 13 |
| `django/db/models/lookups.py` | 12 |
| `django/template/defaultfilters.py` | 12 |
| `django/db/models/query_utils.py` | 12 |
| `django/db/backends/base/schema.py` | 12 |
| `django/db/migrations/operations/models.py` | 12 |
| `django/http/response.py` | 11 |

## Entity Overlap (Graph Connectivity Signal)

- Sample size: 50 issues
- Unique entities: 468
- Entities in 2+ issues: 72
- Entities in 5+ issues: 9
- Entities per issue: mean=12.5, median=11, max=20
- Pairwise overlap fraction: 0.375
- Jaccard mean: 0.0556
- Jaccard max: 0.3333

### Top 20 Entities

| Entity | Count |
|--------|-------|
| django | 27 |
| models | 11 |
| model | 8 |
| settings | 7 |
| pull | 7 |
| manage.py | 6 |
| manage | 5 |
| form | 5 |
| test | 5 |
| execute | 4 |
| command | 4 |
| sql | 4 |
| field | 4 |
| admin | 4 |
| core | 3 |
| conf | 3 |
| base | 3 |
| base.py | 3 |
| app | 3 |
| makemigrations | 3 |

## Competency Split Coverage

| Split | Queries | Coverage | Notes |
|-------|---------|----------|-------|
| AR (Accurate Retrieval) | 159 | 94% | avg 15.1 relevant/query |
| TTL (Test-Time Learning) | 170 | 100% | 16 subsystems |
| CR (Conflict Resolution) | 146 | N/A | avg depth 7.4 |
| LRU (Long-Range Understanding) | 45 | N/A | avg 52 relevant/query |

## Structural Comparison: BEIR SciFact vs SWE-bench Django

| Metric | SciFact (observed) | SWE-bench Django (predicted) |
|--------|--------------------|------------------------------|
| Unique entities | 51,357 | ~4773 |
| Graph edges | 0 | >> 0 (entities in 2+ issues: 72) |
| Connected components | 51,357 | << 4773 |
| Entity overlap fraction | ~0 | 0.375 |
| Unique files | N/A | 313 |
| Files in 5+ issues | N/A | 85 |
| ACT-R crossover | None (best=0.0) | Expected 0.1-0.2 |
| Dream cycle delta | +0.04% | Expected > +1% |

## Temporal Distribution

| Year | Count |
|------|-------|
| 2015 | 2 |
| 2016 | 3 |
| 2017 | 6 |
| 2018 | 17 |
| 2019 | 193 |
| 2020 | 226 |
| 2021 | 177 |
| 2022 | 147 |
| 2023 | 79 |

## Validation Verdict

**PASS** — Dataset has sufficient relational structure for NCMS evaluation.