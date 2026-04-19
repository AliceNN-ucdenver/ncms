"""L1 — Subject-vocabulary induction.

Empty stub.  Implementation lands in Phase 1 per ``docs/p1-plan.md``.

The L1 layer induces a per-subject vocabulary from observed entity
mentions.  Input: entity-linked memories from the IndexWorker.  Output:
``SubjectVocabulary`` records keyed by subject_id, tracking surface
forms, mention counts, and first/last-seen timestamps.

Pure domain: this module defines the data model and the induction
algorithm as pure functions.  Storage + scheduling live in
:mod:`ncms.application.tlg.induction`.
"""

from __future__ import annotations

__all__: list[str] = []
