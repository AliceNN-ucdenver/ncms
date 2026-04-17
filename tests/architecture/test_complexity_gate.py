"""Fitness function: fail if any application method becomes D+ complexity.

We spent Phase 0 driving ``src/ncms/application/`` down to zero D-grade
methods.  This test enforces that invariant so a future change can't
silently reintroduce a 500-line method.

Scope: ``src/ncms/application/`` only.  ``domain/temporal_parser.py``
has one E-grade regex function that is out of scope, and demo/CLI code
is allowed to be more procedural.

If a new method legitimately needs to be D grade (rare), add it to
``ALLOWLIST`` with a comment explaining why, instead of silencing
this test globally.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from radon.complexity import cc_rank, cc_visit

# Methods we've consciously accepted at D.  Empty today — keep it that
# way unless there's a documented reason.  Format: (file_path_suffix,
# qualified_method_name) tuples.
ALLOWLIST: set[tuple[str, str]] = set()

APPLICATION_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "ncms" / "application"
)


def _iter_py_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


@pytest.mark.parametrize(
    "py_file",
    list(_iter_py_files(APPLICATION_DIR)),
    ids=lambda p: str(p.relative_to(APPLICATION_DIR)),
)
def test_no_d_plus_complexity_in_application(py_file: Path) -> None:
    """Every block in application/ must be CC grade C or better."""
    source = py_file.read_text()
    blocks = cc_visit(source)

    offenders = []
    for block in blocks:
        grade = cc_rank(block.complexity)
        if grade in ("D", "E", "F"):
            suffix = str(py_file).split("src/ncms/application/")[-1]
            name = f"{block.classname}.{block.name}" if block.classname else block.name
            key = (suffix, name)
            if key in ALLOWLIST:
                continue
            offenders.append(
                f"{suffix}:{block.lineno} {name} — "
                f"{grade} ({block.complexity})",
            )

    assert not offenders, (
        "Application methods regressed to D+ complexity.\n"
        "Refactor the offenders or add a justified entry to ALLOWLIST "
        "in tests/architecture/test_complexity_gate.py.\n\n"
        + "\n".join(offenders)
    )
