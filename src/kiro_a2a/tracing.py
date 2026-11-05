"""Optional OpenTelemetry tracing.

Active only when `opentelemetry-api` is installed (pip install
"kiro-a2a-bridge[telemetry]") and an SDK/exporter is configured the usual
OpenTelemetry way (OTEL_* environment variables). Trace context travels in
the `traceparent` header, so a caller's trace continues through the bridge.
"""

from __future__ import annotations

import contextlib

try:
    from opentelemetry import propagate, trace

    _tracer = trace.get_tracer("kiro_a2a")
except ImportError:  # tracing is optional
    propagate = trace = _tracer = None


def enabled() -> bool:
    return _tracer is not None


def extract(headers) -> object | None:
    """Trace context from incoming HTTP headers."""
    return propagate.extract(dict(headers)) if _tracer else None


def inject(headers: dict) -> dict:
    """Add trace context to outgoing HTTP headers."""
    if _tracer:
        propagate.inject(headers)
    return headers


@contextlib.contextmanager
def span(name: str, parent=None, **attributes):
    if not _tracer:
        yield None
        return
    with _tracer.start_as_current_span(name, context=parent, attributes=attributes) as current:
        yield current
