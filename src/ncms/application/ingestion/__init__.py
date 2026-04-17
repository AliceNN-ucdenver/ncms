"""Ingestion pipeline: store-side gates, indexing, and node creation.

Extracted from memory_service.py as part of Phase 0 maintainability work.
Covers the full store pipeline: pre-admission gates (dedup, size,
content classification), admission scoring, inline indexing
(BM25/SPLADE/GLiNER), HTMG node creation (L1 atomic + L2 entity_state),
state reconciliation, episode assignment, and deferred contradiction
detection.
"""

from ncms.application.ingestion.pipeline import IngestionPipeline

__all__ = ["IngestionPipeline"]
