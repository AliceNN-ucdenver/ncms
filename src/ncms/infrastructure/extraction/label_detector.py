"""LLM-based entity label detection for domain-specific NER.

Given sample content from a domain, uses an LLM to propose optimal
GLiNER entity labels.  Results are cached in consolidation_state for
reuse across sessions.

Uses litellm for universal LLM backend support.
"""

from __future__ import annotations

import json
import logging

from ncms.infrastructure.extraction.keyword_extractor import _parse_llm_json

logger = logging.getLogger(__name__)

LABEL_DETECTION_PROMPT = """Analyze these sample texts from the "{domain}" knowledge domain.
Propose 5-15 entity type labels that a zero-shot NER model should extract.

Labels should be:
- Noun phrases (1-3 words each)
- Specific enough to be useful but general enough to recur
- Appropriate for the domain's vocabulary

Sample texts:
{samples}

Return ONLY a JSON array of label strings, e.g.: ["technology", "endpoint", "data model"]
Do not include explanation."""


async def detect_labels(
    domain: str,
    sample_texts: list[str],
    model: str = "gpt-4o-mini",
    api_base: str | None = None,
) -> list[str]:
    """Detect optimal entity labels for a domain from sample content.

    Returns a list of label strings (5-15 labels).  On error, returns
    empty list (non-fatal).
    """
    if not sample_texts:
        return []

    try:
        import litellm

        samples_text = "\n---\n".join(t[:500] for t in sample_texts[:10])

        kwargs: dict = dict(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": LABEL_DETECTION_PROMPT.format(
                        domain=domain,
                        samples=samples_text,
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

        labels = _parse_llm_json(raw)
        if not isinstance(labels, list):
            return []

        # Validate: only strings, 1-50 chars each, cap at 15
        return [
            str(lbl).strip()
            for lbl in labels
            if isinstance(lbl, str) and 1 <= len(str(lbl).strip()) <= 50
        ][:15]

    except json.JSONDecodeError:
        logger.warning("Label detection JSON parse failed, raw=%s", raw[:500], exc_info=True)
        return []
    except Exception:
        logger.warning("Label detection failed, returning empty list", exc_info=True)
        return []
