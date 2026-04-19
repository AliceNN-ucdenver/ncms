"""GrammarShape model + dispatch policy.

Empty stub.  Implementation lands in Phase 3 per ``docs/p1-plan.md``.

A ``GrammarShape`` is the materialised view of L1+L2+L3 for a given
subject_id at a given induction epoch.  Cached per-subject in the
``grammar_shape_cache`` table (schema v13) so query-time dispatch is
a single keyed lookup.

Pure domain: the shape model and dispatch-decision logic.
"""

from __future__ import annotations

__all__: list[str] = []
