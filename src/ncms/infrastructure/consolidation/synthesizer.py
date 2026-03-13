"""LLM-based pattern synthesis for knowledge consolidation.

Uses litellm to discover emergent cross-memory patterns from entity clusters.
Supports vLLM and other OpenAI-compatible endpoints via ``api_base``.

Disabled by default; enable via config.consolidation_knowledge_enabled = True.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ncms.infrastructure.llm.caller import call_llm_json

if TYPE_CHECKING:
    from ncms.infrastructure.consolidation.clusterer import MemoryCluster

logger = logging.getLogger(__name__)

CONSOLIDATION_PROMPT = """You are analyzing a cluster of related memories from a knowledge system.
These memories share common entities and may reveal emergent patterns.

Memories:
{memories}

Shared entities: {shared_entities}
Domains: {domains}

Analyze these memories and identify:
1. Cross-memory dependencies or relationships not explicitly stated
2. Architectural patterns or design decisions that emerge from the combination
3. Potential impacts if any of these areas change

Return ONLY a JSON object:
{{"insight": "One paragraph describing the emergent pattern or relationship",
"pattern_type": "dependency|architecture|impact|workflow",
"confidence": 0.0-1.0,
"key_entities": ["entity1", "entity2"]}}"""


async def synthesize_insight(
    cluster: MemoryCluster,
    model: str = "gpt-4o-mini",
    api_base: str | None = None,
) -> dict | None:
    """Synthesize a cross-memory pattern from a cluster via LLM.

    Returns a dict with ``insight``, ``pattern_type``, ``confidence``,
    and ``key_entities`` keys, or ``None`` on failure.

    Supports vLLM/OpenAI-compatible endpoints via ``api_base``.
    """
    if not cluster.memories:
        return None

    try:
        # Format memories for the prompt (truncate each to 2000 chars)
        memories_text = "\n".join(
            f"- [{m.id[:8]}] ({', '.join(m.domains) or 'general'}): {m.content[:2000]}"
            for m in cluster.memories
        )
        shared_text = ", ".join(sorted(cluster.shared_entity_ids)[:15]) or "none"
        domains_text = ", ".join(sorted(cluster.domains)) or "general"

        prompt = CONSOLIDATION_PROMPT.format(
            memories=memories_text,
            shared_entities=shared_text,
            domains=domains_text,
        )

        result = await call_llm_json(prompt, model=model, api_base=api_base)
        if not isinstance(result, dict) or "insight" not in result:
            return None

        # Normalize fields
        return {
            "insight": str(result["insight"]),
            "pattern_type": str(result.get("pattern_type", "unknown")),
            "confidence": float(result.get("confidence", 0.5)),
            "key_entities": list(result.get("key_entities", [])),
        }

    except Exception:
        logger.warning("Insight synthesis failed, returning None", exc_info=True)
        return None
