"""Run the NCMS retrieval pipeline ablation study.

Evaluates each pipeline component's contribution using standard BEIR
IR benchmark datasets. Produces publishable tables and charts.

Usage:
    # Core pipeline only (no LLM required)
    uv run python -m benchmarks.run_ablation
    uv run python -m benchmarks.run_ablation --datasets scifact

    # Include LLM-powered features (keyword bridges + LLM judge)
    uv run python -m benchmarks.run_ablation --llm-model ollama_chat/qwen3.5:35b-a3b
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from benchmarks.configs import ABLATION_CONFIGS, CORE_CONFIGS
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
        "--llm-model",
        type=str,
        default=None,
        help=(
            "LLM model for keyword bridges and judge "
            "(e.g. ollama_chat/qwen3.5:35b-a3b). "
            "If not provided, LLM configs are skipped."
        ),
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
    llm_model: str | None = None,
) -> dict[str, dict[str, dict[str, float]]]:
    """Run the full ablation study.

    Args:
        dataset_names: List of BEIR dataset names to evaluate.
        output_dir: Directory to write results.
        llm_model: LLM model for keyword bridges and judge (optional).

    Returns:
        {dataset_name: {config_name: {metric: value}}}
    """
    # Select configs based on whether LLM is available
    configs = ABLATION_CONFIGS if llm_model else CORE_CONFIGS

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
            llm_model=llm_model,
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

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Parse dataset list
    dataset_names = [d.strip() for d in args.datasets.split(",")]
    for name in dataset_names:
        if name not in SUPPORTED_DATASETS:
            logger.error("Unknown dataset: %s. Supported: %s", name, SUPPORTED_DATASETS)
            sys.exit(1)

    # Determine which configs will run
    configs = ABLATION_CONFIGS if args.llm_model else CORE_CONFIGS

    logger.info("NCMS Retrieval Pipeline Ablation Study")
    logger.info("Datasets: %s", ", ".join(dataset_names))
    logger.info("Configs: %d ablation variants", len(configs))
    if args.llm_model:
        logger.info("LLM model: %s (keyword bridges + judge enabled)", args.llm_model)
    else:
        logger.info("LLM model: none (core pipeline only, use --llm-model to enable)")
    logger.info("Output: %s", args.output_dir)

    asyncio.run(run_ablation(dataset_names, args.output_dir, args.llm_model))


if __name__ == "__main__":
    main()
