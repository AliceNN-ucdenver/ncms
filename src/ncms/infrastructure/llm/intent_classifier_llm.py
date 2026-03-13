"""LLM-based intent classification fallback.

Called when the BM25 exemplar classifier returns low confidence.  The LLM
result is used both for the immediate query and as a signal for tuning
exemplar data (misses are logged for analysis).

Follows the contradiction detector pattern: non-fatal, feature-flagged,
degrades gracefully on any error.
"""

from __future__ import annotations

import logging

from ncms.domain.intent import INTENT_TARGETS, IntentResult, QueryIntent
from ncms.infrastructure.llm.caller import call_llm_json

logger = logging.getLogger(__name__)

_INTENT_CLASSIFICATION_PROMPT = """\
Classify the following search query into exactly one intent type.

Intent types:
- fact_lookup: Direct factual questions about how something works
- current_state_lookup: What is the current status/state/version right now?
- historical_lookup: What was the status/state at some point in the past?
- event_reconstruction: What happened during an incident, outage, or event?
- change_detection: What changed, what is different, how has something evolved?
- pattern_lookup: Are there recurring patterns, common themes, or tendencies?
- strategic_reflection: Lessons learned, insights, takeaways, best practices

Query: "{query}"

Return ONLY a JSON object (no other text):
{{"intent": "<intent_type>", "confidence": <0.0-1.0>}}"""


async def classify_intent_with_llm(
    query: str,
    model: str,
    api_base: str | None = None,
) -> IntentResult | None:
    """Classify a query into an intent class using an LLM.

    Returns an IntentResult on success, or None if the LLM call fails or
    returns an invalid response.  Callers should fall back to the default
    intent (fact_lookup) when None is returned.

    Args:
        query: Natural language search query (truncated to 2000 chars).
        model: litellm model identifier.
        api_base: Optional API base URL.

    Returns:
        IntentResult or None on failure.
    """
    try:
        prompt = _INTENT_CLASSIFICATION_PROMPT.format(query=query[:2000])
        result = await call_llm_json(prompt, model=model, api_base=api_base, max_tokens=100)

        if not isinstance(result, dict):
            return None

        intent_str = result.get("intent", "")
        confidence = float(result.get("confidence", 0.0))

        try:
            intent = QueryIntent(intent_str)
        except ValueError:
            logger.debug("LLM returned unknown intent: %s", intent_str)
            return None

        return IntentResult(
            intent=intent,
            confidence=min(1.0, max(0.0, confidence)),
            target_node_types=INTENT_TARGETS[intent],
        )

    except Exception:
        logger.warning("Intent LLM fallback failed, returning None", exc_info=True)
        return None
