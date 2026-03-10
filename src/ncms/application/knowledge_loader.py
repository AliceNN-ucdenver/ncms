"""Knowledge Loader - "Download knowledge to your agents like Neo in the Matrix."

Imports knowledge from various file formats into the NCMS memory store.
Supports: Markdown, plain text, JSON, YAML, CSV, HTML, and (with `ncms[docs]`)
rich document formats including DOCX, PPTX, PDF, and XLSX via MarkItDown.

Usage:
    loader = KnowledgeLoader(memory_service)
    stats = await loader.load_file("architecture.md", domains=["arch"])
    stats = await loader.load_file("design.pptx", domains=["design"])  # needs ncms[docs]
    stats = await loader.load_directory("docs/", domains=["docs"])
    stats = await loader.load_text("raw knowledge text", domains=["custom"])
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ncms.application.memory_service import MemoryService

logger = logging.getLogger(__name__)

# Dynamic import — markitdown is optional (`pip install ncms[docs]`)
try:
    from markitdown import MarkItDown

    _HAS_MARKITDOWN = True
except ImportError:  # pragma: no cover
    _HAS_MARKITDOWN = False


@dataclass
class LoadStats:
    """Statistics from a knowledge loading operation."""

    files_processed: int = 0
    memories_created: int = 0
    chunks_total: int = 0
    errors: list[str] = field(default_factory=list)


class KnowledgeLoader:
    """Imports knowledge from files and text into NCMS memory.

    Splits content into semantic chunks (by headings for markdown,
    by paragraphs for plain text, by entries for JSON) and stores
    each chunk as an individual memory for precise retrieval.
    """

    # Text-based formats — always available (stdlib parsing)
    TEXT_EXTENSIONS = {
        ".md", ".markdown",  # Markdown
        ".txt", ".text",     # Plain text
        ".json",             # JSON
        ".yaml", ".yml",     # YAML
        ".csv",              # CSV
        ".rst",              # reStructuredText
        ".html", ".htm",     # HTML (basic text extraction)
    }

    # Rich document formats — require `ncms[docs]` (markitdown)
    DOCUMENT_EXTENSIONS = {
        ".docx",  # Microsoft Word
        ".pptx",  # Microsoft PowerPoint
        ".pdf",   # PDF documents
        ".xlsx",  # Microsoft Excel
    }

    @property
    def SUPPORTED_EXTENSIONS(self) -> set[str]:  # noqa: N802
        """All currently loadable extensions (text + documents if markitdown installed)."""
        exts = set(self.TEXT_EXTENSIONS)
        if _HAS_MARKITDOWN:
            exts |= self.DOCUMENT_EXTENSIONS
        return exts

    @staticmethod
    def has_markitdown() -> bool:
        """Check whether markitdown is available for rich document support."""
        return _HAS_MARKITDOWN

    def __init__(
        self,
        memory_service: MemoryService,
        chunk_max_chars: int = 2000,
        source_tag: str = "knowledge-load",
    ):
        self._memory = memory_service
        self._chunk_max_chars = chunk_max_chars
        self._source_tag = source_tag
        self._markitdown = MarkItDown() if _HAS_MARKITDOWN else None

    async def load_file(
        self,
        path: str | Path,
        domains: list[str] | None = None,
        source_agent: str = "knowledge-loader",
        project: str | None = None,
        importance: float = 6.0,
    ) -> LoadStats:
        """Load a single file into memory."""
        stats = LoadStats()
        path = Path(path)

        if not path.exists():
            stats.errors.append(f"File not found: {path}")
            return stats

        ext = path.suffix.lower()
        file_domains = domains or [path.stem]

        # ── Rich document formats (markitdown) ────────────────────────
        if ext in self.DOCUMENT_EXTENSIONS:
            if not self._markitdown:
                stats.errors.append(
                    f"Cannot load {ext} file: install document support with "
                    "`pip install ncms[docs]`"
                )
                return stats
            chunks = self._convert_document(path)
        else:
            # ── Text-based formats (stdlib) ───────────────────────────
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                stats.errors.append(f"Failed to read {path}: {e}")
                return stats

            if ext in (".md", ".markdown", ".rst"):
                chunks = self._chunk_markdown(content)
            elif ext == ".json":
                chunks = self._chunk_json(content)
            elif ext == ".csv":
                chunks = self._chunk_csv(content)
            elif ext in (".html", ".htm"):
                chunks = self._chunk_html(content)
            else:
                chunks = self._chunk_plain_text(content)

        stats.files_processed = 1
        stats.chunks_total = len(chunks)

        for chunk in chunks:
            if not chunk.strip():
                continue
            try:
                await self._memory.store_memory(
                    content=chunk,
                    memory_type="fact",
                    domains=file_domains,
                    tags=[self._source_tag, f"source:{path.name}"],
                    source_agent=source_agent,
                    project=project,
                    importance=importance,
                )
                stats.memories_created += 1
            except Exception as e:
                stats.errors.append(f"Failed to store chunk from {path}: {e}")

        logger.info(
            "Loaded %s: %d chunks -> %d memories",
            path.name,
            stats.chunks_total,
            stats.memories_created,
        )
        return stats

    async def load_directory(
        self,
        path: str | Path,
        domains: list[str] | None = None,
        recursive: bool = True,
        **kwargs: Any,
    ) -> LoadStats:
        """Load all supported files from a directory."""
        stats = LoadStats()
        path = Path(path)

        if not path.is_dir():
            stats.errors.append(f"Not a directory: {path}")
            return stats

        pattern = "**/*" if recursive else "*"
        for file_path in sorted(path.glob(pattern)):
            if file_path.is_file() and file_path.suffix.lower() in self.SUPPORTED_EXTENSIONS:
                file_stats = await self.load_file(file_path, domains=domains, **kwargs)
                stats.files_processed += file_stats.files_processed
                stats.memories_created += file_stats.memories_created
                stats.chunks_total += file_stats.chunks_total
                stats.errors.extend(file_stats.errors)

        return stats

    async def load_text(
        self,
        text: str,
        domains: list[str] | None = None,
        source_agent: str = "knowledge-loader",
        project: str | None = None,
        importance: float = 6.0,
        source_name: str = "inline",
    ) -> LoadStats:
        """Load raw text directly into memory."""
        stats = LoadStats()
        chunks = self._chunk_plain_text(text)
        stats.chunks_total = len(chunks)

        for chunk in chunks:
            if not chunk.strip():
                continue
            try:
                await self._memory.store_memory(
                    content=chunk,
                    memory_type="fact",
                    domains=domains or ["general"],
                    tags=[self._source_tag, f"source:{source_name}"],
                    source_agent=source_agent,
                    project=project,
                    importance=importance,
                )
                stats.memories_created += 1
            except Exception as e:
                stats.errors.append(f"Failed to store text chunk: {e}")

        return stats

    # ── Chunking Strategies ──────────────────────────────────────────────

    def _chunk_markdown(self, content: str) -> list[str]:
        """Split markdown by headings, keeping each section as a chunk."""
        chunks: list[str] = []
        current_chunk: list[str] = []

        for line in content.split("\n"):
            # Detect headings (# ## ### etc.)
            if re.match(r"^#{1,4}\s+", line):
                # Save previous chunk
                if current_chunk:
                    text = "\n".join(current_chunk).strip()
                    if text:
                        chunks.append(text)
                current_chunk = [line]
            else:
                current_chunk.append(line)

            # Check size limit
            joined = "\n".join(current_chunk)
            if len(joined) > self._chunk_max_chars:
                chunks.append(joined.strip())
                current_chunk = []

        # Don't forget the last chunk
        if current_chunk:
            text = "\n".join(current_chunk).strip()
            if text:
                chunks.append(text)

        return chunks

    def _chunk_plain_text(self, content: str) -> list[str]:
        """Split plain text by double newlines (paragraphs)."""
        paragraphs = re.split(r"\n\s*\n", content)
        chunks: list[str] = []
        current = ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if len(current) + len(para) > self._chunk_max_chars:
                if current:
                    chunks.append(current)
                current = para
            else:
                current = f"{current}\n\n{para}" if current else para

        if current:
            chunks.append(current)

        return chunks

    def _chunk_json(self, content: str) -> list[str]:
        """Split JSON arrays into individual entries, or treat objects as single chunks."""
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return self._chunk_plain_text(content)

        if isinstance(data, list):
            return [json.dumps(item, indent=2) for item in data if item]
        elif isinstance(data, dict):
            # If it has clear sections, split by top-level keys
            if len(data) > 3:
                return [
                    f"{key}: {json.dumps(value, indent=2)}"
                    for key, value in data.items()
                ]
            return [json.dumps(data, indent=2)]
        return [content]

    def _chunk_csv(self, content: str) -> list[str]:
        """Split CSV into header + row groups."""
        lines = content.strip().split("\n")
        if not lines:
            return []

        header = lines[0]
        chunks: list[str] = []
        batch: list[str] = [header]

        for line in lines[1:]:
            batch.append(line)
            if len("\n".join(batch)) > self._chunk_max_chars:
                chunks.append("\n".join(batch))
                batch = [header]

        if len(batch) > 1:  # More than just the header
            chunks.append("\n".join(batch))

        return chunks

    def _chunk_html(self, content: str) -> list[str]:
        """Basic HTML text extraction - strip tags and chunk."""
        # Simple tag stripping (no external dependency needed)
        text = re.sub(r"<script[^>]*>.*?</script>", "", content, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return self._chunk_plain_text(text)

    def _convert_document(self, path: Path) -> list[str]:
        """Convert a rich document (DOCX/PPTX/PDF/XLSX) to markdown chunks.

        Uses markitdown to convert the document to markdown, then
        applies the standard markdown chunking strategy.
        """
        assert self._markitdown is not None  # guarded by caller
        try:
            result = self._markitdown.convert(str(path))
            markdown = result.text_content
        except Exception as e:
            logger.error("markitdown conversion failed for %s: %s", path.name, e)
            return []

        if not markdown or not markdown.strip():
            return []

        return self._chunk_markdown(markdown)
