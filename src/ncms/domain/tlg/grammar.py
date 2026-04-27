"""Grammar result models — :class:`LGIntent` + :class:`LGTrace`.

Pure data shapes.  Dispatch logic that produces them lives in
:mod:`ncms.application.tlg.dispatch`.

The four primitive transitions the L2 layer can induce (see
``docs/temporal-linguistic-geometry.md`` §5) are:

* ``introduces`` — a state begins (a subject's first memory).
* ``refines`` — same-zone continuation (additive, no replacement).
* ``supersedes`` — cross-zone transition (ends old, begins new).
* ``retires`` — ends a zone without starting a replacement.

Query-side intents are a smaller set — the dispatcher only needs
enough granularity to pick a production:

* ``current`` — "what's the current X?"
* ``origin`` — "what was the original X?"
* ``still`` — "are we still using X?"
* ``sequence`` — "what came after X?" (Phase 4+)
* ``predecessor`` — "what came before X?" (Phase 4+)
* ``interval`` — "what happened between X and Y?" (Phase 4+)
* ``range`` — date-range filter (already implemented by
  ``retrieval/pipeline.apply_range_filter``; TLG can defer to it)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ncms.domain.tlg.confidence import Confidence


@dataclass(frozen=True)
class LGIntent:
    """The grammar-classified intent of a query.

    Produced by the query classifier; consumed by the dispatcher.
    ``kind`` is an open string so downstream phases can add new
    intents without refactoring the dataclass.  The dispatcher
    recognises a closed set and returns :attr:`Confidence.NONE`
    for kinds it doesn't handle yet.
    """

    kind: str
    subject: str | None = None
    entity: str | None = None
    secondary: str | None = None


@dataclass
class LGTrace:
    """Explainable dispatch result.

    Exposes everything downstream needs to either trust the grammar
    answer or fall back to BM25:

    * :attr:`grammar_answer` — the MemoryNode ID (or Memory ID) the
      grammar picked, ``None`` when dispatch couldn't resolve.
    * :attr:`zone_context` — sibling IDs (same-zone or same-subject)
      that expand the result set without displacing BM25 rank 1.
    * :attr:`confidence` — drives :meth:`has_confident_answer`.
    * :attr:`proof` — a human-readable justification; for dashboards
      and debugging, not parsed programmatically.
    """

    query: str
    intent: LGIntent
    grammar_answer: str | None = None
    zone_context: list[str] = field(default_factory=list)
    admitted_zones: list[str] = field(default_factory=list)
    causal_edges_traversed: list[dict[str, str]] = field(default_factory=list)
    proof: str = ""
    confidence: Confidence = Confidence.NONE

    def has_confident_answer(self) -> bool:
        """Composition-invariant predicate.

        True iff the grammar answer is safe to prepend at rank 1
        (:attr:`Confidence.HIGH` or :attr:`Confidence.MEDIUM`) AND
        :attr:`grammar_answer` is populated.
        """
        if self.grammar_answer is None:
            return False
        return self.confidence in (Confidence.HIGH, Confidence.MEDIUM)
