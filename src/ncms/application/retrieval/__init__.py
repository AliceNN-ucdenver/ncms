"""Retrieval pipeline: candidate discovery, reranking, and expansion.

Extracted from memory_service.py as part of Phase 0 maintainability work.
Covers the "retrieve" and "expand" stages of the search pipeline —
everything that happens between the query arriving and the scoring
pass.
"""

from ncms.application.retrieval.pipeline import RetrievalPipeline

__all__ = ["RetrievalPipeline"]
