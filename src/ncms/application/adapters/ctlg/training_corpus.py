"""Build grammar-safe CTLG training corpora."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ncms.application.adapters.ctlg.audit import CTLGGrammarAuditReport, audit_ctlg_files
from ncms.application.adapters.ctlg.corpus import (
    CTLGDiagnostic,
    CTLGExample,
    dump_ctlg_jsonl,
    validate_ctlg_jsonl,
)
from ncms.domain.tlg.cue_taxonomy import TaggedToken
from ncms.domain.tlg.semantic_parser import synthesize


@dataclass(frozen=True)
class CTLGCorpusExclusion:
    """One row excluded from a prepared CTLG training corpus."""

    path: str
    line_no: int
    text: str
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line_no": self.line_no,
            "text": self.text,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CTLGTrainingCorpusBuild:
    """Result of preparing a grammar-safe CTLG corpus."""

    output_path: str
    audit: CTLGGrammarAuditReport
    examples: tuple[CTLGExample, ...] = ()
    exclusions: tuple[CTLGCorpusExclusion, ...] = ()
    diagnostics: tuple[CTLGDiagnostic, ...] = ()
    by_voice: dict[str, int] = field(default_factory=dict)
    by_split: dict[str, int] = field(default_factory=dict)
    by_domain: dict[str, int] = field(default_factory=dict)

    @property
    def n_written(self) -> int:
        return len(self.examples)

    @property
    def n_excluded(self) -> int:
        return len(self.exclusions)

    @property
    def n_diagnostics(self) -> int:
        return len(self.diagnostics)

    @property
    def by_exclusion_reason(self) -> dict[str, int]:
        return dict(Counter(exclusion.reason for exclusion in self.exclusions))

    def ok(self, *, min_query_synthesis_rate: float = 0.0) -> bool:
        return self.n_diagnostics == 0 and self.audit.ok(
            min_query_synthesis_rate=min_query_synthesis_rate,
        )

    def to_json(self, *, max_exclusions: int = 20) -> dict[str, Any]:
        return {
            "output_path": self.output_path,
            "n_written": self.n_written,
            "n_excluded": self.n_excluded,
            "n_diagnostics": self.n_diagnostics,
            "by_voice": self.by_voice,
            "by_split": self.by_split,
            "by_domain": self.by_domain,
            "by_exclusion_reason": self.by_exclusion_reason,
            "audit": self.audit.to_json(max_misses_per_file=max_exclusions),
            "exclusions": [
                exclusion.to_json() for exclusion in self.exclusions[:max_exclusions]
            ],
            "diagnostics": [
                {
                    "path": d.path,
                    "line_no": d.line_no,
                    "code": d.code,
                    "message": d.message,
                }
                for d in self.diagnostics[:max_exclusions]
            ],
        }


def build_ctlg_training_corpus(
    paths: list[str | Path],
    *,
    output_path: str | Path,
    include_memory: bool = True,
    deduplicate_text: bool = True,
) -> CTLGTrainingCorpusBuild:
    """Write a CTLG corpus containing only grammar-composable query rows.

    Memory-voice rows are kept by default because they train ingest-side causal
    and temporal cue extraction, where the query grammar is not expected to
    synthesize a ``TLGQuery``.
    """
    path_objs = [Path(path) for path in paths]
    audit = audit_ctlg_files(path_objs)
    accepted: list[CTLGExample] = []
    exclusions: list[CTLGCorpusExclusion] = []
    diagnostics: list[CTLGDiagnostic] = []
    seen_texts: set[tuple[str, str]] = set()

    for file_audit in audit.files:
        diagnostics.extend(file_audit.validation_diagnostics)
        validation = validate_ctlg_jsonl(file_audit.path)
        for line_no, example in enumerate(validation.examples, start=1):
            key = (example.voice, example.text.strip().lower())
            if deduplicate_text and key in seen_texts:
                exclusions.append(
                    CTLGCorpusExclusion(
                        path=file_audit.path,
                        line_no=line_no,
                        text=example.text,
                        reason="duplicate_text",
                    )
                )
                continue
            if example.voice == "memory":
                if include_memory:
                    accepted.append(example)
                    seen_texts.add(key)
                continue
            if synthesize(_tagged_tokens(example)) is None:
                exclusions.append(
                    CTLGCorpusExclusion(
                        path=file_audit.path,
                        line_no=line_no,
                        text=example.text,
                        reason="query_not_synthesizable",
                    )
                )
                continue
            accepted.append(example)
            seen_texts.add(key)

    out = Path(output_path)
    dump_ctlg_jsonl(accepted, out)
    return CTLGTrainingCorpusBuild(
        output_path=str(out),
        audit=audit,
        examples=tuple(accepted),
        exclusions=tuple(exclusions),
        diagnostics=tuple(diagnostics),
        by_voice=dict(Counter(ex.voice for ex in accepted)),
        by_split=dict(Counter(ex.split for ex in accepted)),
        by_domain=dict(Counter(ex.domain for ex in accepted)),
    )


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


__all__ = [
    "CTLGCorpusExclusion",
    "CTLGTrainingCorpusBuild",
    "build_ctlg_training_corpus",
]
