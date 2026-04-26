"""Shared RAG evaluation pipeline for conversation-memory benchmarks.

Provides:
- build_context_from_memories: format retrieved memories into LLM context
- generate_answer: call Spark LLM to answer a question given context
- llm_judge: call Spark LLM to judge whether a prediction is correct

All LLM calls use litellm with Nemotron Nano on DGX Spark by default.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"
DEFAULT_API_BASE = "http://spark-ee7d.local:8000/v1"

# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------


def build_context_from_memories(
    memories: list,
    max_chars: int = 4000,
) -> str:
    """Concatenate top-k memory contents with boundaries, truncating to max_chars.

    Args:
        memories: List of ScoredMemory (or any object with .memory.content).
        max_chars: Maximum total characters for the context string.

    Returns:
        Formatted context string with memory boundaries.
    """
    if not memories:
        return ""

    parts: list[str] = []
    total = 0
    for i, scored in enumerate(memories):
        content = scored.memory.content
        header = f"--- Memory {i + 1} ---"
        segment = f"{header}\n{content}\n"
        if total + len(segment) > max_chars:
            remaining = max_chars - total
            if remaining > len(header) + 10:
                parts.append(f"{header}\n{content[: remaining - len(header) - 2]}\n")
            break
        parts.append(segment)
        total += len(segment)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Answer generation
# ---------------------------------------------------------------------------

_DEFAULT_SYSTEM_PROMPT = (
    "Answer the question based on the provided context. Be concise and precise."
)


async def generate_answer(
    question: str,
    context: str,
    *,
    system_prompt: str = "",
    model: str = DEFAULT_MODEL,
    api_base: str = DEFAULT_API_BASE,
    max_tokens: int = 200,
    temperature: float = 0.0,
) -> str:
    """Generate an answer to a question given retrieved context via litellm.

    Args:
        question: The question to answer.
        context: Retrieved context string (from build_context_from_memories).
        system_prompt: System prompt override.
        model: litellm model identifier.
        api_base: LLM API base URL.
        max_tokens: Maximum tokens in the response.
        temperature: Sampling temperature.

    Returns:
        Generated answer string, or empty string on error.
    """
    try:
        import litellm

        user_msg = f"Context:\n{context}\n\nQuestion: {question}"

        kwargs: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt or _DEFAULT_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if api_base:
            kwargs["api_base"] = api_base

        # Disable thinking mode for Nemotron Nano to get clean output
        if "Nemotron" in model or "nemotron" in model:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

        response = await litellm.acompletion(**kwargs)
        text = (response.choices[0].message.content or "").strip()
        if not text:
            logger.warning("Empty response from %s", model)
        return text
    except Exception:
        logger.warning("generate_answer failed", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# LLM Judge
# ---------------------------------------------------------------------------

# Judge system prompt (matches reference kumiho_eval/common.py pattern)
_JUDGE_SYSTEM = (
    "You are an impartial judge evaluating whether a model's response "
    "correctly answers a question given the ground truth. Respond with ONLY "
    '"correct" or "incorrect". Be lenient with phrasing differences but '
    "strict on factual accuracy."
)

# Judge prompt templates by type
_JUDGE_TEMPLATES: dict[str, str] = {
    "default": (
        "Question: {question}\n"
        "Ground truth answer: {ground_truth}\n"
        "Model's response: {prediction}\n\n"
        "Does the model's response correctly answer the question? Consider:\n"
        "- Factual equivalence (different phrasing is OK)\n"
        "- Completeness (all key facts present)\n\n"
        'Answer "correct" or "incorrect":'
    ),
    "temporal": (
        "Question: {question}\n"
        "Ground truth answer: {ground_truth}\n"
        "Model's response: {prediction}\n\n"
        "Does the model's response correctly answer the question? Consider:\n"
        "- Factual equivalence (different phrasing is OK)\n"
        "- Completeness (all key facts present)\n"
        "- For temporal questions, allow off-by-one for day/week/month counts\n"
        "- Do not penalize off-by-one errors for the number of days\n\n"
        'Answer "correct" or "incorrect":'
    ),
    "knowledge-update": (
        "Question: {question}\n"
        "Ground truth answer: {ground_truth}\n"
        "Model's response: {prediction}\n\n"
        "Does the model's response correctly answer the question? Consider:\n"
        "- If the response contains some previous information along with an "
        "updated answer, it should be considered correct as long as the updated "
        "answer matches the ground truth\n\n"
        'Answer "correct" or "incorrect":'
    ),
    "abstention": (
        "Question: {question}\n"
        "Explanation: {ground_truth}\n"
        "Model's response: {prediction}\n\n"
        "Does the model correctly identify the question as unanswerable? "
        "The model could say that the information is incomplete, or some "
        "other information is given but the asked information is not.\n\n"
        'Answer "correct" or "incorrect":'
    ),
}


async def llm_judge(
    question: str,
    ground_truth: str,
    prediction: str,
    *,
    judge_type: str = "default",
    model: str = DEFAULT_MODEL,
    api_base: str = DEFAULT_API_BASE,
) -> bool:
    """Use an LLM to judge whether a prediction matches the ground truth.

    Args:
        question: The original question.
        ground_truth: The expected answer.
        prediction: The model's predicted answer.
        judge_type: One of "default", "temporal", "knowledge-update", "abstention".
        model: litellm model identifier.
        api_base: LLM API base URL.

    Returns:
        True if the prediction is judged correct, False otherwise.
    """
    try:
        import litellm

        template = _JUDGE_TEMPLATES.get(judge_type, _JUDGE_TEMPLATES["default"])
        prompt = template.format(
            question=question,
            ground_truth=ground_truth,
            prediction=prediction,
        )

        kwargs: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 10,
            "temperature": 0.0,
        }
        if api_base:
            kwargs["api_base"] = api_base

        # Disable thinking mode for Nemotron Nano
        if "Nemotron" in model or "nemotron" in model:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

        response = await litellm.acompletion(**kwargs)
        raw_verdict = (response.choices[0].message.content or "").strip()

        # Parse verdict: accept "correct", "yes", "true" as positive
        verdict = raw_verdict.lower().split()[0] if raw_verdict.strip() else ""
        return verdict in ("correct", "yes", "true")
    except Exception:
        logger.warning("llm_judge failed", exc_info=True)
        return False
