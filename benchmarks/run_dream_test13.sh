#!/usr/bin/env bash
#
# Quick smoke test: run dream cycle experiment with only 13 corpus documents.
# Validates the full pipeline (ingest with phases 1-3, consolidation, dream)
# works end-to-end before a full run.
#
# Usage:
#   ./benchmarks/run_dream_test13.sh              # scifact (default)
#   ./benchmarks/run_dream_test13.sh nfcorpus     # specific dataset
#
# LLM Override:
#   LLM_MODEL=ollama_chat/qwen3.5:35b-a3b LLM_API_BASE="" ./benchmarks/run_dream_test13.sh
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RESULTS_DIR="$SCRIPT_DIR/results/dream_test13"
DATASET="${1:-scifact}"

# LLM configuration (env vars with defaults)
LLM_MODEL="${LLM_MODEL:-openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16}"
LLM_API_BASE="${LLM_API_BASE:-http://spark-ee7d.local:8000/v1}"

cd "$PROJECT_ROOT"

export PYTHONUNBUFFERED=1

mkdir -p "$RESULTS_DIR"

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  NCMS Dream Cycle Smoke Test (13 docs)                  ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "[INFO]  Dataset   : $DATASET"
echo "[INFO]  Docs      : 13"
echo "[INFO]  LLM Model : $LLM_MODEL"
echo "[INFO]  LLM API   : $LLM_API_BASE"
echo "[INFO]  Results   : $RESULTS_DIR/"
echo ""

uv run python -c "
import asyncio
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# ── Logging (file + console) ──────────────────────────────────────────
ts = datetime.now().strftime('%Y-%m-%d_%H%M%S')
log_file = Path('$RESULTS_DIR') / f'dream_test13_{ts}.log'

file_h = logging.FileHandler(log_file, mode='w', encoding='utf-8')
file_h.setFormatter(logging.Formatter(
    '%(asctime)s [%(levelname)-7s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
))
console_h = logging.StreamHandler(sys.stdout)
console_h.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)-7s %(message)s',
    datefmt='%H:%M:%S',
))
logging.basicConfig(level=logging.DEBUG, handlers=[file_h, console_h])

logger = logging.getLogger('benchmarks.dream_test13')

# Symlink for tail -f
latest = Path('$RESULTS_DIR') / 'dream_latest.log'
try:
    latest.unlink(missing_ok=True)
    latest.symlink_to(log_file.name)
except OSError:
    pass

async def run_dream_test13():
    dataset_name = '$DATASET'
    llm_model = '$LLM_MODEL'
    llm_api_base = '$LLM_API_BASE'

    logger.info('=' * 60)
    logger.info('Dream cycle smoke test: 13 docs from %s', dataset_name)
    logger.info('LLM model: %s', llm_model)
    logger.info('LLM API: %s', llm_api_base)
    logger.info('Log file: %s', log_file)
    logger.info('PID: %d', __import__('os').getpid())
    logger.info('=' * 60)

    # Step 1: Load dataset
    logger.info('Step 1/6: Loading dataset...')
    from benchmarks.datasets import load_beir_dataset
    corpus, queries, qrels = load_beir_dataset(dataset_name)
    logger.info('  Full corpus: %d docs, %d queries, %d qrels',
                len(corpus), len(queries), len(qrels))

    # Step 2: Slice to 13 docs
    logger.info('Step 2/6: Slicing to 13 documents...')
    corpus_keys = list(corpus.keys())[:13]
    small_corpus = {k: corpus[k] for k in corpus_keys}
    logger.info('  Test corpus: %d docs', len(small_corpus))

    # Find queries that have qrels pointing to our 13 docs
    small_qrels = {}
    for qid, rels in qrels.items():
        matching = {did: score for did, score in rels.items() if did in small_corpus}
        if matching:
            small_qrels[qid] = matching
    small_queries = {qid: queries[qid] for qid in small_qrels if qid in queries}
    logger.info('  Test queries: %d (with relevant docs in slice)', len(small_queries))

    if not small_queries:
        logger.warning('No queries have relevant docs in the 13-doc slice, '
                       'using first 5 queries instead')
        query_keys = list(queries.keys())[:5]
        small_queries = {k: queries[k] for k in query_keys}
        small_qrels = {k: qrels.get(k, {}) for k in query_keys}

    # Step 3: Ingest with phases 1-3
    logger.info('Step 3/6: Ingesting with phases 1-3 (admission, reconciliation, episodes)...')
    t0 = time.perf_counter()

    from benchmarks.dream_harness import (
        ingest_with_phases, force_close_episodes,
        run_consolidation_stage, measure_retrieval,
        inject_access_history,
    )

    state = await ingest_with_phases(small_corpus, dataset_name, llm_model, llm_api_base)
    ingest_time = time.perf_counter() - t0
    logger.info('  Ingestion complete: %.2fs (%.1f docs/sec)',
                ingest_time, len(small_corpus) / ingest_time)

    # Step 4: Force-close episodes
    logger.info('Step 4/6: Force-closing episodes...')
    episodes_closed = await force_close_episodes(state)
    logger.info('  Episodes closed: %d', episodes_closed)

    # Step 5: Run dream stages
    logger.info('Step 5/6: Running dream stages...')
    from benchmarks.dream_configs import DREAM_STAGES

    results = {}
    baseline_ndcg = None

    for stage in DREAM_STAGES:
        logger.info('  Stage: %s', stage.display_name)
        t1 = time.perf_counter()

        # Inject access history before dream stages
        if stage.dream_cycle:
            await inject_access_history(state, small_queries, small_qrels)

        # Run consolidation (skip for baseline)
        consolidation_metrics = {}
        if stage.name != 'baseline':
            consolidation_metrics = await run_consolidation_stage(state, stage)
            logger.info('    Consolidation: %s', consolidation_metrics)

        # Measure retrieval (returns RetrievalResult dataclass)
        retrieval = await measure_retrieval(state, small_queries, small_qrels)
        elapsed = time.perf_counter() - t1
        ir_metrics = retrieval.metrics  # dict with nDCG@10, MRR@10, etc.

        ndcg = ir_metrics.get('nDCG@10', 0.0)
        if baseline_ndcg is None:
            baseline_ndcg = ndcg

        delta_pct = (
            ((ndcg - baseline_ndcg) / baseline_ndcg * 100)
            if baseline_ndcg > 0 else 0.0
        )

        results[stage.name] = {
            'retrieval_metrics': ir_metrics,
            'per_query_ndcg': retrieval.per_query_ndcg,
            'consolidation_metrics': consolidation_metrics,
            'delta_pct': round(delta_pct, 2),
            'elapsed_seconds': round(elapsed, 2),
        }

        logger.info('    nDCG@10=%.4f  MRR@10=%.4f  Recall@100=%.4f  '
                     'delta=%.2f%%  insights=%d  (%.2fs)',
                     ndcg, ir_metrics.get('MRR@10', 0), ir_metrics.get('Recall@100', 0),
                     delta_pct, int(ir_metrics.get('insight_count', 0)), elapsed)

    await state.store.close()

    # Step 6: Summary
    logger.info('Step 6/6: Summary')
    total = time.perf_counter() - t0
    logger.info('=' * 60)
    logger.info('Dream smoke test PASSED')
    logger.info('  Total time : %.1fs', total)
    logger.info('  Stages     : %d', len(results))
    logger.info('  Log file   : %s', log_file)
    logger.info('=' * 60)

asyncio.run(run_dream_test13())
"

EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "╔══════════════════════════════════════════════════════════╗"
    echo "║  Dream smoke test PASSED                                ║"
    echo "╚══════════════════════════════════════════════════════════╝"
else
    echo "╔══════════════════════════════════════════════════════════╗"
    echo "║  Dream smoke test FAILED (exit code: $EXIT_CODE)                 ║"
    echo "╚══════════════════════════════════════════════════════════╝"
fi
echo ""
echo "[INFO]  Log: $RESULTS_DIR/dream_latest.log"
echo ""

exit $EXIT_CODE
