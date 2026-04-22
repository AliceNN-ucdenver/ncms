"""Fitness function: fail if any method becomes D+ complexity.

Phase 0 drove ``src/ncms/application/`` down to zero D-grade methods
and the rest of the codebase to a single documented exception.  This
test enforces that invariant across **all** of ``src/ncms/`` (minus
demo/ and nemoclaw_nd/, which are allowed to be more procedural).

If a new method legitimately needs to be D grade (rare), add it to
``ALLOWLIST`` with a comment explaining why, rather than silencing
this test globally.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from radon.complexity import cc_rank, cc_visit

# Methods we've consciously accepted at D or higher.  Each entry must
# have a documented reason.  Format: (file_path_suffix, qualified_method_name).
ALLOWLIST: set[tuple[str, str]] = {
    # Regex-based temporal expression parser.  CC=31 is a pessimistic
    # count — every branch is a regex dispatch ("if this pattern matches,
    # return this TemporalReference") rather than true conditional logic.
    # Refactoring into a dispatch table would add indirection without
    # clarifying behavior.  Fully unit-tested in
    # tests/unit/domain/test_temporal_parser.py.
    ("domain/temporal_parser.py", "parse_temporal_reference"),
    # Pre-existing F-grade orchestrators targeted by the
    # ``validated-giggling-dove`` refactor plan (phases 2 + 9).  Both
    # are composition-root methods that walk a long sequential pipeline
    # of SLM extraction → admission gate → persist → SLM side-effects
    # → indexing fork → node creation.  The refactor plan extracts
    # 5-6 sub-pipelines from each; holding that work out of the
    # slm-entity-extraction-design PR to keep the diff readable.
    ("application/memory_service.py", "MemoryService.store_memory"),
    ("application/index_worker.py", "IndexWorkerPool._detect_and_create_l2_node"),
    # 6-head SLM merge loop: each head has a 3-clause gate
    # (confident OR last-backend OR null-confidence pass-through).
    # Splitting into per-head helpers would duplicate the control
    # structure without reducing the branching count.  Unit-tested
    # in tests/unit/infrastructure/extraction/intent_slot/.
    ("infrastructure/extraction/intent_slot/factory.py", "ChainedExtractor.extract"),
}

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "ncms"

# Directories allowed to contain higher-complexity procedural code.
# Demo scripts read top-to-bottom; forcing them into tidy functions
# reduces readability for the tutorial use case.
EXCLUDED_DIRS = {"demo"}


def _iter_py_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if any(excluded in path.parts for excluded in EXCLUDED_DIRS):
            continue
        yield path


def _relative_suffix(py_file: Path) -> str:
    """Relative path from SRC_ROOT, used as the allowlist key prefix."""
    return str(py_file.relative_to(SRC_ROOT))


@pytest.mark.parametrize(
    "py_file",
    list(_iter_py_files(SRC_ROOT)),
    ids=_relative_suffix,
)
def test_no_d_plus_complexity(py_file: Path) -> None:
    """Every block in src/ncms/ (except demo/) must be CC grade C or better.

    Exceptions must be explicitly listed in ``ALLOWLIST`` with a
    rationale.
    """
    source = py_file.read_text()
    blocks = cc_visit(source)
    suffix = _relative_suffix(py_file)

    offenders = []
    for block in blocks:
        grade = cc_rank(block.complexity)
        if grade not in ("D", "E", "F"):
            continue
        name = (
            f"{block.classname}.{block.name}"
            if block.classname else block.name
        )
        if (suffix, name) in ALLOWLIST:
            continue
        offenders.append(
            f"{suffix}:{block.lineno} {name} — "
            f"{grade} ({block.complexity})",
        )

    assert not offenders, (
        "Methods regressed to D+ complexity.\n"
        "Refactor the offenders or add a justified entry to ALLOWLIST "
        "in tests/architecture/test_complexity_gate.py.\n\n"
        + "\n".join(offenders)
    )
