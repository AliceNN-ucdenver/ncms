"""CLI runner for the LoCoMo benchmark.

Usage:
    uv run python -m benchmarks.locomo.run_locomo
    uv run python -m benchmarks.locomo.run_locomo --test
    uv run python -m benchmarks.locomo.run_locomo --verbose --output-dir /tmp/results
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.core.runner import log_run_header, run_async, setup_logging

logger = logging.getLogger(__name__)


async def _run(args: argparse.Namespace) -> None:
    """Async entry point for the LoCoMo benchmark."""
    from benchmarks.locomo.harness import run_locomo_benchmark
    from benchmarks.locomo.loader import load_locomo_dataset

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load dataset
    logger.info("Loading LoCoMo dataset...")
    conversations, questions = load_locomo_dataset(cache_dir=cache_dir)

    if not conversations:
        logger.error("No conversations loaded. Check dataset download.")
        sys.exit(1)

    if not questions:
        logger.error("No questions loaded. Check dataset format.")
        sys.exit(1)

    # Test mode: single conversation
    if args.test:
        conversations = conversations[:1]
        conv_ids = {conversations[0].conversation_id}
        questions = [q for q in questions if q.conversation_id in conv_ids]
        logger.info("Test mode: using 1 conversation (%d questions)", len(questions))

    # Run benchmark
    results = await run_locomo_benchmark(
        conversations=conversations,
        questions=questions,
        top_k=args.top_k,
    )

    # Save results
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")

    json_path = output_dir / f"locomo_{timestamp}.json"
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2)
    logger.info("JSON results: %s", json_path)

    # Symlink latest
    latest_json = output_dir / "locomo_latest.json"
    try:
        latest_json.unlink(missing_ok=True)
        latest_json.symlink_to(json_path.name)
    except OSError:
        pass

    # Markdown report
    md_path = output_dir / f"locomo_{timestamp}.md"
    md_content = _format_markdown(results, args.top_k)
    with open(md_path, "w") as f:
        f.write(md_content)
    logger.info("Markdown results: %s", md_path)

    latest_md = output_dir / "locomo_latest.md"
    try:
        latest_md.unlink(missing_ok=True)
        latest_md.symlink_to(md_path.name)
    except OSError:
        pass

    # Print summary
    print()
    print(md_content)


def _format_markdown(results: dict, top_k: int) -> str:
    """Format benchmark results as a markdown report."""
    lines: list[str] = []
    lines.append("# LoCoMo Benchmark Results")
    lines.append("")

    overall = results.get("overall", {})
    lines.append("## Overall Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Recall@{top_k} | {overall.get(f'Recall@{top_k}', 0):.4f} |")
    lines.append(f"| Contains | {overall.get('Contains', 0):.4f} |")
    lines.append(f"| F1 | {overall.get('F1', 0):.4f} |")
    lines.append(f"| Questions | {int(overall.get('num_questions', 0))} |")
    lines.append(f"| Conversations | {int(overall.get('num_conversations', 0))} |")
    lines.append("")

    # Per-conversation table
    per_conv = results.get("per_conversation", {})
    if per_conv:
        lines.append("## Per-Conversation Results")
        lines.append("")
        lines.append(f"| Conversation | Recall@{top_k} | Contains | F1 | Questions |")
        lines.append("|--------------|----------|----------|-----|-----------|")
        for conv_id, metrics in per_conv.items():
            lines.append(
                f"| {conv_id} "
                f"| {metrics.get(f'Recall@{top_k}', 0):.4f} "
                f"| {metrics.get('Contains', 0):.4f} "
                f"| {metrics.get('F1', 0):.4f} "
                f"| {int(metrics.get('num_questions', 0))} |"
            )
        lines.append("")

    # Category breakdown from first conversation (or overall)
    sample_metrics = next(iter(per_conv.values()), {}) if per_conv else {}
    category_keys = [k for k in sample_metrics if k.startswith(f"Recall@{top_k}_")]
    if category_keys:
        lines.append("## Category Breakdown (sample)")
        lines.append("")
        lines.append(f"| Category | Recall@{top_k} | Count |")
        lines.append("|----------|----------|-------|")
        for key in sorted(category_keys):
            cat = key.split("_", 1)[1] if "_" in key else key
            count_key = f"num_{cat}"
            lines.append(
                f"| {cat} "
                f"| {sample_metrics.get(key, 0):.4f} "
                f"| {int(sample_metrics.get(count_key, 0))} |"
            )
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="NCMS LoCoMo Benchmark")
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=None,
        help="Cache directory for dataset download (default: benchmarks/results/.cache)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="benchmarks/results/locomo",
        help="Directory for result files (default: benchmarks/results/locomo)",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Top-k for recall computation (default: 5)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode: run on 1 conversation only",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    setup_logging("locomo", output_dir, verbose=args.verbose)

    # Suppress noisy library loggers
    for name in ("sentence_transformers", "transformers", "torch", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)

    log_run_header("NCMS LoCoMo Benchmark", logger)

    run_async(_run(args), "LoCoMo benchmark")


if __name__ == "__main__":
    main()
