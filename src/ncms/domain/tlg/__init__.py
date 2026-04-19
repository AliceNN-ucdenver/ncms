"""Temporal Linguistic Geometry (TLG) — grammar layer, pure domain.

Zero infrastructure dependencies.  Submodules hold the theory pieces:

* :mod:`.retirement_extractor` — structural extractor for
  ``retires_entities`` (Phase 1)
* :mod:`.vocabulary` — L1 subject-vocabulary induction (Phase 2)
* :mod:`.markers` — L2 transition-marker induction (Phase 2)
* :mod:`.confidence` — four-level confidence model + invariant
  predicate (Phase 3c)
* :mod:`.grammar` — :class:`LGIntent` + :class:`LGTrace` models
  (Phase 3c)
* :mod:`.composition` — grammar ∨ BM25 composition (Phase 3c)
* :mod:`.query_classifier` — minimal regex query-intent classifier
  (Phase 3c)
* :mod:`.productions` — L3 production rules (Phase 4 stub)

Runtime pipelines that use these domain pieces live under
:mod:`ncms.application.tlg`.

See ``docs/p1-plan.md`` and ``docs/temporal-linguistic-geometry.md``
for the theory and integration plan.
"""

from __future__ import annotations

from ncms.domain.tlg.aliases import expand_aliases, induce_aliases
from ncms.domain.tlg.composition import compose
from ncms.domain.tlg.confidence import CONFIDENT_LEVELS, Confidence, is_confident
from ncms.domain.tlg.grammar import LGIntent, LGTrace
from ncms.domain.tlg.markers import (
    VERB_PHRASE_SHAPES,
    EdgeObservation,
    InducedEdgeMarkers,
    extract_verb_heads,
    induce_edge_markers,
    match_intent_from_markers,
    retirement_verbs_from,
)
from ncms.domain.tlg.query_classifier import classify_query_intent
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
from ncms.domain.tlg.zones import (
    ADMISSIBLE_TRANSITIONS,
    Zone,
    ZoneEdge,
    compute_zones,
    current_zone,
    origin_memory,
    retirement_memory,
)

__all__ = [
    # Phase 1 — retirement extraction
    "SEED_RETIREMENT_VERBS",
    "extract_retired",
    # Phase 3d — aliases
    "expand_aliases",
    "induce_aliases",
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
    # Phase 3c — confidence + composition + dispatch models
    "CONFIDENT_LEVELS",
    "Confidence",
    "LGIntent",
    "LGTrace",
    "classify_query_intent",
    "compose",
    "is_confident",
    # Phase 3d — zones (L3 grammar)
    "ADMISSIBLE_TRANSITIONS",
    "Zone",
    "ZoneEdge",
    "compute_zones",
    "current_zone",
    "origin_memory",
    "retirement_memory",
]
