"""Shared CLI runner boilerplate: logging setup, git SHA, metadata header.

Extracts common infrastructure used across benchmark runners (ablation,
dream cycle, etc.) to avoid duplication.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import subprocess
import sys
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("benchmarks")


def get_git_sha() -> str:
    """Return current short git SHA, or 'unknown' if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def setup_logging(
    name: str,
    output_dir: Path,
    verbose: bool = False,
) -> Path:
    """Configure timestamped log file + console logging.

    Creates a uniquely named log file per run and a convenience symlink.

    Args:
        name: Log file prefix (e.g. "ablation", "dream").
        output_dir: Directory for log files (created if needed).
        verbose: If True, set log level to DEBUG; otherwise INFO.

    Returns:
        Path to the created log file.
    """
    # Ensure .env is loaded (HF_TOKEN for SPLADE model access, etc.)
    from benchmarks.env import load_dotenv as _load_dotenv

    _load_dotenv()

    output_dir.mkdir(parents=True, exist_ok=True)

    # Timestamped log file (never overwrites previous runs)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = output_dir / f"{name}_{timestamp}.log"

    level = logging.DEBUG if verbose else logging.INFO

    # File handler: full ISO timestamps for durable review
    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    # Console handler: shorter timestamps for readability
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    ))

    logging.basicConfig(level=level, handlers=[file_handler, console_handler])

    # Symlink latest log for convenience: {name}_latest.log -> {name}_<ts>.log
    latest_link = output_dir / f"{name}_latest.log"
    try:
        latest_link.unlink(missing_ok=True)
        latest_link.symlink_to(log_file.name)
    except OSError:
        pass  # Windows or permission issues

    return log_file


def log_run_header(name: str, run_logger: logging.Logger) -> None:
    """Log system info: Python version, platform, git SHA, PID, NCMS config summary.

    Args:
        name: Human-readable run name (e.g. "NCMS Retrieval Pipeline Ablation Study").
        run_logger: Logger instance to write header lines to.
    """
    run_logger.info("=" * 70)
    run_logger.info("%s", name)
    run_logger.info("=" * 70)
    run_logger.info("  Start time : %s", datetime.now(UTC).isoformat())
    run_logger.info("  Git SHA    : %s", get_git_sha())
    run_logger.info("  Python     : %s", platform.python_version())
    run_logger.info("  Platform   : %s %s", platform.system(), platform.machine())
    run_logger.info("  PID        : %d", os.getpid())

    # Log NCMS config summary (non-default env vars)
    ncms_vars = {k: v for k, v in sorted(os.environ.items()) if k.startswith("NCMS_")}
    if ncms_vars:
        run_logger.info("  NCMS env   :")
        for key, val in ncms_vars.items():
            run_logger.info("    %s = %s", key, val)

    run_logger.info("=" * 70)


def run_async(coro: Coroutine[Any, Any, Any], name: str) -> None:
    """Run an async coroutine with standard error handling.

    Wraps ``asyncio.run()`` with KeyboardInterrupt and Exception handling,
    logging errors and exiting with appropriate codes.

    Args:
        coro: The coroutine to execute.
        name: Human-readable name for error messages (e.g. "Ablation study").
    """
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        logger.warning("Run interrupted by user (Ctrl+C)")
        sys.exit(130)
    except Exception:
        logger.exception("%s failed with unhandled exception", name)
        sys.exit(1)
