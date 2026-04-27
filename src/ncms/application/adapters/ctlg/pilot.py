"""Pilot runner for grammar-gated CTLG LLM generation."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ncms.application.adapters.ctlg.corpus import (
    CTLGDiagnostic,
    CTLGExample,
    dump_ctlg_jsonl,
)
from ncms.application.adapters.ctlg.generator import (
    CTLGGenerationRequest,
    LLMJsonCaller,
    generate_ctlg_examples,
)

CTLG_PILOT_PRESET_NAMES = (
    "predecessor",
    "current",
    "cause_of",
    "after_named",
    "concurrent_with",
    "last",
    "modal_counterfactual",
)

_CTLG_PILOT_PRESETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "predecessor": (
        "Generate only one-anchor temporal predecessor query rows. "
        "Each row asks what came before one concrete software_dev technology. "
        "Use exactly one B-TEMPORAL_BEFORE cue and one B-REFERENT technology cue. "
        "Do not use ASK_CURRENT, ORDINAL, MODAL, or a second REFERENT. "
        "expected_tlg_query must be axis=temporal relation=predecessor.",
        (
            "What did we use before Postgres?",
            "What came before Redis?",
        ),
    ),
    "current": (
        "Generate only current-state query rows. Use a present-state cue such as "
        "currently, current, now, today, or at present as ASK_CURRENT. Label slot "
        "words such as database, cache, broker, language, version, or framework "
        "as SCOPE, not REFERENT. Do not label bare question words as ASK_CURRENT. "
        "expected_tlg_query must be axis=state relation=current and include scope.",
        (
            "Currently, which database do we use?",
            "Which cache is active now?",
        ),
    ),
    "cause_of": (
        "Generate only direct replacement/supersession causal query rows. Use "
        "phrasing like 'Why did X replace Y?' or 'What caused X to replace Y?'. "
        "Label replace/replaced/caused as CAUSAL_EXPLICIT and label both concrete "
        "technologies as REFERENT. Do not generate 'why is X default', 'when did X', "
        "'which version', 'shift from X to Y', or broad adoption questions. Do not "
        "label bare why/what as ASK_CHANGE. expected_tlg_query must be axis=causal "
        "relation=cause_of with referent=X and secondary=Y.",
        (
            "Why did Kafka replace RabbitMQ?",
            "What caused Postgres to replace MySQL?",
        ),
    ),
    "after_named": (
        "Generate only temporal after_named query rows. Each row asks what happened "
        "after one concrete technology, migration, or named event. Use one "
        "B-TEMPORAL_AFTER cue and one B-REFERENT cue. expected_tlg_query must be "
        "axis=temporal relation=after_named.",
        (
            "What happened after Redis?",
            "What changed after the Kubernetes migration?",
        ),
    ),
    "concurrent_with": (
        "Generate only temporal concurrent_with query rows. Each row asks what was "
        "happening during one named technology, incident, rollout, or migration. "
        "Use B-TEMPORAL_DURING and B-REFERENT. Do not use date-only intervals. "
        "expected_tlg_query must be axis=temporal relation=concurrent_with.",
        (
            "What was happening during the Kafka rollout?",
            "What changed during the OAuth migration?",
        ),
    ),
    "last": (
        "Generate only ordinal last query rows. Use an ORDINAL_LAST cue such as "
        "latest, last, most recent, or previous and a SCOPE cue such as database, "
        "cache, framework, broker, or version. Do not label current as ASK_CURRENT "
        "in these rows. expected_tlg_query must be axis=ordinal relation=last.",
        (
            "What was the last database before Postgres?",
            "Which framework was most recent before React?",
        ),
    ),
    "modal_counterfactual": (
        "Generate only modal counterfactual query rows. Use hypothetical cues such "
        "as if, had we, would have, or if X had not. Label the preserved technology "
        "as REFERENT and any slot word as SCOPE. expected_tlg_query must be "
        "axis=modal relation=would_be_current_if. The scenario must match the "
        "grammar convention preserve_<referent>.",
        (
            "If we had kept MySQL, what would be current?",
            "Had we stayed with RabbitMQ, which broker would be current?",
        ),
    ),
}
_CTLG_PILOT_PRESET_EXPECTED: dict[str, tuple[str, str]] = {
    "predecessor": ("temporal", "predecessor"),
    "current": ("state", "current"),
    "cause_of": ("causal", "cause_of"),
    "after_named": ("temporal", "after_named"),
    "concurrent_with": ("temporal", "concurrent_with"),
    "last": ("ordinal", "last"),
    "modal_counterfactual": ("modal", "would_be_current_if"),
}


@dataclass(frozen=True)
class CTLGPilotRequest:
    """Configuration for repeated CTLG generation probes."""

    generation: CTLGGenerationRequest
    target_rows: int
    batch_size: int = 8
    max_batches: int = 10
    deduplicate_text: bool = True
    required_axis: str | None = None
    required_relation: str | None = None


@dataclass(frozen=True)
class CTLGPilotBatch:
    """Summary of one generation batch."""

    batch_no: int
    rows_seen: int
    n_valid: int
    n_diagnostics: int


@dataclass(frozen=True)
class CTLGPilotDiagnostic:
    """Diagnostic annotated with pilot batch provenance."""

    batch_no: int
    line_no: int
    code: str
    message: str

    @classmethod
    def from_ctlg(cls, diagnostic: CTLGDiagnostic, *, batch_no: int) -> CTLGPilotDiagnostic:
        return cls(
            batch_no=batch_no,
            line_no=diagnostic.line_no,
            code=diagnostic.code,
            message=diagnostic.message,
        )


@dataclass(frozen=True)
class CTLGPilotResult:
    """Aggregate output from repeated CTLG generation probes."""

    examples: tuple[CTLGExample, ...]
    diagnostics: tuple[CTLGPilotDiagnostic, ...]
    batches: tuple[CTLGPilotBatch, ...]
    target_rows: int
    batch_size: int
    max_batches: int

    @property
    def rows_seen(self) -> int:
        return sum(batch.rows_seen for batch in self.batches)

    @property
    def valid_yield(self) -> float:
        if self.rows_seen == 0:
            return 0.0
        return len(self.examples) / self.rows_seen

    @property
    def hit_target(self) -> bool:
        return len(self.examples) >= self.target_rows

    def to_json(self) -> dict[str, Any]:
        return {
            "target_rows": self.target_rows,
            "batch_size": self.batch_size,
            "max_batches": self.max_batches,
            "rows_seen": self.rows_seen,
            "n_valid": len(self.examples),
            "n_diagnostics": len(self.diagnostics),
            "valid_yield": self.valid_yield,
            "hit_target": self.hit_target,
            "by_diagnostic_code": dict(Counter(d.code for d in self.diagnostics)),
            "by_expected_axis": dict(
                Counter(
                    ex.expected_tlg_query.axis
                    for ex in self.examples
                    if ex.expected_tlg_query is not None
                )
            ),
            "by_expected_relation": dict(
                Counter(
                    ex.expected_tlg_query.relation
                    for ex in self.examples
                    if ex.expected_tlg_query is not None
                )
            ),
            "by_cue_family": _cue_family_counts(self.examples),
            "batches": [
                {
                    "batch_no": batch.batch_no,
                    "rows_seen": batch.rows_seen,
                    "n_valid": batch.n_valid,
                    "n_diagnostics": batch.n_diagnostics,
                }
                for batch in self.batches
            ],
            "examples": [_example_to_json(ex) for ex in self.examples],
            "diagnostics": [
                {
                    "batch_no": diag.batch_no,
                    "line_no": diag.line_no,
                    "code": diag.code,
                    "message": diag.message,
                }
                for diag in self.diagnostics
            ],
        }


async def generate_ctlg_pilot(
    request: CTLGPilotRequest,
    *,
    call_json: LLMJsonCaller | None = None,
) -> CTLGPilotResult:
    """Run repeated grammar-gated CTLG generation until target or budget."""
    if request.target_rows <= 0:
        raise ValueError("target_rows must be positive")
    if request.batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if request.max_batches <= 0:
        raise ValueError("max_batches must be positive")

    examples: list[CTLGExample] = []
    diagnostics: list[CTLGPilotDiagnostic] = []
    batches: list[CTLGPilotBatch] = []
    seen_text: set[str] = set()

    for batch_no in range(1, request.max_batches + 1):
        result = await generate_ctlg_examples(
            replace(request.generation, n_rows=request.batch_size),
            call_json=call_json,
        )
        batch_diagnostic_start = len(diagnostics)
        accepted = 0
        for example in result.examples:
            off_preset = _off_preset_diagnostic(
                example,
                batch_no=batch_no,
                required_axis=request.required_axis,
                required_relation=request.required_relation,
            )
            if off_preset is not None:
                diagnostics.append(off_preset)
                continue
            if request.deduplicate_text and example.text in seen_text:
                diagnostics.append(
                    CTLGPilotDiagnostic(
                        batch_no=batch_no,
                        line_no=0,
                        code="pilot.duplicate_text",
                        message=f"duplicate generated text skipped: {example.text!r}",
                    )
                )
                continue
            seen_text.add(example.text)
            examples.append(example)
            accepted += 1
            if len(examples) >= request.target_rows:
                break
        diagnostics.extend(
            CTLGPilotDiagnostic.from_ctlg(diag, batch_no=batch_no)
            for diag in result.diagnostics
        )
        batches.append(
            CTLGPilotBatch(
                batch_no=batch_no,
                rows_seen=result.raw_rows_seen,
                n_valid=accepted,
                n_diagnostics=len(diagnostics) - batch_diagnostic_start,
            )
        )
        if len(examples) >= request.target_rows:
            break

    return CTLGPilotResult(
        examples=tuple(examples),
        diagnostics=tuple(diagnostics),
        batches=tuple(batches),
        target_rows=request.target_rows,
        batch_size=request.batch_size,
        max_batches=request.max_batches,
    )


def apply_ctlg_pilot_preset(
    request: CTLGGenerationRequest,
    preset: str | None,
) -> CTLGGenerationRequest:
    """Merge a named grammar-slice preset into a generation request."""
    if not preset:
        return request
    try:
        preset_focus, preset_examples = _CTLG_PILOT_PRESETS[preset]
    except KeyError as exc:
        raise ValueError(f"unknown CTLG pilot preset {preset!r}") from exc
    focus = (
        preset_focus
        if not request.focus
        else f"{preset_focus}\n\nAdditional focus: {request.focus}"
    )
    return replace(
        request,
        focus=focus,
        examples=tuple((*preset_examples, *request.examples)),
    )


def ctlg_pilot_preset_expectation(preset: str | None) -> tuple[str | None, str | None]:
    """Return ``(axis, relation)`` required by a named pilot preset."""
    if not preset:
        return None, None
    try:
        return _CTLG_PILOT_PRESET_EXPECTED[preset]
    except KeyError as exc:
        raise ValueError(f"unknown CTLG pilot preset {preset!r}") from exc


def write_pilot_examples(result: CTLGPilotResult, path: str | Path) -> None:
    """Write accepted pilot examples to JSONL."""
    dump_ctlg_jsonl(result.examples, path)


def _cue_family_counts(examples: tuple[CTLGExample, ...]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for example in examples:
        for label in example.cue_tags:
            if label == "O":
                counts["O"] += 1
                continue
            counts[label.split("-", maxsplit=1)[1]] += 1
    return dict(counts)


def _off_preset_diagnostic(
    example: CTLGExample,
    *,
    batch_no: int,
    required_axis: str | None,
    required_relation: str | None,
) -> CTLGPilotDiagnostic | None:
    expected = example.expected_tlg_query
    if expected is None:
        return None
    if required_axis is not None and expected.axis != required_axis:
        return CTLGPilotDiagnostic(
            batch_no=batch_no,
            line_no=0,
            code="pilot.off_preset_axis",
            message=(
                f"expected preset axis {required_axis!r}, got {expected.axis!r}: "
                f"{example.text!r}"
            ),
        )
    if required_relation is not None and expected.relation != required_relation:
        return CTLGPilotDiagnostic(
            batch_no=batch_no,
            line_no=0,
            code="pilot.off_preset_relation",
            message=(
                f"expected preset relation {required_relation!r}, got "
                f"{expected.relation!r}: {example.text!r}"
            ),
        )
    return None


def _example_to_json(example: CTLGExample) -> dict[str, Any]:
    return {
        "text": example.text,
        "cue_tags": list(example.cue_tags),
        "expected_tlg_query": (
            example.expected_tlg_query.to_json()
            if example.expected_tlg_query is not None
            else None
        ),
    }


__all__ = [
    "CTLG_PILOT_PRESET_NAMES",
    "CTLGPilotBatch",
    "CTLGPilotDiagnostic",
    "CTLGPilotRequest",
    "CTLGPilotResult",
    "apply_ctlg_pilot_preset",
    "ctlg_pilot_preset_expectation",
    "generate_ctlg_pilot",
    "write_pilot_examples",
]
