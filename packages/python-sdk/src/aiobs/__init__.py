"""AI Observability Platform -- Python SDK.

Quick start::

    import aiobs

    client = aiobs.init(service_name="support-bot")

    with client.trace("customer-support-request", subject_id="user-42") as trace:
        with trace.span("retrieve-context", kind="client", category="retrieval") as span:
            span.record_retrieval(query=question, documents=hits, retriever_name="pgvector")

        with trace.span("generate", category="chat_completion") as span:
            span.record_model(provider="anthropic", model="claude-sonnet-4")
            span.set_input(prompt)
            answer = call_the_model(prompt)
            span.set_output(answer)
            span.record_usage(input_tokens=1_200, output_tokens=340)

Configuration comes from ``AIOBS_ENDPOINT`` and ``AIOBS_API_KEY`` by default.
Without an API key the SDK still builds spans -- which is what the test
utilities inspect -- but sends nothing, so instrumented code is safe to run
anywhere.

The SDK never raises into your application. Every public method is wrapped so
that an SDK bug produces a log line rather than a failed request.
"""

from __future__ import annotations

from . import semconv
from .config import Config, from_env
from .context import (
    SpanContext,
    extract,
    get_current_context,
    inject,
    use_context,
)
from .exporter import BatchExporter, ExportResult, MemoryTransport, Transport
from .redaction import Redactor
from .testing import TestClient, capture_spans
from .tracer import Client, Span, Trace, get_client, init, shutdown

__version__ = "0.1.0"

__all__ = [
    "BatchExporter",
    "Client",
    "Config",
    "ExportResult",
    "MemoryTransport",
    "Redactor",
    "Span",
    "SpanContext",
    "TestClient",
    "Trace",
    "Transport",
    "__version__",
    "capture_spans",
    "extract",
    "from_env",
    "get_client",
    "get_current_context",
    "info",
    "init",
    "inject",
    "semconv",
    "shutdown",
    "span",
    "trace",
    "use_context",
]


def trace(name: str, **kwargs: object) -> Trace:
    """Start a trace on the process-wide client."""
    return get_client().trace(name, **kwargs)  # type: ignore[arg-type]


def span(name: str, **kwargs: object) -> Span:
    """Start a span on the process-wide client."""
    return get_client().span(name, **kwargs)  # type: ignore[arg-type]


def flush(timeout: float | None = None) -> bool:
    """Flush the process-wide client's buffer."""
    return get_client().flush(timeout)


def info() -> dict[str, object]:
    """Describe the active configuration, for a health endpoint or a bug report.

    The API key is never included, only whether one is present.
    """
    client = get_client()
    config = client.config
    return {
        "sdk": "aiobs-python",
        "version": __version__,
        "endpoint": config.endpoint,
        "authenticated": bool(config.api_key),
        "service_name": config.service_name,
        "environment": config.environment,
        "enabled": config.enabled,
        "sample_rate": config.sample_rate,
        "capture_payloads": config.capture_payloads,
        "exporter": client.stats(),
    }
