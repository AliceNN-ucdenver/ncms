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

* :class:`SparkBackend` — thin ``litellm`` wrapper that calls a local
  Spark Nemotron endpoint.  JSON-list response, parsed via the
  existing ``infrastructure/llm/json_utils`` helpers.  Used for the
  Phase B'.4 live run.

The :class:`LLMBackend` protocol is intentionally narrow — one method.
Swapping in OpenAI / Ollama / Anthropic is a ~20-line backend
implementation.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMBackend(Protocol):
    """Produce ``n`` text rows from one prompt.

    Implementations should be deterministic when handed a seeded
    :class:`random.Random` (via their constructor or a seed in the
    prompt).  The generator never retries — each backend call is
    the final answer.  Failures should raise, not silently return
    empty lists, so the caller can decide whether to abort.
    """

    def generate(self, prompt: str, *, n: int, rng: random.Random) -> list[str]:
        ...


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
    template: str, rng: random.Random, seen_filled: dict[str, str],
) -> str:
    """Replace every ``{name}`` placeholder not already in ``seen_filled``.

    We memoise per-row so "rationale" picked in the first phrasing
    line stays consistent if referenced a second time.  Unknown
    placeholders get a bland default (``"…"``) rather than raising —
    we'd rather emit a slightly awkward row than crash the batch.
    """
    import re

    def repl(match: "re.Match[str]") -> str:
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
        self, prompt: str, *, n: int, rng: random.Random,  # noqa: ARG002
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
# SparkBackend — live LLM via litellm.
# ---------------------------------------------------------------------------


@dataclass
class SparkBackend:
    """Call a Spark Nemotron (or any OpenAI-compatible) endpoint for
    real generation.  Parses a JSON array of strings response.

    Retries are **NOT** handled here — the generator decides.  A single
    failed batch raises :class:`RuntimeError` so the caller sees
    the problem immediately instead of a silent short-count.

    Parameters
    ----------
    model : str
        litellm model id (e.g. ``"openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"``).
    api_base : str | None
        Endpoint for self-hosted vLLM / Ollama.  ``None`` uses the
        model's default (OpenAI / Anthropic / etc.).
    temperature : float
        Sampling temperature.  0.7–1.0 for creative generation.
    max_tokens : int
        Response cap — keep generous so long examples aren't truncated.
    request_timeout : float
        Per-call timeout in seconds.
    """

    model: str
    api_base: str | None = None
    temperature: float = 0.8
    max_tokens: int = 1024
    request_timeout: float = 120.0
    extra_kwargs: dict[str, object] = field(default_factory=dict)

    def generate(
        self, prompt: str, *, n: int, rng: random.Random,  # noqa: ARG002
    ) -> list[str]:
        import litellm  # lazy — tests mock this out

        from ncms.infrastructure.llm.json_utils import parse_llm_json

        kwargs: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.request_timeout,
            **self.extra_kwargs,
        }
        if self.api_base:
            kwargs["api_base"] = self.api_base
        # Ollama local models prefer thinking=False for clean JSON.
        if self.model.startswith("ollama"):
            kwargs["think"] = False

        response = litellm.completion(**kwargs)
        content: str = response["choices"][0]["message"]["content"]  # type: ignore[index]
        parsed = parse_llm_json(content)
        if not isinstance(parsed, list):
            raise RuntimeError(
                f"SparkBackend: expected JSON list, got {type(parsed).__name__}",
            )
        rows = parsed
        # Keep only string rows; drop anything non-textual.
        text_rows = [str(r).strip() for r in rows if isinstance(r, (str, int, float))]
        # Cap to requested count — the model occasionally overshoots.
        return text_rows[:n]


_SYSTEM_PROMPT = """You generate training data for a small language model.
Follow every instruction in the user prompt exactly.
Always respond with a JSON array of strings — nothing else.
Each string is one training row.  No keys, no commentary, no markdown."""


__all__ = [
    "LLMBackend",
    "SparkBackend",
    "TemplateBackend",
]
