"""TLG runtime pipelines (application layer).

Wires the pure ``ncms.domain.tlg`` grammar pieces to stores, the
IndexWorker, and the retrieval pipeline.

* :mod:`.induction` — L2 marker induction pipeline + retirement-verb
  loader for the reconciliation extractor (Phase 3a)
* :mod:`.vocabulary_cache` — L1 vocabulary cache + subject /
  entity lookups (Phase 3b)
* :mod:`.dispatch` — query-side grammar dispatch (``retrieve_lg``)
  (Phase 3c stub)
* :mod:`.maintenance` — staleness / rebuild / eviction of
  ``grammar_shape_cache`` (Phase 4 stub)

See ``docs/p1-plan.md`` for phase ordering.
"""

from __future__ import annotations

from ncms.application.tlg.induction import (
    induce_and_persist_markers,
    load_retirement_verbs,
    run_marker_induction,
)
from ncms.application.tlg.vocabulary_cache import VocabularyCache

__all__ = [
    "VocabularyCache",
    "induce_and_persist_markers",
    "load_retirement_verbs",
    "run_marker_induction",
]
