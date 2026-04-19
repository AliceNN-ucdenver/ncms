"""Temporal Linguistic Geometry (TLG) — grammar layer, pure domain.

Zero infrastructure dependencies.  Submodules hold the theory pieces:

* :mod:`.retirement_extractor` — structural extractor for
  ``retires_entities`` (Phase 1, ported from
  ``experiments/temporal_trajectory/retirement_extractor.py``)
* :mod:`.vocabulary` — L1 subject-vocabulary induction (Phase 2 stub)
* :mod:`.markers` — L2 transition-marker induction (Phase 2 stub)
* :mod:`.productions` — L3 production rules (Phase 3 stub)
* :mod:`.grammar` — :class:`GrammarShape` model + dispatch policy
  (Phase 3 stub)
* :mod:`.confidence` — four-level confidence model (Phase 3 stub)
* :mod:`.composition` — grammar ∨ BM25 invariant (Phase 3 stub)

Runtime pipelines that use these domain pieces live under
:mod:`ncms.application.tlg`.

See ``docs/p1-plan.md`` and ``docs/temporal-linguistic-geometry.md``
for the theory and integration plan.
"""

from __future__ import annotations

from ncms.domain.tlg.retirement_extractor import (
    SEED_RETIREMENT_VERBS,
    extract_retired,
)

__all__ = [
    "SEED_RETIREMENT_VERBS",
    "extract_retired",
]
