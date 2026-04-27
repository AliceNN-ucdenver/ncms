from __future__ import annotations

import builtins
from typing import Any

import pytest

from ncms.application.entity_extraction_mode import (
    entity_extraction_mode,
    slm_slots_to_entity_dicts,
    structured_slm_entities,
    use_gliner_entities,
    use_slm_entities,
)
from ncms.application.retrieval.pipeline import RetrievalPipeline
from ncms.config import NCMSConfig
from ncms.domain.models import ExtractedLabel


class _PartialConfig:
    pass


class _Store:
    async def get_consolidation_value(self, key: str) -> str | None:
        return None


class _Index:
    def search(self, query: str, limit: int) -> list[tuple[str, float]]:
        return [("m1", 2.0)]


class _Graph:
    pass


def test_mode_defaults_to_gliner_for_partial_config() -> None:
    cfg = _PartialConfig()

    assert entity_extraction_mode(cfg) == "gliner_only"
    assert use_gliner_entities(cfg) is True
    assert use_slm_entities(cfg) is False


def test_live_slm_slots_convert_to_entity_dicts() -> None:
    label = ExtractedLabel(
        slots={"framework": "FastAPI", "tool": "uv"},
        slot_confidences={"framework": 0.91, "tool": 0.2},
    )

    entities = slm_slots_to_entity_dicts(label, confidence_threshold=0.3)

    assert entities == [
        {
            "name": "FastAPI",
            "type": "framework",
            "attributes": {"source": "slm_slot", "confidence": 0.91},
        }
    ]


def test_structured_slm_entities_prefers_role_spans_and_dedupes_slots() -> None:
    structured: dict[str, Any] = {
        "intent_slot": {
            "role_spans": [
                {
                    "surface": "fast api",
                    "canonical": "FastAPI",
                    "slot": "framework",
                    "role": "primary",
                },
                {
                    "surface": "logging",
                    "canonical": "logging",
                    "slot": "concept",
                    "role": "casual",
                },
            ],
            "slots": {"framework": "FastAPI", "package": "Pydantic"},
        }
    }

    entities = structured_slm_entities(structured)

    assert entities == [
        {
            "name": "FastAPI",
            "type": "framework",
            "attributes": {
                "source": "slm_role_span",
                "role": "primary",
                "surface": "fast api",
            },
        },
        {
            "name": "Pydantic",
            "type": "package",
            "attributes": {"source": "slm_slot"},
        },
    ]


@pytest.mark.asyncio
async def test_retrieval_slm_only_does_not_import_gliner(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if "gliner_extractor" in name:
            raise AssertionError("GLiNER should not be imported in slm_only mode")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    cfg = NCMSConfig(entity_extraction_mode="slm_only", splade_enabled=False)
    pipeline = RetrievalPipeline(
        store=_Store(),  # type: ignore[arg-type]
        index=_Index(),  # type: ignore[arg-type]
        graph=_Graph(),  # type: ignore[arg-type]
        config=cfg,
        splade=None,
    )

    result = await pipeline.retrieve_candidates(
        "What uses FastAPI?",
        domain="software_dev",
        emit_stage=lambda *args, **kwargs: None,
    )

    assert result is not None
    assert result[5] == []
