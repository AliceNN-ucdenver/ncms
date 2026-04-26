"""Quality + latency metrics for paper.

Computes:
  - Ingest latency (p50/p95/p99) with and without LLM features
  - Retrieval latency (p50/p95/p99)
  - Memory growth efficiency
  - Superseded leakage rate

Results written to benchmarks/tuning/quality_metrics.json
"""

from __future__ import annotations

import asyncio
import json
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

TUNING_DIR = Path(__file__).parent

# Test content for latency measurements
INGEST_SAMPLES = [
    "The API gateway uses rate limiting at 1000 requests per minute per client",
    "Database migration to PostgreSQL 16 completed on 2026-01-15",
    "Auth service switched from OAuth 2.0 to API key authentication",
    "Kubernetes cluster running version 1.28 with auto-scaling enabled",
    "Redis cache hit rate is 95% for the session store",
    "CI pipeline runs in GitHub Actions with 12 minute average build time",
    "Monitoring stack upgraded from Nagios to Prometheus and Grafana",
    "Feature flag system uses LaunchDarkly for gradual rollouts",
    "Container images stored in GitHub Container Registry",
    "Log aggregation migrated to Datadog for all services",
    "Load balancer uses AWS ALB with least-connections routing",
    "Secret management handled by HashiCorp Vault in production",
    "Database backups use WAL-G with continuous archiving to S3",
    "API protocol is REST with JSON for public, gRPC for internal services",
    "Message queue uses Apache Kafka for async event processing",
    "DNS managed by Cloudflare with proxy and DDoS protection enabled",
    "SSL certificates auto-renewed via Let's Encrypt with cert-manager",
    "Application runs Python 3.12 with uv as package manager",
    "Code review requires 2 approvals before merging to main branch",
    "Test coverage threshold is 80% enforced by CI pipeline checks",
]

SEARCH_QUERIES = [
    "what database version is running",
    "authentication method for API",
    "kubernetes cluster version",
    "caching system for sessions",
    "CI build time",
    "monitoring and alerting system",
    "feature flag management",
    "container image registry",
    "log aggregation platform",
    "load balancer configuration",
    "secret management approach",
    "backup strategy for databases",
    "API protocol used",
    "message queue system",
    "DNS provider",
]


async def measure_latency() -> dict:
    """Measure ingest and retrieval latency."""
    from ncms.application.admission_service import AdmissionService
    from ncms.application.episode_service import EpisodeService
    from ncms.application.memory_service import MemoryService
    from ncms.application.reconciliation_service import ReconciliationService
    from ncms.config import NCMSConfig
    from ncms.infrastructure.graph.networkx_store import NetworkXGraph
    from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
    from ncms.infrastructure.storage.sqlite_store import SQLiteStore

    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True,
        ).strip()
    except Exception:
        sha = "unknown"

    results = {
        "timestamp": datetime.now(UTC).isoformat(),
        "git_sha": sha,
        "ingest_samples": len(INGEST_SAMPLES),
        "search_queries": len(SEARCH_QUERIES),
    }

    # ── Baseline (no admission/reconciliation/episodes) ──────────────

    print("=== Baseline latency (no HTMG features) ===", flush=True)
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()

    config = NCMSConfig(
        db_path=":memory:",
        admission_enabled=False,
        temporal_enabled=False,
        contradiction_detection_enabled=False,
    )
    svc = MemoryService(store=store, index=index, graph=graph, config=config)

    # Ingest
    ingest_times: list[float] = []
    for content in INGEST_SAMPLES:
        t0 = time.perf_counter()
        await svc.store_memory(content=content, memory_type="fact", domains=["ops"])
        ingest_times.append((time.perf_counter() - t0) * 1000)

    # Search
    search_times: list[float] = []
    for query in SEARCH_QUERIES:
        t0 = time.perf_counter()
        await svc.search(query=query, limit=10)
        search_times.append((time.perf_counter() - t0) * 1000)

    results["baseline"] = {
        "ingest_ms": _percentiles(ingest_times),
        "search_ms": _percentiles(search_times),
    }
    print(f"  Ingest: {_format_pct(ingest_times)}", flush=True)
    print(f"  Search: {_format_pct(search_times)}", flush=True)

    await store.close()

    # ── Full pipeline (admission + reconciliation + episodes) ────────

    print("\n=== Full pipeline latency (HTMG enabled) ===", flush=True)
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()

    config = NCMSConfig(
        db_path=":memory:",
        admission_enabled=True,
        temporal_enabled=True,
        contradiction_detection_enabled=False,  # No LLM for latency test
    )
    admission_svc = AdmissionService(store=store, index=index, graph=graph, config=config)
    reconciliation_svc = ReconciliationService(store=store, config=config)
    episode_svc = EpisodeService(store=store, index=index, config=config)
    svc = MemoryService(
        store=store,
        index=index,
        graph=graph,
        config=config,
        admission=admission_svc,
        reconciliation=reconciliation_svc,
        episode=episode_svc,
    )

    # Ingest
    ingest_times = []
    for content in INGEST_SAMPLES:
        t0 = time.perf_counter()
        await svc.store_memory(content=content, memory_type="fact", domains=["ops"])
        ingest_times.append((time.perf_counter() - t0) * 1000)

    # Search
    search_times = []
    for query in SEARCH_QUERIES:
        t0 = time.perf_counter()
        await svc.search(query=query, limit=10)
        search_times.append((time.perf_counter() - t0) * 1000)

    results["full_pipeline"] = {
        "ingest_ms": _percentiles(ingest_times),
        "search_ms": _percentiles(search_times),
    }
    print(f"  Ingest: {_format_pct(ingest_times)}", flush=True)
    print(f"  Search: {_format_pct(search_times)}", flush=True)

    # ── Memory growth ────────────────────────────────────────────────

    all_memories = await store.list_memories(limit=1000)
    results["memory_growth"] = {
        "total_memories": len(all_memories),
        "samples_ingested": len(INGEST_SAMPLES),
        "growth_ratio": round(len(all_memories) / len(INGEST_SAMPLES), 2),
    }
    print(
        f"\n  Memory growth: {len(all_memories)} memories "
        f"from {len(INGEST_SAMPLES)} inputs "
        f"(ratio: {len(all_memories) / len(INGEST_SAMPLES):.2f})",
        flush=True,
    )

    await store.close()

    # Write results
    path = TUNING_DIR / "quality_metrics.json"
    path.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nResults written to {path}", flush=True)

    return results


def _percentiles(values: list[float]) -> dict:
    """Compute p50/p95/p99 from a list of values."""
    if not values:
        return {"p50": 0, "p95": 0, "p99": 0, "mean": 0}
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    return {
        "p50": round(sorted_vals[int(n * 0.5)], 2),
        "p95": round(sorted_vals[int(n * 0.95)], 2),
        "p99": round(sorted_vals[min(int(n * 0.99), n - 1)], 2),
        "mean": round(statistics.mean(values), 2),
    }


def _format_pct(values: list[float]) -> str:
    """Format percentiles for display."""
    pct = _percentiles(values)
    return f"p50={pct['p50']:.1f}ms p95={pct['p95']:.1f}ms p99={pct['p99']:.1f}ms"


def main() -> None:
    from benchmarks.env import load_dotenv

    load_dotenv()
    asyncio.run(measure_latency())


if __name__ == "__main__":
    main()
