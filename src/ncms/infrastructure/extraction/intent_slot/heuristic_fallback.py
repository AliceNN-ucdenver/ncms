"""Heuristic zero-dependency fallback extractor.

Always available — no torch, no transformers, no GLiNER.  Emits
``intent="none"``, ``admission="persist"``, ``state_change="none"``,
empty slots/topic for every input.  This is the end of the
fallback chain: when every learned backend is unavailable or has
abstained, the ingest pipeline uses this so ``store_memory`` keeps
working.

Matches the pre-P2 default behaviour (every memory gets persisted,
no topic auto-tag, no state-change detection).  Kept deliberately
dumb — NCMS must ship something that works on a cold install
without any model downloads.
"""

from __future__ import annotations

from ncms.domain.models import ExtractedLabel


class HeuristicFallbackExtractor:
    """Null-output extractor.  Protocol-compatible, zero dependencies."""

    name = "heuristic_fallback"

    def extract(self, text: str, *, domain: str) -> ExtractedLabel:
        # Every memory persists by default; preference is "none";
        # no topic claim; no state change detected.  Downstream
        # code should treat topic=None as "unclassified".
        return ExtractedLabel(
            intent="none",
            intent_confidence=0.0,
            slots={},
            topic=None,
            topic_confidence=None,
            admission="persist",
            admission_confidence=0.0,
            state_change="none",
            state_change_confidence=0.0,
            method=self.name,
            latency_ms=0.0,
        )

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "backend": "HeuristicFallbackExtractor",
            "version": "null",
            "deps": "stdlib-only",
        }


__all__ = ["HeuristicFallbackExtractor"]
