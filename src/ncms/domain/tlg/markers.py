"""L2 — Transition-marker induction.

Empty stub.  Implementation lands in Phase 2 per ``docs/p1-plan.md``.

The L2 layer induces the set of transition markers (verbs, state-change
phrases) that signal a subject moving between states.  Input: state
transitions from the reconciliation service (SUPERSEDES /
SUPERSEDED_BY edges).  Output: ``TransitionMarker`` records with lift
scores (PMI between marker and confirmed transitions).

Pure domain: data model + induction algorithm.  Runtime pipelines live
in :mod:`ncms.application.tlg.induction`.
"""

from __future__ import annotations

__all__: list[str] = []
