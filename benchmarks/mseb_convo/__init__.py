"""MSEB-Convo — conversational state-evolution + preference benchmark.

Wraps the LongMemEval multi-session corpus into the MSEB schema
and adds hand-authored preference gold queries covering the four
``PreferenceKind`` sub-types (positive / avoidance / habitual /
difficult) that the P2 ``intent_head`` emits.  See
``benchmarks/mseb_convo/README.md`` for methodology.
"""
