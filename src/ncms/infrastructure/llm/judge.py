"""LLM-as-judge for final relevance scoring in the retrieval pipeline.

Uses litellm for universal LLM backend support. Disabled by default;
enable via config.llm_judge_enabled = True.
"""

from __future__ import annotations

import json
import logging

from ncms.domain.models import ScoredMemory

logger = logging.getLogger(__name__)

JUDGE_PROMPT = """You are a relevance judge for a cognitive memory system.
Given a search query and a list of candidate memories, score each memory's
relevance to the query on a scale of 0.0 to 1.0.

Query: {query}

Candidates:
{candidates}

Return a JSON array of objects with "memory_id" and "relevance" (0.0-1.0).
Only return the JSON array, no other text."""


async def judge_relevance(
    query: str,
    candidates: list[ScoredMemory],
    model: str = "gpt-4o-mini",
    api_base: str | None = None,
) -> list[tuple[str, float]]:
    """Use an LLM to judge relevance of candidate memories to a query.

    Returns list of (memory_id, relevance_score) sorted by relevance.
    Supports vLLM/OpenAI-compatible endpoints via ``api_base``.
    """
    try:
        import litellm

        candidate_text = "\n".join(
            f"- [{c.memory.id}]: {c.memory.content[:200]}" for c in candidates
        )

        kwargs: dict = dict(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": JUDGE_PROMPT.format(query=query, candidates=candidate_text),
                }
            ],
            temperature=0.0,
            max_tokens=500,
        )
        if api_base:
            kwargs["api_base"] = api_base
        # Disable thinking mode for reasoning models (Qwen3, etc.)
        if model.startswith("ollama"):
            kwargs["think"] = False

        response = await litellm.acompletion(**kwargs)

        content = response.choices[0].message.content  # type: ignore[union-attr]
        if not content:
            return [(c.memory.id, c.total_activation) for c in candidates]

        scores = json.loads(content)
        return sorted(
            [(s["memory_id"], float(s["relevance"])) for s in scores],
            key=lambda x: x[1],
            reverse=True,
        )
    except Exception:
        logger.warning("LLM judge failed, falling back to activation scores", exc_info=True)
        return [(c.memory.id, c.total_activation) for c in candidates]
