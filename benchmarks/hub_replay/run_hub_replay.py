"""CLI runner for the hub replay benchmark.

Usage:
    uv run python -m benchmarks.hub_replay.run_hub_replay
    uv run python -m benchmarks.hub_replay.run_hub_replay --verbose
    uv run python -m benchmarks.hub_replay.run_hub_replay --output-dir /tmp/results
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.core.runner import log_run_header, run_async, setup_logging

logger = logging.getLogger("hub_replay")


async def _run(args: argparse.Namespace) -> dict:
    """Async entry point for the hub replay benchmark."""
    from benchmarks.hub_replay.fixtures import HUB_MEMORIES, HUB_QUERIES
    from benchmarks.hub_replay.harness import evaluate_replay

    logger.info("Hub Replay Benchmark")
    logger.info("  Memories: %d", len(HUB_MEMORIES))
    logger.info("  Queries: %d", len(HUB_QUERIES))

    results = await evaluate_replay(HUB_MEMORIES, HUB_QUERIES)

    # Save results
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")

    # Save JSON results
    json_path = output_dir / f"hub_replay_{timestamp}.json"
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2)
    logger.info("JSON results: %s", json_path)

    # Also save as latest symlink
    latest_json = output_dir / "hub_replay_latest.json"
    latest_json.unlink(missing_ok=True)
    latest_json.symlink_to(json_path.name)

    # Save markdown table
    md_path = output_dir / f"hub_replay_{timestamp}.md"
    md_content = _format_markdown(results)
    with open(md_path, "w") as f:
        f.write(md_content)
    logger.info("Markdown results: %s", md_path)

    latest_md = output_dir / "hub_replay_latest.md"
    latest_md.unlink(missing_ok=True)
    latest_md.symlink_to(md_path.name)

    # Print summary to stdout
    print()
    print(md_content)

    # Log data integrity findings (informational for baseline, not a failure)
    if results["duplicate_count"] > 0 or results["junk_entity_rate"] > 15:
        logger.warning("Data integrity issues detected — expected for pre-fix baseline")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="NCMS Hub Replay Benchmark")
    parser.add_argument(
        "--output-dir",
        type=str,
        default="benchmarks/results/hub_replay",
        help="Directory for result files (default: benchmarks/results/hub_replay)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    setup_logging("hub_replay", output_dir, verbose=args.verbose)

    # Suppress noisy library loggers
    for name in ("sentence_transformers", "transformers", "torch", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)

    log_run_header("NCMS Hub Replay Benchmark", logger)

    run_async(_run(args), "Hub Replay benchmark")


def _format_markdown(results: dict) -> str:
    """Format results as a markdown report."""
    lines: list[str] = []
    lines.append("# Hub Replay Benchmark Results")
    lines.append("")
    lines.append("## Data Integrity")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Total memories | {results['total_memories']} |")
    lines.append(f"| Ingested | {results['ingested_count']} |")
    lines.append(f"| Duplicates (by content hash) | {results['duplicate_count']} |")
    lines.append(f"| Total entities | {results['total_entities']} |")
    lines.append(f"| Junk entities | {results['junk_entity_count']} |")
    lines.append(f"| Junk entity rate | {results['junk_entity_rate']}% |")
    lines.append("")

    if results.get("junk_entity_samples"):
        lines.append("### Junk Entity Samples")
        lines.append("")
        for sample in results["junk_entity_samples"]:
            lines.append(f"- `{sample}`")
        lines.append("")

    lines.append("## Latency")
    lines.append("")
    lines.append("| Metric | Value (ms) |")
    lines.append("|--------|-----------|")
    lines.append(f"| Ingest p50 | {results['ingest_latency_p50']} |")
    lines.append(f"| Ingest p95 | {results['ingest_latency_p95']} |")
    lines.append(f"| Ingest p99 | {results['ingest_latency_p99']} |")
    lines.append(f"| Search p50 | {results['search_latency_p50']} |")
    lines.append("")

    lines.append("## Query Results")
    lines.append("")
    for name, qdata in results.get("queries", {}).items():
        lines.append(f"### {name}")
        lines.append("")
        lines.append(f"**Query**: {qdata['query']}")
        lines.append(f"**Results**: {qdata['result_count']}")
        lines.append("")
        if qdata.get("top_3"):
            lines.append("| Rank | Score | Content Preview |")
            lines.append("|------|-------|-----------------|")
            for i, r in enumerate(qdata["top_3"], 1):
                preview = r["content_preview"][:80].replace("|", "\\|").replace("\n", " ")
                lines.append(f"| {i} | {r['score']} | {preview} |")
            lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    main()
