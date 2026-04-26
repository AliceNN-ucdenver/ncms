"""Fitness function: enforce import direction between architectural layers.

Two invariants:

1. **Domain purity.**  ``src/ncms/domain/`` must not import from
   ``application/`` or ``infrastructure/``.  Domain is the inner ring
   of Clean Architecture — everything else depends on it, never the
   other way around.

2. **Pipeline isolation.**  The five pipeline packages extracted in
   Phase 0 — ``scoring``, ``retrieval``, ``enrichment``, ``ingestion``,
   ``traversal`` — must not import each other.  They communicate only
   through ``MemoryService``, which composes them.  This keeps each
   pipeline independently replaceable and prevents accidental
   back-channels.

A failing test means someone reached across a boundary.  Either the
dependency direction is wrong (fix the code) or the architecture has
evolved intentionally (update this test with a comment explaining why).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "ncms"
DOMAIN_ROOT = SRC_ROOT / "domain"
APPLICATION_ROOT = SRC_ROOT / "application"

PIPELINE_PACKAGES = (
    "scoring",
    "retrieval",
    "enrichment",
    "ingestion",
    "traversal",
)


def _extract_imports(py_file: Path) -> set[str]:
    """Return all imported module names (including 'from X import ...')."""
    tree = ast.parse(py_file.read_text())
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _iter_py_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


# ── Invariant 1: domain has no outward dependencies ─────────────────


@pytest.mark.parametrize(
    "py_file",
    list(_iter_py_files(DOMAIN_ROOT)),
    ids=lambda p: str(p.relative_to(DOMAIN_ROOT)),
)
def test_domain_has_no_outward_imports(py_file: Path) -> None:
    """No ``domain/`` module may import from application/ or infrastructure/."""
    forbidden = ("ncms.application", "ncms.infrastructure")
    imports = _extract_imports(py_file)
    violations = [imp for imp in imports if any(imp.startswith(f) for f in forbidden)]
    assert not violations, (
        f"domain module {py_file.relative_to(DOMAIN_ROOT)} imports "
        f"outside the domain layer:\n  " + "\n  ".join(sorted(violations))
    )


# ── Invariant 2: pipeline packages don't import each other ───────────


def _pipeline_files(package: str) -> list[Path]:
    return list(_iter_py_files(APPLICATION_ROOT / package))


@pytest.mark.parametrize("package", PIPELINE_PACKAGES)
def test_pipeline_does_not_import_siblings(package: str) -> None:
    """A pipeline package must not import from another pipeline package."""
    sibling_prefixes = tuple(
        f"ncms.application.{other}" for other in PIPELINE_PACKAGES if other != package
    )
    violations: list[str] = []
    for py_file in _pipeline_files(package):
        imports = _extract_imports(py_file)
        for imp in imports:
            if any(imp.startswith(p) for p in sibling_prefixes):
                rel = py_file.relative_to(APPLICATION_ROOT)
                violations.append(f"{rel}: imports {imp}")
    assert not violations, (
        f"Pipeline '{package}' reaches into a sibling pipeline.  "
        f"Pipelines must compose through MemoryService, not each other.\n  "
        + "\n  ".join(violations)
    )


# ── Invariant 3: memory_service is not imported by its own pipelines ──


@pytest.mark.parametrize("package", PIPELINE_PACKAGES)
def test_pipeline_does_not_import_memory_service(package: str) -> None:
    """Pipelines must not depend on their composition root.

    MemoryService imports the pipelines (top-down).  If a pipeline
    imports MemoryService, we have a circular ownership and the
    pipeline is no longer independently testable.
    """
    violations: list[str] = []
    for py_file in _pipeline_files(package):
        imports = _extract_imports(py_file)
        for imp in imports:
            if imp == "ncms.application.memory_service":
                rel = py_file.relative_to(APPLICATION_ROOT)
                violations.append(f"{rel}: imports {imp}")
    assert not violations, (
        f"Pipeline '{package}' imports MemoryService, creating a "
        f"circular dependency.  Accept the dep via constructor "
        f"instead.\n  " + "\n  ".join(violations)
    )
