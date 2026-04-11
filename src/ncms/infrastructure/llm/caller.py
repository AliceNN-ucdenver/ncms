"""Shared LLM calling utility for all LLM-backed features.

Encapsulates the common pattern: build litellm kwargs, disable thinking
mode for reasoning models, call acompletion, parse JSON response.

Used by contradiction detection, knowledge consolidation, and label detection.
"""

from __future__ import annotations

import logging

from ncms.infrastructure.llm.json_utils import parse_llm_json

logger = logging.getLogger(__name__)


def _build_litellm_kwargs(
    prompt: str,
    model: str,
    api_base: str | None,
    max_tokens: int,
    temperature: float,
) -> dict:
    """Build litellm kwargs with api_base and thinking-mode handling."""
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
    return kwargs


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

    kwargs = _build_litellm_kwargs(prompt, model, api_base, max_tokens, temperature)
    response = await litellm.acompletion(**kwargs)

    raw = response.choices[0].message.content  # type: ignore[union-attr]
    if not raw:
        return None

    return parse_llm_json(raw)


async def call_llm_text(
    prompt: str,
    model: str,
    api_base: str | None = None,
    max_tokens: int = 500,
    temperature: float = 0.0,
) -> str | None:
    """Call an LLM and return the raw response content string.

    Same setup as :func:`call_llm_json` (kwargs building, api_base handling,
    thinking mode disable for Ollama / Nemotron / Qwen models) but returns the
    raw text without any JSON parsing.

    Args:
        prompt: The user message to send.
        model: litellm model identifier (e.g. ``ollama_chat/qwen3.5:35b-a3b``).
        api_base: Optional API base URL for vLLM / OpenAI-compatible endpoints.
        max_tokens: Maximum response tokens.
        temperature: Sampling temperature (0.0 = deterministic).

    Returns:
        Raw response content string, or ``None`` if the response is empty.

    Raises:
        Exception: On LLM call failure.  Callers should catch and degrade gracefully.
    """
    import litellm

    kwargs = _build_litellm_kwargs(prompt, model, api_base, max_tokens, temperature)
    response = await litellm.acompletion(**kwargs)

    raw = response.choices[0].message.content  # type: ignore[union-attr]
    if not raw:
        return None

    return raw
