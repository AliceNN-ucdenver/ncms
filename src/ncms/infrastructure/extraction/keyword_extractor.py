"""Keyword bridge node extraction via LLM.

Extracts semantic keywords from memory content that create cross-subgraph
connections in the knowledge graph. Keywords are stored as Entity(type="keyword")
nodes, enabling graph expansion to discover related memories that share no
common entities but relate to the same abstract concept.

Uses litellm for universal LLM backend support. Disabled by default;
enable via config.keyword_bridge_enabled = True.
"""

from __future__ import annotations

import json
import logging
import time

from json_repair import repair_json

logger = logging.getLogger(__name__)

# Module-level counters for keyword extraction observability
_kw_stats: dict[str, int] = {"calls": 0, "success": 0, "empty": 0, "errors": 0}


def _parse_llm_json(raw: str) -> object:
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


KEYWORD_PROMPT = """Extract 3-8 semantic keywords from this text.
Keywords should be abstract domain concepts (not specific identifiers or technology names).
Focus on concepts that would help connect this text to related topics.

Text:
"{content}"

Already-extracted entities: {entity_names}

Return ONLY a JSON array of objects: [{{"name": "keyword", "domain": "category"}}]
Do not include entities already listed above. Return an empty array if no keywords apply."""


async def extract_keywords(
    content: str,
    existing_entities: list[dict[str, str]],
    model: str = "gpt-4o-mini",
    max_keywords: int = 8,
    api_base: str | None = None,
) -> list[dict[str, str]]:
    """Extract semantic keywords via LLM for knowledge graph bridging.

    Returns a list of dicts with ``name`` and ``type`` keys, where type
    is always ``"keyword"``. Deduplicates against existing entities
    (case-insensitive) and caps at ``max_keywords``.

    On any error, logs a warning and returns an empty list (non-fatal).
    """
    if not content or len(content) < 5:
        return []

    _kw_stats["calls"] += 1

    try:
        import litellm

        entity_names_str = ", ".join(e["name"] for e in existing_entities) or "none"

        kwargs: dict = dict(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": KEYWORD_PROMPT.format(
                        content=content[:8000],
                        entity_names=entity_names_str,
                    ),
                }
            ],
            temperature=0.0,
            max_tokens=300,
        )
        if api_base:
            kwargs["api_base"] = api_base
        # Disable thinking mode for reasoning models
        if model.startswith("ollama"):
            kwargs["think"] = False
        elif any(name in model.lower() for name in ("nemotron", "qwen")):
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

        t0 = time.perf_counter()
        response = await litellm.acompletion(**kwargs)
        llm_elapsed = time.perf_counter() - t0

        raw = response.choices[0].message.content  # type: ignore[union-attr]
        if not raw:
            _kw_stats["empty"] += 1
            return []

        keywords = _parse_llm_json(raw)
        if not isinstance(keywords, list):
            return []

        # Deduplicate against existing entities
        existing_lower = {e["name"].lower() for e in existing_entities}
        seen: set[str] = set()
        results: list[dict[str, str]] = []

        for kw in keywords:
            if not isinstance(kw, dict) or "name" not in kw:
                continue
            name = str(kw["name"]).strip()
            lower = name.lower()
            if not name or lower in existing_lower or lower in seen:
                continue
            seen.add(lower)
            results.append({"name": name, "type": "keyword"})
            if len(results) >= max_keywords:
                break

        _kw_stats["success"] += 1
        logger.debug(
            "Keywords extracted: %d in %.2fs (%s)",
            len(results), llm_elapsed, ", ".join(r["name"] for r in results),
        )
        return results

    except json.JSONDecodeError:
        _kw_stats["errors"] += 1
        logger.warning("Keyword extraction JSON parse failed, raw=%s", raw[:500], exc_info=True)
        return []
    except Exception:
        _kw_stats["errors"] += 1
        logger.warning("Keyword extraction failed, returning empty list", exc_info=True)
        return []


def get_keyword_stats() -> dict[str, int]:
    """Return keyword extraction counters for monitoring."""
    return dict(_kw_stats)


def reset_keyword_stats() -> None:
    """Reset keyword extraction counters."""
    _kw_stats.update(calls=0, success=0, empty=0, errors=0)
