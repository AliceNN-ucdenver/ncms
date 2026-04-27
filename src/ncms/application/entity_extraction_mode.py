"""Entity extraction lane policy.

The SLM adapters and GLiNER now represent two experimental lanes for
building/querying the entity graph.  Keep that policy in one small
application module so ingest, query, document publish, and reindex agree.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

EntityExtractionMode = Literal["slm_only", "gliner_only"]


def entity_extraction_mode(config: object) -> EntityExtractionMode:
    """Return the configured entity extraction lane.

    ``gliner_only`` is the compatibility default for older configs and
    ad-hoc tests that instantiate partial config doubles.
    """

    mode = getattr(config, "entity_extraction_mode", "gliner_only")
    if mode == "slm_only":
        return "slm_only"
    return "gliner_only"


def use_gliner_entities(config: object) -> bool:
    """Whether application paths should call GLiNER."""

    return entity_extraction_mode(config) == "gliner_only"


def use_slm_entities(config: object) -> bool:
    """Whether application paths should promote SLM slot/role spans."""

    return entity_extraction_mode(config) == "slm_only"


def slm_slots_to_entity_dicts(
    label: Any | None,
    *,
    confidence_threshold: float = 0.7,
) -> list[dict[str, Any]]:
    """Convert live SLM slot output into downstream entity dicts."""

    if label is None:
        return []
    slots = getattr(label, "slots", None) or {}
    confidences = getattr(label, "slot_confidences", None) or {}
    out: list[dict[str, Any]] = []
    for slot_name, surface in slots.items():
        if not surface:
            continue
        conf = float(confidences.get(slot_name, 1.0))
        if conf < confidence_threshold:
            continue
        out.append(
            {
                "name": str(surface).strip(),
                "type": str(slot_name),
                "attributes": {
                    "source": "slm_slot",
                    "confidence": round(conf, 3),
                },
            }
        )
    return _dedupe_entities(out)


def structured_slm_entities(structured: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Recover SLM entities from ``memory.structured["intent_slot"]``.

    Reindex does not rerun the adapter; it reads the payload persisted at
    ingest time.  Prefer role spans because they preserve primary vs
    alternative grounding, then fill gaps from the derived ``slots`` dict.
    """

    if not isinstance(structured, Mapping):
        return []
    payload = structured.get("intent_slot")
    if not isinstance(payload, Mapping):
        return []

    out: list[dict[str, Any]] = []
    for raw in payload.get("role_spans") or []:
        if not isinstance(raw, Mapping):
            continue
        role = str(raw.get("role") or "")
        if role not in {"primary", "alternative"}:
            continue
        name = str(raw.get("canonical") or raw.get("surface") or "").strip()
        slot = str(raw.get("slot") or "concept").strip() or "concept"
        if not name:
            continue
        attributes: dict[str, Any] = {
            "source": "slm_role_span",
            "role": role,
        }
        surface = str(raw.get("surface") or "").strip()
        if surface and surface != name:
            attributes["surface"] = surface
        out.append({"name": name, "type": slot, "attributes": attributes})

    slots = payload.get("slots") or {}
    if isinstance(slots, Mapping):
        for slot_name, surface in slots.items():
            name = str(surface or "").strip()
            if not name:
                continue
            out.append(
                {
                    "name": name,
                    "type": str(slot_name),
                    "attributes": {"source": "slm_slot"},
                }
            )

    return _dedupe_entities(out)


def _dedupe_entities(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for entity in entities:
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        entity = dict(entity)
        entity["name"] = name
        out.append(entity)
    return out
