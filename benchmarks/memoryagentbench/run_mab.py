"""MemoryAgentBench CLI runner.

Usage:
    uv run python -m benchmarks.memoryagentbench.run_mab
    uv run python -m benchmarks.memoryagentbench.run_mab --competencies ar,ttl
    uv run python -m benchmarks.memoryagentbench.run_mab --test
    uv run python -m benchmarks.memoryagentbench.run_mab --cache-dir /tmp/mab
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = "benchmarks/results/memoryagentbench"
ALL_COMPETENCIES = ("ar", "ttl", "lru", "sf")


def setup_logging(output_dir: Path, verbose: bool = False) -> None:
    """Configure durable logging to file + console."""
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = output_dir / f"mab_{timestamp}.log"
    latest_link = output_dir / "mab_latest.log"

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

    logger.info("MemoryAgentBench log: %s", log_file)


def save_results(results: dict, output_dir: Path) -> None:
    """Save results as JSON and markdown summary table."""
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    # JSON
    json_path = output_dir / f"mab_results_{timestamp}.json"
    json_path.write_text(json.dumps(results, indent=2, default=str))
    logger.info("Results JSON: %s", json_path)

    # Latest symlink
    latest_json = output_dir / "mab_results_latest.json"
    latest_json.unlink(missing_ok=True)
    latest_json.symlink_to(json_path.name)

    # Markdown table
    md_path = output_dir / f"mab_results_{timestamp}.md"
    lines = [
        "# MemoryAgentBench Results",
        "",
        f"**Date**: {timestamp}",
        f"**Total time**: {results.get('total_seconds', 0):.1f}s",
        "",
        "## Ingestion",
        "",
        f"- Documents ingested: {results.get('ingestion', {}).get('docs_ingested', 0)}",
        f"- Ingestion time: {results.get('ingestion', {}).get('ingestion_seconds', 0):.2f}s",
        "",
        "## Competency Results",
        "",
        "| Competency | Primary Metric | Value | Queries |",
        "|------------|---------------|-------|---------|",
    ]

    competencies = results.get("competencies", {})

    for comp_name, comp_data in competencies.items():
        if comp_data.get("skipped"):
            lines.append(
                f"| {comp_name.upper()} | - | SKIPPED | - |"
            )
        elif comp_data.get("error"):
            lines.append(
                f"| {comp_name.upper()} | - | ERROR | - |"
            )
        elif comp_name == "ar":
            lines.append(
                f"| AR | nDCG@10 | {comp_data.get('nDCG@10', 0):.4f} "
                f"| {comp_data.get('num_queries', 0)} |"
            )
        elif comp_name == "ttl":
            lines.append(
                f"| TTL | accuracy | {comp_data.get('accuracy', 0):.4f} "
                f"| {comp_data.get('num_queries', 0)} |"
            )
        elif comp_name == "lru":
            lines.append(
                f"| LRU | nDCG@10 | {comp_data.get('nDCG@10', 0):.4f} "
                f"| {comp_data.get('num_queries', 0)} |"
            )
        elif comp_name == "sf":
            lines.append(
                f"| SF | forgetting_acc | {comp_data.get('forgetting_accuracy', 0):.4f} "
                f"| {comp_data.get('num_queries', 0)} |"
            )

    lines.append("")

    # Detailed metrics per competency
    lines.append("## Detailed Metrics")
    lines.append("")
    for comp_name, comp_data in competencies.items():
        if comp_data.get("skipped") or comp_data.get("error"):
            continue
        lines.append(f"### {comp_name.upper()}")
        lines.append("")
        for key, value in comp_data.items():
            if key == "elapsed_seconds":
                lines.append(f"- Elapsed: {value:.2f}s")
            elif isinstance(value, float):
                lines.append(f"- {key}: {value:.4f}")
            else:
                lines.append(f"- {key}: {value}")
        lines.append("")

    md_path.write_text("\n".join(lines))
    logger.info("Results markdown: %s", md_path)

    # Latest symlink for markdown
    latest_md = output_dir / "mab_results_latest.md"
    latest_md.unlink(missing_ok=True)
    latest_md.symlink_to(md_path.name)


async def run_benchmark(
    cache_dir: Path | None,
    output_dir: Path,
    competencies: tuple[str, ...],
    test_mode: bool = False,
) -> None:
    """Run the MemoryAgentBench benchmark."""
    from benchmarks.memoryagentbench.loader import load_mab_dataset

    # Load dataset
    logger.info("Loading MemoryAgentBench dataset...")
    data = load_mab_dataset(cache_dir=cache_dir)

    if data is None:
        logger.warning(
            "MemoryAgentBench dataset not available. "
            "The dataset may not be publicly released yet (ICLR 2026). "
            "Skipping benchmark."
        )
        # Save a skip record so the run is documented
        skip_result = {
            "status": "skipped",
            "reason": "dataset_not_available",
            "message": (
                "MemoryAgentBench dataset (ai-hyz/MemoryAgentBench) could not "
                "be downloaded. Install with: pip install datasets && "
                "python -c \"from datasets import load_dataset; "
                "load_dataset('ai-hyz/MemoryAgentBench')\""
            ),
        }
        save_results(skip_result, output_dir)
        return

    available = set(data.keys())
    requested = set(competencies)
    missing = requested - available
    if missing:
        logger.warning(
            "Requested competencies not in dataset: %s. Available: %s",
            ", ".join(sorted(missing)), ", ".join(sorted(available)),
        )

    if test_mode:
        # Truncate data for quick testing
        logger.info("TEST MODE: Truncating each split to 10 items")
        for split in data:
            if isinstance(data[split], list) and len(data[split]) > 10:
                data[split] = data[split][:10]

    # Run benchmark
    from benchmarks.memoryagentbench.harness import run_mab_benchmark

    results = await run_mab_benchmark(data, competencies=competencies)

    # Save results
    save_results(results, output_dir)
    logger.info("Benchmark complete. Results saved to %s", output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MemoryAgentBench evaluation harness for NCMS",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Directory for cached MAB dataset (default: ~/.ncms/benchmarks/memoryagentbench/)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(DEFAULT_OUTPUT_DIR),
        help=f"Output directory for results (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--competencies",
        type=str,
        default="ar,ttl,lru,sf",
        help="Comma-separated competencies to evaluate (default: ar,ttl,lru,sf)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode: truncate data to 10 items per split",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Parse competencies
    competencies = tuple(c.strip().lower() for c in args.competencies.split(","))
    invalid = [c for c in competencies if c not in ALL_COMPETENCIES]
    if invalid:
        valid = ", ".join(ALL_COMPETENCIES)
        bad = ", ".join(invalid)
        parser.error(f"Invalid competencies: {bad}. Choose from: {valid}")

    # Load env
    try:
        from benchmarks.env import load_env
        load_env()
    except ImportError:
        pass

    setup_logging(args.output_dir, verbose=args.verbose)

    logger.info("NCMS MemoryAgentBench Evaluation")
    logger.info("  Competencies: %s", ", ".join(competencies))
    logger.info("  Cache dir: %s", args.cache_dir or "(default)")
    logger.info("  Output: %s", args.output_dir)
    logger.info("  Test mode: %s", args.test)

    asyncio.run(run_benchmark(
        cache_dir=args.cache_dir,
        output_dir=args.output_dir,
        competencies=competencies,
        test_mode=args.test,
    ))


if __name__ == "__main__":
    main()
