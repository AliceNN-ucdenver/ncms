"""Adapter artifact loader + manifest validation.

The adapter artifact is produced by
``experiments/intent_slot_distillation/train_adapter.py`` and has
a stable on-disk layout::

    adapter_dir/
    ├── lora_adapter/       ← peft save_pretrained dir
    ├── heads.safetensors   ← 5 classification heads
    ├── manifest.json       ← encoder, labels, lora config, metrics
    ├── taxonomy.yaml       ← human-readable label snapshot
    └── eval_report.md      ← gate report (optional)

This module does NOT import torch / transformers at module load —
only the `lora_adapter.py` inference class does.  That keeps
`IntentSlotExtractor` factory imports cheap when the LoRA backend
isn't in the chain (e.g. zero-shot-only deployments).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class AdapterManifest:
    """Parsed ``manifest.json`` for a trained adapter.

    Mirrors the experiment-side dataclass but lives in NCMS's
    infrastructure layer so production code doesn't depend on the
    experiment package being importable.
    """

    encoder: str = "bert-base-uncased"
    domain: str = ""
    version: str = "v1"
    max_length: int = 128

    intent_labels: list[str] = field(default_factory=list)
    slot_labels: list[str] = field(default_factory=list)
    topic_labels: list[str] = field(default_factory=list)
    admission_labels: list[str] = field(default_factory=list)
    state_change_labels: list[str] = field(default_factory=list)
    # 6th head — query-shape intent.  Empty list means the adapter
    # predates the v6 schema and does not ship this head; callers
    # must gracefully treat it as "abstain".
    shape_intent_labels: list[str] = field(default_factory=list)

    lora_r: int = 8
    lora_alpha: int = 16
    lora_dropout: float = 0.05
    lora_target_modules: list[str] = field(
        default_factory=lambda: ["query", "value"],
    )

    trained_on: dict[str, int] = field(default_factory=dict)
    gate_metrics: dict[str, float] = field(default_factory=dict)
    trained_at: str = ""
    corpus_hash: str = ""


class AdapterIntegrityError(ValueError):
    """Raised when an adapter directory fails structural validation."""


def load_adapter_manifest(adapter_dir: Path) -> AdapterManifest:
    """Parse ``adapter_dir/manifest.json``.

    Raises :class:`AdapterIntegrityError` when the file is missing
    or malformed.  Unknown keys in the JSON are silently dropped
    so the loader stays forward-compatible with newer manifests
    from the experiment side.
    """
    manifest_path = adapter_dir / "manifest.json"
    if not manifest_path.is_file():
        raise AdapterIntegrityError(
            f"no manifest.json at {manifest_path}",
        )
    try:
        raw = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise AdapterIntegrityError(
            f"malformed manifest.json at {manifest_path}: {exc}",
        ) from exc

    allowed = {f.name for f in AdapterManifest.__dataclass_fields__.values()}
    filtered = {k: v for k, v in raw.items() if k in allowed}
    unknown = set(raw) - allowed
    if unknown:
        logger.debug(
            "[intent_slot] ignoring unknown manifest keys: %s",
            sorted(unknown),
        )
    return AdapterManifest(**filtered)


def verify_adapter_dir(adapter_dir: Path) -> AdapterManifest:
    """Validate the full adapter artifact and return its manifest.

    Checks:

    1. ``manifest.json`` exists + parses.
    2. ``heads.safetensors`` exists + is non-empty.
    3. ``lora_adapter/`` directory exists + contains
       ``adapter_config.json``.

    Raises :class:`AdapterIntegrityError` on any failure.  The
    ingest-time factory calls this once at service startup so a
    broken adapter fails loud rather than silently emitting bad
    labels.
    """
    if not adapter_dir.is_dir():
        raise AdapterIntegrityError(
            f"adapter_dir does not exist: {adapter_dir}",
        )

    manifest = load_adapter_manifest(adapter_dir)

    heads_path = adapter_dir / "heads.safetensors"
    if not heads_path.is_file() or heads_path.stat().st_size == 0:
        raise AdapterIntegrityError(
            f"missing or empty heads.safetensors at {heads_path}",
        )

    lora_dir = adapter_dir / "lora_adapter"
    if not lora_dir.is_dir():
        raise AdapterIntegrityError(
            f"missing lora_adapter/ directory at {lora_dir}",
        )
    if not (lora_dir / "adapter_config.json").is_file():
        raise AdapterIntegrityError(
            f"missing lora_adapter/adapter_config.json at {lora_dir}",
        )

    # Log a short hash of manifest.json for audit-trail purposes —
    # an operator can cross-reference this against the corpus_hash
    # in the registry row to detect drift between artifact and DB.
    manifest_bytes = (adapter_dir / "manifest.json").read_bytes()
    short_hash = hashlib.sha256(manifest_bytes).hexdigest()[:12]
    logger.info(
        "[intent_slot] adapter verified: %s domain=%s version=%s "
        "encoder=%s manifest_hash=%s corpus_hash=%s",
        adapter_dir, manifest.domain, manifest.version,
        manifest.encoder, short_hash, manifest.corpus_hash,
    )
    return manifest


__all__ = [
    "AdapterIntegrityError",
    "AdapterManifest",
    "load_adapter_manifest",
    "verify_adapter_dir",
]
