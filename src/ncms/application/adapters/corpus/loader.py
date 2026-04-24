"""JSONL corpus loader + validator.

Every data tier (gold, LLM, SDG, adversarial) serialises to JSONL
with one :class:`GoldExample` per line.  The loader validates the
shape, rejects rows with unknown intents or slots outside the
domain's taxonomy, and returns typed :class:`GoldExample`
records.

Usage::

    from ncms.application.adapters.corpus.loader import load_jsonl
    examples = load_jsonl("corpus/gold_conversational.jsonl")
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from ncms.application.adapters.schemas import (
    ADMISSION_DECISIONS,
    DOMAINS,
    INTENT_CATEGORIES,
    ROLE_LABELS,
    SLOT_TAXONOMY,
    STATE_CHANGES,
    GoldExample,
    RoleSpan,
)


class CorpusValidationError(ValueError):
    """Raised when a JSONL row fails schema validation."""


def _validate_row(row: dict, line_no: int, path: Path) -> GoldExample:
    # Minimum required shape: text + domain.  Other fields default
    # to sensible neutrals so CTLG cue-gold rows (query-voice, no
    # intent or slots) load cleanly alongside classical gold.
    required = {"text", "domain"}
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
    # Default intent to "none" for rows that don't carry one (e.g.
    # query-voice cue-gold rows where the notion of preference
    # doesn't apply).
    intent = row.get("intent", "none")
    if intent not in INTENT_CATEGORIES:
        raise CorpusValidationError(
            f"{path}:{line_no} unknown intent {intent!r}"
        )
    # Default slots to empty dict when absent.
    slots = row.get("slots", {})
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

    # Multi-head optional labels — validate the vocabulary when
    # provided, admit as None when absent.
    admission = row.get("admission")
    if admission is not None and admission not in ADMISSION_DECISIONS:
        raise CorpusValidationError(
            f"{path}:{line_no} unknown admission {admission!r}"
        )
    state_change = row.get("state_change")
    if state_change is not None and state_change not in STATE_CHANGES:
        raise CorpusValidationError(
            f"{path}:{line_no} unknown state_change {state_change!r}"
        )
    topic = row.get("topic")
    if topic is not None and not isinstance(topic, str):
        raise CorpusValidationError(
            f"{path}:{line_no} topic must be a string, got {type(topic)!r}"
        )
    # v6/v7.x ``shape_intent`` field — silently dropped in v8.1.  Corpus
    # rows that carry the legacy field load cleanly; the field just
    # isn't threaded through to GoldExample or the training targets.

    # v7+: role_spans (role head ground-truth).  Optional; validated
    # per-entry when present.  Rows that predate v7 roll forward with
    # an empty list — training loop masks the role loss for those
    # rows (same pattern as topic/admission).
    role_spans_raw = row.get("role_spans") or []
    if not isinstance(role_spans_raw, list):
        raise CorpusValidationError(
            f"{path}:{line_no} role_spans must be a list, got "
            f"{type(role_spans_raw)!r}",
        )
    role_spans: list[RoleSpan] = []
    for idx, entry in enumerate(role_spans_raw):
        if not isinstance(entry, dict):
            raise CorpusValidationError(
                f"{path}:{line_no} role_spans[{idx}] must be a dict",
            )
        try:
            role_val = entry["role"]
            if role_val not in ROLE_LABELS:
                raise CorpusValidationError(
                    f"{path}:{line_no} role_spans[{idx}] "
                    f"unknown role {role_val!r}",
                )
            role_spans.append(RoleSpan(
                char_start=int(entry["char_start"]),
                char_end=int(entry["char_end"]),
                surface=str(entry["surface"]),
                canonical=str(entry["canonical"]),
                slot=str(entry["slot"]),
                role=role_val,
                source=str(entry.get("source", "")),
            ))
        except KeyError as exc:
            raise CorpusValidationError(
                f"{path}:{line_no} role_spans[{idx}] missing field {exc}",
            ) from exc

    # v8+ CTLG cue_tags.  Accept two serialisation shapes for
    # back-compat: the canonical ``cue_tags`` field, OR the raw
    # ``tokens`` field used by cue-labeling scripts
    # (scripts/ctlg/label_cues_llm.py, gen_gap_queries.py).  Either
    # shape is a list of {char_start, char_end, surface, cue_label,
    # confidence} dicts.  Stored on GoldExample as list[dict] —
    # training loop re-parses into TaggedToken shape.
    cue_tags_raw = row.get("cue_tags") or row.get("tokens") or []
    if not isinstance(cue_tags_raw, list):
        raise CorpusValidationError(
            f"{path}:{line_no} cue_tags/tokens must be a list",
        )
    cue_tags: list[dict] = []
    for t in cue_tags_raw:
        if not isinstance(t, dict):
            continue
        # Require the core fields; skip silently on malformed
        # entries rather than failing the whole corpus load.
        try:
            cue_tags.append({
                "char_start": int(t["char_start"]),
                "char_end": int(t["char_end"]),
                "surface": str(t["surface"]),
                "cue_label": str(t["cue_label"]),
                "confidence": float(t.get("confidence", 1.0)),
            })
        except (KeyError, TypeError, ValueError):
            continue

    return GoldExample(
        text=row["text"],
        domain=domain,
        intent=intent,
        slots={str(k): str(v) for k, v in slots.items()},
        topic=topic,
        admission=admission,
        state_change=state_change,
        role_spans=role_spans,
        cue_tags=cue_tags,
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
    """Write examples back to JSONL (for LLM-labeled + SDG outputs).

    Multi-head optional labels are emitted only when present so
    legacy corpora round-trip byte-identically.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for ex in examples:
            row: dict = {
                "text": ex.text,
                "domain": ex.domain,
                "intent": ex.intent,
                "slots": ex.slots,
                "split": ex.split,
                "source": ex.source,
                "note": ex.note,
            }
            if ex.topic is not None:
                row["topic"] = ex.topic
            if ex.admission is not None:
                row["admission"] = ex.admission
            if ex.state_change is not None:
                row["state_change"] = ex.state_change
            if ex.role_spans:
                row["role_spans"] = [
                    {
                        "char_start": s.char_start,
                        "char_end": s.char_end,
                        "surface": s.surface,
                        "canonical": s.canonical,
                        "slot": s.slot,
                        "role": s.role,
                        "source": s.source,
                    }
                    for s in ex.role_spans
                ]
            if ex.cue_tags:
                row["cue_tags"] = list(ex.cue_tags)
            fh.write(json.dumps(row) + "\n")
