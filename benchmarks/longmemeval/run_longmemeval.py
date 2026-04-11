"""CLI runner for the LongMemEval benchmark.

Usage:
    uv run python -m benchmarks longmemeval
    uv run python -m benchmarks longmemeval --test
    uv run python -m benchmarks longmemeval --test --verbose
    uv run python -m benchmarks longmemeval --dataset longmemeval_s_cleaned.json
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
    if args.features_on:
        output_dir = output_dir / "features_on"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load dataset
    logger.info("Loading LongMemEval dataset...")
    sessions_by_question, questions = load_longmemeval_dataset(
        cache_dir=cache_dir,
        dataset_file=args.dataset,
    )

    if not questions:
        logger.error("No questions loaded. Check dataset format.")
        sys.exit(1)

    if not sessions_by_question:
        logger.error("No sessions loaded. Check dataset download.")
        sys.exit(1)

    # Test mode: limit to first N questions
    if args.test:
        test_limit = 3
        questions = questions[:test_limit]
        # Keep only the sessions for the selected questions
        qids = {q.question_id for q in questions}
        sessions_by_question = {
            qid: sessions
            for qid, sessions in sessions_by_question.items()
            if qid in qids
        }
        logger.info(
            "Test mode: %d questions, %d session sets",
            len(questions), len(sessions_by_question),
        )

    # Build config (features-on uses production bundle)
    ncms_config = None
    if args.features_on:
        from ncms.config import NCMSConfig
        ncms_config = NCMSConfig(
            db_path=":memory:",
            actr_noise=0.0,
            # Core retrieval
            splade_enabled=True,
            scoring_weight_bm25=0.6,
            scoring_weight_actr=0.0,
            scoring_weight_splade=0.3,
            scoring_weight_graph=0.3,
            # Phase 1-3: Admission, reconciliation, episodes
            admission_enabled=True,
            reconciliation_enabled=True,
            episodes_enabled=True,
            # Phase 4: Intent-aware retrieval
            intent_classification_enabled=True,
            scoring_weight_hierarchy=0.1,
            # Phase 5: Hierarchical consolidation + level-first
            level_first_enabled=True,
            topic_map_enabled=True,
            # Phase 8: Dream query expansion
            dream_query_expansion_enabled=True,
            # Phase 9: Intent routing
            intent_routing_enabled=True,
            # Phase 10: Cross-encoder reranking
            reranker_enabled=True,
            # Temporal query scoring
            temporal_enabled=True,
            scoring_weight_temporal=0.2,
            # Recency
            scoring_weight_recency=0.1,
            recency_half_life_days=30.0,
            # No LLM-dependent features (no endpoint needed for benchmark)
            contradiction_detection_enabled=False,
            consolidation_knowledge_enabled=False,
            episode_consolidation_enabled=False,
            trajectory_consolidation_enabled=False,
            pattern_consolidation_enabled=False,
            synthesis_enabled=False,
        )
        logger.info("Features ON: production retrieval bundle enabled")

    # Run benchmark
    results = await run_longmemeval_benchmark(
        sessions_by_question=sessions_by_question,
        questions=questions,
        top_k=args.top_k,
        use_rag=args.rag,
        answer_model=args.answer_model,
        answer_api_base=args.answer_api_base,
        judge_model=args.judge_model,
        judge_api_base=args.judge_api_base,
        config=ncms_config,
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
    if "QA_F1" in overall:
        lines.append(f"| QA F1 | {overall.get('QA_F1', 0):.4f} |")
        lines.append(f"| Judge Accuracy | {overall.get('Judge_Accuracy', 0):.4f} |")
    lines.append(f"| Questions | {int(overall.get('num_questions', 0))} |")
    lines.append(f"| Total sessions | {results.get('total_sessions', 0)} |")
    lines.append(f"| Total memories | {results.get('total_memories', 0)} |")
    lines.append(f"| Elapsed | {results.get('elapsed_seconds', 0)}s |")
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
        "--dataset",
        type=str,
        default="longmemeval_oracle.json",
        help="Dataset file to load (default: longmemeval_oracle.json)",
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
        help="Test mode: process first 3 questions only",
    )
    parser.add_argument(
        "--features-on",
        action="store_true",
        help="Enable full production retrieval bundle (admission, episodes, intent, etc.)",
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
    setup_logging("longmemeval", output_dir, verbose=args.verbose)

    # Suppress noisy library loggers
    for name in ("sentence_transformers", "transformers", "torch", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)

    log_run_header("NCMS LongMemEval Benchmark", logger)

    run_async(_run(args), "LongMemEval benchmark")


if __name__ == "__main__":
    main()
