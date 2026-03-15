"""GLiNER-based entity extraction for semantically-aware NER.

Uses the GLiNER zero-shot NER model to extract entities with custom labels.
Model is lazy-loaded and cached for reuse across calls.

Text longer than ~1,200 characters is automatically chunked at sentence
boundaries so that each chunk fits within GLiNER's DeBERTa 384-token window.
Chunks are batch-processed via ``model.inference()`` for throughput.
Entities are deduplicated across chunks by lowercase name.

On Apple Silicon, the model is automatically placed on MPS (Metal GPU) for
accelerated inference.  Falls back to CPU if MPS is unavailable.

Reference: Zaratiana et al. "GLiNER: Generalist Model for Named Entity
Recognition using Bidirectional Transformer" (NAACL 2024)
"""

from __future__ import annotations

import logging
import threading

from ncms.domain.entity_extraction import MAX_ENTITIES, UNIVERSAL_LABELS
from ncms.infrastructure.text.chunking import chunk_text

logger = logging.getLogger(__name__)

# Module-level model cache — loaded once, reused across calls
_model: object | None = None
_model_name: str | None = None
_model_lock = threading.Lock()  # Serializes model load + inference (PyTorch not thread-safe)

# GLiNER's DeBERTa backbone has a 384-token window.  At ~4 chars/token the
# safe character budget is ~1,200 chars, leaving headroom for special tokens
# and label encoding.
_CHUNK_MAX_CHARS: int = 1200
_CHUNK_OVERLAP: int = 100


def _resolve_device() -> str:
    """Pick the best available device: MPS > CUDA > CPU.

    Override with ``NCMS_GLINER_DEVICE=cpu|mps|cuda`` env var.
    """
    import os

    override = os.environ.get("NCMS_GLINER_DEVICE", "").strip().lower()
    if override in ("cpu", "mps", "cuda"):
        return override

    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _get_model(model_name: str, cache_dir: str | None = None) -> object:
    """Lazy-load and cache the GLiNER model.

    Automatically places the model on MPS (Apple Silicon GPU) or CUDA
    if available, falling back to CPU.

    Args:
        model_name: HuggingFace model identifier.
        cache_dir: Directory for downloaded model files.
                   Falls back to HuggingFace default (~/.cache/huggingface/hub).

    Raises ImportError if gliner is not installed.
    """
    global _model, _model_name  # noqa: PLW0603

    with _model_lock:
        if _model is not None and _model_name == model_name:
            return _model

        from gliner import GLiNER  # type: ignore[import-untyped]

        device = _resolve_device()
        logger.info(
            "Loading GLiNER model: %s on %s (first call only)", model_name, device,
        )
        kwargs: dict[str, object] = {"map_location": device}
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
    chunk fits within the model's 384-token window.  All chunks are
    batch-processed in a single ``model.inference()`` call for throughput.
    Entities are deduplicated across chunks by lowercase name (first
    occurrence wins).

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
    chunks = chunk_text(text, max_chars=_CHUNK_MAX_CHARS, overlap=_CHUNK_OVERLAP)

    # Batch inference: process all chunks in one call instead of a per-chunk loop.
    # model.inference() returns List[List[Dict]] — one entity list per chunk.
    # Lock serializes GPU access (PyTorch models are not thread-safe).
    with _model_lock:
        all_chunk_entities = model.inference(  # type: ignore[union-attr]
            chunks,
            extraction_labels,
            threshold=threshold,
            flat_ner=True,
        )

    # Dedup by lowercase name across all chunks, first occurrence wins
    seen: set[str] = set()
    entities: list[dict[str, str]] = []

    for chunk_entities in all_chunk_entities:
        for ent in chunk_entities:
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
