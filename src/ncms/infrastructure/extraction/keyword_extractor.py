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

logger = logging.getLogger(__name__)

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

    try:
        import litellm

        entity_names_str = ", ".join(e["name"] for e in existing_entities) or "none"

        kwargs: dict = dict(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": KEYWORD_PROMPT.format(
                        content=content[:2000],
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

        response = await litellm.acompletion(**kwargs)

        raw = response.choices[0].message.content  # type: ignore[union-attr]
        if not raw:
            return []

        # Strip markdown code fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        # Extract JSON array from reasoning output (safety net)
        if not raw.startswith("[") and not raw.startswith("{"):
            start = raw.find("[")
            end = raw.rfind("]")
            if start != -1 and end != -1 and end > start:
                raw = raw[start : end + 1]

        keywords = json.loads(raw)
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

        return results

    except Exception:
        logger.warning("Keyword extraction failed, returning empty list", exc_info=True)
        return []
