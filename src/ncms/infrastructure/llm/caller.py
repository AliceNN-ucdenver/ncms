"""Shared LLM calling utility for all LLM-backed features.

Encapsulates the common pattern: build litellm kwargs, disable thinking
mode for reasoning models, call acompletion, parse JSON response.

Used by contradiction detection, knowledge consolidation, and label detection.
"""

from __future__ import annotations

import logging

from ncms.infrastructure.llm.json_utils import parse_llm_json

logger = logging.getLogger(__name__)


async def call_llm_json(
    prompt: str,
    model: str,
    api_base: str | None = None,
    max_tokens: int = 500,
    temperature: float = 0.0,
) -> object | None:
    """Call an LLM and parse the response as JSON.

    Handles:
    - litellm kwargs building with optional ``api_base``
    - Thinking mode disable for Ollama / Nemotron / Qwen models
    - JSON extraction and repair via :func:`parse_llm_json`

    Args:
        prompt: The user message to send.
        model: litellm model identifier (e.g. ``ollama_chat/qwen3.5:35b-a3b``).
        api_base: Optional API base URL for vLLM / OpenAI-compatible endpoints.
        max_tokens: Maximum response tokens.
        temperature: Sampling temperature (0.0 = deterministic).

    Returns:
        Parsed JSON object (dict, list, etc.) or ``None`` if the response is empty.

    Raises:
        Exception: On LLM call failure or JSON parse error.  Callers should
            catch and degrade gracefully.
    """
    import litellm

    kwargs: dict = dict(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if api_base:
        kwargs["api_base"] = api_base
        # Local vLLM via openai/ prefix needs a dummy API key
        if model.startswith("openai/"):
            kwargs["api_key"] = "na"
    # Disable thinking mode for reasoning models
    if model.startswith("ollama"):
        kwargs["think"] = False
    elif any(name in model.lower() for name in ("nemotron", "qwen")):
        kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

    response = await litellm.acompletion(**kwargs)

    raw = response.choices[0].message.content  # type: ignore[union-attr]
    if not raw:
        return None

    return parse_llm_json(raw)
