"""GLiNER-based entity extraction for semantically-aware NER.

Uses the GLiNER zero-shot NER model to extract entities with custom labels.
Model is lazy-loaded and cached for reuse across calls.

Text longer than ~1,200 characters is automatically chunked at sentence
boundaries so that each chunk fits within GLiNER's DeBERTa 384-token window.
Entities are deduplicated across chunks by lowercase name.

Reference: Zaratiana et al. "GLiNER: Generalist Model for Named Entity
Recognition using Bidirectional Transformer" (NAACL 2024)
"""

from __future__ import annotations

import logging

from ncms.domain.entity_extraction import MAX_ENTITIES, UNIVERSAL_LABELS

logger = logging.getLogger(__name__)

# Module-level model cache — loaded once, reused across calls
_model: object | None = None
_model_name: str | None = None

# GLiNER's DeBERTa backbone has a 384-token window.  At ~4 chars/token the
# safe character budget is ~1,200 chars, leaving headroom for special tokens
# and label encoding.
_CHUNK_MAX_CHARS: int = 1200
_CHUNK_OVERLAP: int = 100

# Sentence-ending delimiters ordered by preference (longest context first)
_SENTENCE_SEPS: tuple[str, ...] = (". ", ".\n", "? ", "! ", "\n\n", "\n")


def _chunk_text(
    text: str,
    max_chars: int = _CHUNK_MAX_CHARS,
    overlap: int = _CHUNK_OVERLAP,
) -> list[str]:
    """Split *text* into chunks that fit GLiNER's 384-token window.

    Splitting happens at sentence boundaries when possible, falling back to
    word boundaries and finally hard character cuts.  An *overlap* of
    characters between consecutive chunks ensures entities near boundaries
    are not missed.
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
        for sep in _SENTENCE_SEPS:
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


def _get_model(model_name: str, cache_dir: str | None = None) -> object:
    """Lazy-load and cache the GLiNER model.

    Args:
        model_name: HuggingFace model identifier.
        cache_dir: Directory for downloaded model files.
                   Falls back to HuggingFace default (~/.cache/huggingface/hub).

    Raises ImportError if gliner is not installed.
    """
    global _model, _model_name  # noqa: PLW0603

    if _model is not None and _model_name == model_name:
        return _model

    from gliner import GLiNER  # type: ignore[import-untyped]

    logger.info("Loading GLiNER model: %s (first call only)", model_name)
    kwargs: dict[str, object] = {}
    if cache_dir:
        kwargs["cache_dir"] = cache_dir
    _model = GLiNER.from_pretrained(model_name, **kwargs)
    _model_name = model_name
    return _model


def extract_entities_gliner(
    text: str,
    model_name: str = "urchade/gliner_medium-v2.1",
    threshold: float = 0.3,
    labels: list[str] | None = None,
    cache_dir: str | None = None,
) -> list[dict[str, str]]:
    """Extract entities from text using GLiNER zero-shot NER.

    Long texts are automatically chunked at sentence boundaries so each
    chunk fits within the model's 384-token window.  Entities are
    deduplicated across chunks by lowercase name (first occurrence wins).

    Returns a list of dicts with ``name`` and ``type`` keys.

    Args:
        text: Input text to extract entities from.
        model_name: HuggingFace model identifier for GLiNER.
        threshold: Minimum confidence score (0.0-1.0) for entity inclusion.
        labels: Entity type labels for zero-shot extraction.
                Defaults to UNIVERSAL_LABELS.
        cache_dir: Directory for downloaded model files.

    Raises:
        ImportError: If gliner package is not installed.
    """
    if not text or len(text) < 2:
        return []

    model = _get_model(model_name, cache_dir=cache_dir)
    extraction_labels = labels or UNIVERSAL_LABELS

    # Chunk long text so each piece fits GLiNER's 384-token window
    chunks = _chunk_text(text)

    # Dedup by lowercase name across all chunks, first occurrence wins
    seen: set[str] = set()
    entities: list[dict[str, str]] = []

    for chunk in chunks:
        # GLiNER predict_entities returns list of dicts:
        # [{"text": "...", "label": "...", "score": float, "start": int, "end": int}]
        raw_entities = model.predict_entities(  # type: ignore[union-attr]
            chunk, extraction_labels, threshold=threshold,
        )
        for ent in raw_entities:
            name = ent["text"].strip()
            if not name or len(name) < 2:
                continue
            key = name.lower()
            if key not in seen:
                seen.add(key)
                entities.append({"name": name, "type": ent["label"]})

    if len(chunks) > 1:
        logger.debug(
            "GLiNER chunked %d chars into %d chunks, extracted %d entities",
            len(text), len(chunks), len(entities),
        )

    return entities[:MAX_ENTITIES]
