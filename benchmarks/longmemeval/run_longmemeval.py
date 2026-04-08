"""CLI runner for the LongMemEval benchmark.

Usage:
    uv run python -m benchmarks.longmemeval.run_longmemeval
    uv run python -m benchmarks.longmemeval.run_longmemeval --test
    uv run python -m benchmarks.longmemeval.run_longmemeval --verbose --output-dir /tmp/results
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
    """Async entry point for the LongMemEval benchmark."""
    from benchmarks.longmemeval.harness import run_longmemeval_benchmark
    from benchmarks.longmemeval.loader import load_longmemeval_dataset

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load dataset
    logger.info("Loading LongMemEval dataset...")
    sessions, questions = load_longmemeval_dataset(cache_dir=cache_dir)

    if not sessions:
        logger.error("No sessions loaded. Check dataset download.")
        sys.exit(1)

    if not questions:
        logger.error("No questions loaded. Check dataset format.")
        sys.exit(1)

    # Test mode: limit to first 5 sessions and their questions
    if args.test:
        sessions = sessions[:5]
        session_ids = {s.session_id for s in sessions}
        # Keep questions that reference these sessions, or all if no session_ids in questions
        filtered = [
            q for q in questions
            if not q.session_ids or any(sid in session_ids for sid in q.session_ids)
        ]
        # If filtering removed all questions, just take first 20
        if not filtered:
            filtered = questions[:20]
        questions = filtered
        logger.info("Test mode: %d sessions, %d questions", len(sessions), len(questions))

    # Run benchmark
    results = await run_longmemeval_benchmark(
        sessions=sessions,
        questions=questions,
        top_k=args.top_k,
    )

    # Save results
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")

    json_path = output_dir / f"longmemeval_{timestamp}.json"
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2)
    logger.info("JSON results: %s", json_path)

    # Symlink latest
    latest_json = output_dir / "longmemeval_latest.json"
    try:
        latest_json.unlink(missing_ok=True)
        latest_json.symlink_to(json_path.name)
    except OSError:
        pass

    # Markdown report
    md_path = output_dir / f"longmemeval_{timestamp}.md"
    md_content = _format_markdown(results, args.top_k)
    with open(md_path, "w") as f:
        f.write(md_content)
    logger.info("Markdown results: %s", md_path)

    latest_md = output_dir / "longmemeval_latest.md"
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
    lines.append("# LongMemEval Benchmark Results")
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
    lines.append(f"| Sessions | {results.get('sessions_count', 0)} |")
    lines.append(f"| Total turns | {results.get('total_turns', 0)} |")
    lines.append(f"| Memories stored | {results.get('memories_stored', 0)} |")
    lines.append("")

    # Reference comparison
    lines.append("## Reference Comparison")
    lines.append("")
    lines.append(f"| System | Recall@{top_k} |")
    lines.append("|--------|----------|")
    lines.append(f"| NCMS | {overall.get(f'Recall@{top_k}', 0):.4f} |")
    lines.append("| MemPalace (reported) | 0.9660 |")
    lines.append("")

    # Category breakdown
    category_keys = [k for k in overall if k.startswith(f"Recall@{top_k}_")]
    if category_keys:
        lines.append("## Category Breakdown")
        lines.append("")
        lines.append(f"| Category | Recall@{top_k} | Count |")
        lines.append("|----------|----------|-------|")
        for key in sorted(category_keys):
            cat = key.split("_", 1)[1] if "_" in key else key
            count_key = f"num_{cat}"
            lines.append(
                f"| {cat} "
                f"| {overall.get(key, 0):.4f} "
                f"| {int(overall.get(count_key, 0))} |"
            )
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="NCMS LongMemEval Benchmark")
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=None,
        help="Cache directory for dataset download (default: benchmarks/results/.cache)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="benchmarks/results/longmemeval",
        help="Directory for result files (default: benchmarks/results/longmemeval)",
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
        help="Test mode: run on limited data only",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    setup_logging("longmemeval", output_dir, verbose=args.verbose)

    # Suppress noisy library loggers
    for name in ("sentence_transformers", "transformers", "torch", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)

    log_run_header("NCMS LongMemEval Benchmark", logger)

    run_async(_run(args), "LongMemEval benchmark")


if __name__ == "__main__":
    main()
