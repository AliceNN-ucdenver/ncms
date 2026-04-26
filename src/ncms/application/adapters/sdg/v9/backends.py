"""v9 generation backends — pluggable LLM (or mock) producers.

A backend consumes one fully-rendered prompt and returns ``n`` raw
text rows.  The generator orchestrates prompt construction + role
labelling + validation around whatever backend is plugged in.

Two concrete backends ship:

* :class:`TemplateBackend` — zero-LLM, phrasings-driven, deterministic.
  Used for dry-runs, CI, and coverage smoke tests.  Rotates through
  the archetype's ``phrasings`` list and fills free-text placeholders
  (``{condition}``, ``{rationale}``, etc.) from a small canned pool.
  Not a replacement for real generation — the language is templated
  and limited — but sufficient to verify the plumbing end-to-end.

* :class:`SparkBackend` — delegates to
  :func:`ncms.infrastructure.llm.caller.call_llm_json`, which already
  handles the three Nemotron/vLLM quirks we need:

    1. ``api_key = "na"`` so litellm doesn't reject the ``openai/``
       prefix against a self-hosted vLLM endpoint;
    2. ``extra_body.chat_template_kwargs.enable_thinking = False``
       so Nemotron does NOT emit ``<think>...</think>`` tokens inside
       the response content;
    3. JSON fence / reasoning-preamble stripping via
       :func:`parse_llm_json`.

  Adds retry-with-backoff and short-count warnings on top of the
  shared caller.

The :class:`LLMBackend` protocol is intentionally narrow — one method.
Swapping in OpenAI / Ollama / Anthropic is a ~20-line backend
implementation.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class LLMBackend(Protocol):
    """Produce ``n`` text rows from one prompt.

    Implementations should be deterministic when handed a seeded
    :class:`random.Random` (via their constructor or a seed in the
    prompt).  The generator never retries — each backend call is
    the final answer.  Failures should raise, not silently return
    empty lists, so the caller can decide whether to abort.
    """

    def generate(self, prompt: str, *, n: int, rng: random.Random) -> list[str]: ...


# ---------------------------------------------------------------------------
# Canned free-text fillers used by TemplateBackend.
# ---------------------------------------------------------------------------

# Placeholder name → plausible filler pool.  Keep these pools small
# and generic: TemplateBackend is for plumbing verification, not
# realistic corpus generation.  The real variety comes from SparkBackend.
_FREE_TEXT_FILLERS: dict[str, tuple[str, ...]] = {
    "condition": (
        "newly diagnosed hypertension",
        "type 2 diabetes",
        "mild depression",
        "acute sinusitis",
        "chronic pain flare",
    ),
    "rationale": (
        "side effects",
        "better tolerance",
        "cost considerations",
        "insurance formulary change",
        "prior PCP recommendation",
    ),
    "outcome": (
        "symptoms resolved",
        "no improvement at 4 weeks",
        "patient preference",
        "adverse reaction resolved",
        "titrated to effect",
    ),
    "finding": (
        "no acute abnormalities",
        "mild chronic changes",
        "unremarkable",
        "borderline findings requiring follow-up",
        "stable compared to prior",
    ),
    "duration": (
        "for 3 weeks",
        "since last visit",
        "over the past month",
        "ongoing for several days",
        "intermittently for 2 weeks",
    ),
    "severity": (
        "mild",
        "moderate",
        "severe",
        "worsening",
        "stable",
    ),
    "context": (
        "routine",
        "morning schedule",
        "weekend",
        "daily practice",
        "weekly rotation",
    ),
    "area": (
        "the backend service",
        "the data pipeline",
        "the frontend",
        "our CI",
        "deployment tooling",
    ),
    "aside": (
        "(just FYI)",
        "— more details in the PR",
        "after the team retro",
        "as of this sprint",
        "pending review",
    ),
    "role": (
        "as a team lead",
        "on the platform squad",
        "as an engineer",
        "in the architecture group",
        "on the data team",
    ),
}


def _fill_free_text(
    template: str,
    rng: random.Random,
    seen_filled: dict[str, str],
) -> str:
    """Replace every ``{name}`` placeholder not already in ``seen_filled``.

    We memoise per-row so "rationale" picked in the first phrasing
    line stays consistent if referenced a second time.  Unknown
    placeholders get a bland default (``"…"``) rather than raising —
    we'd rather emit a slightly awkward row than crash the batch.
    """
    import re

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in seen_filled:
            return seen_filled[name]
        pool = _FREE_TEXT_FILLERS.get(name)
        value = rng.choice(pool) if pool else "…"
        seen_filled[name] = value
        return value

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", repl, template)


# ---------------------------------------------------------------------------
# TemplateBackend — deterministic, no LLM.
# ---------------------------------------------------------------------------


@dataclass
class TemplateBackend:
    """Phrasings-driven filler.  Expects pre-filled ``{primary}``,
    ``{alternative}``, ``{casual}``, ``{frequency}``, ``{severity}`` etc.
    entity slots (handled by :mod:`generator`); fills free-text
    placeholders from :data:`_FREE_TEXT_FILLERS`.

    The prompt passed in by the generator is ignored — TemplateBackend
    works directly off pre-rendered phrasing strings that the generator
    hands in via ``extra={"phrasings": [...]}`` on the call site.  We
    keep the ``generate(prompt, n, rng)`` signature consistent with
    :class:`LLMBackend` so the generator can switch backends without
    changing its call sites.
    """

    phrasings: tuple[str, ...] = ()

    def generate(
        self,
        prompt: str,
        *,
        n: int,
        rng: random.Random,  # noqa: ARG002
    ) -> list[str]:
        if not self.phrasings:
            return []
        out: list[str] = []
        for i in range(n):
            template = self.phrasings[i % len(self.phrasings)]
            seen: dict[str, str] = {}
            out.append(_fill_free_text(template, rng, seen))
        return out


# ---------------------------------------------------------------------------
# SparkBackend — live LLM via the shared call_llm_json helper.
# ---------------------------------------------------------------------------


@dataclass
class SparkBackend:
    """Call a Spark Nemotron (or any OpenAI-compatible) endpoint for
    real generation.  Parses a JSON array of strings.

    Delegates the litellm call to
    :func:`ncms.infrastructure.llm.caller.call_llm_json` so the
    Nemotron/vLLM-specific kwargs (``api_key="na"``,
    ``enable_thinking=False``, reasoning-preamble stripping) stay
    in exactly one place.

    Retries with exponential backoff on transient failures (timeout,
    5xx, malformed JSON).  A short-count — the model returns fewer
    rows than requested — is logged at WARNING but NOT retried,
    because re-asking usually produces the same short count and
    the generator's own retry loop will refill the shortfall from a
    subsequent batch with a different entity sample.

    The backend is driven synchronously from the generator loop.
    Each ``generate`` call opens a fresh asyncio event loop to
    invoke ``call_llm_json`` — this is intentional: the generator
    stays CPU-bound/sync, and switching to concurrent batches
    would be a separate ``generate_domain_concurrent`` entry point
    (not in scope for B'.4).

    Parameters
    ----------
    model : str
        litellm model id (e.g.
        ``"openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"``).
    api_base : str | None
        Endpoint for self-hosted vLLM.  ``None`` means use the
        model provider's default (OpenAI / Anthropic / etc.).
    temperature : float
        Sampling temperature.  0.6–0.9 for creative generation;
        keep modest because the JSON-list output format is
        fragile at very high temperatures.
    max_tokens : int
        Response token cap.  Set generously — at batch_size=10
        with 180-char rows + JSON scaffolding, Nemotron needs
        ~800 tokens.  Default 1500 leaves headroom.
    max_attempts : int
        Total attempts per batch before giving up (1 = no retry).
    backoff_base_seconds : float
        Initial sleep between retries; doubled on each retry.
    """

    model: str
    api_base: str | None = None
    temperature: float = 0.8
    max_tokens: int = 1500
    max_attempts: int = 3
    backoff_base_seconds: float = 2.0
    extra_kwargs: dict[str, object] = field(default_factory=dict)

    def generate(
        self,
        prompt: str,
        *,
        n: int,
        rng: random.Random,  # noqa: ARG002
    ) -> list[str]:
        """Return up to ``n`` text rows from the LLM, with retries.

        Raises :class:`RuntimeError` only when every retry attempt
        failed — a successful call that returns fewer than ``n``
        rows is NOT an error (logged at WARNING and returned
        truncated; the generator's outer loop re-fills from another
        batch).
        """
        full_prompt = _SYSTEM_PROMPT + "\n\n" + prompt
        last_err: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                parsed = asyncio.run(self._call_once(full_prompt))
            except Exception as exc:  # noqa: BLE001 — we log + retry
                last_err = exc
                if attempt < self.max_attempts:
                    delay = self.backoff_base_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        "SparkBackend attempt %d/%d failed (%s); retrying in %.1fs",
                        attempt,
                        self.max_attempts,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise RuntimeError(
                    f"SparkBackend: all {self.max_attempts} attempts failed; last error: {exc}",
                ) from exc
            # Success path — coerce + return.
            if not isinstance(parsed, list):
                # Treat "not a list" as a retryable malformed response.
                last_err = RuntimeError(
                    f"expected JSON list, got {type(parsed).__name__}",
                )
                if attempt < self.max_attempts:
                    delay = self.backoff_base_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        "SparkBackend attempt %d/%d malformed (%s); retrying in %.1fs",
                        attempt,
                        self.max_attempts,
                        last_err,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise RuntimeError(
                    f"SparkBackend: all {self.max_attempts} attempts malformed; last: {last_err}",
                )
            # Keep only textual rows; drop anything non-scalar.
            rows = [
                str(r).strip()
                for r in parsed
                if isinstance(r, (str, int, float)) and str(r).strip()
            ]
            if len(rows) < n:
                logger.warning(
                    "SparkBackend short-count: asked for %d, got %d (model=%s)",
                    n,
                    len(rows),
                    self.model,
                )
            # Cap — the model occasionally overshoots our ask.
            return rows[:n]
        # Unreachable (loop either returns or raises) — keeps mypy happy.
        raise RuntimeError(
            f"SparkBackend: exhausted retries without verdict (last={last_err})",
        )

    async def _call_once(self, full_prompt: str) -> object:
        """One invocation of the shared LLM caller — extracted so
        tests can patch it without touching the retry loop.
        """
        from ncms.infrastructure.llm.caller import call_llm_json

        return await call_llm_json(
            full_prompt,
            model=self.model,
            api_base=self.api_base,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )


_SYSTEM_PROMPT = """You generate training data for a small language model.
Follow every instruction in the user prompt exactly.
Always respond with a JSON array of strings — nothing else.
Each string is one training row.  No keys, no commentary, no markdown."""


__all__ = [
    "LLMBackend",
    "SparkBackend",
    "TemplateBackend",
]
