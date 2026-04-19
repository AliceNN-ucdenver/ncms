"""Property-based validators for the temporal-trajectory grammar.

Runs on any corpus (hand-labeled or mock-generated) and asserts
structural invariants the grammar assumes.  Violations signal
corpus errors (not grammar errors) — useful both as a CI check
during the experiment and as a template for integration-time
validation inside NCMS.

Invariants validated:

* **Zone reachability**: every subject-assigned memory belongs to
  some zone.
* **Current-zone uniqueness**: every subject has exactly one
  current zone (no zone-terminal fork, no dead-ends).
* **No cycles**: the typed-edge graph per subject is acyclic.
* **Mid-reference hygiene**: no ``retires_entities`` contains a
  mid-like doc reference (``ADR-xxx`` / ``MED-xx`` / ``PROJ-xx``) —
  these are cross-links, not retired entities.
* **Chronological edges**: ``src.observed_at < dst.observed_at`` for
  every typed edge (temporal ordering).
* **Subject consistency**: both endpoints of every typed edge share
  the same subject.
* **Origin uniqueness**: each subject has exactly one earliest
  memory (the zone root of the earliest zone).

Each check returns a list of violation descriptions.  Empty list =
passes.

Usage::

    from experiments.temporal_trajectory.properties import (
        validate_all, ValidationReport,
    )

    report = validate_all()
    if report.has_violations():
        print(report)
        raise SystemExit(1)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from experiments.temporal_trajectory.corpus import (
    ADR_CORPUS,
    EDGES,
)
from experiments.temporal_trajectory.grammar import (
    ADMISSIBLE_TRANSITIONS,
    compute_zones,
    current_zone,
    origin_memory,
)


_MID_PATTERN = r"^[A-Z]{2,5}-\w*$"


@dataclass
class ValidationReport:
    zone_reachability: list[str] = field(default_factory=list)
    current_uniqueness: list[str] = field(default_factory=list)
    acyclic: list[str] = field(default_factory=list)
    mid_hygiene: list[str] = field(default_factory=list)
    chronological: list[str] = field(default_factory=list)
    subject_consistency: list[str] = field(default_factory=list)
    origin_uniqueness: list[str] = field(default_factory=list)

    def has_violations(self) -> bool:
        return any([
            self.zone_reachability,
            self.current_uniqueness,
            self.acyclic,
            self.mid_hygiene,
            self.chronological,
            self.subject_consistency,
            self.origin_uniqueness,
        ])

    def total_violations(self) -> int:
        return sum([
            len(self.zone_reachability),
            len(self.current_uniqueness),
            len(self.acyclic),
            len(self.mid_hygiene),
            len(self.chronological),
            len(self.subject_consistency),
            len(self.origin_uniqueness),
        ])

    def __str__(self) -> str:
        lines = ["Grammar property validation", "=" * 60]
        sections = [
            ("Zone reachability", self.zone_reachability),
            ("Current-zone uniqueness", self.current_uniqueness),
            ("Acyclic per subject", self.acyclic),
            ("Mid-reference hygiene", self.mid_hygiene),
            ("Chronological edges", self.chronological),
            ("Subject consistency", self.subject_consistency),
            ("Origin uniqueness", self.origin_uniqueness),
        ]
        for name, violations in sections:
            if violations:
                lines.append(f"  ✗ {name}: {len(violations)} violation(s)")
                for v in violations[:5]:
                    lines.append(f"      {v}")
                if len(violations) > 5:
                    lines.append(f"      …and {len(violations) - 5} more")
            else:
                lines.append(f"  ✓ {name}")
        lines.append(f"Total violations: {self.total_violations()}")
        return "\n".join(lines)


def _subjects() -> set[str]:
    return {m.subject for m in ADR_CORPUS if m.subject is not None}


def validate_zone_reachability() -> list[str]:
    """Every subject-assigned memory belongs to some zone."""
    violations: list[str] = []
    for subject in _subjects():
        zones = compute_zones(subject)
        all_zone_mids = {mid for z in zones for mid in z.memory_ids}
        subj_mems = [m.mid for m in ADR_CORPUS if m.subject == subject]
        for mid in subj_mems:
            if mid not in all_zone_mids:
                violations.append(
                    f"{mid} (subject={subject}) is not in any zone"
                )
    return violations


def validate_current_zone_uniqueness() -> list[str]:
    """Each subject has exactly one current zone."""
    violations: list[str] = []
    for subject in _subjects():
        zones = compute_zones(subject)
        if not zones:
            violations.append(f"subject={subject}: no zones computed")
            continue
        terminals = [z for z in zones if z.ended_transition is None]
        if len(terminals) == 0:
            violations.append(
                f"subject={subject}: no current zone (all zones closed "
                f"by supersedes/retires)"
            )
        elif len(terminals) > 1:
            mids = [z.terminal_mid for z in terminals]
            violations.append(
                f"subject={subject}: {len(terminals)} candidate current "
                f"zones (terminals: {mids}).  current_zone() "
                f"arbitrates by latest observed_at."
            )
    return violations


def validate_acyclic() -> list[str]:
    """Per-subject typed-edge graph is acyclic."""
    violations: list[str] = []
    mem_subj = {m.mid: m.subject for m in ADR_CORPUS}
    for subject in _subjects():
        edges = [
            e for e in EDGES
            if mem_subj.get(e.src) == subject
            and mem_subj.get(e.dst) == subject
            and e.transition in ADMISSIBLE_TRANSITIONS
        ]
        adj: dict[str, list[str]] = {}
        for e in edges:
            adj.setdefault(e.src, []).append(e.dst)
        # DFS for back-edges.
        WHITE, GREY, BLACK = 0, 1, 2
        colour: dict[str, int] = {}

        def _visit(u: str) -> str | None:
            colour[u] = GREY
            for v in adj.get(u, []):
                c = colour.get(v, WHITE)
                if c == GREY:
                    return f"{u} → {v}"
                if c == WHITE:
                    cycle = _visit(v)
                    if cycle is not None:
                        return cycle
            colour[u] = BLACK
            return None

        for node in adj.keys():
            if colour.get(node, WHITE) == WHITE:
                cycle = _visit(node)
                if cycle is not None:
                    violations.append(
                        f"subject={subject}: cycle detected at edge {cycle}"
                    )
                    break
    return violations


def validate_mid_hygiene() -> list[str]:
    """No retires_entities contains a mid-like doc reference."""
    import re
    pattern = re.compile(_MID_PATTERN)
    violations: list[str] = []
    for e in EDGES:
        for ent in e.retires_entities:
            if pattern.match(ent):
                violations.append(
                    f"edge {e.src}→{e.dst} retires_entities contains "
                    f"mid-like reference {ent!r}"
                )
    return violations


def validate_chronological() -> list[str]:
    """Every admissible edge has src.observed_at < dst.observed_at."""
    violations: list[str] = []
    by_id = {m.mid: m for m in ADR_CORPUS}
    for e in EDGES:
        if e.transition not in ADMISSIBLE_TRANSITIONS:
            continue
        src = by_id.get(e.src)
        dst = by_id.get(e.dst)
        if src is None or dst is None:
            violations.append(
                f"edge {e.src}→{e.dst}: endpoint not in corpus"
            )
            continue
        if not (src.observed_at < dst.observed_at):
            violations.append(
                f"edge {e.src}→{e.dst}: src.observed_at "
                f"({src.observed_at.date()}) >= dst.observed_at "
                f"({dst.observed_at.date()})"
            )
    return violations


def validate_subject_consistency() -> list[str]:
    """Both endpoints of every typed edge share the same subject."""
    violations: list[str] = []
    mem_subj = {m.mid: m.subject for m in ADR_CORPUS}
    for e in EDGES:
        if e.transition not in ADMISSIBLE_TRANSITIONS:
            continue
        src_s = mem_subj.get(e.src)
        dst_s = mem_subj.get(e.dst)
        if src_s != dst_s:
            violations.append(
                f"edge {e.src}({src_s})→{e.dst}({dst_s}): "
                f"subject mismatch"
            )
    return violations


def validate_origin_uniqueness() -> list[str]:
    """origin_memory(subject) is deterministic — returns exactly one mid."""
    violations: list[str] = []
    for subject in _subjects():
        origin = origin_memory(subject)
        if origin is None:
            violations.append(f"subject={subject}: origin_memory = None")
    return violations


def validate_all() -> ValidationReport:
    return ValidationReport(
        zone_reachability=validate_zone_reachability(),
        current_uniqueness=validate_current_zone_uniqueness(),
        acyclic=validate_acyclic(),
        mid_hygiene=validate_mid_hygiene(),
        chronological=validate_chronological(),
        subject_consistency=validate_subject_consistency(),
        origin_uniqueness=validate_origin_uniqueness(),
    )


if __name__ == "__main__":
    report = validate_all()
    print(report)
    if report.has_violations():
        raise SystemExit(1)
