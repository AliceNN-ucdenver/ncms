"""Enrichment pipeline: recall bonuses + RecallResult context decoration.

Extracted from memory_service.py as part of Phase 0 maintainability work.
Handles the "enrich" stage of the recall pipeline — once the base
search results arrive, this pipeline fetches state snapshots, episode
context, causal chains, and document sections to produce complete
RecallResult objects.
"""

from ncms.application.enrichment.pipeline import EnrichmentPipeline

__all__ = ["EnrichmentPipeline"]
