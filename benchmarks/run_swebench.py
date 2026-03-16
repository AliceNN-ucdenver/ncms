"""SWE-bench dream cycle experiment runner.

Usage:
    uv run python -m benchmarks.run_swebench \
        --llm-model openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
        --llm-api-base http://spark-ee7d.local:8000/v1

    # Analysis only (no LLM needed):
    uv run python -m benchmarks.run_swebench --analysis-only

    # With Ollama:
    uv run python -m benchmarks.run_swebench \
        --llm-model ollama_chat/qwen3.5:35b-a3b
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def setup_logging(output_dir: Path, verbose: bool = False) -> None:
    """Configure durable logging to file + console."""
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = output_dir / f"swebench_{timestamp}.log"
    latest_link = output_dir / "swebench_latest.log"

    # Symlink latest
    latest_link.unlink(missing_ok=True)
    latest_link.symlink_to(log_file.name)

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )

    logger.info("SWE-bench experiment log: %s", log_file)


async def run_analysis(output_dir: Path) -> None:
    """Run structural analysis only (no LLM needed)."""
    from benchmarks.swebench_analysis import main as analysis_main
    sys.argv = [
        "swebench_analysis",
        "--output-dir", str(output_dir / "analysis"),
        "--sample-size", "100",
    ]
    analysis_main()


async def run_experiment(
    llm_model: str,
    llm_api_base: str,
    output_dir: Path,
) -> None:
    """Run the full SWE-bench dream cycle experiment."""
    from benchmarks.swebench_harness import run_swebench_experiment
    from benchmarks.swebench_loader import (
        load_swebench_django,
        split_train_test,
    )
    from benchmarks.swebench_qrels import (
        build_ar_qrels,
        build_cr_qrels,
        build_lru_queries,
        build_ttl_labels,
    )
    from benchmarks.swebench_report import save_swebench_results

    # Load and split dataset
    instances = load_swebench_django()
    train, test = split_train_test(instances)

    # Build AR queries (test issue problem_statements)
    ar_queries: dict[str, str] = {}
    for inst in test:
        # Use first 2000 chars as query (enough for BM25/SPLADE matching)
        ar_queries[inst.instance_id] = inst.content[:2000]

    # Build qrels for all splits
    ar_qrels = build_ar_qrels(train, test)
    ttl_labels = build_ttl_labels(test)
    cr_qrels, cr_queries = build_cr_qrels(instances)
    lru_queries, lru_qrels = build_lru_queries(instances)

    # Run experiment
    results = await run_swebench_experiment(
        train=train,
        test=test,
        ar_queries=ar_queries,
        ar_qrels=ar_qrels,
        ttl_labels=ttl_labels,
        cr_queries=cr_queries,
        cr_qrels=cr_qrels,
        lru_queries=lru_queries,
        lru_qrels=lru_qrels,
        llm_model=llm_model,
        llm_api_base=llm_api_base,
    )

    # Save results
    save_swebench_results(results, output_dir)

    logger.info("Experiment complete. Results saved to %s", output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="SWE-bench dream cycle experiment")
    parser.add_argument(
        "--llm-model",
        default="openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16",
        help="LLM model for consolidation",
    )
    parser.add_argument(
        "--llm-api-base",
        default="http://spark-ee7d.local:8000/v1",
        help="LLM API base URL",
    )
    parser.add_argument(
        "--output-dir",
        default="benchmarks/results/swebench",
        help="Output directory",
    )
    parser.add_argument("--analysis-only", action="store_true", help="Run analysis only")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # Load env from .env if present
    try:
        from benchmarks.env import load_env
        load_env()
    except ImportError:
        pass

    # Override from env
    import os
    if os.getenv("LLM_MODEL"):
        args.llm_model = os.environ["LLM_MODEL"]
    if os.getenv("LLM_API_BASE"):
        args.llm_api_base = os.environ["LLM_API_BASE"]

    output_dir = Path(args.output_dir)
    setup_logging(output_dir, verbose=args.verbose)

    logger.info("NCMS SWE-bench Dream Cycle Experiment")
    logger.info("  LLM model: %s", args.llm_model)
    logger.info("  LLM API base: %s", args.llm_api_base)
    logger.info("  Output: %s", output_dir)

    if args.analysis_only:
        asyncio.run(run_analysis(output_dir))
    else:
        asyncio.run(run_experiment(args.llm_model, args.llm_api_base, output_dir))


if __name__ == "__main__":
    main()
