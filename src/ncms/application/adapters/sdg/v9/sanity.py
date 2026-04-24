"""Offline corpus sanity check.

Validates a generated v9 :class:`GoldExample` corpus against every
invariant the downstream trainer and judge both assume.  Runs
offline — no LLM cost — and is the first gate after any
generation run, before the (expensive) LLM judge fires.

The invariants, grouped by what downstream breaks if one fails:

**Labeling** — the 5 classification heads can't train on ``None``.
The intent head is covered upstream (``loader.load_jsonl`` rejects
``intent=null`` at read time), so we only re-check the four heads
the loader permits as ``None``:

  I2. ``admission`` is non-None.
  I3. ``state_change`` is non-None.
  I4. ``topic`` is non-None AND is in the domain's topic vocab.

**Slots** — the role head + slot-value extraction need these:

  S1. ``slots`` is a non-empty dict (for archetypes with
      declared ``role_spans``).

**Role spans** — role-head training targets:

  R1. ``role_spans`` is non-empty (for archetypes with declared
      role_spans).
  R2. Every declared ``(role, slot, count)`` in the archetype's
      role_spans is satisfied by the row's ``role_spans`` —
      same count, same slot, same role.
  R3. Every primary / alternative role span's surface appears
      in the text (case-insensitive substring).

**Text hygiene:**

  T1. Text is non-empty.
  T2. No literal ``{placeholder}`` tokens remain.
  T3. Text length in the archetype's character envelope.

Returns a :class:`SanityReport` with per-invariant hit counts
plus a sample of failing rows for each kind so the operator
can pinpoint the archetype / sampling stage at fault.

Caller is expected to HARD-FAIL on any ``fatal`` counts > 0
before committing the corpus or handing it to the trainer.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ncms.application.adapters.corpus.loader import load_jsonl

if TYPE_CHECKING:
    from ncms.application.adapters.domain_loader import DomainSpec
    from ncms.application.adapters.schemas import GoldExample


_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class InvariantFailure:
    """One row failed one invariant."""

    row_index: int
    archetype: str
    invariant: str
    detail: str
    text_preview: str


@dataclass
class SanityReport:
    """Aggregate sanity-check output for one corpus file.

    Every "bucket" is a :class:`Counter` so callers can render
    per-invariant counts trivially.  ``failures`` keeps up to
    ``sample_cap`` examples per invariant for operator review.

    ``ok`` is ``True`` iff every invariant passed.  Use this as a
    hard gate:

    >>> report = sanity_check(path, spec)
    >>> if not report.ok:
    ...     raise SystemExit(f"sanity failed: {report.summary()}")
    """

    corpus_path: Path
    domain: str
    n_rows: int = 0
    per_archetype_rows: Counter[str] = field(default_factory=Counter)
    failure_counts: Counter[str] = field(default_factory=Counter)
    failures: list[InvariantFailure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failure_counts

    def summary(self) -> str:
        if self.ok:
            return (
                f"sanity OK: {self.n_rows} rows across "
                f"{len(self.per_archetype_rows)} archetypes"
            )
        counts = ", ".join(
            f"{k}={v}" for k, v in self.failure_counts.most_common()
        )
        return (
            f"sanity FAILED: {self.n_rows} rows, "
            f"{sum(self.failure_counts.values())} invariant violations "
            f"({counts})"
        )

    def as_dict(self) -> dict:
        return {
            "corpus_path": str(self.corpus_path),
            "domain": self.domain,
            "n_rows": self.n_rows,
            "per_archetype_rows": dict(self.per_archetype_rows),
            "failure_counts": dict(self.failure_counts),
            "ok": self.ok,
            "failures": [
                {
                    "row_index": f.row_index,
                    "archetype": f.archetype,
                    "invariant": f.invariant,
                    "detail": f.detail,
                    "text_preview": f.text_preview,
                }
                for f in self.failures
            ],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _archetype_name(ex: "GoldExample") -> str:
    """Extract archetype name from the v9 provenance source string."""
    src = ex.source or ""
    marker = "archetype="
    idx = src.find(marker)
    if idx < 0:
        return "unknown"
    tail = src[idx + len(marker):]
    end = tail.find(" ")
    return tail if end < 0 else tail[:end]


def _archetype_lookup(spec: "DomainSpec") -> dict:
    return {a.name: a for a in spec.archetypes}


def _role_span_composition(role_spans) -> dict:
    """Count ``(role, slot)`` pairs in a row's role_spans, excluding
    not_relevant (which is permitted extra)."""
    out: dict[tuple[str, str], int] = {}
    for rs in role_spans:
        if rs.role == "not_relevant":
            continue
        out[(rs.role, rs.slot)] = out.get((rs.role, rs.slot), 0) + 1
    return out


def _record(
    report: SanityReport,
    invariant: str,
    row_index: int,
    archetype: str,
    detail: str,
    text: str,
    sample_cap: int = 5,
) -> None:
    """Accumulate a failure (per-invariant sample cap applies)."""
    report.failure_counts[invariant] += 1
    per_invariant = sum(
        1 for f in report.failures if f.invariant == invariant
    )
    if per_invariant < sample_cap:
        report.failures.append(InvariantFailure(
            row_index=row_index,
            archetype=archetype,
            invariant=invariant,
            detail=detail,
            text_preview=text[:120],
        ))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sanity_check(
    corpus_path: Path,
    spec: "DomainSpec",
    *,
    sample_cap: int = 5,
) -> SanityReport:
    """Run every invariant over ``corpus_path`` and return a report.

    ``sample_cap`` caps the number of failure examples retained per
    invariant (to keep the report readable on big corpora).  Counts
    are always exact.

    This is a pure function: no network, no LLM, no mutation of
    the corpus file.
    """
    rows = load_jsonl(corpus_path)
    report = SanityReport(
        corpus_path=Path(corpus_path),
        domain=spec.name,
        n_rows=len(rows),
    )
    archetype_map = _archetype_lookup(spec)
    topics_ok = set(spec.topics)

    for idx, ex in enumerate(rows):
        arch_name = _archetype_name(ex)
        report.per_archetype_rows[arch_name] += 1
        arch = archetype_map.get(arch_name)
        text = ex.text or ""

        # T1: non-empty text.
        if not text.strip():
            _record(
                report, "T1_text_empty", idx, arch_name,
                "text is empty", text, sample_cap,
            )
            # Remaining invariants don't apply — continue.
            continue

        # T2: no placeholder leakage.
        leaks = _PLACEHOLDER_RE.findall(text)
        if leaks:
            _record(
                report, "T2_placeholder_leak", idx, arch_name,
                f"unfilled placeholders: {leaks}",
                text, sample_cap,
            )

        # I2–I4: label presence.  Intent is enforced upstream by
        # load_jsonl (rejects intent=null at read time) so we don't
        # re-check it here.
        if ex.admission is None:
            _record(report, "I2_admission_none", idx, arch_name,
                    "admission is None", text, sample_cap)
        if ex.state_change is None:
            _record(report, "I3_state_change_none", idx, arch_name,
                    "state_change is None", text, sample_cap)
        if ex.topic is None:
            _record(report, "I4_topic_none", idx, arch_name,
                    "topic is None", text, sample_cap)
        elif ex.topic not in topics_ok:
            _record(report, "I4_topic_unknown", idx, arch_name,
                    f"topic {ex.topic!r} not in domain topic vocab",
                    text, sample_cap)

        # Archetype-linked checks require knowing the archetype spec.
        if arch is None:
            _record(
                report, "P0_unknown_archetype", idx, arch_name,
                f"archetype {arch_name!r} not in spec.archetypes",
                text, sample_cap,
            )
            continue

        # T3: length envelope.
        if len(text) < arch.target_min_chars:
            _record(
                report, "T3_too_short", idx, arch_name,
                f"len={len(text)} < {arch.target_min_chars}",
                text, sample_cap,
            )
        elif len(text) > arch.target_max_chars:
            _record(
                report, "T3_too_long", idx, arch_name,
                f"len={len(text)} > {arch.target_max_chars}",
                text, sample_cap,
            )

        has_role_spans_required = any(rs.count > 0 for rs in arch.role_spans)

        # S1: slots non-empty when role_spans declared.
        if has_role_spans_required and not ex.slots:
            _record(
                report, "S1_slots_empty", idx, arch_name,
                "slots is empty but archetype declares role_spans",
                text, sample_cap,
            )

        # R1: role_spans non-empty when archetype declared them.
        if has_role_spans_required and not ex.role_spans:
            _record(
                report, "R1_role_spans_empty", idx, arch_name,
                "row has no role_spans, archetype required some",
                text, sample_cap,
            )

        # R2: role_span composition matches archetype declaration.
        expected_composition: dict[tuple[str, str], int] = {}
        for rs in arch.role_spans:
            if rs.count > 0:
                key = (rs.role, rs.slot)
                expected_composition[key] = (
                    expected_composition.get(key, 0) + rs.count
                )
        found_composition = _role_span_composition(ex.role_spans or [])
        if has_role_spans_required and found_composition != expected_composition:
            _record(
                report, "R2_role_span_mismatch", idx, arch_name,
                f"expected {expected_composition}, got {found_composition}",
                text, sample_cap,
            )

        # R3: primary / alternative surfaces present in text.
        lowered_text = text.lower()
        for rs in ex.role_spans or []:
            if rs.role not in ("primary", "alternative"):
                continue
            if rs.surface.lower() not in lowered_text:
                _record(
                    report, "R3_surface_missing_from_text", idx, arch_name,
                    f"role_span surface {rs.surface!r} not in text",
                    text, sample_cap,
                )
                break  # one failure per row is enough signal

    return report


def format_report(report: SanityReport) -> str:
    """Render ``report`` for terminal display."""
    lines: list[str] = []
    lines.append(
        f"=== sanity: domain={report.domain} "
        f"rows={report.n_rows} file={report.corpus_path.name} ==="
    )
    if report.ok:
        lines.append("  status: OK — every invariant satisfied")
    else:
        lines.append(
            f"  status: FAILED — "
            f"{sum(report.failure_counts.values())} violations across "
            f"{len(report.failure_counts)} invariants",
        )
    lines.append("  per-archetype row counts:")
    for name, n in sorted(report.per_archetype_rows.items()):
        lines.append(f"    · {name:40s} {n}")
    if report.failure_counts:
        lines.append("  invariant violations:")
        for inv, count in report.failure_counts.most_common():
            lines.append(f"    · {inv:32s} {count}")
        lines.append("  sample failures:")
        for f in report.failures[:10]:
            lines.append(
                f"    [{f.invariant}] row={f.row_index} "
                f"archetype={f.archetype}"
            )
            lines.append(f"      detail: {f.detail}")
            lines.append(f"      text:   {f.text_preview}")
    return "\n".join(lines)


def write_report_json(report: SanityReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")


__all__ = [
    "InvariantFailure",
    "SanityReport",
    "format_report",
    "sanity_check",
    "write_report_json",
]
