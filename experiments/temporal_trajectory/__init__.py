"""Temporal-trajectory rerank experiment — standalone PoC.

Tests whether reranking retrieval candidates by their position in a
chronological entity-graph path outperforms BM25-only, naive-date-sort,
and the Phase B entity-scoped ordinal primitive — specifically on
**state-evolution queries** where the answer is the end of a subject's
evolution chain.

Lives outside the main NCMS pipeline so the experiment can ship or fail
cheaply.  If the path approach shows a signal, the algorithm ports into
``RetrievalPipeline`` as a new primitive.  If not, we've spent ~4 hours
learning something and moved on.

See README.md for usage and interpretation.
"""
