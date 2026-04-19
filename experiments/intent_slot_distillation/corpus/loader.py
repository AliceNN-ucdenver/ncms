"""JSONL corpus loader + validator.

Every data tier (gold, LLM, SDG, adversarial) serialises to JSONL
with one :class:`GoldExample` per line.  The loader validates the
shape, rejects rows with unknown intents or slots outside the
domain's taxonomy, and returns typed :class:`GoldExample`
records.

Usage::

    from experiments.intent_slot_distillation.corpus.loader import load_jsonl
    examples = load_jsonl("corpus/gold_conversational.jsonl")
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from experiments.intent_slot_distillation.schemas import (
    DOMAINS,
    INTENT_CATEGORIES,
    SLOT_TAXONOMY,
    GoldExample,
)


class CorpusValidationError(ValueError):
    """Raised when a JSONL row fails schema validation."""


def _validate_row(row: dict, line_no: int, path: Path) -> GoldExample:
    required = {"text", "domain", "intent", "slots"}
    missing = required - row.keys()
    if missing:
        raise CorpusValidationError(
            f"{path}:{line_no} missing required fields {sorted(missing)}"
        )
    domain = row["domain"]
    if domain not in DOMAINS:
        raise CorpusValidationError(
            f"{path}:{line_no} unknown domain {domain!r}"
        )
    intent = row["intent"]
    if intent not in INTENT_CATEGORIES:
        raise CorpusValidationError(
            f"{path}:{line_no} unknown intent {intent!r}"
        )
    slots = row["slots"]
    if not isinstance(slots, dict):
        raise CorpusValidationError(
            f"{path}:{line_no} slots must be a dict, got {type(slots)!r}"
        )
    allowed_slots = set(SLOT_TAXONOMY[domain])
    # ``object`` is a catch-all slot for the conversational domain;
    # admit it across every domain for back-compat.
    allowed_slots.add("object")
    unknown = set(slots.keys()) - allowed_slots
    if unknown:
        raise CorpusValidationError(
            f"{path}:{line_no} slots {sorted(unknown)} not in "
            f"{domain} taxonomy {sorted(allowed_slots)}"
        )
    split = row.get("split", "gold")
    if split not in {"gold", "llm", "sdg", "adversarial"}:
        raise CorpusValidationError(
            f"{path}:{line_no} unknown split {split!r}"
        )
    return GoldExample(
        text=row["text"],
        domain=domain,
        intent=intent,
        slots={str(k): str(v) for k, v in slots.items()},
        split=split,
        source=row.get("source", ""),
        note=row.get("note", ""),
    )


def load_jsonl(path: str | Path) -> list[GoldExample]:
    """Load + validate a JSONL file of :class:`GoldExample` rows."""
    path = Path(path)
    out: list[GoldExample] = []
    with path.open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CorpusValidationError(
                    f"{path}:{line_no} invalid JSON: {exc}"
                ) from exc
            out.append(_validate_row(row, line_no, path))
    return out


def load_all(
    directory: str | Path, split: str | None = None,
) -> list[GoldExample]:
    """Load every JSONL file under ``directory``; optionally filter
    by split ("gold" / "llm" / "sdg" / "adversarial").
    """
    directory = Path(directory)
    examples: list[GoldExample] = []
    for path in sorted(directory.glob("*.jsonl")):
        examples.extend(load_jsonl(path))
    if split is not None:
        examples = [e for e in examples if e.split == split]
    return examples


def dump_jsonl(
    examples: Iterable[GoldExample], path: str | Path,
) -> None:
    """Write examples back to JSONL (for LLM-labeled + SDG outputs)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for ex in examples:
            fh.write(json.dumps({
                "text": ex.text,
                "domain": ex.domain,
                "intent": ex.intent,
                "slots": ex.slots,
                "split": ex.split,
                "source": ex.source,
                "note": ex.note,
            }) + "\n")
