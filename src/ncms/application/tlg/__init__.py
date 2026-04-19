"""TLG runtime pipelines (application layer).

Wires the pure ``ncms.domain.tlg`` grammar pieces to stores, the
IndexWorker, and the retrieval pipeline.

* :mod:`.induction` — ingest-side L1/L2 induction hook
* :mod:`.dispatch` — query-side grammar dispatch (``retrieve_lg``)
* :mod:`.maintenance` — staleness / rebuild / eviction of
  ``grammar_shape_cache``

See ``docs/p1-plan.md`` for phase ordering.
"""

from __future__ import annotations

__all__: list[str] = []
