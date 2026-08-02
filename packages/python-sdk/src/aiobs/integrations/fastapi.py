"""FastAPI / Starlette integration.

Adds one ASGI middleware that:

* continues an inbound distributed trace from ``traceparent`` when present, so
  a request that arrives from another service joins that trace rather than
  starting a new one;
* creates a server span per request;
* records the route *template* (``/users/{id}``) rather than the concrete path,
  because the concrete path is unbounded cardinality and makes every dashboard
  useless;
* returns the trace id in a response header so a user reporting a slow request
  can hand you something you can look up.

Implemented as raw ASGI rather than ``BaseHTTPMiddleware`` so the trace context
is set in the same task that runs the handler; ``BaseHTTPMiddleware`` runs the
downstream app in a separate task and the contextvar can be lost across that
boundary.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any

from ..context import extract, use_context
from ..tracer import Client, get_client

__all__ = ["AiobsMiddleware", "instrument_app"]

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

TRACE_HEADER = "x-aiobs-trace-id"


class AiobsMiddleware:
    """ASGI middleware creating one server span per HTTP request."""

    def __init__(
        self,
        app: Any,
        *,
        client: Client | None = None,
        excluded_paths: Iterable[str] = ("/health", "/live", "/ready", "/metrics"),
        capture_headers: bool = False,
        expose_trace_header: bool = True,
    ) -> None:
        self.app = app
        self._client = client
        self._excluded = frozenset(excluded_paths)
        self._capture_headers = capture_headers
        self._expose_trace_header = expose_trace_header

    @property
    def client(self) -> Client:
        return self._client or get_client()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self._excluded:
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        parent = extract(headers)
        client = self.client
        method = scope.get("method", "GET")
        # The concrete path is used only until the router resolves the template
        # below; the span is renamed before it ends.
        span = client.span(
            f"{method} {scope.get('path', '')}",
            kind="server",
            category="http_request",
            parent=parent,
        )
        span.set_attributes(
            {
                "http.request.method": method,
                "url.path": scope.get("path", ""),
                "url.scheme": scope.get("scheme", "http"),
                "server.address": headers.get("host", ""),
                "user_agent.original": headers.get("user-agent", "")[:512],
            }
        )
        if self._capture_headers:
            span.set_attributes(
                {f"http.request.header.{key}": value for key, value in headers.items()}
            )

        status_code = 500
        with use_context(span.context):

            async def send_wrapper(message: MutableMapping[str, Any]) -> None:
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = int(message.get("status", 500))
                    if self._expose_trace_header:
                        raw = list(message.get("headers", []))
                        raw.append(
                            (TRACE_HEADER.encode("latin-1"), span.trace_id.encode("latin-1"))
                        )
                        message = {**message, "headers": raw}
                await send(message)

            try:
                await self.app(scope, receive, send_wrapper)
            except Exception as exc:
                span.record_exception(exc)
                span.end()
                raise
            finally:
                # The router populates scope["route"] during dispatch, so the
                # template is only available now. Renaming here is what keeps
                # span-name cardinality bounded.
                route = scope.get("route")
                template = getattr(route, "path", None)
                if template:
                    span.name = f"{method} {template}"
                span.set_attribute("http.response.status_code", status_code)
                if status_code >= 500:
                    span.set_status("error", f"server returned {status_code}")
                elif span._status == "unset":
                    span.set_status("ok")
                span.end()


def instrument_app(
    app: Any,
    *,
    client: Client | None = None,
    excluded_paths: Iterable[str] = ("/health", "/live", "/ready", "/metrics"),
    shutdown_on_exit: bool = True,
) -> Any:
    """Add tracing to a FastAPI or Starlette application.

    ``shutdown_on_exit`` registers a shutdown handler that flushes buffered
    spans. Without it, the last few seconds of telemetry are lost on every
    deploy -- which is exactly the window an engineer looks at after a bad one.
    """
    app.add_middleware(AiobsMiddleware, client=client, excluded_paths=excluded_paths)

    if shutdown_on_exit:
        resolved = client or get_client()

        @app.on_event("shutdown")
        async def _flush_on_shutdown() -> None:  # pragma: no cover - lifecycle hook
            resolved.flush(timeout=5.0)

    return app
