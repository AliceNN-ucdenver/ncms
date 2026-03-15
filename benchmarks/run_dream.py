"""Run the NCMS dream cycle / consolidation experiment.

Evaluates whether LLM-generated abstract memories (episode summaries,
state trajectories, recurring patterns) improve retrieval quality on
standard BEIR benchmarks.

Usage:
    uv run python -m benchmarks.run_dream
    uv run python -m benchmarks.run_dream --datasets scifact
    uv run python -m benchmarks.run_dream --datasets scifact --verbose
    uv run python -m benchmarks.run_dream --llm-model ollama_chat/qwen3.5:35b-a3b

Logging:
    Each run creates a timestamped log file in the output directory:
      benchmarks/results/dream/dream_2026-03-14_150000.log
    The latest run is also symlinked as dream_latest.log for convenience.
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

from benchmarks.datasets import SUPPORTED_DATASETS, load_beir_dataset
from benchmarks.dream_configs import DREAM_STAGES
from benchmarks.dream_harness import run_dream_experiment
from benchmarks.dream_report import save_dream_results

logger = logging.getLogger("benchmarks.dream")

# Default LLM config (DGX Spark with Nemotron)
DEFAULT_LLM_MODEL = "openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"
DEFAULT_LLM_API_BASE = "http://spark-ee7d.local:8000/v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="NCMS Dream Cycle / Consolidation Experiment",
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
        default="benchmarks/results/dream",
        help="Output directory for results (default: benchmarks/results/dream)",
    )
    parser.add_argument(
        "--llm-model",
        type=str,
        default=DEFAULT_LLM_MODEL,
        help=f"LLM model for consolidation synthesis (default: {DEFAULT_LLM_MODEL})",
    )
    parser.add_argument(
        "--llm-api-base",
        type=str,
        default=DEFAULT_LLM_API_BASE,
        help=f"LLM API base URL (default: {DEFAULT_LLM_API_BASE})",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args()


async def run_dream_study(
    dataset_names: list[str],
    output_dir: str,
    llm_model: str,
    llm_api_base: str,
) -> dict[str, dict]:
    """Run the dream experiment across all specified datasets."""
    all_results: dict[str, dict] = {}

    total_start = time.perf_counter()

    for dataset_name in dataset_names:
        logger.info("Loading dataset: %s", dataset_name)
        corpus, queries, qrels = load_beir_dataset(dataset_name)

        dataset_results = await run_dream_experiment(
            dataset_name=dataset_name,
            corpus=corpus,
            queries=queries,
            qrels=qrels,
            llm_model=llm_model,
            llm_api_base=llm_api_base,
        )

        all_results[dataset_name] = dataset_results

    total_elapsed = time.perf_counter() - total_start
    logger.info("=" * 70)
    logger.info("Dream experiment complete")
    logger.info("  End time : %s", datetime.now(UTC).isoformat())
    logger.info(
        "  Duration : %.1f seconds (%.1f minutes)",
        total_elapsed, total_elapsed / 60,
    )
    logger.info("  Datasets : %d evaluated", len(all_results))
    logger.info("=" * 70)

    # Save results
    save_dream_results(all_results, output_dir)

    # Print summary
    from benchmarks.dream_report import generate_dream_table

    for dataset_name, results in all_results.items():
        print(f"\n{'=' * 60}")
        print(f"  {dataset_name}")
        print(f"{'=' * 60}")
        print(generate_dream_table(results))

    return all_results


def _get_git_sha() -> str:
    """Get short git SHA for provenance."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _setup_logging(output_dir: str, verbose: bool) -> Path:
    """Configure timestamped log file + console logging."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = output_path / f"dream_{timestamp}.log"

    level = logging.DEBUG if verbose else logging.INFO

    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    ))

    logging.basicConfig(level=level, handlers=[file_handler, console_handler])

    # Symlink latest log
    latest_link = output_path / "dream_latest.log"
    try:
        latest_link.unlink(missing_ok=True)
        latest_link.symlink_to(log_file.name)
    except OSError:
        pass

    return log_file


def _log_run_header(
    dataset_names: list[str],
    output_dir: str,
    log_file: Path,
    llm_model: str,
    llm_api_base: str,
) -> None:
    """Log a metadata header with system info for provenance."""
    logger.info("=" * 70)
    logger.info("NCMS Dream Cycle / Consolidation Experiment")
    logger.info("=" * 70)
    logger.info("  Start time : %s", datetime.now(UTC).isoformat())
    logger.info("  Git SHA    : %s", _get_git_sha())
    logger.info("  Python     : %s", platform.python_version())
    logger.info("  Platform   : %s %s", platform.system(), platform.machine())
    logger.info("  Datasets   : %s", ", ".join(dataset_names))
    logger.info("  Stages     : %d dream stages", len(DREAM_STAGES))
    logger.info("  LLM model  : %s", llm_model)
    logger.info("  LLM API    : %s", llm_api_base)
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
    _log_run_header(
        dataset_names, args.output_dir, log_file,
        args.llm_model, args.llm_api_base,
    )

    try:
        asyncio.run(run_dream_study(
            dataset_names, args.output_dir,
            args.llm_model, args.llm_api_base,
        ))
    except KeyboardInterrupt:
        logger.warning("Run interrupted by user (Ctrl+C)")
        sys.exit(130)
    except Exception:
        logger.exception("Dream experiment failed with unhandled exception")
        sys.exit(1)


if __name__ == "__main__":
    main()
