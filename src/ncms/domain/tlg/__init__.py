"""Temporal Linguistic Geometry (TLG) — grammar layer, pure domain.

Zero infrastructure dependencies.  Submodules hold the theory pieces:

* :mod:`.vocabulary` — L1 subject-vocabulary induction
* :mod:`.markers` — L2 transition-marker induction
* :mod:`.productions` — L3 production rules
* :mod:`.grammar` — :class:`GrammarShape` model + dispatch policy
* :mod:`.confidence` — four-level confidence model
* :mod:`.composition` — grammar ∨ BM25 invariant

Runtime pipelines that use these domain pieces live under
:mod:`ncms.application.tlg`.

See ``docs/p1-plan.md`` and ``docs/temporal-linguistic-geometry.md``
for the theory and integration plan.
"""

from __future__ import annotations

__all__: list[str] = []
