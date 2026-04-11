"""Section Service — section-aware ingestion for navigable documents.

Handles the ingestion pipeline for content classified as NAVIGABLE:
creates a compact section index (parent) and individual section memories (children).
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from ncms.config import NCMSConfig
from ncms.domain.content_classifier import ContentClassification, Section
from ncms.domain.models import Memory

if TYPE_CHECKING:
    from ncms.application.memory_service import MemoryService

logger = logging.getLogger(__name__)

# Maximum length for the generated section index / TOC
_INDEX_MAX_CHARS = 300


class SectionService:
    """Ingests navigable documents as a section index + section children."""

    def __init__(
        self,
        memory_service: MemoryService,
        config: NCMSConfig | None = None,
    ):
        self._memory_service = memory_service
        self._config = config or NCMSConfig()

    async def ingest_navigable(
        self,
        content: str,
        classification: ContentClassification,
        sections: list[Section],
        memory_type: str,
        importance: float,
        tags: list[str] | None,
        structured: dict | None,
        source: str | None,
        agent_id: str | None,
    ) -> Memory:
        """Ingest navigable content: create section index + section children.

        Returns the parent section-index memory.
        """
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        tags = list(tags or [])

        # ── Generate compact TOC ──────────────────────────────────────
        toc = _build_section_index(sections)

        # ── Store parent section-index memory ─────────────────────────
        index_structured = dict(structured or {})
        index_structured["content_classification"] = {
            "content_class": classification.content_class.value,
            "format_hint": classification.format_hint,
            "section_count": len(sections),
            "original_content_hash": content_hash,
        }

        # Use the underlying store_memory for the index (bypasses classification
        # gate since type=section_index and importance=7.0 signals internal use).
        # We call the memory service's _store_memory_raw to avoid re-triggering
        # our own classification. Since that private method doesn't exist, we
        # temporarily disable classification and call store_memory.
        parent = await self._store_bypassing_classification(
            content=toc,
            memory_type="section_index",
            importance=7.0,
            tags=tags + ["section_index", f"format:{classification.format_hint}"],
            structured=index_structured,
            source=source,
            agent_id=agent_id,
        )

        logger.info(
            "Section index created: %s (%d sections, format=%s)",
            parent.id, len(sections), classification.format_hint,
        )

        # ── Store each section as a child memory ──────────────────────
        for section in sections:
            section_structured = {
                "parent_index_id": parent.id,
                "section_index": section.index,
                "section_heading": section.heading,
                "content_hash": content_hash,
            }

            await self._store_bypassing_classification(
                content=section.text,
                memory_type="document_section",
                importance=importance,
                tags=tags + [
                    "document_section",
                    f"section:{section.index}",
                    f"parent:{parent.id}",
                ],
                structured=section_structured,
                source=source,
                agent_id=agent_id,
            )

        logger.info(
            "Stored %d sections for parent %s", len(sections), parent.id,
        )

        return parent

    async def _store_bypassing_classification(
        self,
        content: str,
        memory_type: str,
        importance: float,
        tags: list[str],
        structured: dict,
        source: str | None,
        agent_id: str | None,
    ) -> Memory:
        """Store a memory bypassing content classification to avoid recursion.

        Temporarily disables content_classification_enabled on the config,
        calls store_memory, then restores the flag.
        """
        original = self._config.content_classification_enabled
        try:
            # Temporarily disable to prevent re-triggering classification
            object.__setattr__(self._config, "content_classification_enabled", False)
            ms = self._memory_service
            return await ms.store_memory(  # type: ignore[union-attr]
                content=content,
                memory_type=memory_type,
                importance=importance,
                tags=tags,
                structured=structured,
                source_agent=agent_id,
            )
        finally:
            object.__setattr__(
                self._config, "content_classification_enabled", original,
            )


def _build_section_index(sections: list[Section]) -> str:
    """Build a compact TOC string from sections, capped at ~300 chars."""
    lines: list[str] = []
    total = 0
    for section in sections:
        entry = f"{section.index + 1}. {section.heading}"
        if total + len(entry) + 1 > _INDEX_MAX_CHARS:
            remaining = len(sections) - len(lines)
            if remaining > 0:
                lines.append(f"... +{remaining} more sections")
            break
        lines.append(entry)
        total += len(entry) + 1  # +1 for newline

    return "\n".join(lines)
