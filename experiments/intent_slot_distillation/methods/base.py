"""Shared protocol for the three method candidates.

Every candidate implements :class:`IntentSlotExtractor`.  The
evaluation harness swaps implementations behind this protocol so
method comparisons are apples-to-apples.  When the experiment
converges, the *same protocol* becomes the NCMS-side entry point
(in ``src/ncms/domain/protocols.py``).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from experiments.intent_slot_distillation.schemas import (
    Domain,
    ExtractedLabel,
)


@runtime_checkable
class IntentSlotExtractor(Protocol):
    """Contract every candidate method fulfils.

    Implementations may hold heavyweight state (models, tokenizers)
    in ``__init__``; ``extract`` itself must be synchronous and
    side-effect-free.
    """

    name: str                   # human-readable method tag (logs, matrix)

    def extract(
        self, text: str, *, domain: Domain,
    ) -> ExtractedLabel:
        """Return the predicted ``(intent, slots, confidences)``.

        The domain is passed at call time (not init) because one
        method instance may serve multiple domains — E5 zero-shot
        is domain-agnostic; joint BERT has domain-specific
        checkpoints that the caller selects via ``domain``.
        """
