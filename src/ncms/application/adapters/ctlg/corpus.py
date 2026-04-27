"""CTLG cue-tag corpus schema, loader, and diagnostics.

This loader is intentionally separate from the v9 five-head SLM corpus
loader.  CTLG rows train a dedicated sequence tagger, so their required
shape is token-level BIO labels rather than pooled classification labels.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast, get_args

from ncms.application.adapters.schemas import DOMAINS, Domain
from ncms.domain.tlg.cue_taxonomy import CUE_LABELS, CueLabel
from ncms.domain.tlg.semantic_parser import TLGAxis, TLGRelation

CTLGVoice = Literal["query", "memory"]
CTLGSplit = Literal["train", "dev", "test", "gold", "llm", "sdg", "adversarial"]

_VALID_VOICES: frozenset[str] = frozenset(("query", "memory"))
_VALID_SPLITS: frozenset[str] = frozenset(
    ("train", "dev", "test", "gold", "llm", "sdg", "adversarial"),
)
_VALID_CUE_LABELS: frozenset[str] = frozenset(CUE_LABELS)
_VALID_TLG_AXES: frozenset[str] = frozenset(get_args(TLGAxis))
_VALID_TLG_RELATIONS: frozenset[str] = frozenset(get_args(TLGRelation))
_SURFACE_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[._/#:+-][A-Za-z0-9]+)*|[^\w\s]")


class CTLGCorpusError(ValueError):
    """Raised when a CTLG corpus row fails validation."""


@dataclass(frozen=True)
class CTLGDiagnostic:
    """One validation diagnostic with file/line provenance."""

    path: str
    line_no: int
    code: str
    message: str

    def format(self) -> str:
        return f"{self.path}:{self.line_no} {self.code}: {self.message}"


@dataclass(frozen=True)
class CTLGExpectedQuery:
    """Intended cue-to-query composition for query-side CTLG rows."""

    axis: str
    relation: str
    referent: str | None = None
    secondary: str | None = None
    subject: str | None = None
    scope: str | None = None
    depth: int = 1
    scenario: str | None = None
    temporal_anchor: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "relation": self.relation,
            "referent": self.referent,
            "secondary": self.secondary,
            "subject": self.subject,
            "scope": self.scope,
            "depth": self.depth,
            "scenario": self.scenario,
            "temporal_anchor": self.temporal_anchor,
        }


@dataclass(frozen=True)
class CTLGExample:
    """One cue-tagged CTLG training/evaluation row."""

    text: str
    tokens: tuple[str, ...]
    cue_tags: tuple[CueLabel, ...]
    char_offsets: tuple[tuple[int, int], ...]
    domain: Domain
    voice: CTLGVoice
    split: CTLGSplit
    source: str = ""
    note: str = ""
    expected_tlg_query: CTLGExpectedQuery | None = None


@dataclass(frozen=True)
class CTLGValidationReport:
    """Corpus validation result with aggregate diagnostics."""

    examples: tuple[CTLGExample, ...] = ()
    diagnostics: tuple[CTLGDiagnostic, ...] = ()
    rows_seen: int = 0
    by_domain: dict[str, int] = field(default_factory=dict)
    by_voice: dict[str, int] = field(default_factory=dict)
    by_split: dict[str, int] = field(default_factory=dict)
    by_label: dict[str, int] = field(default_factory=dict)
    by_cue_family: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.diagnostics

    def summary(self) -> str:
        if self.ok:
            return f"ctlg corpus OK: {len(self.examples)} rows"
        return f"ctlg corpus FAILED: {len(self.diagnostics)} diagnostics / {self.rows_seen} rows"


def _error(path: Path, line_no: int, code: str, message: str) -> CTLGCorpusError:
    return CTLGCorpusError(f"{path}:{line_no} {code}: {message}")


def _require_fields(row: dict[str, Any], path: Path, line_no: int) -> None:
    required = {"text", "tokens", "cue_tags", "domain", "voice", "split"}
    missing = required - row.keys()
    if missing:
        raise _error(path, line_no, "schema.missing", f"missing fields {sorted(missing)}")


def _normalize_legacy_tagged_tokens(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy ``tokens=[TaggedToken dict...]`` rows.

    Early CTLG corpora stored cue labels inside token dictionaries.  Some of
    those rows also have overlapping spans from markdown/punctuation handling,
    so the compatibility path makes local surface tokenization authoritative
    and projects legacy labels onto it by maximum character overlap.
    """
    text = row.get("text")
    raw_tokens = row.get("tokens")
    if "cue_tags" in row or not isinstance(text, str) or not isinstance(raw_tokens, list):
        return row
    if not raw_tokens or not all(isinstance(token, dict) for token in raw_tokens):
        return row

    canonical = tuple(
        (match.group(0), (match.start(), match.end())) for match in _SURFACE_TOKEN_RE.finditer(text)
    )
    if not canonical:
        return row

    labels = _repair_bio(
        tuple(
            _legacy_label_for_offset(offset, raw_tokens)
            for _surface, offset in canonical
        )
    )
    normalized = dict(row)
    normalized["tokens"] = [surface for surface, _offset in canonical]
    normalized["cue_tags"] = list(labels)
    normalized["char_offsets"] = [
        {"char_start": start, "char_end": end} for _surface, (start, end) in canonical
    ]
    return normalized


def _legacy_label_for_offset(offset: tuple[int, int], raw_tokens: list[Any]) -> str:
    start, end = offset
    best_label = "O"
    best_overlap = 0
    for raw in raw_tokens:
        if not isinstance(raw, dict):
            continue
        raw_start_value = raw.get("char_start", raw.get("start"))
        raw_end_value = raw.get("char_end", raw.get("end"))
        if raw_start_value is None or raw_end_value is None:
            continue
        try:
            raw_start = int(raw_start_value)
            raw_end = int(raw_end_value)
        except (TypeError, ValueError):
            continue
        overlap = max(0, min(end, raw_end) - max(start, raw_start))
        if overlap <= best_overlap:
            continue
        label = str(raw.get("cue_label", "O"))
        best_label = label if label in _VALID_CUE_LABELS else "O"
        best_overlap = overlap
    return best_label


def _repair_bio(labels: tuple[str, ...]) -> tuple[CueLabel, ...]:
    repaired: list[CueLabel] = []
    previous_type: str | None = None
    for label in labels:
        if label == "O" or "-" not in label:
            repaired.append("O")
            previous_type = None
            continue
        prefix, cue_type = label.split("-", 1)
        if prefix == "I" and previous_type != cue_type:
            label = f"B-{cue_type}"
        elif prefix not in {"B", "I"}:
            repaired.append("O")
            previous_type = None
            continue
        repaired.append(cast(CueLabel, label))
        previous_type = cue_type
    return tuple(repaired)


def _validate_domain(value: Any, path: Path, line_no: int) -> Domain:
    if value not in DOMAINS:
        raise _error(path, line_no, "schema.domain", f"unknown domain {value!r}")
    return cast(Domain, value)


def _validate_voice(value: Any, path: Path, line_no: int) -> CTLGVoice:
    if value not in _VALID_VOICES:
        raise _error(path, line_no, "schema.voice", f"unknown voice {value!r}")
    return cast(CTLGVoice, value)


def _validate_split(value: Any, path: Path, line_no: int) -> CTLGSplit:
    if value not in _VALID_SPLITS:
        raise _error(path, line_no, "schema.split", f"unknown split {value!r}")
    return cast(CTLGSplit, value)


def _validate_tokens(value: Any, path: Path, line_no: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise _error(path, line_no, "tokens.type", "tokens must be a non-empty list")
    tokens = tuple(str(tok) for tok in value)
    if any(not tok for tok in tokens):
        raise _error(path, line_no, "tokens.empty", "tokens cannot contain empty strings")
    return tokens


def _validate_cue_tags(
    value: Any,
    *,
    expected_len: int,
    path: Path,
    line_no: int,
) -> tuple[CueLabel, ...]:
    if not isinstance(value, list):
        raise _error(path, line_no, "cue_tags.type", "cue_tags must be a list")
    if len(value) != expected_len:
        raise _error(
            path,
            line_no,
            "cue_tags.length",
            f"cue_tags length {len(value)} != tokens length {expected_len}",
        )
    tags: list[CueLabel] = []
    for idx, raw in enumerate(value):
        if raw not in _VALID_CUE_LABELS:
            raise _error(path, line_no, "cue_tags.label", f"unknown cue_tags[{idx}] {raw!r}")
        tags.append(cast(CueLabel, raw))
    _validate_bio(tags, path=path, line_no=line_no)
    return tuple(tags)


def _validate_bio(tags: list[CueLabel], *, path: Path, line_no: int) -> None:
    previous_type: str | None = None
    for idx, tag in enumerate(tags):
        if tag == "O":
            previous_type = None
            continue
        prefix, cue_type = tag.split("-", 1)
        if prefix == "B":
            previous_type = cue_type
            continue
        if prefix != "I":
            raise _error(path, line_no, "bio.prefix", f"invalid BIO prefix in {tag!r}")
        if previous_type != cue_type:
            raise _error(
                path,
                line_no,
                "bio.illegal_i",
                f"cue_tags[{idx}]={tag!r} cannot follow {tags[idx - 1] if idx else None!r}",
            )


def _parse_offset_item(item: Any, path: Path, line_no: int, idx: int) -> tuple[int, int]:
    if isinstance(item, dict):
        start = item.get("char_start", item.get("start"))
        end = item.get("char_end", item.get("end"))
    elif isinstance(item, list | tuple) and len(item) == 2:
        start, end = item
    else:
        raise _error(path, line_no, "offsets.type", f"char_offsets[{idx}] has invalid shape")
    if start is None or end is None:
        raise _error(path, line_no, "offsets.type", f"char_offsets[{idx}] missing start/end")
    try:
        return int(start), int(end)
    except (TypeError, ValueError) as exc:
        raise _error(path, line_no, "offsets.type", f"char_offsets[{idx}] is not integer") from exc


def _validate_offsets(
    value: Any,
    *,
    text: str,
    tokens: tuple[str, ...],
    path: Path,
    line_no: int,
) -> tuple[tuple[int, int], ...]:
    if value is None:
        return _derive_offsets(text=text, tokens=tokens, path=path, line_no=line_no)
    if not isinstance(value, list):
        raise _error(path, line_no, "offsets.type", "char_offsets must be a list")
    if len(value) != len(tokens):
        raise _error(
            path,
            line_no,
            "offsets.length",
            f"char_offsets length {len(value)} != tokens length {len(tokens)}",
        )
    offsets = tuple(_parse_offset_item(item, path, line_no, idx) for idx, item in enumerate(value))
    _validate_offset_slices(text=text, tokens=tokens, offsets=offsets, path=path, line_no=line_no)
    return offsets


def _optional_str(value: Any, field_name: str, path: Path, line_no: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _error(path, line_no, "expected_tlg.type", f"{field_name} must be string or null")
    cleaned = value.strip().lower()
    return cleaned or None


def _validate_expected_tlg_query(
    value: Any,
    *,
    path: Path,
    line_no: int,
) -> CTLGExpectedQuery | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _error(path, line_no, "expected_tlg.type", "expected_tlg_query must be an object")
    axis = str(value.get("axis", "")).strip()
    relation = str(value.get("relation", "")).strip()
    if axis not in _VALID_TLG_AXES:
        raise _error(path, line_no, "expected_tlg.axis", f"unknown axis {axis!r}")
    if relation not in _VALID_TLG_RELATIONS:
        raise _error(path, line_no, "expected_tlg.relation", f"unknown relation {relation!r}")
    try:
        depth = int(value.get("depth", 1))
    except (TypeError, ValueError) as exc:
        raise _error(path, line_no, "expected_tlg.depth", "depth must be an integer") from exc
    if depth < 1:
        raise _error(path, line_no, "expected_tlg.depth", "depth must be >= 1")
    return CTLGExpectedQuery(
        axis=axis,
        relation=relation,
        referent=_optional_str(value.get("referent"), "referent", path, line_no),
        secondary=_optional_str(value.get("secondary"), "secondary", path, line_no),
        subject=_optional_str(value.get("subject"), "subject", path, line_no),
        scope=_optional_str(value.get("scope"), "scope", path, line_no),
        depth=depth,
        scenario=_optional_str(value.get("scenario"), "scenario", path, line_no),
        temporal_anchor=_optional_str(
            value.get("temporal_anchor"),
            "temporal_anchor",
            path,
            line_no,
        ),
    )


def _derive_offsets(
    *,
    text: str,
    tokens: tuple[str, ...],
    path: Path,
    line_no: int,
) -> tuple[tuple[int, int], ...]:
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for idx, token in enumerate(tokens):
        start = text.find(token, cursor)
        if start < 0:
            raise _error(
                path,
                line_no,
                "offsets.derive",
                f"could not align tokens[{idx}]={token!r} after char {cursor}",
            )
        end = start + len(token)
        offsets.append((start, end))
        cursor = end
    return tuple(offsets)


def _validate_offset_slices(
    *,
    text: str,
    tokens: tuple[str, ...],
    offsets: tuple[tuple[int, int], ...],
    path: Path,
    line_no: int,
) -> None:
    previous_end = -1
    for idx, ((start, end), token) in enumerate(zip(offsets, tokens, strict=True)):
        if not 0 <= start < end <= len(text):
            raise _error(path, line_no, "offsets.bounds", f"char_offsets[{idx}] out of bounds")
        if start < previous_end:
            raise _error(path, line_no, "offsets.order", f"char_offsets[{idx}] overlaps previous")
        if text[start:end] != token:
            raise _error(
                path,
                line_no,
                "offsets.slice",
                f"text[{start}:{end}]={text[start:end]!r} != tokens[{idx}]={token!r}",
            )
        previous_end = end


def validate_ctlg_row(
    row: dict[str, Any],
    *,
    line_no: int = 1,
    path: str | Path = "<memory>",
) -> CTLGExample:
    """Validate one decoded CTLG JSON object."""
    path = Path(path)
    row = _normalize_legacy_tagged_tokens(row)
    _require_fields(row, path, line_no)
    text = row["text"]
    if not isinstance(text, str) or not text.strip():
        raise _error(path, line_no, "schema.text", "text must be a non-empty string")
    domain = _validate_domain(row["domain"], path, line_no)
    voice = _validate_voice(row["voice"], path, line_no)
    split = _validate_split(row["split"], path, line_no)
    tokens = _validate_tokens(row["tokens"], path, line_no)
    cue_tags = _validate_cue_tags(
        row["cue_tags"],
        expected_len=len(tokens),
        path=path,
        line_no=line_no,
    )
    offsets = _validate_offsets(
        row.get("char_offsets", row.get("offsets")),
        text=text,
        tokens=tokens,
        path=path,
        line_no=line_no,
    )
    expected_tlg_query = _validate_expected_tlg_query(
        row.get("expected_tlg_query"),
        path=path,
        line_no=line_no,
    )
    return CTLGExample(
        text=text,
        tokens=tokens,
        cue_tags=cue_tags,
        char_offsets=offsets,
        domain=domain,
        voice=voice,
        split=split,
        source=str(row.get("source", "")),
        note=str(row.get("note", "")),
        expected_tlg_query=expected_tlg_query,
    )


def _label_family(label: str) -> str:
    if label == "O":
        return "O"
    return label.split("-", 1)[1]


def _build_report(
    *,
    examples: list[CTLGExample],
    diagnostics: list[CTLGDiagnostic],
    rows_seen: int,
) -> CTLGValidationReport:
    by_domain: Counter[str] = Counter(ex.domain for ex in examples)
    by_voice: Counter[str] = Counter(ex.voice for ex in examples)
    by_split: Counter[str] = Counter(ex.split for ex in examples)
    by_label: Counter[str] = Counter()
    by_family: Counter[str] = Counter()
    for ex in examples:
        by_label.update(ex.cue_tags)
        by_family.update(_label_family(label) for label in ex.cue_tags)
    return CTLGValidationReport(
        examples=tuple(examples),
        diagnostics=tuple(diagnostics),
        rows_seen=rows_seen,
        by_domain=dict(by_domain),
        by_voice=dict(by_voice),
        by_split=dict(by_split),
        by_label=dict(by_label),
        by_cue_family=dict(by_family),
    )


def validate_ctlg_jsonl(path: str | Path) -> CTLGValidationReport:
    """Validate a JSONL CTLG corpus and collect diagnostics."""
    path = Path(path)
    examples: list[CTLGExample] = []
    diagnostics: list[CTLGDiagnostic] = []
    rows_seen = 0
    with path.open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            rows_seen += 1
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise _error(path, line_no, "json.object", "row must be a JSON object")
                examples.append(validate_ctlg_row(row, line_no=line_no, path=path))
            except json.JSONDecodeError as exc:
                diagnostics.append(
                    CTLGDiagnostic(str(path), line_no, "json.decode", str(exc)),
                )
            except CTLGCorpusError as exc:
                diagnostics.append(_diagnostic_from_error(exc, path=path, line_no=line_no))
    return _build_report(examples=examples, diagnostics=diagnostics, rows_seen=rows_seen)


def _diagnostic_from_error(
    exc: CTLGCorpusError,
    *,
    path: Path,
    line_no: int,
) -> CTLGDiagnostic:
    message = str(exc)
    prefix = f"{path}:{line_no} "
    if message.startswith(prefix):
        rest = message[len(prefix) :]
        code, _, detail = rest.partition(": ")
        return CTLGDiagnostic(str(path), line_no, code or "validation", detail or rest)
    return CTLGDiagnostic(str(path), line_no, "validation", message)


def load_ctlg_jsonl(path: str | Path) -> list[CTLGExample]:
    """Load a CTLG JSONL corpus, raising on any validation diagnostic."""
    report = validate_ctlg_jsonl(path)
    if not report.ok:
        raise CTLGCorpusError("\n".join(diag.format() for diag in report.diagnostics))
    return list(report.examples)


def dump_ctlg_jsonl(examples: Iterable[CTLGExample], path: str | Path) -> None:
    """Write CTLG examples to JSONL using the canonical schema."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for ex in examples:
            row = {
                "text": ex.text,
                "tokens": list(ex.tokens),
                "cue_tags": list(ex.cue_tags),
                "char_offsets": [
                    {"char_start": start, "char_end": end} for start, end in ex.char_offsets
                ],
                "domain": ex.domain,
                "voice": ex.voice,
                "split": ex.split,
                "source": ex.source,
                "note": ex.note,
            }
            if ex.expected_tlg_query is not None:
                row["expected_tlg_query"] = ex.expected_tlg_query.to_json()
            fh.write(json.dumps(row) + "\n")
