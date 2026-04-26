"""Content classification for incoming memories.

Classifies content as ATOMIC (no internal structure) or NAVIGABLE
(has sections/headings) using pure heuristics — no LLM, no infrastructure deps.

This module lives in the domain layer and has zero infrastructure dependencies.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class ContentClass(StrEnum):
    """Top-level content classification."""

    ATOMIC = "atomic"
    NAVIGABLE = "navigable"


@dataclass(frozen=True)
class ContentClassification:
    """Result of classifying a piece of content."""

    content_class: ContentClass
    format_hint: str  # "markdown", "json", "yaml", "structured_text", "plain"
    section_count: int  # 0 for atomic


@dataclass(frozen=True)
class Section:
    """A single section extracted from navigable content."""

    heading: str
    text: str
    index: int


# ── Heading regex (matches Markdown H1-H4) ────────────────────────────────
# Requires exactly one space after the '#' block. This avoids matching YAML/code
# comment lines like "#   - id: CTL-001" or "#     category: authentication"
# which have 3+ spaces after '#' and aren't headings.
_MD_HEADING_RE = re.compile(r"^#{1,4} (?! )(.+)", re.MULTILINE)


# Minimum content length for NAVIGABLE classification.  Content shorter than
# this is stored as a single ATOMIC memory — it's already small enough to be
# fully searchable and splitting it produces useless fragments.
_MIN_NAVIGABLE_CHARS = 500


# Map file extensions to format hints.  The agent/caller knows the filename —
# using it directly is more reliable than regex-guessing content structure.
_EXT_TO_FORMAT: dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".txt": "plain",
    ".rst": "structured_text",
}


def classify_content(
    content: str,
    memory_type: str | None = None,
    source_format: str | None = None,
) -> ContentClassification:
    """Classify content for section splitting.

    When ``source_format`` is provided (filename or extension like ".md",
    "markdown", "security-controls.yaml"), that is the authoritative signal
    and heuristics are skipped.  This is the preferred path — callers know
    the file extension; guessing from content is fragile.

    Detection order:
    1. source_format — authoritative when provided
    2. Explicit memory_type override (document, document_chunk, section_index)
    3. Size gate: content < 500 chars -> ATOMIC
    4. Heuristic fallback (YAML keys, markdown headings, JSON, structured text)
    5. Default: ATOMIC/plain
    """
    # ── 1. source_format — authoritative signal ───────────────────────
    if source_format:
        fmt = _resolve_format(source_format)
        if fmt != "plain":
            section_count = _count_sections_for_format(content, fmt)
            if section_count >= 2 and len(content) >= _MIN_NAVIGABLE_CHARS:
                logger.info(
                    "[classifier] source_format=%r -> %s, NAVIGABLE (%d sections, %d chars)",
                    source_format,
                    fmt,
                    section_count,
                    len(content),
                )
                return ContentClassification(ContentClass.NAVIGABLE, fmt, section_count)
            logger.info(
                "[classifier] source_format=%r -> %s, ATOMIC (sections=%d, chars=%d, "
                "need ≥2 sections and ≥%d chars)",
                source_format,
                fmt,
                section_count,
                len(content),
                _MIN_NAVIGABLE_CHARS,
            )
        else:
            logger.warning(
                "[classifier] source_format=%r resolved to 'plain' — "
                "unrecognized format, falling through to ATOMIC",
                source_format,
            )
        return ContentClassification(ContentClass.ATOMIC, fmt, 0)

    # No source_format provided — log and fall through to heuristics
    logger.debug(
        "[classifier] No source_format provided (%d chars), using heuristic fallback",
        len(content),
    )

    # ── 2. Explicit memory_type override ──────────────────────────────
    if memory_type in ("document_chunk", "document", "section_index"):
        heading_count = len(_MD_HEADING_RE.findall(content))
        if heading_count >= 2:
            return ContentClassification(
                ContentClass.NAVIGABLE,
                "markdown",
                heading_count,
            )
        return ContentClassification(ContentClass.NAVIGABLE, "structured_text", 0)

    # ── 3. Size gate: small content is always ATOMIC ──────────────────
    if len(content) < _MIN_NAVIGABLE_CHARS:
        return ContentClassification(ContentClass.ATOMIC, "plain", 0)

    # ── 4. Heuristic fallback ─────────────────────────────────────────
    return _classify_by_heuristic(content)


def _resolve_format(source_format: str) -> str:
    """Resolve a source_format hint to a canonical format string.

    Accepts filenames ("security-controls.yaml"), extensions (".md"),
    or direct format names ("markdown", "json", "yaml").
    """
    sf = source_format.strip().lower()

    # Direct format name
    if sf in ("markdown", "json", "yaml", "plain", "structured_text"):
        return sf

    # Direct extension lookup (handles ".md", ".yaml", ".json" etc.)
    if sf in _EXT_TO_FORMAT:
        return _EXT_TO_FORMAT[sf]

    # Extract extension from filename (handles "security-controls.yaml")
    import os

    _, ext = os.path.splitext(sf)
    if ext and ext in _EXT_TO_FORMAT:
        return _EXT_TO_FORMAT[ext]

    # Bare extension without dot (handles "md", "yaml")
    dotted = f".{sf}"
    if dotted in _EXT_TO_FORMAT:
        return _EXT_TO_FORMAT[dotted]

    return "plain"


def _count_sections_for_format(content: str, fmt: str) -> int:
    """Count expected sections for a given format (without extracting them)."""
    if fmt == "markdown":
        return len(_MD_HEADING_RE.findall(content))
    elif fmt == "json":
        stripped = content.strip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    return len(obj)
            except (json.JSONDecodeError, ValueError):
                pass
        return 0
    elif fmt == "yaml":
        count = 0
        for line in content.split("\n"):
            m = re.match(r"^([a-z][\w\-]*):\s*", line)
            if m and not line.startswith(" ") and not line.startswith("\t"):
                count += 1
        return count
    elif fmt == "structured_text":
        sections = [s.strip() for s in re.split(r"\n\s*\n", content) if s.strip()]
        return len(sections)
    return 0


def _classify_by_heuristic(content: str) -> ContentClassification:
    """Heuristic fallback when no source_format is provided.

    This is the legacy path — kept for backward compatibility with callers
    that don't pass source_format (e.g. MCP standalone mode, tests).
    """
    # YAML detection (before markdown to avoid misclassifying YAML doc comments)
    top_level_keys: list[str] = []
    for line in content.split("\n"):
        m = re.match(r"^([a-z][\w\-]*):\s*", line)
        if m and not line.startswith(" ") and not line.startswith("\t"):
            top_level_keys.append(m.group(1))
    if len(top_level_keys) >= 2:
        result = ContentClassification(ContentClass.NAVIGABLE, "yaml", len(top_level_keys))
        logger.info(
            "[classifier] Heuristic: detected YAML (%d top-level keys: %s), %d chars",
            len(top_level_keys),
            ", ".join(top_level_keys[:5]),
            len(content),
        )
        return result

    # Markdown headings
    heading_count = len(_MD_HEADING_RE.findall(content))

    # Headings + YAML keys → likely YAML with doc comments, not markdown
    if heading_count >= 2 and top_level_keys:
        logger.info(
            "[classifier] Heuristic: %d headings + %d YAML keys — "
            "treating as YAML-with-comments, ATOMIC (%d chars)",
            heading_count,
            len(top_level_keys),
            len(content),
        )
        return ContentClassification(ContentClass.ATOMIC, "plain", 0)

    if heading_count >= 2:
        result = ContentClassification(ContentClass.NAVIGABLE, "markdown", heading_count)
        logger.info(
            "[classifier] Heuristic: detected markdown (%d headings), NAVIGABLE (%d chars)",
            heading_count,
            len(content),
        )
        return result

    # JSON object
    stripped = content.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict) and len(obj) >= 2:
                result = ContentClassification(ContentClass.NAVIGABLE, "json", len(obj))
                logger.info(
                    "[classifier] Heuristic: detected JSON (%d keys), NAVIGABLE (%d chars)",
                    len(obj),
                    len(content),
                )
                return result
        except (json.JSONDecodeError, ValueError):
            pass

    # Long + structured text
    if len(content) > 1000 and content.count("\n\n") >= 3:
        sections = [s.strip() for s in re.split(r"\n\s*\n", content) if s.strip()]
        if len(sections) >= 3:
            result = ContentClassification(ContentClass.NAVIGABLE, "structured_text", len(sections))
            logger.info(
                "[classifier] Heuristic: detected structured text (%d paragraphs), "
                "NAVIGABLE (%d chars)",
                len(sections),
                len(content),
            )
            return result

    logger.info(
        "[classifier] Heuristic: no structure detected, ATOMIC (%d chars)",
        len(content),
    )
    return ContentClassification(ContentClass.ATOMIC, "plain", 0)


def extract_sections(
    content: str,
    classification: ContentClassification,
) -> list[Section]:
    """Extract sections from navigable content.

    Uses format-specific splitting strategies that mirror the chunking logic
    in knowledge_loader.py (_chunk_markdown, _chunk_json).
    """
    if classification.content_class == ContentClass.ATOMIC:
        return []

    fmt = classification.format_hint
    if fmt == "markdown":
        return _extract_markdown_sections(content)
    elif fmt == "json":
        return _extract_json_sections(content)
    elif fmt == "yaml":
        return _extract_yaml_sections(content)
    elif fmt == "structured_text":
        return _extract_structured_text_sections(content)

    return []


def _extract_markdown_sections(content: str) -> list[Section]:
    """Split markdown by headings, mirroring knowledge_loader._chunk_markdown."""
    sections: list[Section] = []
    current_heading = ""
    current_lines: list[str] = []

    for line in content.split("\n"):
        heading_match = re.match(r"^#{1,4} (?! )(.+)", line)
        if heading_match:
            # Save previous section
            if current_lines:
                text = "\n".join(current_lines).strip()
                if text:
                    sections.append(
                        Section(
                            heading=current_heading or "(preamble)",
                            text=text,
                            index=len(sections),
                        )
                    )
            current_heading = heading_match.group(1).strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    # Last section
    if current_lines:
        text = "\n".join(current_lines).strip()
        if text:
            sections.append(
                Section(
                    heading=current_heading or "(preamble)",
                    text=text,
                    index=len(sections),
                )
            )

    return sections


def _extract_json_sections(content: str) -> list[Section]:
    """Split JSON objects by top-level keys, mirroring knowledge_loader._chunk_json."""
    try:
        data = json.loads(content.strip())
    except (json.JSONDecodeError, ValueError):
        return []

    if not isinstance(data, dict):
        return []

    sections: list[Section] = []
    for i, (key, value) in enumerate(data.items()):
        sections.append(
            Section(
                heading=str(key),
                text=f"{key}: {json.dumps(value, indent=2)}",
                index=i,
            )
        )
    return sections


def _extract_yaml_sections(content: str) -> list[Section]:
    """Split YAML content by top-level keys."""
    sections: list[Section] = []
    current_key = ""
    current_lines: list[str] = []

    for line in content.split("\n"):
        # Detect top-level key (no leading whitespace)
        m = re.match(r"^([a-z][\w\-]*):\s*(.*)", line)
        if m and not line.startswith(" ") and not line.startswith("\t"):
            # Save previous section
            if current_key and current_lines:
                text = "\n".join(current_lines).strip()
                if text:
                    sections.append(
                        Section(
                            heading=current_key,
                            text=text,
                            index=len(sections),
                        )
                    )
            current_key = m.group(1)
            current_lines = [line]
        else:
            current_lines.append(line)

    # Last section
    if current_key and current_lines:
        text = "\n".join(current_lines).strip()
        if text:
            sections.append(
                Section(
                    heading=current_key,
                    text=text,
                    index=len(sections),
                )
            )

    return sections


def _extract_structured_text_sections(content: str) -> list[Section]:
    """Split structured text by double-newline paragraph boundaries."""
    paragraphs = [s.strip() for s in re.split(r"\n\s*\n", content) if s.strip()]
    sections: list[Section] = []
    for i, para in enumerate(paragraphs):
        # Use the first line (up to 80 chars) as the heading
        first_line = para.split("\n")[0][:80].strip()
        sections.append(
            Section(
                heading=first_line or f"Section {i + 1}",
                text=para,
                index=i,
            )
        )
    return sections
