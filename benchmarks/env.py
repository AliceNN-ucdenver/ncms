"""Shared environment loader for benchmark scripts.

Loads .env from the project root (for HF_TOKEN, etc.) without
requiring python-dotenv as a dependency.
"""

from __future__ import annotations

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv() -> None:
    """Load .env from project root into os.environ (does not override)."""
    env_file = _PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value
