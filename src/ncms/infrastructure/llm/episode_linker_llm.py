"""LLM-based episode linking fallback.

Called when the BM25/SPLADE/entity scoring pipeline cannot match a fragment
to any open episode above the threshold.  The LLM is given the fragment
content and a summary of open episodes and asked to suggest matches.

Results are logged for analysis to help tune exemplar data and episode
scoring weights over time.

Follows the contradiction detector pattern: non-fatal, feature-flagged,
degrades gracefully on any error.
"""

from __future__ import annotations

import logging

from ncms.infrastructure.llm.caller import call_llm_json

logger = logging.getLogger(__name__)

_EPISODE_LINKING_PROMPT = """\
You are an episode linking assistant.  Given a new knowledge fragment and a
list of open episodes, determine which episode(s) the fragment belongs to
based on topical overlap, shared entities, or event continuity.

NEW FRAGMENT:
Content: "{fragment_content}"
Entities: {fragment_entities}
Domains: {fragment_domains}
Source: {fragment_agent}

OPEN EPISODES (id | topic | entities | domains):
{episode_summaries}

Return ONLY a JSON array of matching episodes (empty array [] if none match):
[{{"episode_id": "<id>", "confidence": <0.0-1.0>, "reasoning": "<brief>"}}]"""


async def suggest_episode_links(
    fragment_content: str,
    fragment_entities: list[str],
    fragment_domains: list[str],
    fragment_agent: str | None,
    episode_summaries: list[dict[str, str]],
    model: str,
    api_base: str | None = None,
) -> list[dict[str, object]]:
    """Ask an LLM to suggest episode matches for a fragment.

    Returns a list of ``{"episode_id": str, "confidence": float}`` dicts,
    or an empty list on any error.  Callers should validate episode IDs
    against the actual open episode set before using.

    Args:
        fragment_content: Fragment text (truncated by caller to 2000 chars).
        fragment_entities: Entity names extracted from fragment.
        fragment_domains: Domain tags.
        fragment_agent: Source agent ID.
        episode_summaries: List of dicts with keys: id, topic, entities, domains.
        model: litellm model identifier.
        api_base: Optional API base URL.

    Returns:
        List of episode match suggestions, or empty list on failure.
    """
    if not episode_summaries:
        return []

    try:
        # Format episode summaries (truncate to 5 to fit context)
        ep_lines = []
        for ep in episode_summaries[:5]:
            ep_lines.append(
                f"- {ep['id']} | {ep.get('topic', 'unknown')[:200]} "
                f"| entities: {ep.get('entities', '')[:200]} "
                f"| domains: {ep.get('domains', '')}"
            )

        prompt = _EPISODE_LINKING_PROMPT.format(
            fragment_content=fragment_content[:2000],
            fragment_entities=", ".join(fragment_entities[:20]),
            fragment_domains=", ".join(fragment_domains[:10]),
            fragment_agent=fragment_agent or "unknown",
            episode_summaries="\n".join(ep_lines),
        )

        result = await call_llm_json(prompt, model=model, api_base=api_base, max_tokens=300)

        if not isinstance(result, list):
            return []

        valid: list[dict[str, object]] = []
        known_ids = {ep["id"] for ep in episode_summaries}
        for entry in result:
            if not isinstance(entry, dict):
                continue
            ep_id = str(entry.get("episode_id", ""))
            if ep_id not in known_ids:
                continue
            confidence = float(entry.get("confidence", 0.0))
            valid.append(
                {
                    "episode_id": ep_id,
                    "confidence": min(1.0, max(0.0, confidence)),
                    "reasoning": str(entry.get("reasoning", ""))[:200],
                }
            )

        return valid

    except Exception:
        logger.warning("Episode LLM fallback failed, returning empty list", exc_info=True)
        return []
