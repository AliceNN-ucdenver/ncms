"""Subject canonicalization + multi-subject ingest (Phase A).

This package owns every subject-related concern in the application
layer:

* :mod:`surface` — deterministic string normalization (``slugify``,
  ``normalize_surface``).  No I/O, no state.
* :mod:`registry` — the :class:`SubjectRegistry` class with the
  three-tier alias lookup + the ``subject.alias_collision``
  audit event.
* :mod:`resolver` — the A.3 precedence chain
  (caller subjects → caller string → SLM auto-suggest → empty)
  plus the cross-kwarg conflict raise.
* :mod:`bake` — :func:`bake_subjects_payload` writes the resolved
  list into ``Memory.structured["subjects"]`` as JSON-stable
  ``list[dict]``.
* :mod:`l2_emission` — multi-subject L2 ENTITY_STATE node creation
  (claim A.6).
* :mod:`edges` — ``MENTIONS_ENTITY`` graph edges with role metadata
  (claim A.7).
* :mod:`inheritance` — parent-doc primary-subject inheritance
  (claim A.10) via ``structured["source_doc_id"]`` lookup.

Re-exports below are the canonical import surface; downstream code
imports ``from ncms.application.subject import …`` rather than
reaching into the submodules.
"""

from ncms.application.subject.bake import bake_subjects_payload
from ncms.application.subject.edges import create_mentions_entity_edges
from ncms.application.subject.inheritance import (
    inherit_primary_subject_from_parent_doc,
)
from ncms.application.subject.l2_emission import (
    create_l2_nodes_for_subjects,
    subjects_from_memory,
)
from ncms.application.subject.registry import SubjectRegistry
from ncms.application.subject.resolver import (
    link_resolved_subject_entities,
    resolve_subjects,
)
from ncms.application.subject.surface import normalize_surface, slugify

__all__ = [
    "SubjectRegistry",
    "bake_subjects_payload",
    "create_l2_nodes_for_subjects",
    "create_mentions_entity_edges",
    "inherit_primary_subject_from_parent_doc",
    "link_resolved_subject_entities",
    "normalize_surface",
    "resolve_subjects",
    "slugify",
    "subjects_from_memory",
]
