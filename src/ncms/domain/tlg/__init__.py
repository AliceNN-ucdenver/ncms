"""Temporal Linguistic Geometry (TLG) — grammar layer, pure domain.

Zero infrastructure dependencies.  Submodules hold the theory pieces:

* :mod:`.retirement_extractor` — structural extractor for
  ``retires_entities`` (Phase 1)
* :mod:`.vocabulary` — L1 subject-vocabulary induction (Phase 2)
* :mod:`.markers` — L2 transition-marker induction (Phase 2)
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

from ncms.domain.tlg.markers import (
    VERB_PHRASE_SHAPES,
    EdgeObservation,
    InducedEdgeMarkers,
    extract_verb_heads,
    induce_edge_markers,
    match_intent_from_markers,
    retirement_verbs_from,
)
from ncms.domain.tlg.retirement_extractor import (
    SEED_RETIREMENT_VERBS,
    extract_retired,
)
from ncms.domain.tlg.vocabulary import (
    InducedVocabulary,
    SubjectMemory,
    induce_vocabulary,
    lookup_entity,
    lookup_subject,
)

__all__ = [
    # Phase 1 — retirement extraction
    "SEED_RETIREMENT_VERBS",
    "extract_retired",
    # Phase 2 — L1 vocabulary
    "InducedVocabulary",
    "SubjectMemory",
    "induce_vocabulary",
    "lookup_entity",
    "lookup_subject",
    # Phase 2 — L2 markers
    "EdgeObservation",
    "InducedEdgeMarkers",
    "VERB_PHRASE_SHAPES",
    "extract_verb_heads",
    "induce_edge_markers",
    "match_intent_from_markers",
    "retirement_verbs_from",
]
