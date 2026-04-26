"""Fitness function: no dead code in src/ncms/.

Runs vulture at 80% confidence and fails if anything new surfaces.
Intentional false positives (signal handler ``frame``, Prometheus shim
``amount``) are suppressed via ``.vulture_whitelist.py`` at repo root.

Skipped cleanly when vulture is not installed, so the default test run
doesn't require it.  Install with ``uv sync --extra dev`` or run
directly with ``uv run --with vulture pytest tests/architecture/``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "ncms"
WHITELIST = REPO_ROOT / ".vulture_whitelist.py"


def _vulture_available() -> bool:
    try:
        subprocess.run(
            [sys.executable, "-c", "import vulture"],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return True


@pytest.mark.skipif(
    not _vulture_available(),
    reason="vulture not installed; run `uv run --with vulture pytest`",
)
def test_no_dead_code_in_src() -> None:
    """No new dead-code findings at vulture confidence >= 80%."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "vulture",
            str(SRC),
            str(WHITELIST),
            "--min-confidence",
            "80",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "Vulture found dead code in src/ncms/.\n"
        "Either remove the dead code or add to .vulture_whitelist.py "
        "with a comment explaining why.\n\n"
        f"{result.stdout}{result.stderr}"
    )
