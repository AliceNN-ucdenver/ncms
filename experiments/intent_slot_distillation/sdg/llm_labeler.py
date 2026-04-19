"""LLM-based labeler for real corpus data.

Takes a newline-delimited text file of raw utterances and asks an
LLM (Qwen / Nemotron via the existing Spark endpoint or Ollama)
to return structured ``(intent, slots)`` labels.  Output is
JSONL-compatible with :func:`load_jsonl`.

Why a separate labeler:

* **Real distribution.**  Template SDG is fast but produces
  phrasings biased toward the template authors (us).  LLM
  labeling over real utterances captures natural variation.
* **Different model from training.**  To avoid train/test
  contamination we label with a *different model family* from
  whatever we end up fine-tuning.  Default here is Qwen 3.5 via
  Ollama; swap via ``--model`` / ``--api-base``.
* **Calibration against gold.**  Before trusting bulk LLM labels,
  run against the gold set and audit agreement — see
  ``--calibrate``.

Usage::

    # Label a real corpus file (one utterance per line)
    uv run python -m experiments.intent_slot_distillation.sdg.llm_labeler \\
        --input /path/to/utterances.txt --domain conversational \\
        --output corpus/llm_conversational.jsonl

    # Calibration run against gold
    uv run python -m experiments.intent_slot_distillation.sdg.llm_labeler \\
        --calibrate corpus/gold_conversational.jsonl --domain conversational
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from experiments.intent_slot_distillation.corpus.loader import (
    dump_jsonl,
    load_jsonl,
)
from experiments.intent_slot_distillation.schemas import (
    DOMAINS,
    INTENT_CATEGORIES,
    SLOT_TAXONOMY,
    Domain,
    GoldExample,
    Intent,
)

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "You label utterances for intent and slot extraction. Return ONE JSON "
    "object with fields 'intent' and 'slots'. Do not return any other text."
)


def _user_prompt(text: str, domain: Domain) -> str:
    slot_names = ", ".join(SLOT_TAXONOMY[domain] + ("object",))
    intents = ", ".join(INTENT_CATEGORIES)
    return (
        f"Domain: {domain}\n"
        f"Allowed intents: {intents}\n"
        f"Allowed slot keys: {slot_names}\n"
        f"Utterance: {text!r}\n"
        "Respond ONLY with a JSON object like "
        '{"intent": "<one of the allowed intents>", '
        '"slots": {"<slot_key>": "<surface form>"}}.'
    )


def _call_llm(
    text: str,
    domain: Domain,
    model: str,
    api_base: str | None,
) -> dict[str, Any] | None:
    """Call the LLM endpoint and parse the first JSON object in the reply.

    Returns ``None`` on any failure — caller skips those rows.
    """
    try:
        import litellm  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "llm_labeler requires litellm. Install via `uv sync`."
        ) from exc

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(text, domain)},
        ],
        "temperature": 0.0,
        "max_tokens": 256,
    }
    if api_base:
        kwargs["api_base"] = api_base
    if model.startswith("ollama"):
        kwargs["think"] = False  # Ollama thinking-mode adds noise
    try:
        response = litellm.completion(**kwargs)
    except Exception as exc:  # pragma: no cover — live endpoint
        logger.warning("LLM call failed for %r: %s", text[:80], exc)
        return None
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    # Scan for the first JSON object (LLMs sometimes wrap in markdown).
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(content[start:end + 1])
    except json.JSONDecodeError:
        return None


def _parse_to_example(
    reply: dict[str, Any],
    text: str,
    domain: Domain,
    source: str,
) -> GoldExample | None:
    intent = reply.get("intent", "").strip()
    if intent not in INTENT_CATEGORIES:
        return None
    raw_slots = reply.get("slots") or {}
    if not isinstance(raw_slots, dict):
        return None
    allowed = set(SLOT_TAXONOMY[domain]) | {"object"}
    slots = {
        str(k): str(v)
        for k, v in raw_slots.items()
        if k in allowed and v
    }
    return GoldExample(
        text=text,
        domain=domain,
        intent=intent,
        slots=slots,
        split="llm",
        source=source,
    )


def label_utterances(
    utterances: list[str],
    domain: Domain,
    *,
    model: str,
    api_base: str | None,
) -> list[GoldExample]:
    """Label ``utterances`` one by one; skip rows the LLM couldn't parse."""
    out: list[GoldExample] = []
    source = f"llm-labeled model={model}"
    for text in utterances:
        reply = _call_llm(text, domain, model, api_base)
        if reply is None:
            continue
        example = _parse_to_example(reply, text, domain, source)
        if example is not None:
            out.append(example)
    return out


def calibrate(
    gold_path: Path,
    *,
    model: str,
    api_base: str | None,
) -> None:
    """Audit LLM agreement with hand-labeled gold.

    Runs the labeler against every gold example and reports intent
    accuracy + slot overlap.  Prints a report — doesn't write data.
    """
    gold = load_jsonl(gold_path)
    domain = gold[0].domain if gold else None
    if domain is None:
        print("[calibrate] empty gold file")
        return
    correct_intent = 0
    slot_overlap_total = 0
    slot_overlap_correct = 0
    for ex in gold:
        reply = _call_llm(ex.text, domain, model, api_base)
        if reply is None:
            continue
        predicted = _parse_to_example(reply, ex.text, domain, "calibrate")
        if predicted is None:
            continue
        if predicted.intent == ex.intent:
            correct_intent += 1
        for key, value in ex.slots.items():
            slot_overlap_total += 1
            if predicted.slots.get(key, "").lower() == value.lower():
                slot_overlap_correct += 1
    n = len(gold)
    print(f"[calibrate] gold n={n}")
    print(f"[calibrate] intent accuracy: {correct_intent}/{n} = {correct_intent/n:.2%}")
    if slot_overlap_total:
        print(
            f"[calibrate] slot overlap: "
            f"{slot_overlap_correct}/{slot_overlap_total} = "
            f"{slot_overlap_correct/slot_overlap_total:.2%}"
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="LLM-based labeler for the intent+slot experiment",
    )
    parser.add_argument(
        "--input", type=Path,
        help="Newline-delimited utterances file (one per line).",
    )
    parser.add_argument(
        "--output", type=Path,
        help="JSONL output path (for label mode).",
    )
    parser.add_argument(
        "--calibrate", type=Path,
        help="Run against a gold JSONL file and print accuracy.",
    )
    parser.add_argument("--domain", required=True, choices=DOMAINS)
    parser.add_argument(
        "--model",
        default=os.environ.get(
            "TLG_LABEL_MODEL",
            "openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16",
        ),
    )
    parser.add_argument(
        "--api-base",
        default=os.environ.get(
            "TLG_LABEL_API_BASE", "http://spark-ee7d.local:8000/v1",
        ),
    )
    args = parser.parse_args()

    if args.calibrate is not None:
        calibrate(args.calibrate, model=args.model, api_base=args.api_base)
        return

    if args.input is None or args.output is None:
        parser.error("--input and --output required in label mode")

    utterances = [
        line.strip() for line in args.input.read_text().splitlines() if line.strip()
    ]
    labeled = label_utterances(
        utterances, args.domain, model=args.model, api_base=args.api_base,
    )
    dump_jsonl(labeled, args.output)
    print(
        f"[llm-labeler] domain={args.domain} "
        f"input={len(utterances)} labeled={len(labeled)} → {args.output}"
    )


if __name__ == "__main__":
    main()
