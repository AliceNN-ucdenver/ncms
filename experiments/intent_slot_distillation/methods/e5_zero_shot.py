"""Tier 1 method — E5-small-v2 zero-shot intent + naïve slot.

Intent is resolved via cosine-similarity between the E5-encoded
user text and the E5-encoded descriptive label prompts from
:data:`INTENT_LABEL_DESCRIPTIONS`.  No training; hot-path cost is
two E5 forward passes (one for the query, one for the labels —
the label embeddings are cached after the first call).

Slot extraction is deliberately naïve: we regex-scan for slot
anchors (``"every morning"``, ``"instead of X"``, etc.) and
extract the tokenized tail.  This is a weak baseline by design —
it shows how much value we get from E5 alone vs. a proper slot
tagger (the GLiNER+E5 baseline, or Joint BERT).

No state is mutated after construction; the class is safe to
call concurrently if the underlying sentence-transformers model
is thread-safe (which it is).
"""

from __future__ import annotations

import re

try:
    import numpy as np
    from sentence_transformers import SentenceTransformer
except ImportError as exc:  # pragma: no cover — experiment-only dep
    raise RuntimeError(
        "E5ZeroShot requires sentence-transformers + numpy"
    ) from exc

from experiments.intent_slot_distillation.methods.base import (
    IntentSlotExtractor,
)
from experiments.intent_slot_distillation.schemas import (
    INTENT_CATEGORIES,
    INTENT_LABEL_DESCRIPTIONS,
    SLOT_TAXONOMY,
    Domain,
    ExtractedLabel,
    Intent,
)


# Simple frequency anchors for the habitual slot.
_FREQ_PATTERNS = [
    re.compile(r"\b(every\s+\w+(?:\s+\w+)?)\b", re.IGNORECASE),
    re.compile(r"\b(always|usually|rarely|often|nightly|daily|weekly)\b",
               re.IGNORECASE),
    re.compile(r"\bon\s+(?:the\s+)?(\w+day(?:s)?)\b", re.IGNORECASE),
]


# Choice anchors — "X instead of Y", "went with X over Y".
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


class E5ZeroShot(IntentSlotExtractor):
    """E5-based zero-shot intent classifier + naïve slot scanner."""

    name = "e5_zero_shot"

    def __init__(
        self, model_name: str = "intfloat/e5-small-v2",
    ) -> None:
        self._model = SentenceTransformer(model_name)
        # Pre-encode intent label descriptions once — they don't change.
        self._label_intents: list[Intent] = list(INTENT_LABEL_DESCRIPTIONS.keys())
        label_texts = [
            INTENT_LABEL_DESCRIPTIONS[intent]
            for intent in self._label_intents
        ]
        self._label_embeddings = self._model.encode(
            label_texts, normalize_embeddings=True,
        )

    # ── IntentSlotExtractor ─────────────────────────────────────────

    def extract(
        self, text: str, *, domain: Domain,
    ) -> ExtractedLabel:
        intent, confidence = self._classify_intent(text)
        slots = self._extract_slots(text, domain, intent)
        return ExtractedLabel(
            intent=intent,
            intent_confidence=confidence,
            slots=slots,
            method=self.name,
        )

    # ── Intent: cosine against E5-encoded labels ────────────────────

    def _classify_intent(self, text: str) -> tuple[Intent, float]:
        if not text.strip():
            return "none", 0.0
        query_embedding = self._model.encode(
            [f"query: {text.strip()}"],
            normalize_embeddings=True,
        )
        # Cosine = dot product of normalized vectors.
        scores = np.dot(query_embedding, self._label_embeddings.T)[0]
        best_idx = int(np.argmax(scores))
        return self._label_intents[best_idx], float(scores[best_idx])

    # ── Slots: regex anchors + whole-text fallback ──────────────────

    def _extract_slots(
        self,
        text: str,
        domain: Domain,
        intent: Intent,
    ) -> dict[str, str]:
        slots: dict[str, str] = {}
        # Frequency — only meaningful for habitual intent.
        if intent == "habitual":
            for pat in _FREQ_PATTERNS:
                m = pat.search(text)
                if m is not None:
                    slots["frequency"] = m.group(0)
                    break
        # Choice anchors.
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
        # Fallback primary slot — full first noun phrase after the
        # verb / copula.  Cheap heuristic: everything after the first
        # verb-ish token, trimmed.  Intended as a *baseline*.
        primary_slot = self._primary_slot(domain)
        if primary_slot not in slots:
            stripped = text.strip().rstrip(".,!?")
            slots[primary_slot] = stripped.split(maxsplit=1)[-1] if " " in stripped else stripped
        return slots

    @staticmethod
    def _primary_slot(domain: Domain) -> str:
        # Pick the most prototypical slot per domain.
        if domain == "conversational":
            return "object"
        if domain == "software_dev":
            return "library"
        if domain == "clinical":
            return "medication"
        # Fallback: first slot in the taxonomy.
        return SLOT_TAXONOMY[domain][0]


# Keep the symbol list tight — prevents accidental leakage of
# helpers into the evaluation harness.
__all__ = ["E5ZeroShot"]
