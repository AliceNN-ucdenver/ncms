"""Run the NCMS retrieval pipeline ablation study.

Evaluates each pipeline component's contribution using standard BEIR
IR benchmark datasets. Produces publishable tables and charts.

Usage:
    uv run python -m benchmarks.beir.run_ablation
    uv run python -m benchmarks.beir.run_ablation --datasets scifact
    uv run python -m benchmarks.beir.run_ablation --datasets scifact,nfcorpus --verbose

Logging:
    Each run creates a timestamped log file in the output directory:
      benchmarks/results/ablation_2026-03-14_140532.log
    The latest run is also symlinked as ablation_latest.log for convenience.
    All output goes to both the log file and stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.beir.harness import evaluate_dataset
from benchmarks.core.configs import ABLATION_CONFIGS
from benchmarks.core.datasets import SUPPORTED_DATASETS, load_beir_dataset
from benchmarks.core.report import save_results

logger = logging.getLogger("benchmarks")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="NCMS Retrieval Pipeline Ablation Study",
    )
    parser.add_argument(
        "--datasets",
        type=str,
        default=",".join(SUPPORTED_DATASETS),
        help=f"Comma-separated dataset names (default: {','.join(SUPPORTED_DATASETS)})",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="benchmarks/results",
        help="Output directory for results (default: benchmarks/results)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args()


async def run_ablation(
    dataset_names: list[str],
    output_dir: str,
) -> dict[str, dict[str, dict[str, float]]]:
    """Run the full ablation study.

    Args:
        dataset_names: List of BEIR dataset names to evaluate.
        output_dir: Directory to write results.

    Returns:
        {dataset_name: {config_name: {metric: value}}}
    """
    configs = ABLATION_CONFIGS

    all_results: dict[str, dict[str, dict[str, float]]] = {}

    total_start = time.perf_counter()

    for dataset_name in dataset_names:
        logger.info("Loading dataset: %s", dataset_name)
        corpus, queries, qrels = load_beir_dataset(dataset_name)

        dataset_results = await evaluate_dataset(
            dataset_name=dataset_name,
            corpus=corpus,
            queries=queries,
            qrels=qrels,
            configs=configs,
        )

        all_results[dataset_name] = dataset_results

    total_elapsed = time.perf_counter() - total_start
    logger.info("=" * 70)
    logger.info("Ablation study complete")
    logger.info("  End time : %s", datetime.now(UTC).isoformat())
    logger.info("  Duration : %.1f seconds (%.1f minutes)", total_elapsed, total_elapsed / 60)
    logger.info("  Datasets : %d evaluated", len(all_results))
    logger.info("=" * 70)

    # Save results
    save_results(all_results, output_dir)

    # Print summary to stdout
    from benchmarks.core.report import generate_summary_table

    print("\n" + generate_summary_table(all_results))

    return all_results


def _get_git_sha() -> str:
    """Get short git SHA for provenance, or 'unknown' if not in a repo."""
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


def _setup_logging(output_dir: str, verbose: bool) -> Path:
    """Configure timestamped log file + console logging.

    Creates a uniquely named log file per run and a convenience symlink.
    Returns the path to the log file.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Timestamped log file (never overwrites previous runs)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = output_path / f"ablation_{timestamp}.log"

    level = logging.DEBUG if verbose else logging.INFO

    # File handler: full ISO timestamps for durable review
    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    # Console handler: shorter timestamps for readability
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    logging.basicConfig(level=level, handlers=[file_handler, console_handler])

    # Symlink latest log for convenience: ablation_latest.log -> ablation_<ts>.log
    latest_link = output_path / "ablation_latest.log"
    try:
        latest_link.unlink(missing_ok=True)
        latest_link.symlink_to(log_file.name)
    except OSError:
        pass  # Windows or permission issues

    return log_file


def _log_run_header(dataset_names: list[str], output_dir: str, log_file: Path) -> None:
    """Log a metadata header with system info for provenance."""
    logger.info("=" * 70)
    logger.info("NCMS Retrieval Pipeline Ablation Study")
    logger.info("=" * 70)
    logger.info("  Start time : %s", datetime.now(UTC).isoformat())
    logger.info("  Git SHA    : %s", _get_git_sha())
    logger.info("  Python     : %s", platform.python_version())
    logger.info("  Platform   : %s %s", platform.system(), platform.machine())
    logger.info("  Datasets   : %s", ", ".join(dataset_names))
    logger.info("  Configs    : %d ablation variants", len(ABLATION_CONFIGS))
    logger.info("  Output dir : %s", output_dir)
    logger.info("  Log file   : %s", log_file)
    logger.info("  PID        : %d", os.getpid())
    logger.info("=" * 70)


def main() -> None:
    from benchmarks.env import load_dotenv

    load_dotenv()

    args = parse_args()

    # Parse dataset list early so we fail fast on bad names
    dataset_names = [d.strip() for d in args.datasets.split(",")]
    for name in dataset_names:
        if name not in SUPPORTED_DATASETS:
            print(f"ERROR: Unknown dataset '{name}'. Supported: {SUPPORTED_DATASETS}")
            sys.exit(1)

    # Set up durable logging
    log_file = _setup_logging(args.output_dir, args.verbose)
    _log_run_header(dataset_names, args.output_dir, log_file)

    try:
        asyncio.run(run_ablation(dataset_names, args.output_dir))
    except KeyboardInterrupt:
        logger.warning("Run interrupted by user (Ctrl+C)")
        sys.exit(130)
    except Exception:
        logger.exception("Ablation study failed with unhandled exception")
        sys.exit(1)


if __name__ == "__main__":
    main()
