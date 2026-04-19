"""Four-level confidence model: high / medium / low / abstain.

Empty stub.  Implementation lands in Phase 3 per ``docs/p1-plan.md``.

The confidence level is the structural signal that drives the
composition invariant: only ``high`` and ``medium`` grammar answers
are prepended to the BM25 ranking; ``low`` and ``abstain`` fall back
to BM25 unchanged.  This is the core safety property —
``grammar ∨ BM25`` guarantees zero confidently-wrong answers
(Proposition 1, empirically 0/500 on LongMemEval).
"""

from __future__ import annotations

__all__: list[str] = []
