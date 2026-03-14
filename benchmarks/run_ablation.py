"""Run the NCMS retrieval pipeline ablation study.

Evaluates each pipeline component's contribution using standard BEIR
IR benchmark datasets. Produces publishable tables and charts.

Usage:
    uv run python -m benchmarks.run_ablation
    uv run python -m benchmarks.run_ablation --datasets scifact
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from benchmarks.configs import ABLATION_CONFIGS
from benchmarks.datasets import SUPPORTED_DATASETS, load_beir_dataset
from benchmarks.harness import evaluate_dataset
from benchmarks.report import save_results

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
        "--verbose", "-v",
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
    logger.info("=" * 60)
    logger.info("Ablation study complete in %.1f seconds", total_elapsed)
    logger.info("=" * 60)

    # Save results
    save_results(all_results, output_dir)

    # Print summary to stdout
    from benchmarks.report import generate_summary_table

    print("\n" + generate_summary_table(all_results))

    return all_results


def main() -> None:
    args = parse_args()

    # Configure logging — file + console for provenance
    level = logging.DEBUG if args.verbose else logging.INFO
    log_path = f"{args.output_dir}/ablation_run.log"
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_path, mode="w"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    # Parse dataset list
    dataset_names = [d.strip() for d in args.datasets.split(",")]
    for name in dataset_names:
        if name not in SUPPORTED_DATASETS:
            logger.error("Unknown dataset: %s. Supported: %s", name, SUPPORTED_DATASETS)
            sys.exit(1)

    logger.info("NCMS Retrieval Pipeline Ablation Study")
    logger.info("Datasets: %s", ", ".join(dataset_names))
    logger.info("Configs: %d ablation variants", len(ABLATION_CONFIGS))
    logger.info("Output: %s", args.output_dir)

    asyncio.run(run_ablation(dataset_names, args.output_dir))


if __name__ == "__main__":
    main()
