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
    from benchmarks.locomo.loader import load_locomo_dataset

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load conversations (shared by standard and Plus benchmarks)
    logger.info("Loading LoCoMo dataset...")
    conversations, questions = load_locomo_dataset(cache_dir=cache_dir)

    if not conversations:
        logger.error("No conversations loaded. Check dataset download.")
        sys.exit(1)

    if args.plus:
        await _run_plus(args, conversations, output_dir)
    else:
        await _run_standard(args, conversations, questions, output_dir)


async def _run_standard(
    args: argparse.Namespace,
    conversations: list,
    questions: list,
    output_dir: Path,
) -> None:
    """Run the standard LoCoMo benchmark."""
    from benchmarks.locomo.harness import run_locomo_benchmark

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
        use_rag=args.rag,
        answer_model=args.answer_model,
        answer_api_base=args.answer_api_base,
        judge_model=args.judge_model,
        judge_api_base=args.judge_api_base,
    )

    _save_results(results, output_dir, "locomo", args.top_k)


async def _run_plus(
    args: argparse.Namespace,
    conversations: list,
    output_dir: Path,
) -> None:
    """Run the LoCoMo-Plus benchmark."""
    from benchmarks.locomo.harness import run_locomo_plus_benchmark
    from benchmarks.locomo.loader import load_locomo_plus_dataset

    logger.info("Loading LoCoMo-Plus questions...")
    plus_questions = load_locomo_plus_dataset()

    if not plus_questions:
        logger.error("No LoCoMo-Plus questions loaded.")
        sys.exit(1)

    # Test mode: single conversation (index 0)
    if args.test:
        conversations = conversations[:1]
        plus_questions = [q for q in plus_questions if q.base_conv_idx == 0]
        logger.info(
            "Test mode: using 1 conversation (%d Plus questions)", len(plus_questions),
        )

    results = await run_locomo_plus_benchmark(
        conversations=conversations,
        plus_questions=plus_questions,
        top_k=args.top_k,
        use_llm_judge=args.llm_judge,
        llm_model=args.llm_model,
        llm_api_base=args.llm_api_base,
    )

    _save_results(results, output_dir, "locomo_plus", args.top_k, plus=True)


def _save_results(
    results: dict,
    output_dir: Path,
    prefix: str,
    top_k: int,
    *,
    plus: bool = False,
) -> None:
    """Save benchmark results to JSON and Markdown."""
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")

    json_path = output_dir / f"{prefix}_{timestamp}.json"
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2)
    logger.info("JSON results: %s", json_path)

    # Symlink latest
    latest_json = output_dir / f"{prefix}_latest.json"
    try:
        latest_json.unlink(missing_ok=True)
        latest_json.symlink_to(json_path.name)
    except OSError:
        pass

    # Markdown report
    md_path = output_dir / f"{prefix}_{timestamp}.md"
    md_content = _format_plus_markdown(results, top_k) if plus else _format_markdown(results, top_k)
    with open(md_path, "w") as f:
        f.write(md_content)
    logger.info("Markdown results: %s", md_path)

    latest_md = output_dir / f"{prefix}_latest.md"
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


def _format_plus_markdown(results: dict, top_k: int) -> str:
    """Format LoCoMo-Plus benchmark results as a markdown report."""
    lines: list[str] = []
    lines.append("# LoCoMo-Plus Benchmark Results")
    lines.append("")

    overall = results.get("overall", {})
    lines.append("## Overall Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Recall@{top_k} | {overall.get(f'Recall@{top_k}', 0):.4f} |")
    lines.append(f"| Contains | {overall.get('Contains', 0):.4f} |")
    lines.append(f"| F1 (retrieval) | {overall.get('F1', 0):.4f} |")
    lines.append(f"| F1 (token) | {overall.get('F1_token', 0):.4f} |")
    lines.append(f"| EM | {overall.get('EM', 0):.4f} |")
    if "llm_judge_score" in overall:
        lines.append(f"| LLM Judge | {overall.get('llm_judge_score', 0):.4f} |")
    lines.append(f"| Questions | {int(overall.get('num_questions', 0))} |")
    lines.append(f"| Conversations | {int(overall.get('num_conversations', 0))} |")
    lines.append("")

    # Per question type
    type_metrics = results.get("per_question_type", {})
    if type_metrics:
        lines.append("## Per Question Type")
        lines.append("")
        lines.append(f"| Question Type | Recall@{top_k} | Questions |")
        lines.append("|---------------|----------|-----------|")
        for qtype, metrics in sorted(type_metrics.items()):
            lines.append(
                f"| {qtype} "
                f"| {metrics.get(f'Recall@{top_k}', 0):.4f} "
                f"| {int(metrics.get('num_questions', 0))} |"
            )
        lines.append("")

    # Per conversation
    per_conv = results.get("per_conversation", {})
    if per_conv:
        lines.append("## Per-Conversation Results")
        lines.append("")
        lines.append(f"| Conversation | Recall@{top_k} | F1 | Contains | Questions |")
        lines.append("|--------------|----------|----|----------|-----------|")
        for conv_id, metrics in per_conv.items():
            lines.append(
                f"| {conv_id} "
                f"| {metrics.get(f'Recall@{top_k}', 0):.4f} "
                f"| {metrics.get('F1', 0):.4f} "
                f"| {metrics.get('Contains', 0):.4f} "
                f"| {int(metrics.get('num_questions', 0))} |"
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
        "--plus",
        action="store_true",
        help="Run LoCoMo-Plus cognitive reasoning evaluation (401 questions)",
    )
    parser.add_argument(
        "--llm-judge",
        action="store_true",
        help="Enable LLM judge scoring (requires --llm-model and --llm-api-base)",
    )
    parser.add_argument(
        "--llm-model",
        type=str,
        default="openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16",
        help="LLM model for judge scoring",
    )
    parser.add_argument(
        "--llm-api-base",
        type=str,
        default="http://spark-ee7d.local:8000/v1",
        help="LLM API base URL for judge scoring",
    )
    parser.add_argument(
        "--rag",
        action="store_true",
        help="Enable RAG evaluation: generate answers via LLM, judge via LLM",
    )
    parser.add_argument(
        "--answer-model",
        type=str,
        default="openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16",
        help="LLM model for answer generation (RAG mode)",
    )
    parser.add_argument(
        "--answer-api-base",
        type=str,
        default="http://spark-ee7d.local:8000/v1",
        help="LLM API base URL for answer generation (RAG mode)",
    )
    parser.add_argument(
        "--judge-model",
        type=str,
        default="openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16",
        help="LLM model for judge scoring (RAG mode)",
    )
    parser.add_argument(
        "--judge-api-base",
        type=str,
        default="http://spark-ee7d.local:8000/v1",
        help="LLM API base URL for judge scoring (RAG mode)",
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

    label = "NCMS LoCoMo-Plus Benchmark" if args.plus else "NCMS LoCoMo Benchmark"
    log_run_header(label, logger)

    run_async(_run(args), label)


if __name__ == "__main__":
    main()
