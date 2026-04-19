"""Profile the full NCMS store + search pipeline with per-stage tracing.

Hooks into the EventLog pipeline_stage() events to show exactly where
time is spent inside store_memory() and search().

Supports two modes:
  --async    Enable background indexing (store returns in ~2ms, workers index)
  (default)  Inline indexing (store blocks until fully indexed)

Usage:
    uv run python -m benchmarks.profile_pipeline
    uv run python -m benchmarks.profile_pipeline --async
    uv run python -m benchmarks.profile_pipeline --no-episodes
    uv run python -m benchmarks.profile_pipeline --no-splade
    uv run python -m benchmarks.profile_pipeline --contradiction
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from benchmarks.env import load_dotenv

load_dotenv()

logger = logging.getLogger("profile")


# ── Tracing EventLog ────────────────────────────────────────────────────
@dataclass
class StageEvent:
    pipeline_id: str
    pipeline_type: str  # "store", "search", or "index"
    stage: str
    duration_ms: float
    data: dict[str, Any] = field(default_factory=dict)
    memory_id: str | None = None


class TracingEventLog:
    """Captures pipeline_stage events for profiling display."""

    def __init__(self) -> None:
        self.events: list[StageEvent] = []

    def pipeline_stage(
        self,
        pipeline_id: str,
        pipeline_type: str,
        stage: str,
        duration_ms: float,
        data: dict[str, Any] | None = None,
        agent_id: str | None = None,
        memory_id: str | None = None,
    ) -> None:
        self.events.append(StageEvent(
            pipeline_id=pipeline_id,
            pipeline_type=pipeline_type,
            stage=stage,
            duration_ms=duration_ms,
            data=data or {},
            memory_id=memory_id,
        ))

    def get_pipeline_events(self, pipeline_id: str) -> list[StageEvent]:
        return [e for e in self.events if e.pipeline_id == pipeline_id]

    def get_last_pipeline(self, pipeline_type: str) -> list[StageEvent]:
        pids = [
            e.pipeline_id for e in reversed(self.events)
            if e.pipeline_type == pipeline_type
        ]
        if not pids:
            return []
        return self.get_pipeline_events(pids[0])

    def get_all_of_type(self, pipeline_type: str) -> list[StageEvent]:
        return [e for e in self.events if e.pipeline_type == pipeline_type]

    def clear(self) -> None:
        self.events.clear()

    # No-op for all other EventLog methods
    def __getattr__(self, name: str) -> object:
        return _noop


def _noop(*args: object, **kwargs: object) -> None:
    pass


# ── Display helpers ─────────────────────────────────────────────────────
def print_trace(label: str, stages: list[StageEvent], wall_ms: float) -> None:
    """Print a single pipeline trace with stage breakdown."""
    if not stages:
        print(f"  {label}: {wall_ms:.0f}ms (no stage events)")
        return

    detail_stages = [s for s in stages if s.stage not in ("start",)]

    print(f"  {label}: {wall_ms:>7.1f}ms wall")
    for s in detail_stages:
        extra = ""
        if s.stage == "complete":
            extra = f"  [total: {s.duration_ms:.1f}ms]"
        elif s.stage == "admission":
            extra = f"  route={s.data.get('route', '?')} score={s.data.get('score', '?')}"
        elif s.stage == "dedup_skip":
            extra = f"  existing={s.data.get('existing_memory_id', '?')[:8]}"
        elif s.stage == "enqueued":
            extra = f"  task={s.data.get('task_id', '?')} depth={s.data.get('queue_depth', '?')}"
        elif s.stage == "entity_extraction":
            count = s.data.get("total_count", s.data.get("auto_count", "?"))
            names = s.data.get("entity_names", [])[:5]
            extra = f"  entities={count} {names}"
        elif s.stage == "graph_linking" or s.stage == "entity_linking":
            extra = f"  linked={s.data.get('entities_linked', '?')}"
        elif s.stage == "cooccurrence_edges":
            extra = f"  new={s.data.get('edges_new', 0)} inc={s.data.get('edges_incremented', 0)}"
        elif s.stage == "memory_node":
            extra = f"  {s.data.get('layer', '?')}:{s.data.get('node_type', '?')}"
        elif s.stage == "episode_formation":
            extra = f"  ep={s.data.get('episode_id', '?')}"
        elif s.stage == "parallel_indexing":
            extra = (
                f"  bm25={s.data.get('bm25_ms', 0):.0f}ms"
                f" splade={s.data.get('splade_ms', 0):.0f}ms"
                f" gliner={s.data.get('gliner_ms', 0):.0f}ms"
                f" entities={s.data.get('entity_count', '?')}"
            )
        elif s.stage == "started":
            extra = f"  worker={s.data.get('worker_id', '?')} attempt={s.data.get('attempt', 0)}"
        elif s.stage == "bm25" or s.stage == "splade":
            extra = f"  candidates={s.data.get('candidate_count', '?')}"
        elif s.stage == "rrf_fusion":
            extra = f"  fused={s.data.get('fused_count', '?')}"
        elif s.stage == "graph_expansion":
            extra = f"  novel={s.data.get('novel_candidates', 0)} total={s.data.get('total_candidates', '?')}"
        elif s.stage == "node_preload":
            extra = f"  nodes={s.data.get('nodes_loaded', '?')}"
        elif s.stage == "intent_classification":
            extra = f"  {s.data.get('intent', '?')} conf={s.data.get('confidence', '?')}"
        elif s.stage == "intent_supplement":
            extra = f"  +{s.data.get('supplement_count', 0)} candidates"
        elif s.stage == "cross_encoder_rerank":
            extra = f"  {s.data.get('input_count', '?')}→{s.data.get('output_count', '?')}"
        elif s.stage in ("retry", "failed"):
            extra = f"  error={s.data.get('error', '?')[:60]}"

        ms_str = f"{s.duration_ms:>7.1f}ms" if s.duration_ms > 0 else "       "
        print(f"    {s.stage:<25} {ms_str}{extra}")


# ── Main profiler ───────────────────────────────────────────────────────
async def run_profile(
    episodes: bool = True,
    admission: bool = True,
    contradiction: bool = False,
    splade: bool = True,
    async_indexing: bool = False,
    n_memories: int = 10,
    index_workers: int = 3,
) -> None:
    from ncms.application.memory_service import MemoryService
    from ncms.config import NCMSConfig
    from ncms.infrastructure.graph.networkx_store import NetworkXGraph
    from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
    from ncms.infrastructure.storage.sqlite_store import SQLiteStore

    # ── Setup ────────────────────────────────────────────────────────
    t_setup = time.perf_counter()

    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()

    tracer = TracingEventLog()

    splade_engine = None
    if splade:
        from ncms.infrastructure.indexing.splade_engine import SpladeEngine
        splade_engine = SpladeEngine()

    config = NCMSConfig(
        db_path=":memory:",
        splade_enabled=splade,
        actr_noise=0.0,
        episodes_enabled=episodes,
        admission_enabled=admission,
        contradiction_detection_enabled=contradiction,
        intent_classification_enabled=True,
        index_workers=index_workers,
        scoring_weight_bm25=0.6,
        scoring_weight_splade=0.3 if splade else 0.0,
        scoring_weight_graph=0.3,
        scoring_weight_actr=0.0,
    )

    episode_svc = None
    if episodes:
        from ncms.application.episode_service import EpisodeService
        episode_svc = EpisodeService(
            store=store, index=index,
            splade=splade_engine, config=config,
        )

    admission_svc = None
    if admission:
        from ncms.application.admission_service import AdmissionService
        admission_svc = AdmissionService(
            store=store, index=index, graph=graph, config=config,
        )

    svc = MemoryService(
        store=store, index=index, graph=graph, config=config,
        splade=splade_engine, episode=episode_svc, admission=admission_svc,
    )
    # Inject our tracing event log
    svc._event_log = tracer

    # Start background indexing pool if requested
    if async_indexing:
        await svc.start_index_pool()

    setup_ms = (time.perf_counter() - t_setup) * 1000
    print(f"Setup: {setup_ms:.0f}ms")
    flags = []
    if episodes:
        flags.append("Episodes")
    if admission:
        flags.append("Admission")
    if contradiction:
        flags.append("Contradiction")
    if splade:
        flags.append("SPLADE")
    if config.intent_classification_enabled:
        flags.append("IntentClassification")
    if async_indexing:
        flags.append(f"AsyncIndexing({index_workers}w)")
    print(f"  Features: {', '.join(flags)}")
    print()

    # ── Test content ─────────────────────────────────────────────────
    contents = [
        "ADR-003: JWT with inline RBAC for authentication and authorization",
        "Security compliance checklist: passwords hashed with bcrypt cost 12",
        "MongoDB connection pooling configured with 20 max connections",
        "API rate limiting: 100 requests per minute per user token",
        "Deployment v2.3.1 includes fix for JIRA-4521 payment timeout",
        "React frontend uses Redux for global state management",
        "WebSocket server handles real-time notifications to clients",
        "PostgreSQL 16 migration completed with zero downtime strategy",
        "Kubernetes pod autoscaling configured for CPU threshold 70%",
        "CI/CD pipeline runs unit tests, lint, and integration tests",
        "Redis cache TTL set to 3600 seconds for session data",
        "GraphQL API schema defines Movie, Actor, and Review types",
        "Docker multi-stage build reduces image size from 1.2GB to 180MB",
        "Load balancer health check endpoint returns 200 every 10 seconds",
        "Error tracking integrated with Sentry for production monitoring",
    ][:n_memories]

    # ── STORE with full traces ──────────────────────────────────────
    mode_label = "ASYNC" if async_indexing else "INLINE"
    print(f"{'=' * 60}")
    print(f"STORE PIPELINE — {mode_label} ({len(contents)} memories)")
    print(f"{'=' * 60}")
    print()

    store_times: list[float] = []
    for i, content in enumerate(contents):
        tracer.clear()
        t0 = time.perf_counter()
        try:
            await svc.store_memory(
                content=content, memory_type="fact", importance=5.0,
                domains=["architecture"], source_agent="architect",
            )
            wall_ms = (time.perf_counter() - t0) * 1000
            store_times.append(wall_ms)

            stages = tracer.get_last_pipeline("store")
            tag = " (cold start)" if i == 0 and wall_ms > 2000 else ""
            print_trace(f"mem {i + 1:>2}{tag}", stages, wall_ms)
            print()

        except Exception as e:
            wall_ms = (time.perf_counter() - t0) * 1000
            print(f"  mem {i + 1:>2}: {wall_ms:.1f}ms  ERROR: {e}")
            import traceback
            traceback.print_exc()
            print()

    # Store summary
    if store_times:
        warm = store_times[1:] if len(store_times) > 1 else store_times
        avg = sum(warm) / len(warm)
        p50 = sorted(warm)[len(warm) // 2]
        p95 = sorted(warm)[int(len(warm) * 0.95)]
        dedup_count = sum(1 for t in warm if t < 5)
        print(f"{'─' * 60}")
        print(f"Store summary — {mode_label} (warm, excl. cold start):")
        print(f"  avg={avg:.0f}ms  p50={p50:.0f}ms  p95={p95:.0f}ms  "
              f"total={sum(store_times):.0f}ms")
        print(f"  dedup hits: {dedup_count}/{len(warm)}")

    # ── Wait for background indexing to finish ──────────────────────
    if async_indexing and svc._index_pool is not None:
        print()
        print("Waiting for background indexing to drain...")
        t_drain = time.perf_counter()
        pool = svc._index_pool
        poll_count = 0
        while True:
            stats = pool.stats()  # type: ignore[union-attr]
            if stats.queue_depth == 0 and stats.workers_busy == 0:
                break
            poll_count += 1
            if poll_count % 10 == 0:
                print(
                    f"  queue={stats.queue_depth} busy={stats.workers_busy} "
                    f"processed={stats.processed_total} "
                    f"({(time.perf_counter() - t_drain) * 1000:.0f}ms)"
                )
            await asyncio.sleep(0.1)
        drain_ms = (time.perf_counter() - t_drain) * 1000
        final_stats = pool.stats()  # type: ignore[union-attr]
        print(
            f"  Drained in {drain_ms:.0f}ms — "
            f"processed={final_stats.processed_total} "
            f"failed={final_stats.failed_total} "
            f"retried={final_stats.retried_total} "
            f"avg={final_stats.avg_process_ms:.0f}ms"
        )

        # Show background worker traces
        print()
        print(f"{'=' * 60}")
        print("BACKGROUND INDEX TRACES")
        print(f"{'=' * 60}")
        print()
        # Group by pipeline_id
        idx_events = tracer.get_all_of_type("index")
        pids = list(dict.fromkeys(e.pipeline_id for e in idx_events))
        for pid in pids:
            stages = tracer.get_pipeline_events(pid)
            mem_id = next((s.memory_id for s in stages if s.memory_id), "?")
            complete = next((s for s in stages if s.stage == "complete"), None)
            wall = complete.duration_ms if complete else 0
            print_trace(f"idx {mem_id[:8]}", stages, wall)
            print()

    # Stats
    cursor = await store.db.execute("SELECT count(*) FROM memories")
    mem_count = (await cursor.fetchone())[0]
    cursor = await store.db.execute("SELECT count(*) FROM entities")
    ent_count = (await cursor.fetchone())[0]
    cursor = await store.db.execute("SELECT count(*) FROM memory_nodes")
    node_count = (await cursor.fetchone())[0]
    episodes_list = await store.get_open_episodes() if episodes else []

    print(f"{'─' * 60}")
    print(f"  memories={mem_count}  entities={ent_count}  nodes={node_count}  "
          f"episodes={len(episodes_list)}")
    print(f"  graph: {graph._graph.number_of_nodes()} nodes, "
          f"{graph._graph.number_of_edges()} edges")
    print()

    # ── SEARCH with full traces ─────────────────────────────────────
    queries = [
        ("fact_lookup", "What authentication method is used?"),
        ("current_state", "What is the MongoDB connection pool size?"),
        ("temporal", "What was deployed recently?"),
        ("pattern", "What patterns in the API design?"),
        ("change_detect", "What changed in the deployment?"),
    ]

    print(f"{'=' * 60}")
    print(f"SEARCH PIPELINE ({len(queries)} queries)")
    print(f"{'=' * 60}")
    print()

    for name, query in queries:
        tracer.clear()
        t0 = time.perf_counter()
        results = await svc.search(query=query, limit=5)
        wall_ms = (time.perf_counter() - t0) * 1000

        stages = tracer.get_last_pipeline("search")
        top = results[0].total_activation if results else 0
        print_trace(f"{name} ({len(results)} results, top={top:.3f})", stages, wall_ms)
        print()

    print(f"{'─' * 60}")
    print("Done.")
    print()

    # Cleanup
    if async_indexing:
        await svc.stop_index_pool()
    await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile NCMS pipeline")
    parser.add_argument("--no-episodes", action="store_true")
    parser.add_argument("--no-admission", action="store_true")
    parser.add_argument("--no-splade", action="store_true")
    parser.add_argument("--contradiction", action="store_true")
    parser.add_argument("--async", dest="async_indexing", action="store_true",
                        help="Enable background indexing")
    parser.add_argument("--workers", type=int, default=3,
                        help="Number of index workers (async mode)")
    parser.add_argument("-n", type=int, default=10, help="Number of memories")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    print()
    print("NCMS Pipeline Profiler — Full Trace")
    print("=" * 60)
    print()

    try:
        asyncio.run(run_profile(
            episodes=not args.no_episodes,
            admission=not args.no_admission,
            contradiction=args.contradiction,
            splade=not args.no_splade,
            async_indexing=args.async_indexing,
            n_memories=args.n,
            index_workers=args.workers,
        ))
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(130)
    except Exception as e:
        print(f"\nFATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
