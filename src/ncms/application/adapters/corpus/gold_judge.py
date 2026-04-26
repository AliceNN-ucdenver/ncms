"""LLM-based gold-quality judge.

Sample N rows from a gold JSONL, ask a judge LLM to grade each
row's slot / state_change / intent labels, and aggregate:

  - correct          (labels match what the content says)
  - partially_wrong  (some labels OK, some wrong)
  - severely_wrong   (most labels wrong or missing)

Goal: catch data quality problems BEFORE the 20-minute retrain
gate-fail cycle.

The judge prompt shows the judge the content + the proposed labels
and asks for a structured verdict per slot, plus a recommended
correction when the slot is wrong.  Output both aggregate numbers
and per-row reports so the operator can fix prompts / canonical
maps for systematic failure modes.
"""

from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path

from ncms.application.adapters.corpus.loader import load_jsonl
from ncms.application.adapters.schemas import (
    SLOT_TAXONOMY,
    Domain,
    GoldExample,
)
from ncms.infrastructure.llm.caller import call_llm_json

logger = logging.getLogger(__name__)


_VERDICT_PROMPT = """You are a strict but FAIR data-quality judge for multi-head SLM labels.

Domain: {domain}
Allowed slot labels: {slot_labels}

CONTENT:
\"\"\"
{content}
\"\"\"

PROPOSED LABELS:
  intent: {intent}
  state_change: {state_change}
  slots: {slots}

Judging rules — apply strictly but do NOT invent complaints:

  LITERAL PRESENCE: A slot value "literally appears" when the exact string
  (case-insensitive, allowing small punctuation differences like "SvelteKit"
  vs "Sveltekit" or "Google Cloud Platform" vs "google cloud platform") is
  in the content.  Before flagging a value as absent, re-scan the content
  carefully.  Do NOT flag values that ARE in the content.

  SLOT SEMANTICS:
    - library    = imported code dep that is NOT a framework
    - framework  = opinionated app framework (Django, React, Rails…)
    - language   = programming language (Python, Rust, Go…)
    - database   = data store / cache / queue / search index
    - platform   = orchestration / cloud runtime (Docker, AWS, K8s…)
    - tool       = dev-time tooling (ruff, pytest, Selenium, VS Code…)
    - pattern    = NAMED architectural pattern (async, CQRS, microservices…)
                   Generic adjectives ("overkill", "scalable") are WRONG.
    - alternative = explicit X-vs-Y contrast partner only
    - frequency   = time-interval expression ONLY

  STATE_CHANGE:
    - declaration = requires explicit new-state language in content
                    ("we have decided", "adopted", "going forward we will use",
                     "effective immediately", "Decided on X")
    - retirement  = requires explicit retirement language
                    ("deprecated", "migrated away", "replaced by", "sunset")
    - none        = default

  INTENT:
    - choice = requires X-vs-Y contrast IN THE CONTENT
    - others (positive / negative / habitual / difficulty) need first-person
      preference-expressing language
    - none = default

Verdict levels:
  - "correct"         = labels are faithful to content.  Tolerate minor
                        capitalisation / punctuation differences on slot values.
  - "partially_wrong" = 1-2 labels are wrong / extra / missing, but the rest
                        are fine.
  - "severely_wrong"  = most labels don't match the content.

Return ONLY a JSON object (no prose, no markdown fences):
{{
  "verdict": "correct" | "partially_wrong" | "severely_wrong",
  "issues": ["short one-line description of each mistake, empty list if correct"],
  "corrections": {{"<slot_or_head>": "<suggested_value_or_remove>"}}
}}
"""


def _format_slots(slots: dict[str, str]) -> str:
    if not slots:
        return "{} (empty)"
    return ", ".join(f"{k}={v!r}" for k, v in slots.items())


async def _judge_one(
    ex: GoldExample,
    *,
    domain: Domain,
    model: str,
    api_base: str | None,
) -> dict | None:
    prompt = _VERDICT_PROMPT.format(
        domain=domain,
        slot_labels=list(SLOT_TAXONOMY[domain]),
        content=ex.text.strip()[:600],
        intent=ex.intent,
        state_change=ex.state_change,
        slots=_format_slots(ex.slots or {}),
    )
    try:
        result = await call_llm_json(
            prompt=prompt,
            model=model,
            api_base=api_base,
            max_tokens=400,
            temperature=0.0,
        )
    except Exception as exc:
        logger.warning("judge failed on text[:60]=%r: %s", ex.text[:60], exc)
        return None
    if not isinstance(result, dict):
        return None
    verdict = str(result.get("verdict") or "").lower()
    if verdict not in ("correct", "partially_wrong", "severely_wrong"):
        verdict = "severely_wrong"
    return {
        "verdict": verdict,
        "issues": list(result.get("issues") or []),
        "corrections": dict(result.get("corrections") or {}),
    }


async def judge_gold(
    *,
    domain: Domain,
    gold_path: Path,
    n_samples: int,
    model: str,
    api_base: str | None,
    seed: int = 42,
) -> dict:
    """Judge ``n_samples`` rows drawn from ``gold_path``.

    Returns::

      {
        "n_sampled": int,
        "verdicts": {"correct": int, "partially_wrong": int, "severely_wrong": int},
        "pct_correct": float,
        "issue_histogram": {issue_string: count},
        "failures": [ {text, slots, verdict, issues, corrections}, ... ],
      }
    """
    rows = load_jsonl(gold_path)
    with_content = [r for r in rows if r.text.strip()]
    rng = random.Random(seed)
    sample = rng.sample(with_content, min(n_samples, len(with_content)))

    verdicts = {"correct": 0, "partially_wrong": 0, "severely_wrong": 0}
    failures: list[dict] = []
    issue_hist: dict[str, int] = {}

    for i, ex in enumerate(sample, 1):
        j = await _judge_one(ex, domain=domain, model=model, api_base=api_base)
        if j is None:
            verdicts["severely_wrong"] += 1
            continue
        verdicts[j["verdict"]] += 1
        for issue in j["issues"]:
            # Normalize leading colon/brackets for histogramming.
            issue_hist[issue.strip()] = issue_hist.get(issue.strip(), 0) + 1
        if j["verdict"] != "correct":
            failures.append(
                {
                    "text": ex.text[:200],
                    "slots": ex.slots,
                    "state_change": ex.state_change,
                    "intent": ex.intent,
                    "verdict": j["verdict"],
                    "issues": j["issues"],
                    "corrections": j["corrections"],
                }
            )
        if i % 10 == 0:
            logger.info(
                "[judge-gold] %s: judged %d / %d (correct=%d)",
                domain,
                i,
                len(sample),
                verdicts["correct"],
            )

    total = sum(verdicts.values()) or 1
    return {
        "n_sampled": len(sample),
        "verdicts": verdicts,
        "pct_correct": verdicts["correct"] / total * 100,
        "issue_histogram": dict(
            sorted(
                issue_hist.items(),
                key=lambda x: -x[1],
            )
        ),
        "failures": failures,
    }


def sync_judge_gold(**kwargs) -> dict:
    return asyncio.run(judge_gold(**kwargs))


__all__ = ["judge_gold", "sync_judge_gold"]
