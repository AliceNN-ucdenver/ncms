"""MemoryAgentBench CLI runner.

Usage:
    uv run python -m benchmarks mab
    uv run python -m benchmarks mab --splits ar,ttl
    uv run python -m benchmarks mab --test
    uv run python -m benchmarks mab --cache-dir /tmp/mab
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = "benchmarks/results/memoryagentbench"
ALL_SPLITS = ("ar", "ttl", "lru", "cr")


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


def save_results(results: dict[str, Any], output_dir: Path) -> None:
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
        "## Per-Split Results",
        "",
        "| Split | Samples | Questions | Contains | F1 | Substring | EM |",
        "|-------|---------|-----------|----------|------|-----------|-----|",
    ]

    splits = results.get("splits", {})

    for split_name, sm in splits.items():
        if sm.get("skipped"):
            lines.append(f"| {split_name.upper()} | - | - | SKIPPED | - | - | - |")
        else:
            lines.append(
                f"| {split_name.upper()} "
                f"| {sm.get('num_samples', 0)} "
                f"| {sm.get('num_questions', 0)} "
                f"| {sm.get('contains_any', 0):.4f} "
                f"| {sm.get('f1', 0):.4f} "
                f"| {sm.get('substring', 0):.4f} "
                f"| {sm.get('exact_match', 0):.4f} |"
            )

    overall = results.get("overall", {})
    if overall:
        lines.append(
            f"| **TOTAL** "
            f"| {overall.get('num_samples', 0)} "
            f"| {overall.get('num_questions', 0)} "
            f"| {overall.get('contains_any', 0):.4f} "
            f"| {overall.get('f1', 0):.4f} "
            f"| {overall.get('substring', 0):.4f} "
            f"| {overall.get('exact_match', 0):.4f} |"
        )

    lines.append("")

    # Per-question-type breakdown if available
    for split_name, sm in splits.items():
        by_qt = sm.get("by_question_type")
        if by_qt:
            lines.append(f"### {split_name.upper()} by question type")
            lines.append("")
            lines.append("| Type | Count | Contains | F1 | Substring |")
            lines.append("|------|-------|----------|------|-----------|")
            for qt, qm in by_qt.items():
                lines.append(
                    f"| {qt} | {qm['count']} "
                    f"| {qm['contains_any']:.4f} "
                    f"| {qm['f1']:.4f} "
                    f"| {qm['substring']:.4f} |"
                )
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
    splits: tuple[str, ...],
    test_mode: bool = False,
    top_k: int = 10,
    chunk_size: int = 2000,
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
        skip_result: dict[str, Any] = {
            "status": "skipped",
            "reason": "dataset_not_available",
            "total_seconds": 0.0,
        }
        save_results(skip_result, output_dir)
        return

    available = set(data.keys())
    requested = set(splits)
    missing = requested - available
    if missing:
        logger.warning(
            "Requested splits not in dataset: %s. Available: %s",
            ", ".join(sorted(missing)),
            ", ".join(sorted(available)),
        )

    # In test mode, limit samples and truncate large contexts
    max_samples = 2 if test_mode else None
    max_context_chars = 50_000 if test_mode else None
    if test_mode:
        logger.info(
            "TEST MODE: limiting to %d samples per split, max context %d chars",
            max_samples,
            max_context_chars,
        )
        # Truncate large contexts to speed up test runs
        for _split_name, split_data in data.items():
            if isinstance(split_data, list):
                for sample in split_data:
                    ctx = sample.get("context", "")
                    if max_context_chars and len(ctx) > max_context_chars:
                        sample["context"] = ctx[:max_context_chars]

    # Run benchmark
    from benchmarks.memoryagentbench.harness import run_mab_benchmark

    results = await run_mab_benchmark(
        data,
        splits=splits,
        top_k=top_k,
        chunk_size=chunk_size,
        max_samples=max_samples,
    )

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
        "--splits",
        type=str,
        default="ar,ttl,lru,cr",
        help="Comma-separated splits to evaluate (default: ar,ttl,lru,cr)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode: limit to 2 samples per split for quick validation",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="Number of results to retrieve per question (default: 10)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=2000,
        help="Context chunk size in characters (default: 2000)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Parse splits
    splits = tuple(c.strip().lower() for c in args.splits.split(","))
    invalid = [c for c in splits if c not in ALL_SPLITS]
    if invalid:
        valid = ", ".join(ALL_SPLITS)
        bad = ", ".join(invalid)
        parser.error(f"Invalid splits: {bad}. Choose from: {valid}")

    # Load env
    try:
        from benchmarks.env import load_env

        load_env()
    except ImportError:
        pass

    setup_logging(args.output_dir, verbose=args.verbose)

    logger.info("NCMS MemoryAgentBench Evaluation")
    logger.info("  Splits: %s", ", ".join(splits))
    logger.info("  Cache dir: %s", args.cache_dir or "(default)")
    logger.info("  Output: %s", args.output_dir)
    logger.info("  Test mode: %s", args.test)
    logger.info("  Top-K: %d, Chunk size: %d", args.top_k, args.chunk_size)

    asyncio.run(
        run_benchmark(
            cache_dir=args.cache_dir,
            output_dir=args.output_dir,
            splits=splits,
            test_mode=args.test,
            top_k=args.top_k,
            chunk_size=args.chunk_size,
        )
    )


if __name__ == "__main__":
    main()
