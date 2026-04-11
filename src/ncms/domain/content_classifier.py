"""Content classification for incoming memories.

Classifies content as ATOMIC (no internal structure) or NAVIGABLE
(has sections/headings) using pure heuristics — no LLM, no infrastructure deps.

This module lives in the domain layer and has zero infrastructure dependencies.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum


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
_MD_HEADING_RE = re.compile(r"^#{1,4}\s+(.+)", re.MULTILINE)


def classify_content(
    content: str,
    memory_type: str | None = None,
) -> ContentClassification:
    """Classify content using heuristics (no LLM).

    Detection order:
    1. Explicit memory_type override (document, document_chunk, section_index)
    2. Markdown headings (2+ headings -> NAVIGABLE/markdown)
    3. JSON object with 2+ top-level keys -> NAVIGABLE/json
    4. YAML with 2+ top-level keys -> NAVIGABLE/yaml
    5. Long + structured text (>1000 chars, 3+ double-newline breaks)
    6. Default: ATOMIC/plain
    """
    # ── 1. Explicit type override ──────────────────────────────────────
    if memory_type in ("document_chunk", "document", "section_index"):
        # Still detect format for extraction
        heading_count = len(_MD_HEADING_RE.findall(content))
        if heading_count >= 2:
            return ContentClassification(
                ContentClass.NAVIGABLE, "markdown", heading_count,
            )
        return ContentClassification(ContentClass.NAVIGABLE, "structured_text", 0)

    # ── 2. Markdown headings ──────────────────────────────────────────
    heading_count = len(_MD_HEADING_RE.findall(content))
    if heading_count >= 2:
        return ContentClassification(
            ContentClass.NAVIGABLE, "markdown", heading_count,
        )

    # ── 3. JSON object ────────────────────────────────────────────────
    stripped = content.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict) and len(obj) >= 2:
                return ContentClassification(
                    ContentClass.NAVIGABLE, "json", len(obj),
                )
        except (json.JSONDecodeError, ValueError):
            pass

    # ── 4. YAML detection ─────────────────────────────────────────────
    # Filter to actual top-level keys: only lines at column 0
    top_level_keys: list[str] = []
    for line in content.split("\n"):
        m = re.match(r"^(\w[\w\-]*):\s*", line)
        if m and not line.startswith(" ") and not line.startswith("\t"):
            top_level_keys.append(m.group(1))
    if len(top_level_keys) >= 2:
        return ContentClassification(
            ContentClass.NAVIGABLE, "yaml", len(top_level_keys),
        )

    # ── 5. Long + structured text ─────────────────────────────────────
    if len(content) > 1000 and content.count("\n\n") >= 3:
        # Count paragraph-like sections separated by double newlines
        sections = [s.strip() for s in re.split(r"\n\s*\n", content) if s.strip()]
        if len(sections) >= 3:
            return ContentClassification(
                ContentClass.NAVIGABLE, "structured_text", len(sections),
            )

    # ── 6. Default: ATOMIC ────────────────────────────────────────────
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
        heading_match = re.match(r"^#{1,4}\s+(.+)", line)
        if heading_match:
            # Save previous section
            if current_lines:
                text = "\n".join(current_lines).strip()
                if text:
                    sections.append(Section(
                        heading=current_heading or "(preamble)",
                        text=text,
                        index=len(sections),
                    ))
            current_heading = heading_match.group(1).strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    # Last section
    if current_lines:
        text = "\n".join(current_lines).strip()
        if text:
            sections.append(Section(
                heading=current_heading or "(preamble)",
                text=text,
                index=len(sections),
            ))

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
        sections.append(Section(
            heading=str(key),
            text=f"{key}: {json.dumps(value, indent=2)}",
            index=i,
        ))
    return sections


def _extract_yaml_sections(content: str) -> list[Section]:
    """Split YAML content by top-level keys."""
    sections: list[Section] = []
    current_key = ""
    current_lines: list[str] = []

    for line in content.split("\n"):
        # Detect top-level key (no leading whitespace)
        m = re.match(r"^(\w[\w\-]*):\s*(.*)", line)
        if m and not line.startswith(" ") and not line.startswith("\t"):
            # Save previous section
            if current_key and current_lines:
                text = "\n".join(current_lines).strip()
                if text:
                    sections.append(Section(
                        heading=current_key,
                        text=text,
                        index=len(sections),
                    ))
            current_key = m.group(1)
            current_lines = [line]
        else:
            current_lines.append(line)

    # Last section
    if current_key and current_lines:
        text = "\n".join(current_lines).strip()
        if text:
            sections.append(Section(
                heading=current_key,
                text=text,
                index=len(sections),
            ))

    return sections


def _extract_structured_text_sections(content: str) -> list[Section]:
    """Split structured text by double-newline paragraph boundaries."""
    paragraphs = [s.strip() for s in re.split(r"\n\s*\n", content) if s.strip()]
    sections: list[Section] = []
    for i, para in enumerate(paragraphs):
        # Use the first line (up to 80 chars) as the heading
        first_line = para.split("\n")[0][:80].strip()
        sections.append(Section(
            heading=first_line or f"Section {i + 1}",
            text=para,
            index=i,
        ))
    return sections
