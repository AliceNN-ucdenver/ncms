"""Temporal primitives — pure, infrastructure-agnostic.

This package holds deterministic temporal helpers that feed both the
current ordinal/range filter retrieval path *and* (soon) the TLG
grammar dispatch layer in :mod:`ncms.domain.tlg`.

Public surface (re-exported from submodules so existing call sites can
keep ``from ncms.domain.temporal import X``):

* :class:`~ncms.domain.temporal.parser.TemporalReference` + regex parser
* :class:`~ncms.domain.temporal.intent.TemporalIntent` + classifier
  (marked for retirement once TLG grammar dispatch proves equivalence
  on LME500 — see ``docs/p1-plan.md`` Appendix F)
* :func:`~ncms.domain.temporal.normalizer.normalize_spans` +
  :class:`NormalizedInterval` for GLiNER → ISO-interval conversion
"""

from __future__ import annotations

from ncms.domain.temporal.intent import (
    ArithmeticSpec,
    TemporalIntent,
    classify_temporal_intent,
    parse_arithmetic_spec,
)
from ncms.domain.temporal.normalizer import (
    NormalizedInterval,
    RawSpan,
    merge_intervals,
    normalize_spans,
)
from ncms.domain.temporal.parser import (
    TemporalReference,
    compute_temporal_proximity,
    parse_temporal_reference,
)

__all__ = [
    "ArithmeticSpec",
    "NormalizedInterval",
    "RawSpan",
    "TemporalIntent",
    "TemporalReference",
    "classify_temporal_intent",
    "compute_temporal_proximity",
    "merge_intervals",
    "normalize_spans",
    "parse_arithmetic_spec",
    "parse_temporal_reference",
]
