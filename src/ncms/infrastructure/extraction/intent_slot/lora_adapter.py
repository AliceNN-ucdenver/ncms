"""Per-deployment LoRA adapter backend (boundary wrapper).

Wraps the unified LoRA adapter at
:mod:`ncms.application.adapters.methods.joint_bert_lora` — the
single source of truth for training + inference — and converts
its dataclass output to the domain Pydantic
:class:`ncms.domain.models.ExtractedLabel` at the
application↔infrastructure boundary.

History: the codebase previously shipped two parallel
``LoraJointModel`` / ``LoraJointBert`` implementations — one
experiment-side, one production-side.  The production mirror
silently diverged at every head addition (v7 role head, v8 cue
head), breaking inference every time.  The production mirror is
now deleted; this module is the only bridge between the unified
inference class and the :class:`IntentSlotExtractor` protocol.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ncms.application.adapters.methods.joint_bert_lora import (
    AdapterManifest,
    LoraJointBert,
)
from ncms.application.adapters.schemas import (
    ExtractedLabel as _AdapterExtractedLabel,
)
from ncms.domain.models import ExtractedLabel
from ncms.infrastructure.extraction.intent_slot.adapter_loader import (
    verify_adapter_dir,
)

logger = logging.getLogger(__name__)


def _to_domain_label(src: _AdapterExtractedLabel) -> ExtractedLabel:
    """Convert adapter-side dataclass label → domain Pydantic label.

    The adapter-side dataclass carries typed ``RoleSpan`` entries;
    the domain model is Pydantic and uses list-of-dict so it
    crosses the JSON boundary (memory structured column, MCP tool
    responses) without leaking adapter-layer types into the domain.

    ``cue_tags`` on the domain ExtractedLabel defaults to ``[]``
    and is left empty here — the v9 joint adapter produces five
    heads, none of which emit cue tags.  The dedicated CTLG adapter
    uses its own boundary and stores output under
    ``memory.structured["ctlg"]``.
    """
    role_spans = [
        {
            "char_start": rs.char_start,
            "char_end": rs.char_end,
            "surface": rs.surface,
            "canonical": rs.canonical,
            "slot": rs.slot,
            "role": rs.role,
            "source": getattr(rs, "source", ""),
        }
        for rs in (src.role_spans or ())
    ]
    return ExtractedLabel(
        intent=src.intent,  # type: ignore[arg-type]
        intent_confidence=float(src.intent_confidence or 0.0),
        slots=dict(src.slots or {}),
        slot_confidences=dict(src.slot_confidences or {}),
        topic=src.topic,
        topic_confidence=src.topic_confidence,
        admission=src.admission,  # type: ignore[arg-type]
        admission_confidence=src.admission_confidence,
        state_change=src.state_change,  # type: ignore[arg-type]
        state_change_confidence=src.state_change_confidence,
        role_spans=role_spans,
        method=src.method or "joint_bert_lora",
    )


class LoraJointExtractor:
    """Per-deployment LoRA adapter producing the full multi-head output.

    Thin wrapper: verifies the artifact at construction, delegates
    every ``extract()`` call to the unified
    :class:`ncms.application.adapters.methods.joint_bert_lora.
    LoraJointBert`, then converts to the domain boundary type.
    """

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
        # The unified implementation loads the manifest itself from
        # ``adapter_dir/manifest.json`` — we don't need to pass it.
        self._backend = LoraJointBert(
            self._adapter_dir,
            device=device,
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
        # The unified backend returns the adapter-layer dataclass;
        # convert to the domain Pydantic label at the boundary.
        src = self._backend.extract(text, domain=domain)  # type: ignore[arg-type]
        return _to_domain_label(src)

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "backend": "LoraJointExtractor",
            "version": self._manifest.version,
            "domain": self._manifest.domain,
            "encoder": self._manifest.encoder,
            "corpus_hash": self._manifest.corpus_hash,
            "intent_labels": list(self._manifest.intent_labels),
            "role_labels": list(self._manifest.role_labels),
            "topic_labels": list(self._manifest.topic_labels),
            "admission_labels": list(self._manifest.admission_labels),
            "state_change_labels": list(self._manifest.state_change_labels),
        }


__all__ = ["LoraJointExtractor"]
