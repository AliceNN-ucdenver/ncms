"""GLiNER-based entity extraction for semantically-aware NER.

Uses the GLiNER zero-shot NER model to extract entities with custom labels.
Model is lazy-loaded and cached for reuse across calls.

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

    # GLiNER predict_entities returns list of dicts:
    # [{"text": "...", "label": "...", "score": float, "start": int, "end": int}]
    raw_entities = model.predict_entities(text, extraction_labels, threshold=threshold)  # type: ignore[union-attr]

    # Dedup by lowercase name, preserving first occurrence (highest confidence)
    seen: set[str] = set()
    entities: list[dict[str, str]] = []

    for ent in raw_entities:
        name = ent["text"].strip()
        if not name or len(name) < 2:
            continue
        key = name.lower()
        if key not in seen:
            seen.add(key)
            entities.append({"name": name, "type": ent["label"]})

    return entities[:MAX_ENTITIES]
