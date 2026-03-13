"""Shared JSON parsing utilities for LLM output.

Handles common LLM JSON issues: markdown fences, reasoning preamble,
trailing commas, swapped brackets, and other malformed output.
"""

from __future__ import annotations

import json

from json_repair import repair_json


def parse_llm_json(raw: str) -> object:
    """Parse JSON from LLM output with automatic error repair.

    Uses json-repair to handle common LLM JSON issues:
    - Swapped closing brackets (]} → }])
    - Trailing commas before ] or }
    - Missing quotes, unescaped characters
    - Markdown code fences (```json ... ```)
    - Reasoning text before/after JSON
    """
    # Strip markdown code fences
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    # Extract JSON from reasoning output
    if not raw.startswith("[") and not raw.startswith("{"):
        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end != -1 and end > start:
            raw = raw[start : end + 1]
        else:
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1 and end > start:
                raw = raw[start : end + 1]

    # Try parsing as-is first (fast path)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Repair malformed JSON (swapped brackets, trailing commas, etc.)
    repaired = repair_json(raw, return_objects=True)
    return repaired
