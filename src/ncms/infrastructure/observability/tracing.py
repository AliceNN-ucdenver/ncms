"""OpenTelemetry tracing integration for NCMS.

Provides automatic tracing for retrieval pipelines, store/search operations,
and Knowledge Bus activity. Exports spans via OTLP to any compatible collector
(Jaeger, Honeycomb, Grafana Tempo, etc.).

Setup:
    pip install ncms[otel]

    # Environment variables:
    NCMS_OTEL_ENABLED=true
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
    OTEL_SERVICE_NAME=ncms

Usage:
    tracer = setup_tracing()
    with tracer.start_as_current_span("my.operation") as span:
        span.set_attribute("ncms.query", query)
        # ... do work
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

# Dynamic import — opentelemetry is optional
try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False

# Module-level tracer (initialized by setup_tracing or NullTracer)
_tracer: Any = None


class NullSpan:
    """No-op span for when OTEL is disabled."""

    def set_attribute(self, key: str, value: object) -> None:
        pass

    def set_status(self, *args: object, **kwargs: object) -> None:
        pass

    def record_exception(self, exc: BaseException) -> None:
        pass

    def __enter__(self) -> NullSpan:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class NullTracer:
    """No-op tracer that returns NullSpans."""

    def start_as_current_span(
        self,
        name: str,
        **kwargs: object,
    ) -> NullSpan:
        return NullSpan()

    @contextmanager
    def start_span(self, name: str, **kwargs: object) -> Generator[NullSpan, None, None]:
        yield NullSpan()


def setup_tracing(
    service_name: str = "ncms",
    endpoint: str | None = None,
    protocol: str = "http/protobuf",
) -> Any:
    """Initialize OpenTelemetry tracing.

    Args:
        service_name: Service name for traces.
        endpoint: OTLP collector endpoint. If None, uses OTEL_EXPORTER_OTLP_ENDPOINT env var.
        protocol: OTLP protocol ("http/protobuf" or "grpc").

    Returns:
        A tracer instance (real OTEL tracer or NullTracer if OTEL unavailable).
    """
    global _tracer

    if not _HAS_OTEL:
        logger.info("OpenTelemetry not installed — tracing disabled")
        _tracer = NullTracer()
        return _tracer

    try:
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": "0.1.0",
            }
        )

        provider = TracerProvider(resource=resource)

        # Configure exporter based on protocol
        if protocol == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

        exporter_kwargs: dict[str, str] = {}
        if endpoint:
            exporter_kwargs["endpoint"] = endpoint

        exporter = OTLPSpanExporter(**exporter_kwargs)
        provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("ncms", "0.1.0")

        logger.info(
            "OpenTelemetry tracing enabled (service=%s, protocol=%s)",
            service_name,
            protocol,
        )
        return _tracer

    except Exception:
        logger.warning("Failed to initialize OpenTelemetry — using null tracer", exc_info=True)
        _tracer = NullTracer()
        return _tracer


def get_tracer() -> Any:
    """Get the module-level tracer (initializes NullTracer if not set up)."""
    global _tracer
    if _tracer is None:
        _tracer = NullTracer()
    return _tracer


# ── Convenience decorators ────────────────────────────────────────────────


def traced(operation_name: str | None = None) -> Any:
    """Decorator that wraps an async function with a trace span.

    Usage:
        @traced("ncms.search")
        async def search(query: str) -> list:
            ...
    """
    import functools

    def decorator(func: Any) -> Any:
        name = operation_name or f"ncms.{func.__qualname__}"

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer()
            with tracer.start_as_current_span(name) as span:
                try:
                    result = await func(*args, **kwargs)
                    return result
                except Exception as exc:
                    if hasattr(span, "record_exception"):
                        span.record_exception(exc)
                    raise

        return wrapper

    return decorator
