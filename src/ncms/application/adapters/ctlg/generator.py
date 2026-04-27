"""LLM-backed CTLG corpus generation.

Generation is deliberately validation-first: LLM output is coerced only at the
metadata boundary (domain / split / source / query-vs-memory voice), then passed
through the canonical CTLG corpus validator before it can be written.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from ncms.application.adapters.ctlg.corpus import (
    CTLGDiagnostic,
    CTLGExample,
    CTLGExpectedQuery,
    CTLGValidationReport,
    dump_ctlg_jsonl,
    validate_ctlg_row,
)
from ncms.application.adapters.ctlg.prompts import (
    CTLGPromptSpec,
    CTLGPromptVoice,
    build_generation_prompt,
)
from ncms.domain.tlg.cue_taxonomy import CUE_LABELS, CueLabel, TaggedToken
from ncms.domain.tlg.semantic_parser import TLGQuery, synthesize

CTLGSplit = Literal["train", "dev", "test", "gold", "llm", "sdg", "adversarial"]
LLMJsonCaller = Callable[[str, str, str | None, int, float], Awaitable[object | None]]
_SURFACE_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[._/#:+-][A-Za-z0-9]+)*|[^\w\s]")
_VALID_LABELS = frozenset(CUE_LABELS)
_BARE_QUESTION_WORDS = frozenset({"what", "which", "who", "where", "when", "why", "how"})
_SCOPE_WORDS = frozenset(
    {
        "architecture",
        "broker",
        "cache",
        "database",
        "framework",
        "language",
        "library",
        "pattern",
        "platform",
        "runtime",
        "service",
        "stack",
        "state",
        "version",
    }
)
_NON_REFERENT_WORDS = frozenset(
    {
        "deploy",
        "deployed",
        "kept",
        "picked",
        "replace",
        "replaced",
        "stayed",
        "use",
        "used",
        "using",
    }
)
_CAUSAL_CUE_WORDS = frozenset(
    {
        "cause",
        "caused",
        "causes",
        "driven",
        "drove",
        "force",
        "forced",
        "led",
        "replace",
        "replaced",
        "trigger",
        "triggered",
    }
)
_LABEL_ALIASES: dict[str, CueLabel] = {
    "B-ASK_CAUSE": "B-CAUSAL_EXPLICIT",
    "I-ASK_CAUSE": "I-CAUSAL_EXPLICIT",
    "B-ASK_REASON": "B-CAUSAL_EXPLICIT",
    "I-ASK_REASON": "I-CAUSAL_EXPLICIT",
}


@dataclass(frozen=True)
class CTLGGenerationRequest:
    """Configuration for one CTLG generation batch."""

    domain: str
    voice: CTLGPromptVoice
    n_rows: int
    split: CTLGSplit = "llm"
    source: str = "llm_generated"
    focus: str = ""
    examples: tuple[str, ...] = ()
    model: str = ""
    api_base: str | None = None
    temperature: float = 0.4
    max_tokens: int = 4000


@dataclass(frozen=True)
class CTLGGenerationResult:
    """Validated CTLG generation output."""

    examples: tuple[CTLGExample, ...]
    diagnostics: tuple[CTLGDiagnostic, ...]
    raw_rows_seen: int
    prompt: str

    @property
    def is_valid(self) -> bool:
        return not self.diagnostics

    def as_report(self) -> CTLGValidationReport:
        return CTLGValidationReport(
            examples=self.examples,
            diagnostics=self.diagnostics,
            rows_seen=self.raw_rows_seen,
        )


async def generate_ctlg_examples(
    request: CTLGGenerationRequest,
    *,
    call_json: LLMJsonCaller | None = None,
) -> CTLGGenerationResult:
    """Generate and validate one batch of CTLG examples."""
    if request.n_rows <= 0:
        raise ValueError("n_rows must be positive")
    if not request.model and call_json is None:
        raise ValueError("model is required unless call_json is injected")

    prompt = build_generation_prompt(
        CTLGPromptSpec(
            domain=request.domain,
            voice=request.voice,
            n_rows=request.n_rows,
            focus=request.focus,
            examples=request.examples,
        )
    )
    caller = call_json or _default_call_json
    raw = await caller(
        prompt,
        request.model,
        request.api_base,
        request.max_tokens,
        request.temperature,
    )
    rows = _coerce_rows(raw)
    return _validate_generated_rows(rows, request=request, prompt=prompt)


def write_generation_result(result: CTLGGenerationResult, path: str | Path) -> None:
    """Write validated CTLG examples to JSONL."""
    if result.diagnostics:
        raise ValueError("cannot write CTLG generation result with validation diagnostics")
    dump_ctlg_jsonl(list(result.examples), path)


def _validate_generated_rows(
    rows: Sequence[dict[str, Any]],
    *,
    request: CTLGGenerationRequest,
    prompt: str,
) -> CTLGGenerationResult:
    examples: list[CTLGExample] = []
    diagnostics: list[CTLGDiagnostic] = []
    path = Path("<ctlg-generation>")
    selected_rows = rows[: request.n_rows]
    for line_no, row in enumerate(selected_rows, start=1):
        coerced = _coerce_row_metadata(row, request=request)
        try:
            example = validate_ctlg_row(coerced, line_no=line_no, path=path)
            quality_diagnostic = _validate_generated_cue_quality(
                example,
                line_no=line_no,
                path=path,
            )
            if quality_diagnostic is not None:
                diagnostics.append(quality_diagnostic)
                continue
            example, diagnostic = _validate_expected_tlg_for_generated_query(
                example,
                request=request,
                line_no=line_no,
                path=path,
            )
            if diagnostic is not None:
                diagnostics.append(diagnostic)
                continue
            examples.append(example)
        except Exception as exc:
            diagnostics.append(CTLGDiagnostic(str(path), line_no, "validation", str(exc)))
    return CTLGGenerationResult(
        examples=tuple(examples),
        diagnostics=tuple(diagnostics),
        raw_rows_seen=len(rows),
        prompt=prompt,
    )


def _validate_generated_cue_quality(
    example: CTLGExample,
    *,
    line_no: int,
    path: Path,
) -> CTLGDiagnostic | None:
    for token, label in zip(example.tokens, example.cue_tags, strict=True):
        if label.endswith("ASK_CURRENT") and token.lower() in _BARE_QUESTION_WORDS:
            return CTLGDiagnostic(
                str(path),
                line_no,
                "cue_quality.ask_current_question_word",
                (
                    "ASK_CURRENT should mark present-state words like current/currently/now, "
                    f"not the bare question word {token!r}; text={example.text!r}"
                ),
            )
    return None


def _coerce_rows(raw: object | None) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        if isinstance(raw.get("rows"), list):
            raw = raw["rows"]
        else:
            return [raw]
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _coerce_row_metadata(
    row: dict[str, Any],
    *,
    request: CTLGGenerationRequest,
) -> dict[str, Any]:
    coerced = dict(row)
    coerced["domain"] = request.domain
    coerced["split"] = request.split
    coerced["source"] = request.source
    if request.voice == "counterfactual":
        coerced["voice"] = "query"
    else:
        coerced["voice"] = request.voice
    coerced.setdefault("note", request.focus or request.voice)
    return _normalize_generated_alignment(coerced)


def _normalize_generated_alignment(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize LLM rows to canonical surface tokens before validation.

    The LLM is useful for proposing CTLG cue spans but unreliable as a tokenizer.
    This repair step makes local tokenization authoritative and projects the
    model's labels onto those deterministic spans.
    """
    text = row.get("text")
    raw_tokens = row.get("tokens")
    raw_labels = row.get("cue_tags")
    row = _normalize_label_aliases(row)
    raw_labels = row.get("cue_tags")
    if (
        not isinstance(text, str)
        or not isinstance(raw_tokens, list)
        or not isinstance(raw_labels, list)
    ):
        return row

    canonical = _surface_token_offsets(text)
    if not canonical:
        return row

    raw_token_strings = tuple(str(token) for token in raw_tokens if str(token))
    raw_label_strings = tuple(str(label) for label in raw_labels)
    raw_offsets = _derive_lenient_offsets(text=text, tokens=raw_token_strings)
    if raw_offsets is None:
        labels = _project_labels_by_position(
            canonical_tokens=tuple(token for token, _ in canonical),
            raw_labels=raw_label_strings,
        )
    else:
        labels = _project_labels(
            canonical_offsets=tuple(offset for _, offset in canonical),
            raw_offsets=raw_offsets,
            raw_labels=raw_label_strings,
            canonical_tokens=tuple(token for token, _ in canonical),
        )
    canonical_tokens = [token for token, _ in canonical]
    normalized = dict(row)
    normalized["tokens"] = canonical_tokens
    normalized["cue_tags"] = list(
        _clean_generated_cue_noise(
            tokens=tuple(canonical_tokens),
            labels=labels,
        )
    )
    normalized["char_offsets"] = [
        {"char_start": start, "char_end": end} for _, (start, end) in canonical
    ]
    return normalized


def _clean_generated_cue_noise(
    *,
    tokens: tuple[str, ...],
    labels: tuple[str, ...],
) -> tuple[str, ...]:
    """Remove common LLM annotation artifacts from generated rows."""
    cleaned = list(labels)
    has_causal = any(label.endswith(("CAUSAL_EXPLICIT", "CAUSAL_ALTLEX")) for label in labels)
    for idx, (token, label) in enumerate(zip(tokens, labels, strict=True)):
        if label.endswith("ASK_CURRENT") and token.lower() in _BARE_QUESTION_WORDS:
            cleaned[idx] = "O"
        if label.endswith("ASK_CURRENT") and token.lower() in _SCOPE_WORDS:
            prefix = label.split("-", maxsplit=1)[0]
            cleaned[idx] = f"{prefix}-SCOPE"
        if label.endswith("REFERENT") and token.lower() in _BARE_QUESTION_WORDS:
            cleaned[idx] = "O"
        if label.endswith("ORDINAL_NTH") and token.lower() in _BARE_QUESTION_WORDS:
            cleaned[idx] = "O"
        if (
            label.endswith("ASK_CHANGE")
            and token.lower() in _BARE_QUESTION_WORDS
            and not _continues_change_phrase(tokens, labels, idx)
        ):
            cleaned[idx] = "O"
        if label.endswith("REFERENT") and token.lower() in _SCOPE_WORDS:
            prefix = label.split("-", maxsplit=1)[0]
            cleaned[idx] = f"{prefix}-SCOPE"
        if label.endswith("REFERENT") and token.lower() in _NON_REFERENT_WORDS:
            cleaned[idx] = "O"
        if cleaned[idx].endswith("SCOPE") and idx > 0 and cleaned[idx - 1].endswith("SCOPE"):
            cleaned[idx] = "O"
        if (
            cleaned[idx] == "B-REFERENT"
            and idx > 0
            and cleaned[idx - 1].endswith("REFERENT")
        ):
            cleaned[idx] = "I-REFERENT"
    if not has_causal:
        for idx, token in enumerate(tokens):
            if token.lower() in _CAUSAL_CUE_WORDS and cleaned[idx] == "O":
                cleaned[idx] = "B-CAUSAL_EXPLICIT"
                break
    return tuple(cleaned)


def _continues_change_phrase(tokens: tuple[str, ...], labels: tuple[str, ...], idx: int) -> bool:
    if idx + 1 >= len(tokens):
        return False
    return (
        tokens[idx + 1].lower() in {"change", "changed", "changes"}
        or labels[idx + 1].endswith("ASK_CHANGE")
    )


def _validate_expected_tlg_for_generated_query(
    example: CTLGExample,
    *,
    request: CTLGGenerationRequest,
    line_no: int,
    path: Path,
) -> tuple[CTLGExample, CTLGDiagnostic | None]:
    if request.voice == "memory":
        return example, None
    expected = example.expected_tlg_query
    if expected is None:
        return (
            example,
            CTLGDiagnostic(
                str(path),
                line_no,
                "expected_tlg.missing",
                "query/counterfactual CTLG rows must include expected_tlg_query",
            ),
        )
    actual = synthesize(_tagged_tokens(example))
    if actual is None:
        return (
            example,
            CTLGDiagnostic(
                str(path),
                line_no,
                "expected_tlg.synthesizer_no_match",
                (
                    "cue_tags did not synthesize to any TLGQuery; "
                    f"text={example.text!r}; cues={_cue_brief(example)}"
                ),
            ),
        )
    expected = _fill_expected_tlg_omissions(expected, actual)
    mismatch = _expected_tlg_mismatch(expected, actual)
    if mismatch:
        return (
            example,
            CTLGDiagnostic(
                str(path),
                line_no,
                "expected_tlg.mismatch",
                (
                    f"{mismatch}; text={example.text!r}; "
                    f"cues={_cue_brief(example)}; "
                    f"actual={_tlg_query_brief(actual)}"
                ),
            ),
        )
    return replace(example, expected_tlg_query=expected), None


def _tagged_tokens(example: CTLGExample) -> list[TaggedToken]:
    return [
        TaggedToken(
            char_start=start,
            char_end=end,
            surface=surface,
            cue_label=label,
            confidence=0.99,
        )
        for surface, label, (start, end) in zip(
            example.tokens,
            example.cue_tags,
            example.char_offsets,
            strict=True,
        )
    ]


def _cue_brief(example: CTLGExample) -> list[tuple[str, str]]:
    return [
        (token, label)
        for token, label in zip(example.tokens, example.cue_tags, strict=True)
        if label != "O"
    ]


def _expected_tlg_mismatch(expected: CTLGExpectedQuery, actual: TLGQuery) -> str:
    fields = (
        "axis",
        "relation",
        "referent",
        "secondary",
        "subject",
        "scope",
        "depth",
        "scenario",
        "temporal_anchor",
    )
    for field in fields:
        if getattr(actual, field) != getattr(expected, field):
            return (
                f"{field}: expected {getattr(expected, field)!r}, "
                f"got {getattr(actual, field)!r}"
            )
    return ""


def _fill_expected_tlg_omissions(
    expected: CTLGExpectedQuery,
    actual: TLGQuery,
) -> CTLGExpectedQuery:
    """Fill optional expected fields omitted by the LLM from grammar output."""
    return CTLGExpectedQuery(
        axis=expected.axis,
        relation=expected.relation,
        referent=expected.referent if expected.referent is not None else actual.referent,
        secondary=expected.secondary if expected.secondary is not None else actual.secondary,
        subject=expected.subject if expected.subject is not None else actual.subject,
        scope=expected.scope if expected.scope is not None else actual.scope,
        depth=expected.depth,
        scenario=expected.scenario if expected.scenario is not None else actual.scenario,
        temporal_anchor=(
            expected.temporal_anchor
            if expected.temporal_anchor is not None
            else actual.temporal_anchor
        ),
    )


def _tlg_query_brief(query: TLGQuery) -> dict[str, object]:
    return {
        "axis": query.axis,
        "relation": query.relation,
        "referent": query.referent,
        "secondary": query.secondary,
        "subject": query.subject,
        "scope": query.scope,
        "depth": query.depth,
        "scenario": query.scenario,
        "temporal_anchor": query.temporal_anchor,
        "matched_rule": query.matched_rule,
    }


def _normalize_label_aliases(row: dict[str, Any]) -> dict[str, Any]:
    raw_labels = row.get("cue_tags")
    if not isinstance(raw_labels, list):
        return row
    normalized = dict(row)
    normalized["cue_tags"] = [
        _LABEL_ALIASES.get(str(label), str(label)) for label in raw_labels
    ]
    return normalized


def _surface_token_offsets(text: str) -> tuple[tuple[str, tuple[int, int]], ...]:
    return tuple(
        (match.group(0), (match.start(), match.end()))
        for match in _SURFACE_TOKEN_RE.finditer(text)
    )


def _derive_lenient_offsets(
    *,
    text: str,
    tokens: tuple[str, ...],
) -> tuple[tuple[int, int], ...] | None:
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for token in tokens:
        start = text.find(token, cursor)
        if start < 0:
            return None
        end = start + len(token)
        offsets.append((start, end))
        cursor = end
    return tuple(offsets)


def _project_labels(
    *,
    canonical_offsets: tuple[tuple[int, int], ...],
    raw_offsets: tuple[tuple[int, int], ...],
    raw_labels: tuple[str, ...],
    canonical_tokens: tuple[str, ...],
) -> tuple[CueLabel, ...]:
    labels: list[str] = []
    for token, offset in zip(canonical_tokens, canonical_offsets, strict=True):
        if _is_punctuation(token):
            labels.append("O")
            continue
        labels.append(_label_for_offset(offset, raw_offsets=raw_offsets, raw_labels=raw_labels))
    return _repair_bio(tuple(labels))


def _project_labels_by_position(
    *,
    canonical_tokens: tuple[str, ...],
    raw_labels: tuple[str, ...],
) -> tuple[CueLabel, ...]:
    labels: list[str] = []
    for idx, token in enumerate(canonical_tokens):
        if _is_punctuation(token) or idx >= len(raw_labels):
            labels.append("O")
            continue
        label = raw_labels[idx]
        labels.append(label if label in _VALID_LABELS else "O")
    return _repair_bio(tuple(labels))


def _is_punctuation(token: str) -> bool:
    return not any(char.isalnum() for char in token)


def _label_for_offset(
    offset: tuple[int, int],
    *,
    raw_offsets: tuple[tuple[int, int], ...],
    raw_labels: tuple[str, ...],
) -> str:
    best_idx: int | None = None
    best_overlap = 0
    start, end = offset
    for idx, (raw_start, raw_end) in enumerate(raw_offsets):
        overlap = max(0, min(end, raw_end) - max(start, raw_start))
        if overlap > best_overlap:
            best_idx = idx
            best_overlap = overlap
    if best_idx is None or best_idx >= len(raw_labels):
        return "O"
    label = raw_labels[best_idx]
    return label if label in _VALID_LABELS else "O"


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
            label = "O"
            previous_type = None
            repaired.append("O")
            continue
        repaired.append(label)  # type: ignore[arg-type]
        previous_type = cue_type if label != "O" else None
    return tuple(repaired)


async def _default_call_json(
    prompt: str,
    model: str,
    api_base: str | None,
    max_tokens: int,
    temperature: float,
) -> object | None:
    from ncms.infrastructure.llm.caller import call_llm_json

    return await call_llm_json(
        prompt,
        model=model,
        api_base=api_base,
        max_tokens=max_tokens,
        temperature=temperature,
    )


__all__ = [
    "CTLGGenerationRequest",
    "CTLGGenerationResult",
    "LLMJsonCaller",
    "generate_ctlg_examples",
    "write_generation_result",
]
