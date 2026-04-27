"""CTLG boundary helpers.

This module keeps CTLG cue-tagging separate from the 5-head intent-slot
adapter.  The future CTLG adapter owns cue tags; intent-slot owns
content classification.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, NamedTuple, cast

from ncms.domain.tlg.cue_taxonomy import CueLabel, TaggedToken


class CTLGExtraction(NamedTuple):
    """Cue-tag extraction result plus wall-clock timing."""

    tokens: list[TaggedToken]
    latency_ms: float


def cue_tags_to_payload(tokens: list[TaggedToken]) -> list[dict[str, Any]]:
    """Serialise CTLG cue tags for ``memory.structured["ctlg"]``."""
    return [
        {
            "char_start": tok.char_start,
            "char_end": tok.char_end,
            "surface": tok.surface,
            "cue_label": tok.cue_label,
            "confidence": tok.confidence,
        }
        for tok in tokens
    ]


def payload_to_tagged_tokens(payload: Any) -> list[TaggedToken]:
    """Coerce a JSON/list boundary payload back to ``TaggedToken`` values."""
    if not payload:
        return []
    tokens: list[TaggedToken] = []
    for item in payload:
        try:
            tokens.append(
                TaggedToken(
                    char_start=int(item["char_start"]),
                    char_end=int(item["char_end"]),
                    surface=str(item["surface"]),
                    cue_label=cast(CueLabel, str(item["cue_label"])),
                    confidence=float(item.get("confidence", 1.0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tokens


def bake_ctlg_payload(
    *,
    structured: dict | None,
    cue_tags: list[TaggedToken],
    method: str,
    latency_ms: float,
    voice: str,
) -> dict:
    """Attach CTLG output under its own structured payload key."""
    result = dict(structured or {})
    result["ctlg"] = {
        "schema_version": 1,
        "method": method,
        "voice": voice,
        "latency_ms": latency_ms,
        "cue_tags": cue_tags_to_payload(cue_tags),
    }
    return result


async def extract_ctlg_cues(
    cue_tagger: Any | None,
    text: str,
    *,
    domain: str,
) -> CTLGExtraction:
    """Run a dedicated CTLG cue tagger if one is wired.

    The protocol is intentionally narrow: CTLG implementations expose
    ``extract_cues(text, domain=...)`` and return either ``TaggedToken``
    instances or dicts with the same fields.
    """
    if cue_tagger is None:
        return CTLGExtraction([], 0.0)
    extract = getattr(cue_tagger, "extract_cues", None)
    if extract is None:
        return CTLGExtraction([], 0.0)

    t0 = time.perf_counter()
    if inspect.iscoroutinefunction(extract):
        raw = await extract(text, domain=domain)
    else:
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, lambda: extract(text, domain=domain))
    latency_ms = (time.perf_counter() - t0) * 1000

    if isinstance(raw, CTLGExtraction):
        return raw
    raw_items = list(raw or [])
    tokens = [
        tok
        for tok in raw_items
        if isinstance(tok, TaggedToken)
    ]
    if len(tokens) == len(raw_items):
        return CTLGExtraction(tokens, latency_ms)
    return CTLGExtraction(payload_to_tagged_tokens(raw_items), latency_ms)
