#!/usr/bin/env bash
#
# Quick smoke test: run ablation with only 13 corpus documents.
# Useful for verifying the pipeline works end-to-end before a full run.
#
# Usage:
#   ./benchmarks/run_test13.sh              # scifact (default)
#   ./benchmarks/run_test13.sh nfcorpus     # specific dataset
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RESULTS_DIR="$SCRIPT_DIR/results/test13"
DATASET="${1:-scifact}"

cd "$PROJECT_ROOT"

# Load .env if present (HF_TOKEN, etc.)
if [[ -f .env ]]; then
    set -a
    source .env
    set +a
fi

export PYTHONUNBUFFERED=1

mkdir -p "$RESULTS_DIR"

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  NCMS Ablation Smoke Test (13 docs)                     ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "[INFO]  Dataset  : $DATASET"
echo "[INFO]  Docs     : 13"
echo "[INFO]  Results  : $RESULTS_DIR/"
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
log_file = Path('$RESULTS_DIR') / f'test13_{ts}.log'

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

logger = logging.getLogger('benchmarks.test13')

# Symlink for tail -f
latest = Path('$RESULTS_DIR') / 'ablation_latest.log'
try:
    latest.unlink(missing_ok=True)
    latest.symlink_to(log_file.name)
except OSError:
    pass

async def run_test13():
    dataset_name = '$DATASET'

    logger.info('=' * 60)
    logger.info('Smoke test: 13 docs from %s', dataset_name)
    logger.info('Log file: %s', log_file)
    logger.info('PID: %d', __import__('os').getpid())
    logger.info('=' * 60)

    # Load dataset
    logger.info('Step 1/5: Loading dataset...')
    from benchmarks.datasets import load_beir_dataset
    corpus, queries, qrels = load_beir_dataset(dataset_name)
    logger.info('  Full corpus: %d docs, %d queries, %d qrels', len(corpus), len(queries), len(qrels))

    # Slice to 13 docs
    logger.info('Step 2/5: Slicing to 13 documents...')
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
        logger.warning('No queries have relevant docs in the 13-doc slice, using first 5 queries instead')
        query_keys = list(queries.keys())[:5]
        small_queries = {k: queries[k] for k in query_keys}
        small_qrels = {k: qrels.get(k, {}) for k in query_keys}

    # Ingest
    logger.info('Step 3/5: Ingesting %d documents...', len(small_corpus))
    t0 = time.perf_counter()

    from benchmarks.harness import ingest_corpus
    store, index, graph, splade, config, doc_to_mem, mem_to_doc = (
        await ingest_corpus(small_corpus, dataset_name)
    )

    ingest_time = time.perf_counter() - t0
    logger.info('  Ingestion complete: %.2fs (%.1f docs/sec)', ingest_time, len(small_corpus) / ingest_time)

    # Run configs
    logger.info('Step 4/5: Running ablation configs...')
    from benchmarks.configs import ABLATION_CONFIGS
    from benchmarks.harness import run_config_queries
    from benchmarks.metrics import compute_all_metrics
    from benchmarks.datasets import DATASET_TOPICS

    topic_info = DATASET_TOPICS.get(dataset_name, {})
    domain = topic_info.get('domain', 'general') if topic_info else 'general'

    eval_queries = {qid: small_queries[qid] for qid in small_qrels if qid in small_queries}
    if not eval_queries:
        eval_queries = small_queries

    results = {}
    for cfg in ABLATION_CONFIGS:
        logger.info('  Config: %s', cfg.display_name)
        t1 = time.perf_counter()

        rankings = await run_config_queries(
            store=store, index=index, graph=graph,
            splade_engine=splade, ablation_config=cfg,
            queries=eval_queries, mem_to_doc=mem_to_doc, domain=domain,
        )

        metrics = compute_all_metrics(rankings, small_qrels)
        elapsed = time.perf_counter() - t1

        results[cfg.name] = {**metrics, 'elapsed_seconds': round(elapsed, 2)}
        logger.info('    nDCG@10=%.4f  MRR@10=%.4f  Recall@100=%.4f  (%.2fs)',
                     metrics['nDCG@10'], metrics['MRR@10'], metrics['Recall@100'], elapsed)

    await store.close()

    # Summary
    logger.info('Step 5/5: Summary')
    total = time.perf_counter() - t0
    logger.info('=' * 60)
    logger.info('Smoke test PASSED')
    logger.info('  Total time : %.1fs', total)
    logger.info('  Configs    : %d', len(results))
    logger.info('  Log file   : %s', log_file)
    logger.info('=' * 60)

asyncio.run(run_test13())
"

EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "╔══════════════════════════════════════════════════════════╗"
    echo "║  Smoke test PASSED                                      ║"
    echo "╚══════════════════════════════════════════════════════════╝"
else
    echo "╔══════════════════════════════════════════════════════════╗"
    echo "║  Smoke test FAILED (exit code: $EXIT_CODE)                       ║"
    echo "╚══════════════════════════════════════════════════════════╝"
fi
echo ""
echo "[INFO]  Log: $RESULTS_DIR/ablation_latest.log"
echo ""

exit $EXIT_CODE
