"""Grammar shape cache maintenance.

Empty stub.  Implementation lands in Phase 4 per ``docs/p1-plan.md``.

Rebuilds stale ``GrammarShape`` entries on a configurable schedule
(plumbed into the existing ``MaintenanceScheduler``).  Staleness is
driven by: (a) number of new memories for the subject since last
induction, (b) wall-clock age of the shape, (c) explicit invalidation
markers dropped by the ingest-side induction hook.
"""

from __future__ import annotations

__all__: list[str] = []
