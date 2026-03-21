"""Prometheus metrics for NCMS.

Provides a /metrics endpoint compatible with Prometheus scraping.
Tracks retrieval latency, memory count, entity count, bus activity,
and pipeline throughput.

Setup:
    pip install ncms[otel]  # prometheus-client included

Usage in dashboard/HTTP server:
    from ncms.infrastructure.observability.metrics import (
        MetricsCollector, metrics_endpoint,
    )

    collector = MetricsCollector()
    collector.observe_search_latency(0.045)  # 45ms search
    collector.inc_memories_stored()

    # Starlette endpoint
    Route("/metrics", metrics_endpoint)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

# Dynamic import — prometheus_client is optional
try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _HAS_PROMETHEUS = True
except ImportError:
    _HAS_PROMETHEUS = False


class NullMetric:
    """No-op metric for when prometheus_client is not installed."""

    def inc(self, amount: float = 1) -> None:
        pass

    def dec(self, amount: float = 1) -> None:
        pass

    def set(self, value: float) -> None:
        pass

    def observe(self, value: float) -> None:
        pass

    def labels(self, **kwargs: str) -> NullMetric:
        return self


class MetricsCollector:
    """Prometheus metrics for NCMS operations.

    All methods are safe to call even when prometheus_client is not installed —
    they silently no-op via NullMetric.
    """

    def __init__(self) -> None:
        if _HAS_PROMETHEUS:
            # Retrieval pipeline
            self.search_latency = Histogram(
                "ncms_search_latency_seconds",
                "Search query latency",
                ["intent"],
                buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
            )
            self.recall_latency = Histogram(
                "ncms_recall_latency_seconds",
                "Structured recall latency",
                buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
            )
            self.search_result_count = Histogram(
                "ncms_search_result_count",
                "Number of results returned per search",
                buckets=[0, 1, 5, 10, 20, 50],
            )

            # Store operations
            self.memories_stored = Counter(
                "ncms_memories_stored_total",
                "Total memories stored",
                ["type", "domain"],
            )
            self.memories_deleted = Counter(
                "ncms_memories_deleted_total",
                "Total memories deleted",
            )

            # Current state gauges
            self.memory_count = Gauge(
                "ncms_memory_count",
                "Current number of memories in store",
            )
            self.entity_count = Gauge(
                "ncms_entity_count",
                "Current number of entities in graph",
            )
            self.relationship_count = Gauge(
                "ncms_relationship_count",
                "Current number of relationships in graph",
            )
            self.agent_count = Gauge(
                "ncms_agent_count",
                "Current number of registered agents",
                ["status"],
            )

            # Knowledge Bus
            self.bus_asks = Counter(
                "ncms_bus_asks_total",
                "Total Knowledge Bus ask requests",
            )
            self.bus_announces = Counter(
                "ncms_bus_announces_total",
                "Total Knowledge Bus announcements",
            )
            self.bus_surrogates = Counter(
                "ncms_bus_surrogates_total",
                "Total surrogate responses",
            )

            # Admission
            self.admission_decisions = Counter(
                "ncms_admission_decisions_total",
                "Admission scoring decisions",
                ["route"],  # persist, ephemeral_cache, discard
            )

            # Pipeline stages
            self.pipeline_stage_latency = Histogram(
                "ncms_pipeline_stage_seconds",
                "Pipeline stage latency",
                ["pipeline_type", "stage"],
                buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5],
            )

            # Filesystem watcher
            self.watch_files_ingested = Counter(
                "ncms_watch_files_ingested_total",
                "Files ingested by filesystem watcher",
            )
            self.watch_files_skipped = Counter(
                "ncms_watch_files_skipped_total",
                "Files skipped by filesystem watcher",
                ["reason"],  # hash_match, unsupported, error
            )
        else:
            # All metrics are NullMetric when prometheus_client is not available
            null = NullMetric()
            self.search_latency = null  # type: ignore[assignment]
            self.recall_latency = null  # type: ignore[assignment]
            self.search_result_count = null  # type: ignore[assignment]
            self.memories_stored = null  # type: ignore[assignment]
            self.memories_deleted = null  # type: ignore[assignment]
            self.memory_count = null  # type: ignore[assignment]
            self.entity_count = null  # type: ignore[assignment]
            self.relationship_count = null  # type: ignore[assignment]
            self.agent_count = null  # type: ignore[assignment]
            self.bus_asks = null  # type: ignore[assignment]
            self.bus_announces = null  # type: ignore[assignment]
            self.bus_surrogates = null  # type: ignore[assignment]
            self.admission_decisions = null  # type: ignore[assignment]
            self.pipeline_stage_latency = null  # type: ignore[assignment]
            self.watch_files_ingested = null  # type: ignore[assignment]
            self.watch_files_skipped = null  # type: ignore[assignment]

    # ── Convenience Methods ──────────────────────────────────────────────

    def observe_search_latency(self, seconds: float, intent: str = "unknown") -> None:
        """Record search query latency."""
        self.search_latency.labels(intent=intent).observe(seconds)

    def observe_recall_latency(self, seconds: float) -> None:
        """Record structured recall latency."""
        self.recall_latency.observe(seconds)

    def inc_memories_stored(self, memory_type: str = "fact", domain: str = "general") -> None:
        """Increment stored memory counter."""
        self.memories_stored.labels(type=memory_type, domain=domain).inc()

    def inc_bus_ask(self) -> None:
        self.bus_asks.inc()

    def inc_bus_announce(self) -> None:
        self.bus_announces.inc()

    def inc_bus_surrogate(self) -> None:
        self.bus_surrogates.inc()

    def set_admission_decision(self, route: str) -> None:
        self.admission_decisions.labels(route=route).inc()

    def update_gauges(
        self,
        memories: int,
        entities: int,
        relationships: int,
        agents_online: int = 0,
        agents_sleeping: int = 0,
    ) -> None:
        """Bulk update gauge metrics."""
        self.memory_count.set(memories)
        self.entity_count.set(entities)
        self.relationship_count.set(relationships)
        self.agent_count.labels(status="online").set(agents_online)
        self.agent_count.labels(status="sleeping").set(agents_sleeping)

    @contextmanager
    def time_pipeline_stage(
        self, pipeline_type: str, stage: str,
    ) -> Generator[None, None, None]:
        """Context manager to time a pipeline stage."""
        start = time.perf_counter()
        yield
        elapsed = time.perf_counter() - start
        self.pipeline_stage_latency.labels(
            pipeline_type=pipeline_type, stage=stage,
        ).observe(elapsed)


def generate_metrics_text() -> str:
    """Generate Prometheus text format metrics output."""
    if not _HAS_PROMETHEUS:
        return "# prometheus_client not installed\n"
    return generate_latest().decode("utf-8")


async def metrics_endpoint(request: Any) -> Any:
    """Starlette endpoint handler for /metrics."""
    from starlette.responses import Response

    return Response(
        content=generate_metrics_text(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
