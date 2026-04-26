"""LLM-based abstract memory synthesis for hierarchical consolidation (Phase 5).

Generates three types of abstractions from lower-level memory traces:
- Episode summaries from closed episodes
- State trajectories from entity state histories
- Recurring patterns from similar episode clusters

All functions are non-fatal: they return ``None`` on LLM failure and log warnings.
Uses ``call_llm_json`` from the shared LLM caller infrastructure.
"""

from __future__ import annotations

import logging

from ncms.infrastructure.llm.caller import call_llm_json

logger = logging.getLogger(__name__)

# ── Prompts ──────────────────────────────────────────────────────────────

EPISODE_SUMMARY_PROMPT = """You are summarizing a completed episode from a knowledge system.
An episode groups related events, decisions, and changes into a bounded narrative arc.

Episode title: {title}

Member fragments (chronological):
{members}

Analyze the episode and produce a concise summary.
Return ONLY a JSON object:
{{"summary": "2-4 sentence narrative covering what happened, who was involved, and the outcome",
"actors": ["actor1", "actor2"],
"artifacts": ["artifact1", "artifact2"],
"decisions": ["decision1"],
"outcome": "one sentence describing the final state or resolution",
"confidence": 0.0-1.0}}"""

STATE_TRAJECTORY_PROMPT = """You are analyzing the state history of an entity in a knowledge system.
Describe how this entity's state has evolved over time.

Entity: {entity_name}
State key: {state_key}

State history (chronological):
{states}

Analyze the progression and identify trends.
Return ONLY a JSON object:
{{"narrative": "2-4 sentence description of how this state evolved",
"trend": "improving|degrading|stable|oscillating|unknown",
"key_transitions": ["transition1", "transition2"],
"confidence": 0.0-1.0}}"""

RECURRING_PATTERN_PROMPT = """\
You are analyzing a cluster of similar episodes from a knowledge system.
These episodes share common entities and may reveal recurring patterns.

Episode summaries:
{summaries}

Shared entities across episodes: {shared_entities}

Identify what recurs across these episodes.
Return ONLY a JSON object:
{{"pattern": "One paragraph describing the recurring pattern",
"pattern_type": "dependency|architecture|workflow|incident|decision",
"recurrence_count": {episode_count},
"confidence": 0.0-1.0,
"key_entities": ["entity1", "entity2"]}}"""

_MAX_MEMBERS = 20
_MAX_MEMBER_CHARS = 2000
_MAX_STATES = 20
_MAX_STATE_CHARS = 500
_MAX_SUMMARIES = 10
_MAX_SUMMARY_CHARS = 1000


# ── Synthesis Functions ──────────────────────────────────────────────────


async def synthesize_episode_summary(
    episode_title: str,
    member_contents: list[str],
    model: str,
    api_base: str | None = None,
) -> dict | None:
    """Synthesize a narrative summary from episode member fragments.

    Returns a dict with ``summary``, ``actors``, ``artifacts``, ``decisions``,
    ``outcome``, and ``confidence`` keys, or ``None`` on failure.
    """
    if not member_contents:
        return None

    try:
        members_text = "\n".join(
            f"- {content[:_MAX_MEMBER_CHARS]}" for content in member_contents[:_MAX_MEMBERS]
        )

        prompt = EPISODE_SUMMARY_PROMPT.format(
            title=episode_title,
            members=members_text,
        )

        result = await call_llm_json(prompt, model=model, api_base=api_base)
        if not isinstance(result, dict) or "summary" not in result:
            return None

        return {
            "summary": str(result["summary"]),
            "actors": list(result.get("actors", [])),
            "artifacts": list(result.get("artifacts", [])),
            "decisions": list(result.get("decisions", [])),
            "outcome": str(result.get("outcome", "")),
            "confidence": max(0.0, min(1.0, float(result.get("confidence", 0.5)))),
        }

    except Exception:
        logger.warning("Episode summary synthesis failed", exc_info=True)
        return None


async def synthesize_state_trajectory(
    entity_name: str,
    state_key: str,
    states: list[dict[str, str]],
    model: str,
    api_base: str | None = None,
) -> dict | None:
    """Synthesize a temporal progression narrative from state history.

    Each state dict should have ``value`` and ``timestamp`` keys.
    Returns a dict with ``narrative``, ``trend``, ``key_transitions``,
    and ``confidence`` keys, or ``None`` on failure.
    """
    if not states:
        return None

    try:
        states_text = "\n".join(
            f"- [{s.get('timestamp', 'unknown')}] {str(s.get('value', ''))[:_MAX_STATE_CHARS]}"
            for s in states[:_MAX_STATES]
        )

        prompt = STATE_TRAJECTORY_PROMPT.format(
            entity_name=entity_name,
            state_key=state_key,
            states=states_text,
        )

        result = await call_llm_json(prompt, model=model, api_base=api_base)
        if not isinstance(result, dict) or "narrative" not in result:
            return None

        return {
            "narrative": str(result["narrative"]),
            "trend": str(result.get("trend", "unknown")),
            "key_transitions": list(result.get("key_transitions", [])),
            "confidence": max(0.0, min(1.0, float(result.get("confidence", 0.5)))),
        }

    except Exception:
        logger.warning("State trajectory synthesis failed", exc_info=True)
        return None


async def synthesize_recurring_pattern(
    episode_summaries: list[str],
    shared_entities: list[str],
    model: str,
    api_base: str | None = None,
) -> dict | None:
    """Synthesize a recurring pattern from similar episode summaries.

    Returns a dict with ``pattern``, ``pattern_type``, ``recurrence_count``,
    ``confidence``, and ``key_entities`` keys, or ``None`` on failure.
    """
    if not episode_summaries:
        return None

    try:
        summaries_text = "\n".join(
            f"- {s[:_MAX_SUMMARY_CHARS]}" for s in episode_summaries[:_MAX_SUMMARIES]
        )
        entities_text = ", ".join(shared_entities[:15]) or "none"

        prompt = RECURRING_PATTERN_PROMPT.format(
            summaries=summaries_text,
            shared_entities=entities_text,
            episode_count=len(episode_summaries),
        )

        result = await call_llm_json(prompt, model=model, api_base=api_base)
        if not isinstance(result, dict) or "pattern" not in result:
            return None

        return {
            "pattern": str(result["pattern"]),
            "pattern_type": str(result.get("pattern_type", "unknown")),
            "recurrence_count": int(result.get("recurrence_count", len(episode_summaries))),
            "confidence": max(0.0, min(1.0, float(result.get("confidence", 0.5)))),
            "key_entities": list(result.get("key_entities", [])),
        }

    except Exception:
        logger.warning("Recurring pattern synthesis failed", exc_info=True)
        return None
