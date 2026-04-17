"""Scoring pipeline: multi-signal candidate scoring and ranking.

Extracted from memory_service.py as part of Phase 0 maintainability work.
Handles the "score" stage of the search pipeline — once candidates have
been retrieved, reranked, and expanded, this pipeline produces the
final ranked list of ScoredMemory results.
"""

from ncms.application.scoring.pipeline import ScoringPipeline

__all__ = ["ScoringPipeline"]
