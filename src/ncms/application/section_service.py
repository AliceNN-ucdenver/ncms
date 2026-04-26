"""Section Service — section-aware ingestion for navigable documents.

Handles the ingestion pipeline for content classified as NAVIGABLE:
creates ONE rich document profile memory in the memory store and stores
the full document + individual sections in the document store.

At retrieval time, when a document profile is found, the memory service
expands it into relevant document sections from the document store.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from ncms.config import NCMSConfig
from ncms.domain.content_classifier import ContentClassification, Section
from ncms.domain.models import Memory

if TYPE_CHECKING:
    from ncms.application.document_service import DocumentService
    from ncms.application.memory_service import MemoryService

logger = logging.getLogger(__name__)


class SectionService:
    """Ingests navigable documents as a document profile + document store sections."""

    def __init__(
        self,
        memory_service: MemoryService,
        config: NCMSConfig | None = None,
        document_service: DocumentService | None = None,
    ):
        self._memory_service = memory_service
        self._config = config or NCMSConfig()
        self._document_service = document_service

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
        domains: list[str] | None = None,
    ) -> Memory:
        """Ingest navigable content.

        When document_service is available:
        - Stores ONE document profile memory in the memory store
        - Stores the full document + individual sections in the document store

        When document_service is NOT available (standalone mode):
        - Falls back to the legacy approach of storing section index + section children
          as individual memories in the memory store.

        Returns the profile memory (or section-index memory in fallback mode).
        """
        if self._document_service is not None:
            return await self._ingest_with_doc_store(
                content=content,
                classification=classification,
                sections=sections,
                memory_type=memory_type,
                importance=importance,
                tags=tags,
                structured=structured,
                source=source,
                agent_id=agent_id,
                domains=domains,
            )
        else:
            logger.info(
                "[section-svc] No document_service available, falling back to legacy ingestion"
            )
            return await self._ingest_legacy(
                content=content,
                classification=classification,
                sections=sections,
                memory_type=memory_type,
                importance=importance,
                tags=tags,
                structured=structured,
                source=source,
                agent_id=agent_id,
                domains=domains,
            )

    # ── New approach: document profile + document store ───────────────

    async def _ingest_with_doc_store(
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
        domains: list[str] | None = None,
    ) -> Memory:
        """Ingest via document store: one profile memory + full doc + child sections.

        If structured contains 'source_doc_id', the parent document was already
        published (e.g. by the publish_document API endpoint). We skip creating
        the parent and only store child sections + the profile memory.
        """
        assert self._document_service is not None  # noqa: S101

        content_hash = hashlib.sha256(content.encode()).hexdigest()
        tags = list(tags or [])

        # Derive a title from the first section heading or source
        doc_title = sections[0].heading if sections else (source or "Untitled Document")

        logger.info(
            "[section-svc] Building document profile for '%s' (%d sections, format=%s)",
            doc_title[:60],
            len(sections),
            classification.format_hint,
        )

        # ── Parent document: reuse existing or create new ───────────
        existing_doc_id = (structured or {}).get("source_doc_id")
        if existing_doc_id:
            parent_doc = await self._document_service.get_document(existing_doc_id)
            if parent_doc:
                logger.info(
                    "[section-svc] Reusing existing document %s from document store",
                    parent_doc.id,
                )
            else:
                logger.warning(
                    "[section-svc] source_doc_id %s not found, creating new parent document",
                    existing_doc_id,
                )
                parent_doc = None

        if not existing_doc_id or not parent_doc:
            parent_doc = await self._document_service.publish_document(
                title=doc_title,
                content=content,
                from_agent=agent_id,
                doc_type=classification.format_hint,
                metadata={
                    "content_classification": classification.content_class.value,
                    "section_count": len(sections),
                    "content_hash": content_hash,
                },
            )
            logger.info(
                "[section-svc] Stored parent document %s in document store (%d bytes)",
                parent_doc.id,
                parent_doc.size_bytes,
            )

        # ── Store each section as a child document ───────────────────
        section_headings: list[str] = []
        for section in sections:
            section_headings.append(section.heading)
            await self._document_service.publish_document(
                title=section.heading,
                content=section.text,
                from_agent=agent_id,
                doc_type="section",
                parent_doc_id=parent_doc.id,
                metadata={
                    "section_index": section.index,
                    "parent_content_hash": content_hash,
                },
            )

        logger.info(
            "[section-svc] Stored %d child sections for parent %s",
            len(sections),
            parent_doc.id,
        )

        # ── Build rich document profile ──────────────────────────────
        profile_text = _build_document_profile(
            sections=sections,
            source=source,
            classification=classification,
        )

        # ── Store ONE profile memory in memory store ─────────────────
        profile_structured = dict(structured or {})
        profile_structured["doc_id"] = parent_doc.id
        profile_structured["section_count"] = len(sections)
        profile_structured["content_hash"] = content_hash
        profile_structured["section_headings"] = section_headings
        profile_structured["content_classification"] = {
            "content_class": classification.content_class.value,
            "format_hint": classification.format_hint,
            "section_count": len(sections),
            "original_content_hash": content_hash,
        }

        profile_memory = await self._store_bypassing_classification(
            content=profile_text,
            memory_type="document_profile",
            importance=max(importance, 8.0),
            tags=tags
            + [
                "document_profile",
                f"format:{classification.format_hint}",
                f"doc:{parent_doc.id}",
            ],
            structured=profile_structured,
            source=source,
            agent_id=agent_id,
            domains=domains,
        )

        logger.info(
            "[section-svc] Document profile memory created: %s (doc_id=%s, %d sections)",
            profile_memory.id,
            parent_doc.id,
            len(sections),
        )

        return profile_memory

    # ── Legacy approach: section index + N section memories ───────────

    async def _ingest_legacy(
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
        domains: list[str] | None = None,
    ) -> Memory:
        """Legacy ingestion: creates section_index + section children in memory store."""
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        tags = list(tags or [])

        # Generate compact TOC
        toc = _build_section_index(sections)

        # Store parent section-index memory
        index_structured = dict(structured or {})
        index_structured["content_classification"] = {
            "content_class": classification.content_class.value,
            "format_hint": classification.format_hint,
            "section_count": len(sections),
            "original_content_hash": content_hash,
        }

        parent = await self._store_bypassing_classification(
            content=toc,
            memory_type="section_index",
            importance=8.0,
            tags=tags + ["section_index", f"format:{classification.format_hint}"],
            structured=index_structured,
            source=source,
            agent_id=agent_id,
            domains=domains,
        )

        logger.info(
            "[section-svc] Section index created (legacy): %s (%d sections, format=%s)",
            parent.id,
            len(sections),
            classification.format_hint,
        )

        # Store each section as a child memory
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
                tags=tags
                + [
                    "document_section",
                    f"section:{section.index}",
                    f"parent:{parent.id}",
                ],
                structured=section_structured,
                source=source,
                agent_id=agent_id,
            )

        logger.info(
            "[section-svc] Stored %d sections for parent %s (legacy)",
            len(sections),
            parent.id,
        )

        return parent

    # ── Shared helpers ────────────────────────────────────────────────

    async def _store_bypassing_classification(
        self,
        content: str,
        memory_type: str,
        importance: float,
        tags: list[str],
        structured: dict,
        source: str | None,
        agent_id: str | None,
        domains: list[str] | None = None,
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
                domains=domains or [],
            )
        finally:
            object.__setattr__(
                self._config,
                "content_classification_enabled",
                original,
            )


def _first_sentence(text: str, max_chars: int = 80) -> str:
    """Extract the first meaningful sentence from text, capped at max_chars."""
    # Strip leading whitespace and common markers
    text = text.strip()
    if not text:
        return ""

    # Find first sentence boundary
    for end_char in ".!?\n":
        idx = text.find(end_char)
        if 0 < idx < max_chars:
            return text[: idx + 1].strip()

    # No sentence boundary found within limit — truncate at word boundary
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > max_chars // 2:
        return truncated[:last_space] + "..."
    return truncated + "..."


def _build_document_profile(
    sections: list[Section],
    source: str | None,
    classification: ContentClassification,
) -> str:
    """Build a rich document profile (~500-800 chars) for BM25/SPLADE indexing.

    Contains:
    - Title/heading of the document
    - Each section heading + first meaningful sentence (50-80 chars per section)
    - Metadata (agent, type, char count, section count)
    """
    lines: list[str] = []

    # Document header
    doc_title = sections[0].heading if sections else "Untitled"
    lines.append(f"Document: {doc_title}")
    if source:
        lines.append(f"Source: {source}")
    lines.append(f"Format: {classification.format_hint} | Sections: {len(sections)}")
    lines.append("")

    # Section summaries — heading + first sentence
    total_chars = sum(len(s.text) for s in sections)
    for section in sections:
        first = _first_sentence(section.text)
        if first:
            lines.append(f"- {section.heading}: {first}")
        else:
            lines.append(f"- {section.heading}")

    lines.append("")
    lines.append(f"Total content: {total_chars:,} chars across {len(sections)} sections")

    profile = "\n".join(lines)

    # Cap at ~800 chars to keep the profile concise
    if len(profile) > 800:
        profile = profile[:797] + "..."

    return profile


def _build_section_index(sections: list[Section]) -> str:
    """Build a compact TOC string from sections, capped at ~300 chars (legacy)."""
    lines: list[str] = []
    total = 0
    _index_max_chars = 300
    for section in sections:
        entry = f"{section.index + 1}. {section.heading}"
        if total + len(entry) + 1 > _index_max_chars:
            remaining = len(sections) - len(lines)
            if remaining > 0:
                lines.append(f"... +{remaining} more sections")
            break
        lines.append(entry)
        total += len(entry) + 1  # +1 for newline

    return "\n".join(lines)
