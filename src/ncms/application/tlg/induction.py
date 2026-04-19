"""Ingest-side L1/L2 induction.

Empty stub.  Implementation lands in Phase 1–2 per ``docs/p1-plan.md``.

Runs inside the IndexWorker after reconciliation (hook point C, see
``docs/p1-plan.md`` §3.4.5) so ``retires_entities`` on the typed
edges is already populated.  Updates per-subject vocabulary and
transition-marker counts, invalidates the grammar shape cache for
affected subjects.
"""

from __future__ import annotations

__all__: list[str] = []
