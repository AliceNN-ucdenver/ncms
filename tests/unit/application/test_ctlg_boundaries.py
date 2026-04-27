"""Unit tests for CTLG's boundary with the five-head content SLM."""

from __future__ import annotations

import pytest

from ncms.application.ctlg import bake_ctlg_payload, extract_ctlg_cues
from ncms.application.ingestion.causal_edges import _parse_cue_tags
from ncms.application.ingestion.store_helpers import bake_intent_slot_payload
from ncms.domain.models import ExtractedLabel, Memory
from ncms.domain.tlg.cue_taxonomy import TaggedToken


def _cue_dict(label: str = "B-CAUSAL_EXPLICIT") -> dict:
    return {
        "char_start": 0,
        "char_end": 7,
        "surface": "because",
        "cue_label": label,
        "confidence": 0.92,
    }


def test_intent_slot_payload_does_not_own_ctlg_cue_tags() -> None:
    label = ExtractedLabel(
        intent="none",
        intent_confidence=0.0,
        cue_tags=[_cue_dict()],
        method="test",
    )

    payload = bake_intent_slot_payload(intent_slot_label=label, structured=None)

    assert "cue_tags" not in payload["intent_slot"]
    assert "ctlg" not in payload


def test_ctlg_payload_is_separate_from_intent_slot() -> None:
    token = TaggedToken(
        char_start=0,
        char_end=7,
        surface="because",
        cue_label="B-CAUSAL_EXPLICIT",
        confidence=0.92,
    )

    payload = bake_ctlg_payload(
        structured={"intent_slot": {"intent": "none"}},
        cue_tags=[token],
        method="stub_ctlg",
        latency_ms=1.5,
        voice="memory",
    )

    assert payload["intent_slot"] == {"intent": "none"}
    assert payload["ctlg"]["method"] == "stub_ctlg"
    assert payload["ctlg"]["voice"] == "memory"
    assert payload["ctlg"]["cue_tags"][0]["cue_label"] == "B-CAUSAL_EXPLICIT"


def test_causal_edge_reader_prefers_ctlg_payload() -> None:
    memory = Memory(
        content="x",
        structured={
            "ctlg": {"cue_tags": [_cue_dict("B-CAUSAL_EXPLICIT")]},
            "intent_slot": {"cue_tags": [_cue_dict("B-TEMPORAL_BEFORE")]},
        },
    )

    tokens = _parse_cue_tags(memory)

    assert len(tokens) == 1
    assert tokens[0].cue_label == "B-CAUSAL_EXPLICIT"


def test_causal_edge_reader_keeps_legacy_fallback() -> None:
    memory = Memory(
        content="x",
        structured={"intent_slot": {"cue_tags": [_cue_dict("B-CAUSAL_EXPLICIT")]}},
    )

    tokens = _parse_cue_tags(memory)

    assert len(tokens) == 1
    assert tokens[0].cue_label == "B-CAUSAL_EXPLICIT"


@pytest.mark.asyncio
async def test_extract_ctlg_cues_uses_dedicated_protocol() -> None:
    class StubCueTagger:
        name = "stub_ctlg"

        def extract_cues(self, text: str, *, domain: str):
            return [_cue_dict()]

    result = await extract_ctlg_cues(StubCueTagger(), "why did it happen?", domain="software_dev")

    assert len(result.tokens) == 1
    assert result.tokens[0].surface == "because"
    assert result.latency_ms >= 0.0
