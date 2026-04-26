"""Entity-state metadata extraction — six regex patterns + SLM
role-span path + first-line fallback.

Extracted from :class:`IngestionPipeline` in the Phase D MI cleanup.
The strategy chain is unchanged — first match wins — but the
patterns and helpers are now plain module-level free functions, which
keeps the orchestrator under the B+ MI bar.

Public entry point: :func:`extract_entity_state_meta`.
"""

from __future__ import annotations

import re

# Compiled patterns — module-level for reuse across calls.
_RX_ASSIGN = re.compile(
    r"^([a-zA-Z0-9_\-]+)\s*:\s*([a-zA-Z0-9_\-]+)\s*=\s*(.+)$",
    re.MULTILINE,
)
_RX_TRANSITION = re.compile(
    r"^([a-zA-Z0-9_\-]+)\s+([a-zA-Z0-9_\-]+)\s+"
    r"(?:changed|updated)\s+from\s+(.+?)\s+to\s+(.+?)"
    r"(?:\s+(?:due|for|after|because)\b.*)?$",
    re.MULTILINE | re.IGNORECASE,
)
_RX_COLON_TRANSITION = re.compile(
    r"^([a-zA-Z0-9_\-]+)\s*:\s*([a-zA-Z0-9_\-]+)\s+"
    r"(?:changed|updated)\s+from\s+(.+?)\s+to\s+(.+?)"
    r"(?:\s+(?:due|for|after|because|per)\b.*)?$",
    re.MULTILINE | re.IGNORECASE,
)
_RX_DECLARATION = re.compile(
    r"^([a-zA-Z0-9_\-]+)\s+([a-zA-Z0-9_\-]+)\s+"
    r"(?:is|are|was|were|changed to|updated to|set to)\s+(.+)$",
    re.MULTILINE | re.IGNORECASE,
)
_RX_MD_STATUS = re.compile(
    r"^#\s+(.+?)$.*?^##?\s*[Ss]tatus\s*$\s*^(\w[\w\s]*)$",
    re.MULTILINE | re.DOTALL,
)
_RX_YAML_STATUS = re.compile(
    r"^\s*status\s*:\s*(\w[\w_\-]*)",
    re.MULTILINE | re.IGNORECASE,
)


def _meta_from_role_spans(
    slm_label: dict | None,
    entities: list[dict],
) -> dict | None:
    """Strategy 1 — v7+ SLM role-span path (preferred).

    Subject-resolution: prefer a GLiNER entity that ISN'T the primary
    or alternative canonical, so the state belongs to something other
    than the value that changed.
    """
    if not (slm_label and slm_label.get("role_spans")):
        return None
    role_spans = slm_label["role_spans"]
    primary = next((r for r in role_spans if r.get("role") == "primary"), None)
    if not primary:
        return None
    alt = next((r for r in role_spans if r.get("role") == "alternative"), None)
    primary_canon = primary["canonical"].lower()
    alt_canon = alt["canonical"].lower() if alt else None
    subject_entity = None
    for ent in entities:
        name_l = ent["name"].lower()
        if name_l == primary_canon:
            continue
        if alt_canon and name_l == alt_canon:
            continue
        subject_entity = ent["name"]
        break
    meta = {
        "entity_id": subject_entity or primary["canonical"],
        "state_key": primary["slot"],
        "state_value": primary["canonical"],
        "source": "slm_role_span",
    }
    if alt:
        meta["state_previous"] = alt["canonical"]
        meta["state_alternative"] = alt["canonical"]
    return meta


def _meta_assign(content: str, _entities: list[dict]) -> dict | None:
    """Pattern 1 — ``EntityName: key = value``."""
    m = _RX_ASSIGN.search(content)
    if not m:
        return None
    return {
        "entity_id": m.group(1).strip(),
        "state_key": m.group(2).strip(),
        "state_value": m.group(3).strip(),
    }


def _meta_transition(content: str, _entities: list[dict]) -> dict | None:
    """Pattern 2 — ``Entity key changed/updated from X to Y``."""
    m = _RX_TRANSITION.search(content)
    if not m:
        return None
    return {
        "entity_id": m.group(1).strip(),
        "state_key": m.group(2).strip(),
        "state_value": m.group(4).strip(),
        "state_previous": m.group(3).strip(),
    }


def _meta_colon_transition(content: str, _entities: list[dict]) -> dict | None:
    """Pattern 3 — ``Entity: key changed/updated from X to Y``."""
    m = _RX_COLON_TRANSITION.search(content)
    if not m:
        return None
    return {
        "entity_id": m.group(1).strip(),
        "state_key": m.group(2).strip(),
        "state_value": m.group(4).strip(),
        "state_previous": m.group(3).strip(),
    }


def _meta_declaration(content: str, _entities: list[dict]) -> dict | None:
    """Pattern 4 — ``Entity key is/was/set to value``."""
    m = _RX_DECLARATION.search(content)
    if not m:
        return None
    return {
        "entity_id": m.group(1).strip(),
        "state_key": m.group(2).strip(),
        "state_value": m.group(3).strip(),
    }


def _meta_md_status(content: str, entities: list[dict]) -> dict | None:
    """Pattern 5 — Markdown ``## Status\\n\\nvalue`` (ADRs)."""
    m = _RX_MD_STATUS.search(content)
    if not (m and entities):
        return None
    title_lower = m.group(1).strip().lower()
    status_val = m.group(2).strip()
    entity_id = entities[0]["name"]
    for ent in entities:
        if ent["name"].lower() in title_lower:
            entity_id = ent["name"]
            break
    return {
        "entity_id": entity_id,
        "state_key": "status",
        "state_value": status_val,
    }


def _meta_yaml_status(content: str, entities: list[dict]) -> dict | None:
    """Pattern 6 — YAML ``status: value``."""
    m = _RX_YAML_STATUS.search(content)
    if not (m and entities):
        return None
    return {
        "entity_id": entities[0]["name"],
        "state_key": "status",
        "state_value": m.group(1).strip(),
    }


def _meta_first_line_fallback(content: str, entities: list[dict]) -> dict:
    """Final fallback — first entity + first assignment-like line.

    Returns ``{}`` when no entities (caller skips L2 creation).
    """
    if not entities:
        return {}
    best_line = ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and any(c in stripped for c in ("=", ":", "→")):
            best_line = stripped[:200]
            break
    if not best_line:
        best_line = content.splitlines()[0].strip()[:200] if content else ""
    return {
        "entity_id": entities[0]["name"],
        "state_key": "state",
        "state_value": best_line,
    }


# Strategy chain — first match wins.  More-specific patterns
# (assign / transition) before less-specific (yaml / markdown).
_STATE_PATTERN_FNS: tuple = (
    _meta_assign,
    _meta_transition,
    _meta_colon_transition,
    _meta_declaration,
    _meta_md_status,
    _meta_yaml_status,
)


def extract_entity_state_meta(
    content: str,
    entities: list[dict],
    slm_label: dict | None = None,
) -> dict:
    """Extract entity state metadata from content + SLM output.

    Strategy chain — first match wins:

    1. **SLM role-span path** (v7+ preferred).
    2. **Regex pattern chain** (cold-start fallback).
    3. **First-line fallback** — when no pattern matches but entities
       are present.

    Returns ``{}`` when nothing matches (no entities, no patterns —
    caller skips L2 creation).
    """
    meta = _meta_from_role_spans(slm_label, entities)
    if meta is not None:
        return meta
    for pattern_fn in _STATE_PATTERN_FNS:
        meta = pattern_fn(content, entities)
        if meta is not None:
            return meta
    return _meta_first_line_fallback(content, entities)
