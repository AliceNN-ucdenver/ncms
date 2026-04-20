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
import re
import threading

from ncms.domain.entity_extraction import (
    MAX_ENTITIES,
    TEMPORAL_LABELS,
    UNIVERSAL_LABELS,
)
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

# Post-extraction quality filter: reject noise entities from structured content
_ENTITY_REJECT_PATTERNS = [
    re.compile(r"^\d+(\.\d+)?%?$"),        # Pure numeric: "85%", "25789"
    re.compile(r"^\d+ \w+\(s\)$"),          # Count patterns: "1 item(s)"
    re.compile(r"^\d+ chars$"),             # Size patterns: "2783 chars"
    re.compile(r"^[a-f0-9]{8,}$"),          # Hex IDs: "6f01603fe96a"
    re.compile(r"^Document: "),             # Prefixed IDs
    re.compile(r"^[A-Z]\d+$"),              # Citation labels: "S5", "S6"
    re.compile(r"^avg \d"),                 # Aggregate labels: "avg 85%"
]


def _is_junk_entity(name: str) -> bool:
    """Return True if the entity name matches a known noise pattern."""
    if len(name) <= 1:
        return True
    return any(p.search(name) for p in _ENTITY_REJECT_PATTERNS)


def _resolve_device() -> str:
    """Delegate to :func:`ncms.infrastructure.hardware.resolve_device`.

    Honours ``NCMS_GLINER_DEVICE`` then falls back to ``NCMS_DEVICE``
    then to the CUDA > MPS > CPU auto-detect path.  Kept as a thin
    wrapper so historical import sites continue to work.
    """
    from ncms.infrastructure.hardware import resolve_device
    return resolve_device("NCMS_GLINER_DEVICE")


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
            logger.debug("[GLiNER] Model already loaded, skipping init")
            return _model

        import time as _time

        from gliner import GLiNER  # type: ignore[import-untyped]

        device = _resolve_device()
        logger.info(
            "[GLiNER] Loading model: %s on %s (first call only)", model_name, device,
        )
        kwargs: dict[str, object] = {}
        if cache_dir:
            kwargs["cache_dir"] = cache_dir

        t0 = _time.perf_counter()
        if device == "mps":
            # GLiNER's from_pretrained with map_location="mps" fails on
            # meta tensors. Load on CPU first, then move to MPS.
            kwargs["map_location"] = "cpu"
            _model = GLiNER.from_pretrained(model_name, **kwargs)
            try:
                import torch
                _model.model = _model.model.to(torch.device("mps"))  # type: ignore[union-attr,attr-defined]
            except Exception:
                logger.warning("Failed to move GLiNER to MPS, using CPU")
        else:
            kwargs["map_location"] = device
            _model = GLiNER.from_pretrained(model_name, **kwargs)

        load_ms = (_time.perf_counter() - t0) * 1000
        logger.info("[GLiNER] Model loaded on %s (%.0fms)", device, load_ms)

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
        all_chunk_entities = model.inference(  # type: ignore[attr-defined]
            chunks,
            extraction_labels,
            threshold=threshold,
            flat_ner=True,
        )

    # Dedup by (lowercase name, label) across all chunks, first occurrence wins.
    # Deduping by name+label (not just name) lets the same token surface as
    # both an entity and a temporal span when GLiNER returns it twice —
    # essential for downstream temporal normalization.
    seen: set[tuple[str, str]] = set()
    entities: list[dict[str, str | int]] = []

    for chunk_entities in all_chunk_entities:
        for ent in chunk_entities:
            name = ent["text"].strip()
            if not name or len(name) < 2:
                continue
            if _is_junk_entity(name):
                continue
            label = ent["label"]
            key = (name.lower(), label)
            if key in seen:
                continue
            seen.add(key)
            entities.append({
                "name": name,
                "type": label,
                "char_start": int(ent.get("start", 0)),
                "char_end": int(ent.get("end", 0)),
            })

    if len(chunks) > 1:
        logger.debug(
            "GLiNER chunked %d chars into %d chunks, extracted %d entities",
            len(text), len(chunks), len(entities),
        )

    return entities[:MAX_ENTITIES]


# Label-count threshold above which we split entity labels and temporal
# labels into separate GLiNER calls.  Derived from the Phase A ablation
# (docs/p1-experiment-diary.md 2026-04-18 entry): 17 labels saw
# p95 = 3589 ms on combined but only 1280 ms split, while labels ≤ 10
# showed no tail issue and the extra call-setup cost dominates.
LABEL_BUDGET_PER_CALL = 10


def extract_with_label_budget(
    text: str,
    labels: list[str],
    *,
    model_name: str = "urchade/gliner_medium-v2.1",
    threshold: float = 0.3,
    cache_dir: str | None = None,
    max_labels_per_call: int = LABEL_BUDGET_PER_CALL,
) -> list[dict[str, object]]:
    """Extract entities, splitting into two GLiNER calls when the label
    count exceeds the tail-latency threshold.

    Behaviour:

    * ``len(labels) <= max_labels_per_call`` — single call, identical
      to ``extract_entities_gliner``.
    * otherwise — two serial calls: temporal labels (whichever
      ``TEMPORAL_LABELS`` appear in ``labels``) in one call, everything
      else in the other.  Results are concatenated; downstream
      deduplication is the caller's responsibility.

    The split boundary is chosen to isolate the semantically-orthogonal
    dimension (temporal vs entity) rather than to balance call sizes.
    Temporal extraction is the feature-flagged addition, so it's the
    natural axis along which to split.  If either side alone still
    exceeds ``max_labels_per_call``, the per-call cost is already
    unavoidable and splitting further would only add overhead.

    Rationale is documented in ``docs/p1-experiment-diary.md``
    (Phase A ablation) and ``docs/retired/p1-temporal-experiment.md`` §17.4.
    """
    if len(labels) <= max_labels_per_call:
        return extract_entities_gliner(
            text, labels=labels,
            model_name=model_name, threshold=threshold,
            cache_dir=cache_dir,
        )
    temporal_set = {t.lower() for t in TEMPORAL_LABELS}
    temporal = [label for label in labels if label.lower() in temporal_set]
    entity = [label for label in labels if label.lower() not in temporal_set]
    if not entity or not temporal:
        # Degenerate: all labels are on one side.  Splitting would do
        # nothing — just run the single combined call.
        return extract_entities_gliner(
            text, labels=labels,
            model_name=model_name, threshold=threshold,
            cache_dir=cache_dir,
        )
    entity_out = extract_entities_gliner(
        text, labels=entity,
        model_name=model_name, threshold=threshold,
        cache_dir=cache_dir,
    )
    temporal_out = extract_entities_gliner(
        text, labels=temporal,
        model_name=model_name, threshold=threshold,
        cache_dir=cache_dir,
    )
    # Concatenate; caller's splitter deduplicates by (text, label).
    merged = list(entity_out) + list(temporal_out)
    return merged[:MAX_ENTITIES]
