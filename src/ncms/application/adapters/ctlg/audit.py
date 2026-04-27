"""Grammar audit for CTLG corpora."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ncms.application.adapters.ctlg.corpus import CTLGDiagnostic, CTLGExample, validate_ctlg_jsonl
from ncms.domain.tlg.cue_taxonomy import TaggedToken
from ncms.domain.tlg.semantic_parser import synthesize


@dataclass(frozen=True)
class CTLGGrammarMiss:
    """One query row that did not synthesize into a TLGQuery."""

    path: str
    line_no: int
    text: str
    note: str
    cue_tags: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line_no": self.line_no,
            "text": self.text,
            "note": self.note,
            "cue_tags": list(self.cue_tags),
        }


@dataclass(frozen=True)
class CTLGFileAudit:
    """Grammar audit for one CTLG corpus file."""

    path: str
    rows_seen: int
    n_valid: int
    n_diagnostics: int
    n_query: int
    n_query_with_expected_tlg: int
    n_query_synthesized: int
    by_axis: dict[str, int] = field(default_factory=dict)
    by_relation: dict[str, int] = field(default_factory=dict)
    by_miss_note: dict[str, int] = field(default_factory=dict)
    validation_diagnostics: tuple[CTLGDiagnostic, ...] = ()
    misses: tuple[CTLGGrammarMiss, ...] = ()

    @property
    def query_synthesis_rate(self) -> float:
        if self.n_query == 0:
            return 1.0
        return self.n_query_synthesized / self.n_query

    @property
    def expected_tlg_coverage(self) -> float:
        if self.n_query == 0:
            return 1.0
        return self.n_query_with_expected_tlg / self.n_query

    def to_json(self, *, max_misses: int = 10) -> dict[str, Any]:
        return {
            "path": self.path,
            "rows_seen": self.rows_seen,
            "n_valid": self.n_valid,
            "n_diagnostics": self.n_diagnostics,
            "n_query": self.n_query,
            "n_query_with_expected_tlg": self.n_query_with_expected_tlg,
            "n_query_synthesized": self.n_query_synthesized,
            "query_synthesis_rate": self.query_synthesis_rate,
            "expected_tlg_coverage": self.expected_tlg_coverage,
            "by_axis": self.by_axis,
            "by_relation": self.by_relation,
            "by_miss_note": self.by_miss_note,
            "validation_diagnostics": [
                {
                    "path": d.path,
                    "line_no": d.line_no,
                    "code": d.code,
                    "message": d.message,
                }
                for d in self.validation_diagnostics[:max_misses]
            ],
            "misses": [miss.to_json() for miss in self.misses[:max_misses]],
        }


@dataclass(frozen=True)
class CTLGGrammarAuditReport:
    """Aggregate grammar audit over one or more CTLG corpus files."""

    files: tuple[CTLGFileAudit, ...]

    @property
    def rows_seen(self) -> int:
        return sum(file.rows_seen for file in self.files)

    @property
    def n_query(self) -> int:
        return sum(file.n_query for file in self.files)

    @property
    def n_query_synthesized(self) -> int:
        return sum(file.n_query_synthesized for file in self.files)

    @property
    def query_synthesis_rate(self) -> float:
        if self.n_query == 0:
            return 1.0
        return self.n_query_synthesized / self.n_query

    @property
    def expected_tlg_coverage(self) -> float:
        if self.n_query == 0:
            return 1.0
        expected = sum(file.n_query_with_expected_tlg for file in self.files)
        return expected / self.n_query

    @property
    def n_diagnostics(self) -> int:
        return sum(file.n_diagnostics for file in self.files)

    def ok(self, *, min_query_synthesis_rate: float = 0.0) -> bool:
        return self.n_diagnostics == 0 and self.query_synthesis_rate >= min_query_synthesis_rate

    def to_json(self, *, max_misses_per_file: int = 10) -> dict[str, Any]:
        return {
            "rows_seen": self.rows_seen,
            "n_query": self.n_query,
            "n_query_synthesized": self.n_query_synthesized,
            "query_synthesis_rate": self.query_synthesis_rate,
            "expected_tlg_coverage": self.expected_tlg_coverage,
            "n_diagnostics": self.n_diagnostics,
            "files": [
                file.to_json(max_misses=max_misses_per_file)
                for file in self.files
            ],
        }


def audit_ctlg_files(paths: list[str | Path]) -> CTLGGrammarAuditReport:
    """Validate CTLG JSONL files and audit query rows against the grammar."""
    return CTLGGrammarAuditReport(tuple(_audit_one(Path(path)) for path in paths))


def _audit_one(path: Path) -> CTLGFileAudit:
    validation = validate_ctlg_jsonl(path)
    axis: Counter[str] = Counter()
    relation: Counter[str] = Counter()
    miss_note: Counter[str] = Counter()
    misses: list[CTLGGrammarMiss] = []
    n_query = 0
    n_expected = 0
    n_synthesized = 0
    for idx, example in enumerate(validation.examples, start=1):
        if example.voice != "query":
            continue
        n_query += 1
        if example.expected_tlg_query is not None:
            n_expected += 1
        tlg_query = synthesize(_tagged_tokens(example))
        if tlg_query is None:
            miss_note[example.note] += 1
            misses.append(
                CTLGGrammarMiss(
                    path=str(path),
                    line_no=idx,
                    text=example.text,
                    note=example.note,
                    cue_tags=example.cue_tags,
                )
            )
            continue
        n_synthesized += 1
        axis[tlg_query.axis] += 1
        relation[tlg_query.relation] += 1
    return CTLGFileAudit(
        path=str(path),
        rows_seen=validation.rows_seen,
        n_valid=len(validation.examples),
        n_diagnostics=len(validation.diagnostics),
        n_query=n_query,
        n_query_with_expected_tlg=n_expected,
        n_query_synthesized=n_synthesized,
        by_axis=dict(axis),
        by_relation=dict(relation),
        by_miss_note=dict(miss_note),
        validation_diagnostics=validation.diagnostics,
        misses=tuple(misses),
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
    "CTLGFileAudit",
    "CTLGGrammarAuditReport",
    "CTLGGrammarMiss",
    "audit_ctlg_files",
]
