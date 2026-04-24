"""Intent-slot extraction (P2 ingest-side content understanding).

Production code for the ingest-time classifier that unifies
admission scoring, state-change detection, topic labelling, and
preference extraction under one LoRA-adapter multi-head model.

Protocol contract lives at :class:`ncms.domain.protocols.
IntentSlotExtractor`; output shape at :class:`ncms.domain.models.
ExtractedLabel`; domain vocabularies at
:mod:`ncms.domain.intent_slot_taxonomy`.

Build the runtime extractor via :func:`build_extractor_chain`.
The chain is intentionally GLiNER-free — GLiNER's slot extraction
was strictly worse than the LoRA BIO head on every trained domain
(see ``docs/intent-slot-sprints-1-3.md`` §9.5).  GLiNER remains
in NCMS under ``infrastructure/extraction/gliner_extractor.py``
for entity extraction (a separate pipeline).
"""

from ncms.infrastructure.extraction.intent_slot.adapter_loader import (
    AdapterIntegrityError,
    AdapterManifest,
    load_adapter_manifest,
    verify_adapter_dir,
)
from ncms.infrastructure.extraction.intent_slot.e5_zero_shot import (
    E5ZeroShotExtractor,
)
from ncms.infrastructure.extraction.intent_slot.factory import (
    ChainedExtractor,
    build_extractor_chain,
)
from ncms.infrastructure.extraction.intent_slot.heuristic_fallback import (
    HeuristicFallbackExtractor,
)
from ncms.infrastructure.extraction.intent_slot.lora_adapter import (
    LoraJointExtractor,
)

__all__ = [
    "AdapterIntegrityError",
    "AdapterManifest",
    "ChainedExtractor",
    "E5ZeroShotExtractor",
    "HeuristicFallbackExtractor",
    "LoraJointExtractor",
    "build_extractor_chain",
    "load_adapter_manifest",
    "verify_adapter_dir",
]
