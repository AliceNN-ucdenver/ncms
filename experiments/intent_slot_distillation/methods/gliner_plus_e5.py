"""Baseline method — GLiNER (slots) + E5 (intent) two-pass.

Combines the two models NCMS already ships.  GLiNER extracts
entity slots using the domain's slot taxonomy as a runtime label
list (zero-shot; no training).  E5 handles intent via the same
label-description similarity path as :mod:`.e5_zero_shot`.

This is the *current* stack with the regex families replaced.
It sits between Tier 1 (pure E5, naïve slots) and Tier 2
(Joint BERT, one forward pass) — better slots than E5-only,
same intent quality, two forward passes instead of one.
"""

from __future__ import annotations

from ncms.application.adapters.methods.base import (
    IntentSlotExtractor,
)
from experiments.intent_slot_distillation.methods.e5_zero_shot import (
    E5ZeroShot,
)
from ncms.application.adapters.schemas import (
    SLOT_TAXONOMY,
    Domain,
    ExtractedLabel,
)

try:
    from gliner import GLiNER
except ImportError as exc:  # pragma: no cover — experiment-only dep
    raise RuntimeError(
        "gliner_plus_e5 requires the `gliner` package"
    ) from exc


class GlinerPlusE5(IntentSlotExtractor):
    """GLiNER slots + E5 intent.

    GLiNER labels are the domain's slot taxonomy.  Inline
    ``object`` label is added for the conversational domain since
    the starter gold uses it as a catch-all.
    """

    name = "gliner_plus_e5"

    def __init__(
        self,
        gliner_model: str = "urchade/gliner_medium-v2.1",
        e5_model: str = "intfloat/e5-small-v2",
        threshold: float = 0.35,
    ) -> None:
        self._gliner = GLiNER.from_pretrained(gliner_model)
        self._e5 = E5ZeroShot(model_name=e5_model)
        self._threshold = threshold

    def extract(
        self, text: str, *, domain: Domain,
    ) -> ExtractedLabel:
        # Intent via E5.
        intent, intent_conf = self._e5._classify_intent(text)
        # Slots via GLiNER using the domain taxonomy.
        labels = list(SLOT_TAXONOMY[domain]) + ["object"]
        entities = self._gliner.predict_entities(
            text, labels, threshold=self._threshold,
        )
        slots: dict[str, str] = {}
        slot_confidences: dict[str, float] = {}
        for entity in entities:
            label = str(entity.get("label", ""))
            surface = str(entity.get("text", "")).strip()
            score = float(entity.get("score", 0.0))
            if not label or not surface:
                continue
            # First match per slot wins.
            if label not in slots:
                slots[label] = surface
                slot_confidences[label] = score
        return ExtractedLabel(
            intent=intent,
            intent_confidence=intent_conf,
            slots=slots,
            slot_confidences=slot_confidences,
            method=self.name,
        )


__all__ = ["GlinerPlusE5"]
