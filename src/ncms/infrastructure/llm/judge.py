"""LLM-as-judge for final relevance scoring in the retrieval pipeline.

Uses litellm for universal LLM backend support. Disabled by default;
enable via config.llm_judge_enabled = True.
"""

from __future__ import annotations

import json
import logging
import time

from ncms.domain.models import ScoredMemory
from ncms.infrastructure.extraction.keyword_extractor import _parse_llm_json

logger = logging.getLogger(__name__)

# Module-level counters for judge observability
_judge_stats: dict[str, int | float] = {"calls": 0, "success": 0, "errors": 0, "total_time": 0.0}

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
    _judge_stats["calls"] += 1

    try:
        import litellm

        candidate_text = "\n".join(
            f"- [{c.memory.id}]: {c.memory.content[:4000]}" for c in candidates
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
        # Disable thinking mode for reasoning models
        if model.startswith("ollama"):
            kwargs["think"] = False
        elif any(name in model.lower() for name in ("nemotron", "qwen")):
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

        t0 = time.perf_counter()
        response = await litellm.acompletion(**kwargs)
        llm_elapsed = time.perf_counter() - t0
        _judge_stats["total_time"] += llm_elapsed

        content = response.choices[0].message.content  # type: ignore[union-attr]
        if not content:
            return [(c.memory.id, c.total_activation) for c in candidates]

        scores = _parse_llm_json(content)
        _judge_stats["success"] += 1
        logger.debug("LLM judge scored %d candidates in %.2fs", len(scores), llm_elapsed)
        return sorted(
            [(s["memory_id"], float(s["relevance"])) for s in scores],
            key=lambda x: x[1],
            reverse=True,
        )
    except json.JSONDecodeError:
        _judge_stats["errors"] += 1
        logger.warning("LLM judge JSON parse failed, raw=%s", content[:500], exc_info=True)
        return [(c.memory.id, c.total_activation) for c in candidates]
    except Exception:
        _judge_stats["errors"] += 1
        logger.warning("LLM judge failed, falling back to activation scores", exc_info=True)
        return [(c.memory.id, c.total_activation) for c in candidates]


def get_judge_stats() -> dict[str, int | float]:
    """Return judge counters for monitoring."""
    return dict(_judge_stats)


def reset_judge_stats() -> None:
    """Reset judge counters."""
    _judge_stats.update(calls=0, success=0, errors=0, total_time=0.0)
