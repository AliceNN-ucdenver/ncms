"""Authoritative per-domain catalogs for SDG + LLM labeller + judge.

A catalog is a :class:`~primitives.CatalogEntry`-indexed mapping
from lowercased surface forms (+ aliases) to authoritative slot +
topic assignments.  Acts as ground truth that:

  - SDG pools are built from (so templates emit canonical surfaces).
  - The LLM labeller normalises through (so a known surface always
    gets its authoritative slot, overriding LLM interpretation).
  - The judge validates against for known surfaces (reducing
    LLM-judge-hallucinated disagreement).

See ``software_dev.py``, ``clinical.py``, etc. for per-domain
catalogs.  ``normalize.py`` is the public lookup API.
"""

from ncms.application.adapters.sdg.catalog.normalize import (
    all_canonical_surfaces,
    canonical_slot,
    detect_spans,
    lookup,
    pool_topic,
    pool_values,
    topic_for,
)
from ncms.application.adapters.sdg.catalog.primitives import (
    CatalogEntry,
)

__all__ = [
    "CatalogEntry",
    "all_canonical_surfaces",
    "canonical_slot",
    "detect_spans",
    "lookup",
    "pool_topic",
    "pool_values",
    "topic_for",
]
