"""Traversal pipeline: HTMG hierarchy walks and topic clustering.

Extracted from memory_service.py as part of Phase 0 maintainability work.
Provides the four traversal strategies (top-down, bottom-up, temporal,
lateral) used by the public ``traverse`` and ``synthesize`` APIs, plus
the topic-map clustering over L4 abstract nodes.
"""

from ncms.application.traversal.pipeline import TraversalPipeline

__all__ = ["TraversalPipeline"]
