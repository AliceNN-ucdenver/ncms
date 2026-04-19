"""TLG runtime pipelines (application layer).

Wires the pure ``ncms.domain.tlg`` grammar pieces to stores, the
IndexWorker, and the retrieval pipeline.

* :mod:`.induction` — L2 marker induction pipeline + retirement-verb
  loader (Phase 3a)
* :mod:`.vocabulary_cache` — L1 vocabulary cache + subject / entity
  lookups (Phase 3b)
* :mod:`.dispatch` — query-side grammar dispatch (``retrieve_lg``)
  for current / origin / still intents (Phase 3c)
* :mod:`.maintenance` — staleness / rebuild / eviction of
  ``grammar_shape_cache`` (Phase 4 stub)

See ``docs/p1-plan.md`` for phase ordering.
"""

from __future__ import annotations

from ncms.application.tlg.dispatch import retrieve_lg
from ncms.application.tlg.induction import (
    induce_and_persist_markers,
    load_retirement_verbs,
    run_marker_induction,
)
from ncms.application.tlg.shape_cache_store import ShapeCacheStore
from ncms.application.tlg.vocabulary_cache import VocabularyCache

__all__ = [
    "ShapeCacheStore",
    "VocabularyCache",
    "induce_and_persist_markers",
    "load_retirement_verbs",
    "retrieve_lg",
    "run_marker_induction",
]
