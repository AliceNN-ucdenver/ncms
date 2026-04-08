"""Profile store_memory pipeline to identify per-stage bottlenecks.

Uses 13 scifact documents (same as dream smoke test) and instruments
every stage of the store_memory pipeline with wall-clock timings.

Usage:
    uv run python -m benchmarks.profile_store
    uv run python -m benchmarks.profile_store --with-phases  # admission/reconcil/episodes
    uv run python -m benchmarks.profile_store --docs 50       # More documents
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import statistics
import time

from benchmarks.core.datasets import DATASET_TOPICS, load_beir_dataset

logger = logging.getLogger(__name__)

# Stage names matching memory_service.py pipeline
STAGE_NAMES = [
    "admission",
    "persist",
    "bm25_index",
    "splade_index",
    "entity_extraction",
    "graph_linking",
    "contradiction",
    "memory_node",
    "reconciliation",
    "episode_formation",
    "complete",
]


class TimingCollector:
    """Intercept pipeline_stage events to collect per-stage timings."""

    def __init__(self) -> None:
        self.timings: dict[str, list[float]] = {}  # stage -> [duration_ms, ...]
        self.current_doc_stages: dict[str, float] = {}

    def pipeline_stage(
        self, *, pipeline_id: str, pipeline_type: str, stage: str,
        duration_ms: float, data: dict | None = None,
        agent_id: str | None = None, memory_id: str | None = None,
    ) -> None:
        if stage in ("start",):
            self.current_doc_stages = {}
            return
        if stage == "complete":
            # Record total from the 'complete' event
            self.timings.setdefault("total", []).append(duration_ms)
            return
        self.timings.setdefault(stage, []).append(duration_ms)

    # Stubs for other event_log methods that MemoryService calls
    def admission_scored(self, **kwargs) -> None:
        pass

    def memory_stored(self, **kwargs) -> None:
        pass

    def __getattr__(self, name: str):
        """Catch any other event_log method calls silently."""
        return lambda **kwargs: None


async def run_profile(n_docs: int = 13, with_phases: bool = False) -> None:
    from ncms.application.memory_service import MemoryService
    from ncms.config import NCMSConfig
    from ncms.infrastructure.graph.networkx_store import NetworkXGraph
    from ncms.infrastructure.indexing.splade_engine import SpladeEngine
    from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
    from ncms.infrastructure.storage.sqlite_store import SQLiteStore

    # Load dataset
    print(f"Loading scifact dataset (first {n_docs} docs)...")
    corpus, _, _ = load_beir_dataset("scifact")

    # Slice to N docs
    doc_ids = list(corpus.keys())[:n_docs]
    docs = [(doc_id, corpus[doc_id]) for doc_id in doc_ids]
    print(f"  Selected {len(docs)} documents")

    # Create backends
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()

    index = TantivyEngine()
    index.initialize()

    graph = NetworkXGraph()
    splade = SpladeEngine()

    config_kwargs: dict = {
        "db_path": ":memory:",
        "actr_noise": 0.0,
        "splade_enabled": True,
    }

    admission = None
    reconciliation = None
    episode = None

    if with_phases:
        from ncms.application.admission_service import AdmissionService
        from ncms.application.episode_service import EpisodeService
        from ncms.application.reconciliation_service import ReconciliationService

        config_kwargs.update({
            "admission_enabled": True,
            "reconciliation_enabled": True,
            "episodes_enabled": True,
        })
        config = NCMSConfig(**config_kwargs)
        admission = AdmissionService(store=store, index=index, graph=graph, config=config)
        reconciliation = ReconciliationService(store=store, config=config)
        episode = EpisodeService(store=store, index=index, config=config, splade=splade)
    else:
        config = NCMSConfig(**config_kwargs)

    # Seed domain topics
    topic_info = DATASET_TOPICS.get("scifact", {})
    domain = topic_info.get("domain", "general") if topic_info else "general"
    labels = topic_info.get("labels", []) if topic_info else []
    if labels:
        await store.set_consolidation_value(
            f"entity_labels:{domain}", json.dumps(labels),
        )
        print(f"  Seeded topics for '{domain}': {labels}")

    # Create timing collector (replaces event_log)
    collector = TimingCollector()

    svc = MemoryService(
        store=store, index=index, graph=graph, config=config,
        splade=splade, event_log=collector,
        admission=admission, reconciliation=reconciliation, episode=episode,
    )

    # Warm up GLiNER model (first call downloads/loads)
    print("\nWarming up GLiNER model (first-call load)...")
    t_warm = time.perf_counter()
    from ncms.infrastructure.extraction.gliner_extractor import extract_entities_gliner
    extract_entities_gliner("Test warmup text for model loading", labels=["person"])
    warm_ms = (time.perf_counter() - t_warm) * 1000
    print(f"  GLiNER warm-up: {warm_ms:.0f}ms")

    # Warm up SPLADE model via a dummy memory
    print("Warming up SPLADE model...")
    t_warm = time.perf_counter()
    from ncms.domain.models import Memory as _WarmupMemory
    _dummy = _WarmupMemory(content="Test warmup text for SPLADE model loading", type="fact")
    with contextlib.suppress(Exception):
        splade.index_memory(_dummy)  # May fail without full init, but model loads
    warm_ms = (time.perf_counter() - t_warm) * 1000
    print(f"  SPLADE warm-up: {warm_ms:.0f}ms")

    # Ingest with per-doc timing
    print(f"\n{'='*70}")
    print(f"Ingesting {len(docs)} documents (phases {'1-3' if with_phases else 'off'})...")
    print(f"{'='*70}\n")

    per_doc_times: list[float] = []

    for i, (_doc_id, doc) in enumerate(docs):
        title = doc.get("title", "")
        text = doc.get("text", "")
        content = f"{title}\n{text}".strip() if title else text
        if not content:
            continue
        content = content[:10000]

        t0 = time.perf_counter()
        await svc.store_memory(
            content=content,
            memory_type="fact",
            domains=[domain] if domain != "general" else [],
        )
        doc_ms = (time.perf_counter() - t0) * 1000
        per_doc_times.append(doc_ms)

        content_len = len(content)
        print(f"  [{i+1:3d}/{len(docs)}] {doc_ms:7.0f}ms  ({content_len:5d} chars)  {title[:60]}")

    # Cleanup
    await store.close()

    # Report
    print(f"\n{'='*70}")
    print("PROFILE RESULTS")
    print(f"{'='*70}\n")

    print(f"Documents: {len(per_doc_times)}")
    print(f"Total wall time: {sum(per_doc_times):.0f}ms ({sum(per_doc_times)/1000:.1f}s)")
    print(f"Throughput: {len(per_doc_times) / (sum(per_doc_times) / 1000):.2f} docs/sec")
    print(f"Per-doc: mean={statistics.mean(per_doc_times):.0f}ms  "
          f"median={statistics.median(per_doc_times):.0f}ms  "
          f"min={min(per_doc_times):.0f}ms  max={max(per_doc_times):.0f}ms")
    if len(per_doc_times) > 2:
        print(f"  stdev={statistics.stdev(per_doc_times):.0f}ms")

    print(f"\n{'─'*70}")
    print(f"{'Stage':<25s}  {'Count':>5s}  {'Total ms':>9s}  {'Mean ms':>8s}  "
          f"{'Median':>8s}  {'Min':>7s}  {'Max':>7s}  {'% of total':>9s}")
    print(f"{'─'*70}")

    total_pipeline_ms = sum(collector.timings.get("total", [0]))

    # Sort stages by total time descending
    stage_totals = []
    for stage, times in sorted(collector.timings.items()):
        if stage == "total":
            continue
        stage_totals.append((stage, times))
    stage_totals.sort(key=lambda x: sum(x[1]), reverse=True)

    for stage, times in stage_totals:
        total_ms = sum(times)
        mean_ms = statistics.mean(times)
        median_ms = statistics.median(times)
        min_ms = min(times)
        max_ms = max(times)
        pct = (total_ms / total_pipeline_ms * 100) if total_pipeline_ms > 0 else 0
        print(
            f"  {stage:<23s}  {len(times):5d}  {total_ms:9.1f}  {mean_ms:8.1f}  "
            f"{median_ms:8.1f}  {min_ms:7.1f}  {max_ms:7.1f}  {pct:8.1f}%"
        )

    print(f"{'─'*70}")
    print(f"  {'TOTAL':<23s}  {'':5s}  {total_pipeline_ms:9.1f}")

    # Show unaccounted time
    accounted = sum(sum(t) for s, t in collector.timings.items() if s != "total")
    unaccounted = total_pipeline_ms - accounted
    if total_pipeline_ms > 0:
        print(f"  {'(unaccounted)':<23s}  {'':5s}  {unaccounted:9.1f}  "
              f"{'':8s}  {'':8s}  {'':7s}  {'':7s}  {unaccounted/total_pipeline_ms*100:8.1f}%")

    print()

    # Log to file
    log_path = "benchmarks/results/profile_store.log"
    with open(log_path, "w") as f:
        f.write(f"Profile: {len(per_doc_times)} docs, phases={'1-3' if with_phases else 'off'}\n")
        f.write(f"Throughput: {len(per_doc_times) / (sum(per_doc_times) / 1000):.2f} docs/sec\n\n")
        for stage, times in stage_totals:
            f.write(f"{stage}: mean={statistics.mean(times):.1f}ms  "
                    f"total={sum(times):.1f}ms  count={len(times)}\n")
    print(f"Results written to {log_path}")


def main() -> None:
    from benchmarks.env import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="Profile store_memory pipeline")
    parser.add_argument("--docs", type=int, default=13, help="Number of documents to ingest")
    parser.add_argument("--with-phases", action="store_true",
                        help="Enable admission, reconciliation, and episodes (phases 1-3)")
    parser.add_argument("--device", choices=["cpu", "mps", "cuda"],
                        help="Force GLiNER device (default: auto-detect)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.device:
        import os
        os.environ["NCMS_GLINER_DEVICE"] = args.device

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    # Suppress noisy loggers even in verbose mode
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("gliner").setLevel(logging.WARNING)

    asyncio.run(run_profile(n_docs=args.docs, with_phases=args.with_phases))


if __name__ == "__main__":
    main()
