"""Per-deployment LoRA adapter backend.

Wraps :class:`ncms.infrastructure.extraction.intent_slot.lora_model.
LoraJointBert` with the :class:`IntentSlotExtractor` protocol
surface.  Verifies the adapter artifact on construction via
:func:`verify_adapter_dir` so a broken adapter fails loud at
service startup rather than silently degrading ingest quality.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ncms.domain.models import ExtractedLabel
from ncms.infrastructure.extraction.intent_slot.adapter_loader import (
    AdapterManifest,
    verify_adapter_dir,
)
from ncms.infrastructure.extraction.intent_slot.lora_model import (
    LoraJointBert,
)

logger = logging.getLogger(__name__)


class LoraJointExtractor:
    """Per-deployment LoRA adapter producing all 5 heads."""

    name = "joint_bert_lora"

    def __init__(
        self,
        adapter_dir: Path,
        *,
        manifest: AdapterManifest | None = None,
        device: str | None = None,
    ) -> None:
        self._adapter_dir = Path(adapter_dir)
        self._manifest = manifest or verify_adapter_dir(self._adapter_dir)
        self._backend = LoraJointBert(
            self._adapter_dir, self._manifest, device=device,
        )
        logger.info(
            "[intent_slot] loaded LoRA adapter: %s domain=%s version=%s",
            self._adapter_dir,
            self._manifest.domain,
            self._manifest.version,
        )

    @property
    def manifest(self) -> AdapterManifest:
        return self._manifest

    def extract(self, text: str, *, domain: str) -> ExtractedLabel:
        return self._backend.extract(text, domain=domain)

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "backend": "LoraJointExtractor",
            "version": self._manifest.version,
            "domain": self._manifest.domain,
            "encoder": self._manifest.encoder,
            "corpus_hash": self._manifest.corpus_hash,
            "intent_labels": list(self._manifest.intent_labels),
            "slot_labels": list(self._manifest.slot_labels),
            "topic_labels": list(self._manifest.topic_labels),
            "admission_labels": list(self._manifest.admission_labels),
            "state_change_labels": list(self._manifest.state_change_labels),
        }


__all__ = ["LoraJointExtractor"]
