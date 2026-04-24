"""Adapter artifact loader + manifest validation.

The adapter artifact is produced by
:func:`ncms.application.adapters.methods.joint_bert_lora.train`
and has a stable on-disk layout::

    adapter_dir/
    ├── lora_adapter/       ← peft save_pretrained dir
    ├── heads.safetensors   ← LoRA heads (intent/role/topic/
    │                          admission/state_change/shape_cue;
    │                          legacy slot_head / shape_intent_head
    │                          present on pre-v8 adapters)
    ├── manifest.json       ← encoder, labels, lora config, metrics
    ├── taxonomy.yaml       ← human-readable label snapshot
    └── eval_report.md      ← gate report (optional)

This module re-exports the single authoritative
:class:`AdapterManifest` dataclass from the adapter training
module — there is no infrastructure-side duplicate.  The loader
adds forward-compat key filtering so ``verify_adapter_dir``
tolerates older / newer manifest schemas without failing the
service boot.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from ncms.application.adapters.methods.joint_bert_lora import (
    AdapterManifest,
)

logger = logging.getLogger(__name__)


class AdapterIntegrityError(ValueError):
    """Raised when an adapter directory fails structural validation."""


def load_adapter_manifest(adapter_dir: Path) -> AdapterManifest:
    """Parse ``adapter_dir/manifest.json``.

    Raises :class:`AdapterIntegrityError` when the file is missing
    or malformed.  Unknown keys in the JSON are silently dropped so
    the loader stays forward-compatible with newer manifests from
    training runs that add fields.
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

    allowed = set(AdapterManifest.__dataclass_fields__.keys())
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
