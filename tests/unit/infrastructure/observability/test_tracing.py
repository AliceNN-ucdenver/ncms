"""Tests for OpenTelemetry tracing integration."""

from __future__ import annotations

from ncms.infrastructure.observability.tracing import (
    NullSpan,
    NullTracer,
    get_tracer,
)


class TestNullTracer:
    """NullTracer should be a no-op drop-in for the real tracer."""

    def test_start_as_current_span_returns_null_span(self) -> None:
        tracer = NullTracer()
        span = tracer.start_as_current_span("test.op")
        assert isinstance(span, NullSpan)

    def test_null_span_context_manager(self) -> None:
        tracer = NullTracer()
        span = tracer.start_as_current_span("test.op")
        with span as s:
            assert isinstance(s, NullSpan)

    def test_null_span_set_attribute(self) -> None:
        span = NullSpan()
        # Should not raise
        span.set_attribute("key", "value")
        span.set_attribute("count", 42)

    def test_null_span_record_exception(self) -> None:
        span = NullSpan()
        span.record_exception(ValueError("test"))

    def test_null_span_set_status(self) -> None:
        span = NullSpan()
        span.set_status("OK")

    def test_start_span_context_manager(self) -> None:
        tracer = NullTracer()
        with tracer.start_span("test.op") as span:
            assert isinstance(span, NullSpan)


class TestGetTracer:
    """get_tracer should return a NullTracer when OTEL is not configured."""

    def test_returns_null_tracer_by_default(self) -> None:
        tracer = get_tracer()
        assert isinstance(tracer, NullTracer)
