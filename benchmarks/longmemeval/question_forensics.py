"""Single-question forensic trace.

Given a LongMemEval question_id, re-ingest the haystack and re-run
search, capturing per-stage state: BM25 top-10, SPLADE top-10,
GLiNER query entities, subject graph links, temporal classifier
intent, primitive dispatch, and the answer memory's rank at each
stage.

Usage::

    uv run python -m benchmarks.longmemeval.question_forensics \\
        --qid gpt4_c27434e8

Designed for diagnosis of rerank-opportunity failures — the ~4
questions in the Phase B diagnostic that sit in top-20 but miss
top-5.  Keeps the config matched to the diagnostic so behavior
reproduces.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.core.runner import log_run_header, run_async, setup_logging
from benchmarks.longmemeval.harness import (
    _create_ncms_instance,
    _ingest_sessions,
    _parse_lme_date,
)
from benchmarks.longmemeval.loader import load_longmemeval_dataset

logger = logging.getLogger(__name__)


def _containing_answer(content: str, answer: str) -> bool:
    """Case-insensitive substring check, matching the diagnostic's
    recall-hit definition."""
    if not content or not answer:
        return False
    return answer.lower() in content.lower()


async def _run(args: argparse.Namespace) -> None:
    from benchmarks.core.runner import wait_for_indexing
    from ncms.application.retrieval.pipeline import RetrievalPipeline
    from ncms.config import NCMSConfig
    from ncms.domain.entity_extraction import (
        add_temporal_labels,
        resolve_labels,
    )
    from ncms.domain.temporal_intent import classify_temporal_intent
    from ncms.domain.temporal_parser import parse_temporal_reference
    from ncms.infrastructure.extraction.gliner_extractor import (
        extract_with_label_budget,
    )
    from ncms.infrastructure.indexing.splade_engine import SpladeEngine

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    sessions_by_q, questions = load_longmemeval_dataset(
        cache_dir=cache_dir, dataset_file=args.dataset,
    )
    q = next((x for x in questions if x.question_id == args.qid), None)
    if q is None:
        raise SystemExit(f"question {args.qid!r} not found")

    logger.info("=" * 70)
    logger.info("FORENSICS — %s", args.qid)
    logger.info("=" * 70)
    logger.info("pattern: %s", q.category)
    logger.info("question: %s", q.question)
    logger.info("answer: %s", q.answer[:200])

    # Config matches the diagnostic exactly.
    config = NCMSConfig(
        db_path=":memory:", actr_noise=0.0,
        splade_enabled=True,
        scoring_weight_bm25=0.6, scoring_weight_actr=0.0,
        scoring_weight_splade=0.3, scoring_weight_graph=0.3,
        admission_enabled=True,
        reconciliation_enabled=True,
        episodes_enabled=True,
        intent_classification_enabled=True,
        scoring_weight_hierarchy=0.1,
        level_first_enabled=True, topic_map_enabled=True,
        dream_query_expansion_enabled=True,
        intent_routing_enabled=True,
        reranker_enabled=True,
        temporal_enabled=True, scoring_weight_temporal=0.2,
        temporal_range_filter_enabled=args.phase_b,
        scoring_weight_recency=0.1, recency_half_life_days=30.0,
    )
    shared_splade = SpladeEngine()
    shared_splade._vectors = {}

    sessions = sessions_by_q.get(q.question_id, [])
    store, _index, _graph, _splade, _cfg, svc = (
        await _create_ncms_instance(
            config=config, shared_splade=shared_splade,
        )
    )
    try:
        await _ingest_sessions(
            svc, sessions, haystack_dates=q.haystack_dates,
        )
        await wait_for_indexing(svc, run_logger=logger)
        # Full haystack contents.
        all_mems = await store.list_memories()
        logger.info("haystack size: %d memories", len(all_mems))

        # Find answer memories (ground truth set).
        answer_mem_ids = {
            m.id for m in all_mems
            if _containing_answer(m.content, q.answer)
        }
        logger.info(
            "answer substring matches %d memories in haystack",
            len(answer_mem_ids),
        )
        for mid in list(answer_mem_ids)[:3]:
            mem = next(m for m in all_mems if m.id == mid)
            logger.info(
                "  answer-memory %s (observed_at=%s): %s",
                mid[:8], mem.observed_at, mem.content[:120],
            )

        # Stage 0: Query parsing.
        ref_time = _parse_lme_date(q.question_date) or datetime.now(UTC)
        t_ref = parse_temporal_reference(q.question, now=ref_time)
        logger.info("temporal_parser: %s", t_ref)

        # Stage 1: GLiNER extraction on query.
        labels = add_temporal_labels(
            resolve_labels([], cached_labels={}),
        ) if config.temporal_range_filter_enabled else resolve_labels(
            [], cached_labels={},
        )
        gliner_out = extract_with_label_budget(
            q.question, labels,
            model_name=config.gliner_model,
            threshold=config.gliner_threshold,
        )
        entities, temporal_spans = (
            RetrievalPipeline.split_entity_and_temporal_spans(gliner_out)
        )
        logger.info(
            "GLiNER query entities: %s",
            [(e['name'], e['type']) for e in entities],
        )
        logger.info(
            "GLiNER query temporal spans: %s",
            [(s.text, s.label) for s in temporal_spans],
        )

        # Stage 2: Intent classification.
        intent = classify_temporal_intent(
            q.question,
            ordinal=getattr(t_ref, 'ordinal', None) if t_ref else None,
            has_range=bool(t_ref and (
                t_ref.range_start or t_ref.range_end)),
            has_relative=bool(t_ref and t_ref.recency_bias),
            subject_count=len(entities),
        )
        logger.info("temporal_intent: %s", intent)

        # Stage 3: Raw BM25 and SPLADE top-10.
        bm25_raw = await asyncio.to_thread(
            svc._index.search, q.question, 50,
        )
        logger.info(
            "BM25 top-10 (answer-memory? ← mark):",
        )
        for i, (mid, score) in enumerate(bm25_raw[:10], 1):
            mark = "  ← ANSWER" if mid in answer_mem_ids else ""
            content = next(
                (m.content for m in all_mems if m.id == mid), "",
            )[:80]
            logger.info(
                "  %2d. %s score=%.2f  %s%s",
                i, mid[:8], score, content, mark,
            )
        bm25_answer_ranks = [
            i + 1 for i, (mid, _) in enumerate(bm25_raw)
            if mid in answer_mem_ids
        ]
        logger.info("BM25 answer-memory rank(s): %s", bm25_answer_ranks)

        try:
            splade_raw = svc._splade.search(q.question, 50)
            splade_answer_ranks = [
                i + 1 for i, (mid, _) in enumerate(splade_raw)
                if mid in answer_mem_ids
            ]
            logger.info(
                "SPLADE answer-memory rank(s): %s",
                splade_answer_ranks,
            )
        except Exception as e:
            logger.warning("SPLADE probe failed: %s", e)

        # Stage 4: Full search pipeline, capture final ranking.
        results = await svc.search(
            query=q.question, limit=50,
            reference_time=ref_time,
        )
        logger.info("Full pipeline top-10:")
        final_answer_ranks = []
        for i, sm in enumerate(results[:10], 1):
            mark = "  ← ANSWER" if sm.memory.id in answer_mem_ids else ""
            logger.info(
                "  %2d. %s score=%.3f observed=%s  %s%s",
                i, sm.memory.id[:8], sm.total_activation,
                sm.memory.observed_at.date() if sm.memory.observed_at else None,
                sm.memory.content[:80], mark,
            )
        for i, sm in enumerate(results, 1):
            if sm.memory.id in answer_mem_ids:
                final_answer_ranks.append(i)
        logger.info("Final pipeline answer-memory rank(s): %s", final_answer_ranks)
        logger.info(
            "R@5=%d  R@20=%d  R@50=%d",
            int(any(r <= 5 for r in final_answer_ranks)),
            int(any(r <= 20 for r in final_answer_ranks)),
            int(any(r <= 50 for r in final_answer_ranks)),
        )
    finally:
        await store.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--qid", required=True)
    p.add_argument("--dataset", default="longmemeval_s")
    p.add_argument("--cache-dir", default=None)
    p.add_argument(
        "--phase-b", action="store_true", default=True,
        help="Run with temporal_range_filter_enabled=True (Phase B)",
    )
    p.add_argument(
        "--p1a", dest="phase_b", action="store_false",
        help="Run with Phase B disabled (P1a baseline)",
    )
    p.add_argument(
        "--output-dir",
        default="benchmarks/results/temporal_diagnostic/forensics",
    )
    args = p.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = setup_logging(f"forensics_{args.qid}", output_dir)
    log_run_header(
        f"Question forensics — {args.qid} "
        f"[{'Phase B' if args.phase_b else 'P1a'}]",
        logger,
    )
    logger.info("Log file: %s", log_file)
    run_async(_run(args), f"forensics_{args.qid}")


if __name__ == "__main__":
    main()
