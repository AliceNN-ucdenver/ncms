"""L3 — Production rules.

Empty stub.  Implementation lands in Phase 3 per ``docs/p1-plan.md``.

The L3 layer holds compiled production rules keyed by
(intent, subject_shape).  Each production maps a query shape to a
retrieval plan (which candidates to pull, how to order them, which
confidence level to assign the answer).

Pure domain: the rule data model, matching, and confidence assignment.
"""

from __future__ import annotations

__all__: list[str] = []
