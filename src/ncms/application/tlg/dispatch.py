"""Query-side grammar dispatch (``retrieve_lg``).

Empty stub.  Implementation lands in Phase 3 per ``docs/p1-plan.md``.

Runs alongside BM25/SPLADE at query time.  Classifies the query
against the subject's cached ``GrammarShape``, dispatches to the
matching production, and returns a ``(answer, confidence)`` pair.
The composition step (``grammar ∨ BM25``) lives in
:mod:`ncms.domain.tlg.composition` — this module just produces the
grammar answer, it never mutates the BM25 list.
"""

from __future__ import annotations

__all__: list[str] = []
