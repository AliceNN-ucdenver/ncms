"""Sentence-boundary text chunking for model token windows.

Shared by GLiNER (384-token DeBERTa, ~1200 chars) and SPLADE (128-token
transformer, ~400 chars).  Each caller specifies ``max_chars`` and ``overlap``
for their model's context window.
"""

from __future__ import annotations

# Sentence-ending delimiters ordered by preference (longest context first)
SENTENCE_SEPS: tuple[str, ...] = (". ", ".\n", "? ", "! ", "\n\n", "\n")


def chunk_text(
    text: str,
    max_chars: int,
    overlap: int = 100,
) -> list[str]:
    """Split *text* into chunks at sentence boundaries.

    Splitting happens at sentence boundaries when possible, falling back to
    word boundaries and finally hard character cuts.  An *overlap* of
    characters between consecutive chunks ensures entities or terms near
    boundaries are not missed.

    Args:
        text: Input text to chunk.
        max_chars: Maximum characters per chunk.
        overlap: Character overlap between consecutive chunks.

    Returns:
        List of text chunks.  Single-element list if text fits in one chunk.
    """
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + max_chars
        if end >= len(text):
            chunks.append(text[start:])
            break

        # Find last sentence boundary before max_chars
        boundary = -1
        for sep in SENTENCE_SEPS:
            pos = text.rfind(sep, start, end)
            if pos > boundary:
                boundary = pos + len(sep)

        if boundary <= start:
            # No sentence boundary — fall back to word boundary
            boundary = text.rfind(" ", start, end)
            if boundary <= start:
                boundary = end  # Last resort: hard cut

        chunks.append(text[start:boundary])
        start = max(start + 1, boundary - overlap)

    return chunks
