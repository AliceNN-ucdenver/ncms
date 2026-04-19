"""Composition invariant — ``grammar ∨ BM25``.

Pure function that composes a grammar dispatch result with the
existing BM25 (or hybrid-retrieval) ranking.  The invariant, stated
as Proposition 1 in ``docs/temporal-linguistic-geometry.md``:

    The composed ranking is confidently-wrong iff *both* branches
    are confidently-wrong.  Because BM25 is never "confidently"
    wrong (it doesn't claim confidence at all) the composition's
    confident-wrong rate is bounded by the grammar's.  In the LME
    experiment that rate is empirically 0 / 500.

Concrete rules:

* If :meth:`LGTrace.has_confident_answer` is True:
    * :attr:`grammar_answer` lands at rank 1.
    * :attr:`zone_context` IDs follow in order, de-duplicated.
    * The original BM25 list (less the IDs already placed) follows.
* Otherwise the BM25 list is returned unchanged.  The grammar answer
  (if any) stays in-place within the BM25 list — we do NOT demote
  items below their BM25 rank, only promote.

The function is memory-ID-agnostic: it takes IDs in + IDs out,
lets callers decide whether to hydrate to Memory objects before or
after composition.
"""

from __future__ import annotations

from collections.abc import Iterable

from ncms.domain.tlg.grammar import LGTrace


def compose(
    bm25_ranking: Iterable[str],
    trace: LGTrace,
) -> list[str]:
    """Return the composed ID ranking: grammar-first-then-BM25 when
    the trace is confident; BM25 unchanged otherwise.

    Deduplicates while preserving the chosen order.
    """
    bm25_list = list(bm25_ranking)
    if not trace.has_confident_answer():
        return bm25_list

    seen: set[str] = set()
    out: list[str] = []

    # Grammar answer — rank 1 when present.  has_confident_answer()
    # already guaranteed grammar_answer is not None.
    answer = trace.grammar_answer
    if answer is not None and answer not in seen:
        out.append(answer)
        seen.add(answer)

    # Zone context — sibling candidates that expand the set.
    for mid in trace.zone_context:
        if mid not in seen:
            out.append(mid)
            seen.add(mid)

    # BM25 fallback — whatever else was ranked, in order.
    for mid in bm25_list:
        if mid not in seen:
            out.append(mid)
            seen.add(mid)

    return out
