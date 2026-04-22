"""Factory + confidence-gated fallback-chain extractor.

Clean architecture: the intent-slot fallback chain has **three
tiers**, one per realistic deployment mode.  The v4 adapters hit
F1 = 1.000 on gold across all five heads, so most deployments
only ever hit the primary.  The other tiers exist for
cold-start and minimal-dependency installs:

    primary:    LoraJointExtractor       ← trained-adapter deployment
    fallback 1: E5ZeroShotExtractor      ← cold-start (no corpus yet)
    fallback 2: HeuristicFallbackExtractor ← minimal-deps (no torch)

GLiNER is **not** in this chain — it runs in parallel for entity
extraction into the knowledge graph (a different pipeline).  A
"generic adapter" tier was considered and rejected: it would
have required training + shipping a broad adapter, which is P3
work, and the cold-start E5 path already handles the "no corpus
yet" case well enough.

The chain preserves the zero-confidently-wrong invariant:
abstain is always an option (heuristic is always appended).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ncms.domain.models import ExtractedLabel
from ncms.domain.protocols import IntentSlotExtractor
from ncms.infrastructure.extraction.intent_slot.heuristic_fallback import (
    HeuristicFallbackExtractor,
)

logger = logging.getLogger(__name__)


class ChainedExtractor:
    """Confidence-gated fallback chain.

    Runs each backend; for every head, keeps the first confident
    output.  When every backend abstains on a head, the heuristic
    null-output (always appended last) wins.
    """

    name = "chained"

    def __init__(
        self,
        backends: list[IntentSlotExtractor],
        *,
        confidence_threshold: float = 0.7,
    ) -> None:
        if not backends:
            raise ValueError("ChainedExtractor requires at least one backend")
        self._backends = backends
        self._threshold = confidence_threshold
        logger.info(
            "[intent_slot] chain order: %s  threshold=%.2f",
            [b.name for b in backends], confidence_threshold,
        )

    @property
    def adapter_domain(self) -> str | None:
        """Return the primary backend's adapter domain when available.

        Convenience accessor for callers that need to know "which
        adapter did this chain load?" — e.g. to pass the right
        ``domain`` arg to :meth:`extract` without having to thread
        it through from the construction site.  Returns ``None``
        when the primary backend has no manifest (heuristic-only
        chains).
        """
        primary = self._backends[0]
        manifest = getattr(primary, "_manifest", None) or getattr(
            primary, "manifest", None,
        )
        if manifest is not None:
            return getattr(manifest, "domain", None)
        return None

    def extract(self, text: str, *, domain: str) -> ExtractedLabel:
        """Run backends in order, merge confident heads.

        The primary backend's ``method`` string is kept in the
        final output for observability so the dashboard can show
        which backend "owned" the extraction even when some heads
        came from fallbacks.
        """
        total_latency_ms = 0.0
        merged = ExtractedLabel(method="", latency_ms=0.0)
        chain_notes: list[str] = []

        # Walk backends in reverse so earlier (more-trusted)
        # backends overwrite later ones — we want the primary's
        # confident fields to win.
        for backend in reversed(self._backends):
            try:
                label = backend.extract(text, domain=domain)
            except Exception as exc:
                logger.warning(
                    "[intent_slot] backend %s raised on domain=%s: %s",
                    backend.name, domain, exc,
                )
                chain_notes.append(f"{backend.name}:error")
                continue
            total_latency_ms += label.latency_ms

            # The final backend in the chain is the heuristic null-
            # output — always accept its values as the floor, then
            # overwrite with confident predictions from earlier
            # backends in subsequent iterations.
            is_last_backend = backend is self._backends[-1]

            if (label.intent_confidence >= self._threshold
                    or is_last_backend):
                merged.intent = label.intent
                merged.intent_confidence = label.intent_confidence
            if label.slots:
                merged.slots = label.slots
                merged.slot_confidences = label.slot_confidences
            if label.topic is not None and (
                label.topic_confidence is None
                or label.topic_confidence >= self._threshold
            ):
                merged.topic = label.topic
                merged.topic_confidence = label.topic_confidence
            if label.admission is not None and (
                label.admission_confidence is None
                or label.admission_confidence >= self._threshold
                or is_last_backend
            ):
                merged.admission = label.admission
                merged.admission_confidence = label.admission_confidence
            if label.state_change is not None and (
                label.state_change_confidence is None
                or label.state_change_confidence >= self._threshold
                or is_last_backend
            ):
                merged.state_change = label.state_change
                merged.state_change_confidence = label.state_change_confidence
            chain_notes.append(f"{backend.name}:ok")

        merged.method = self._backends[0].name
        merged.latency_ms = total_latency_ms

        logger.debug(
            "[intent_slot] chain extract: method=%s intent=%s "
            "topic=%s admission=%s state_change=%s chain=%s "
            "latency_ms=%.1f",
            merged.method, merged.intent, merged.topic,
            merged.admission, merged.state_change,
            ",".join(reversed(chain_notes)), total_latency_ms,
        )
        return merged

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "backend": "ChainedExtractor",
            "version": "v1",
            "confidence_threshold": self._threshold,
            "chain": [b.describe() for b in self._backends],
        }

    @property
    def primary(self) -> IntentSlotExtractor:
        """The first (highest-priority) backend in the chain."""
        return self._backends[0]

    @property
    def backends(self) -> list[IntentSlotExtractor]:
        return list(self._backends)


def build_extractor_chain(
    *,
    checkpoint_dir: Path | str | None = None,
    confidence_threshold: float = 0.7,
    device: str | None = None,
    include_e5_fallback: bool = True,
) -> IntentSlotExtractor:
    """Build a three-tier chained extractor from config.

    1. **Primary** — per-deployment LoRA adapter at
       ``checkpoint_dir``.  Skipped when the path is ``None`` or
       the artifact fails verification.  This is the hot path in
       a trained deployment (F1 = 1.000 on gold for v4 adapters).

    2. **E5 zero-shot** — intent-only learned fallback for
       cold-start deployments with no trained corpus yet.
       Disable via ``include_e5_fallback=False`` for
       minimal-dependency installs that also lack torch.

    3. **Heuristic** — always appended so ``extract()`` never
       raises or returns an empty label.  Populates
       ``admission="persist"`` + ``intent="none"``; every other
       head stays ``None``.
    """
    chain: list[IntentSlotExtractor] = []

    if checkpoint_dir is not None:
        adapter = _try_load_adapter(Path(checkpoint_dir), device=device)
        if adapter is not None:
            chain.append(adapter)

    if include_e5_fallback:
        try:
            from ncms.infrastructure.extraction.intent_slot.e5_zero_shot import (
                E5ZeroShotExtractor,
            )
            chain.append(E5ZeroShotExtractor())
        except Exception as exc:
            logger.warning(
                "[intent_slot] E5 fallback unavailable: %s", exc,
            )

    chain.append(HeuristicFallbackExtractor())
    return ChainedExtractor(chain, confidence_threshold=confidence_threshold)


def _try_load_adapter(
    adapter_dir: Path,
    *,
    device: str | None = None,
) -> IntentSlotExtractor | None:
    """Verify + load a LoRA adapter, returning None on failure.

    Loud logs on any failure so ops know when a configured
    adapter has been silently skipped in favour of the fallback
    chain.
    """
    try:
        from ncms.infrastructure.extraction.intent_slot.adapter_loader import (
            verify_adapter_dir,
        )
        from ncms.infrastructure.extraction.intent_slot.lora_adapter import (
            LoraJointExtractor,
        )

        manifest = verify_adapter_dir(adapter_dir)
        return LoraJointExtractor(
            adapter_dir, manifest=manifest, device=device,
        )
    except Exception as exc:
        logger.warning(
            "[intent_slot] adapter load failed at %s; skipping: %s",
            adapter_dir, exc,
        )
        return None


__all__ = ["ChainedExtractor", "build_extractor_chain"]
