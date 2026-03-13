"""LLM-based contradiction detection for memory ingest.

Compares a new memory against existing similar memories to detect
factual contradictions. Both sides are annotated so stale knowledge
is surfaced during retrieval.

Uses litellm for universal LLM backend support. Disabled by default;
enable via config.contradiction_detection_enabled = True.
"""

from __future__ import annotations

import json
import logging

from ncms.domain.models import Memory
from ncms.infrastructure.llm.json_utils import parse_llm_json

logger = logging.getLogger(__name__)

CONTRADICTION_PROMPT = """You are a contradiction detector for a knowledge management system.
Compare a NEW memory against EXISTING memories and identify direct factual contradictions.

A contradiction exists when two statements make incompatible claims about the same subject.
Do NOT flag differences in scope, detail level, or complementary information as contradictions.

NEW memory:
"{new_content}"

EXISTING memories:
{existing_memories}

Return ONLY a JSON array of contradiction objects (empty array [] if no contradictions):
[{{"existing_memory_id": "id", "contradiction_type": "factual|temporal|configuration",
"explanation": "brief explanation", "severity": "low|medium|high"}}]"""


async def detect_contradictions(
    new_memory: Memory,
    existing_memories: list[Memory],
    model: str = "gpt-4o-mini",
    api_base: str | None = None,
) -> list[dict]:
    """Detect contradictions between a new memory and existing memories.

    Returns a list of dicts with ``existing_memory_id``, ``contradiction_type``,
    ``explanation``, and ``severity`` keys.  Returns empty list on any error
    (non-fatal).
    """
    if not existing_memories:
        return []

    try:
        import litellm

        existing_text = "\n".join(
            f"- [{m.id}]: {m.content[:2000]}" for m in existing_memories
        )

        kwargs: dict = dict(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": CONTRADICTION_PROMPT.format(
                        new_content=new_memory.content[:8000],
                        existing_memories=existing_text,
                    ),
                }
            ],
            temperature=0.0,
            max_tokens=500,
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

        contradictions = parse_llm_json(raw)
        if not isinstance(contradictions, list):
            return []

        # Validate and normalize each contradiction
        valid_ids = {m.id for m in existing_memories}
        results: list[dict] = []

        for c in contradictions:
            if not isinstance(c, dict):
                continue
            mid = c.get("existing_memory_id", "")
            if mid not in valid_ids:
                continue
            results.append(
                {
                    "existing_memory_id": mid,
                    "contradiction_type": str(c.get("contradiction_type", "factual")),
                    "explanation": str(c.get("explanation", "")),
                    "severity": str(c.get("severity", "medium")),
                }
            )

        return results

    except json.JSONDecodeError:
        logger.warning(
            "Contradiction detection JSON parse failed, raw=%s", raw[:500], exc_info=True
        )
        return []
    except Exception:
        logger.warning(
            "Contradiction detection failed, returning empty list", exc_info=True
        )
        return []
