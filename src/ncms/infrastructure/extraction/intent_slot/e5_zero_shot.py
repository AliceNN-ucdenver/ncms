"""E5-small-v2 zero-shot intent classifier (NCMS-native port).

Intent is resolved via cosine similarity between the E5-encoded
query and the E5-encoded intent label descriptions from
:data:`ncms.domain.intent_slot_taxonomy.INTENT_LABEL_DESCRIPTIONS`.
No training; hot-path cost is one E5 forward pass on the query
(label embeddings cached on construction).

Slot extraction is deliberately lightweight here — regex anchors
for the habitual / choice cases and a fallback primary-slot
heuristic.  The point of this backend is intent coverage when no
trained adapter is available; callers needing high slot F1 train
a LoRA adapter (see :mod:`ncms.training.intent_slot.train_lora`).

This is an **intent-only learned fallback**; topic, admission,
and state_change stay ``None`` and the heuristic fallback fills
them in downstream.
"""

from __future__ import annotations

import logging
import re
import time

try:
    import numpy as np
    from sentence_transformers import SentenceTransformer
except ImportError as exc:  # pragma: no cover — P2 optional dep
    raise RuntimeError(
        "intent_slot.e5_zero_shot requires sentence-transformers + numpy",
    ) from exc

from ncms.domain.intent_slot_taxonomy import (
    INTENT_LABEL_DESCRIPTIONS,
    SLOT_TAXONOMY,
)
from ncms.domain.models import ExtractedLabel

logger = logging.getLogger(__name__)


_FREQ_PATTERNS = [
    re.compile(r"\b(every\s+\w+(?:\s+\w+)?)\b", re.IGNORECASE),
    re.compile(
        r"\b(always|usually|rarely|often|nightly|daily|weekly)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bon\s+(?:the\s+)?(\w+day(?:s)?)\b", re.IGNORECASE),
]

_CHOICE_PATTERNS = [
    re.compile(
        r"\b(.+?)\s+(?:instead\s+of|rather\s+than|over)\s+(.+?)(?:\.|,|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwent\s+with\s+(.+?)(?:\.|,|\s+instead\s+of\s+(.+?))?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bpicked\s+(.+?)(?:\s+over\s+(.+?))?(?:\.|,|$)",
        re.IGNORECASE,
    ),
]


class E5ZeroShotExtractor:
    """Intent-only zero-shot classifier.  No training required."""

    name = "e5_zero_shot"

    def __init__(
        self,
        *,
        model_name: str = "intfloat/e5-small-v2",
    ) -> None:
        self._model_name = model_name
        self._model = SentenceTransformer(model_name)
        self._label_intents: list[str] = list(INTENT_LABEL_DESCRIPTIONS.keys())
        self._label_embeddings = self._model.encode(
            [INTENT_LABEL_DESCRIPTIONS[i] for i in self._label_intents],
            normalize_embeddings=True,
        )
        logger.info(
            "[intent_slot] E5ZeroShot loaded: model=%s", model_name,
        )

    def extract(self, text: str, *, domain: str) -> ExtractedLabel:
        t0 = time.perf_counter()
        intent, confidence = self._classify_intent(text)
        slots = self._extract_slots(text, domain, intent)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return ExtractedLabel(
            intent=intent,  # type: ignore[arg-type]
            intent_confidence=confidence,
            slots=slots,
            # Topic / admission / state_change left None — the
            # chained extractor's heuristic fallback fills those in.
            method=self.name,
            latency_ms=latency_ms,
        )

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "backend": "E5ZeroShotExtractor",
            "version": "zero_shot",
            "model": self._model_name,
            "intent_labels": list(self._label_intents),
        }

    # ── Intent: cosine against E5-encoded labels ─────────────────

    def _classify_intent(self, text: str) -> tuple[str, float]:
        if not text.strip():
            return "none", 0.0
        query_embedding = self._model.encode(
            [f"query: {text.strip()}"],
            normalize_embeddings=True,
        )
        scores = np.dot(query_embedding, self._label_embeddings.T)[0]
        best_idx = int(np.argmax(scores))
        label = self._label_intents[best_idx]
        return label, float(scores[best_idx])

    # ── Slots: regex anchors + whole-text fallback ──────────────

    def _extract_slots(
        self, text: str, domain: str, intent: str,
    ) -> dict[str, str]:
        slots: dict[str, str] = {}
        if intent == "habitual":
            for pat in _FREQ_PATTERNS:
                m = pat.search(text)
                if m is not None:
                    slots["frequency"] = m.group(0)
                    break
        if intent == "choice":
            for pat in _CHOICE_PATTERNS:
                m = pat.search(text)
                if m is None:
                    continue
                obj = (m.group(1) or "").strip(".,")
                if obj:
                    slots[self._primary_slot(domain)] = obj
                if m.lastindex and m.lastindex >= 2:
                    alt = (m.group(2) or "").strip(".,")
                    if alt:
                        slots["alternative"] = alt
                break
        primary = self._primary_slot(domain)
        if primary not in slots:
            stripped = text.strip().rstrip(".,!?")
            slots[primary] = (
                stripped.split(maxsplit=1)[-1] if " " in stripped
                else stripped
            )
        return slots

    @staticmethod
    def _primary_slot(domain: str) -> str:
        if domain == "conversational":
            return "object"
        if domain == "software_dev":
            return "library"
        if domain == "clinical":
            return "medication"
        # Unknown domain: fallback to "object" which is the
        # conversational catch-all.
        return SLOT_TAXONOMY.get(domain, ("object",))[0]


__all__ = ["E5ZeroShotExtractor"]
