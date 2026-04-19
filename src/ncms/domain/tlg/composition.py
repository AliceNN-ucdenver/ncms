"""Composition invariant: grammar ∨ BM25.

Empty stub.  Implementation lands in Phase 3 per ``docs/p1-plan.md``.

Pure function: given a BM25 ranked list and an optional confident
grammar answer, returns the composed ranking.  Invariant: grammar
answer is prepended iff confidence ∈ {high, medium}; otherwise the
BM25 list is returned unchanged.  No scoring-weight tuning, no
re-ranking of BM25 internals.
"""

from __future__ import annotations

__all__: list[str] = []
