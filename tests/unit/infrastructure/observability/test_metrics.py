"""Tests for Prometheus metrics integration."""

from __future__ import annotations

from ncms.infrastructure.observability.metrics import (
    MetricsCollector,
    NullMetric,
    generate_metrics_text,
)


class TestNullMetric:
    """NullMetric should be a no-op drop-in."""

    def test_inc(self) -> None:
        m = NullMetric()
        m.inc()
        m.inc(5)

    def test_dec(self) -> None:
        m = NullMetric()
        m.dec()

    def test_set(self) -> None:
        m = NullMetric()
        m.set(42.0)

    def test_observe(self) -> None:
        m = NullMetric()
        m.observe(0.5)

    def test_labels_returns_self(self) -> None:
        m = NullMetric()
        result = m.labels(intent="fact_lookup")
        assert isinstance(result, NullMetric)


class TestMetricsCollector:
    """MetricsCollector should work regardless of prometheus_client install."""

    def test_observe_search_latency(self) -> None:
        collector = MetricsCollector()
        collector.observe_search_latency(0.045, intent="fact_lookup")

    def test_observe_recall_latency(self) -> None:
        collector = MetricsCollector()
        collector.observe_recall_latency(0.1)

    def test_inc_memories_stored(self) -> None:
        collector = MetricsCollector()
        collector.inc_memories_stored("fact", "api")

    def test_inc_bus_ask(self) -> None:
        collector = MetricsCollector()
        collector.inc_bus_ask()

    def test_inc_bus_announce(self) -> None:
        collector = MetricsCollector()
        collector.inc_bus_announce()

    def test_inc_bus_surrogate(self) -> None:
        collector = MetricsCollector()
        collector.inc_bus_surrogate()

    def test_set_admission_decision(self) -> None:
        collector = MetricsCollector()
        collector.set_admission_decision("persist")

    def test_update_gauges(self) -> None:
        collector = MetricsCollector()
        collector.update_gauges(
            memories=100, entities=50, relationships=200,
            agents_online=3, agents_sleeping=1,
        )

    def test_time_pipeline_stage(self) -> None:
        collector = MetricsCollector()
        with collector.time_pipeline_stage("search", "bm25"):
            pass  # simulated work

    def test_watch_counters(self) -> None:
        collector = MetricsCollector()
        collector.watch_files_ingested.inc()
        collector.watch_files_skipped.labels(reason="hash_match").inc()


class TestGenerateMetricsText:
    """generate_metrics_text should always return a string."""

    def test_returns_string(self) -> None:
        text = generate_metrics_text()
        assert isinstance(text, str)
