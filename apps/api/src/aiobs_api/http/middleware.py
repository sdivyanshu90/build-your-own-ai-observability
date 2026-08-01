"""HTTP middleware.

Ordering matters and is asserted by a test. Outermost first:

1. :class:`RequestContextMiddleware` -- assigns the request id and establishes
   the context every log line and audit record reads. It must be outermost so
   even a rejected request is attributable.
2. :class:`SecurityHeadersMiddleware` -- adds response headers unconditionally,
   including on error responses, where they matter most.
3. :class:`BodySizeLimitMiddleware` -- rejects oversized bodies before any
   handler reads them.
4. :class:`AccessLogMiddleware` -- one structured line per request, with the
   status and duration.

CORS is installed by Starlette's own middleware, inside these.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from aiobs_schemas.errors import ErrorCode

from ..core.config import Settings
from ..core.context import RequestContext, reset_context, set_context
from ..core.logging import get_logger

__all__ = [
    "AccessLogMiddleware",
    "BodySizeLimitMiddleware",
    "RequestContextMiddleware",
    "SecurityHeadersMiddleware",
    "UnhandledErrorMiddleware",
]

log = get_logger(__name__)

_REQUEST_ID_HEADER = "x-request-id"
_TRACEPARENT_HEADER = "traceparent"


def _new_request_id() -> str:
    return f"req_{secrets.token_hex(12)}"


def _parse_traceparent(value: str | None) -> tuple[str | None, str | None]:
    """Extract ``(trace_id, span_id)`` from a W3C traceparent header.

    Accepting the caller's trace context is what lets a request into the
    observability platform itself be correlated with the application trace that
    triggered it -- useful when debugging why an SDK's export is failing.
    """
    if not value:
        return None, None
    parts = value.split("-")
    if len(parts) < 4 or parts[0] != "00":
        return None, None
    trace_id, span_id = parts[1], parts[2]
    if len(trace_id) != 32 or len(span_id) != 16:
        return None, None
    return trace_id, span_id


class UnhandledErrorMiddleware(BaseHTTPMiddleware):
    """Converts an unhandled exception into the standard error envelope.

    FastAPI's ``@app.exception_handler(Exception)`` is installed on Starlette's
    ``ServerErrorMiddleware``, which sits *outside* every application
    middleware -- including CORS. A 500 produced there therefore reaches a
    browser with no ``Access-Control-Allow-Origin`` header, so the fetch fails
    with an opaque "Failed to fetch" and the operator sees a network error
    instead of an error message with a request id.

    Catching here, innermost of the application stack, means the response still
    travels back out through CORS and the security headers, and the envelope is
    identical to every other error the API produces.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            # exc_info is what makes this debuggable; the response carries only
            # the request id, which correlates to this log line.
            log.error("http.unhandled_exception", error=str(exc), exc_info=True)
            request_id = getattr(request.state, "request_id", None)
            return JSONResponse(
                status_code=500,
                content={
                    "code": ErrorCode.INTERNAL_ERROR.value,
                    "message": (
                        "an internal error occurred; quote the request id when reporting it"
                    ),
                    "request_id": request_id,
                    "details": [],
                    "retry_after_seconds": None,
                    "context": {},
                },
            )


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Establishes the per-request context and echoes the request id."""

    def __init__(self, app: ASGIApp, *, trusted_proxy_hops: int = 0) -> None:
        super().__init__(app)
        self._trusted_proxy_hops = trusted_proxy_hops

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(_REQUEST_ID_HEADER)
        # A client-supplied id is echoed for correlation but never trusted as a
        # key: it is bounded and sanitised first.
        request_id = (
            "".join(char for char in incoming if char.isalnum() or char in "-_")[:64]
            if incoming
            else _new_request_id()
        ) or _new_request_id()

        trace_id, span_id = _parse_traceparent(request.headers.get(_TRACEPARENT_HEADER))
        context = RequestContext(
            request_id=request_id,
            trace_id=trace_id,
            span_id=span_id,
            idempotency_key=request.headers.get("idempotency-key"),
            route=request.url.path,
            method=request.method,
            client_ip=self._client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        token = set_context(context)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            reset_context(token)
        response.headers[_REQUEST_ID_HEADER] = request_id
        return response

    def _client_ip(self, request: Request) -> str | None:
        """Resolve the client IP, honouring only the configured proxy depth.

        Taking the leftmost ``X-Forwarded-For`` entry unconditionally lets any
        client spoof its address, which would poison rate limiting and audit
        records. With ``trusted_proxy_hops = n`` the address ``n`` from the
        right is used -- the one the outermost trusted proxy actually observed.
        """
        if self._trusted_proxy_hops > 0:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                candidates = [item.strip() for item in forwarded.split(",") if item.strip()]
                index = len(candidates) - self._trusted_proxy_hops
                if 0 <= index < len(candidates):
                    return candidates[index]
        return request.client.host if request.client else None


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds hardening headers to every response."""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        # response.headers is already a MutableHeaders view over raw_headers;
        # constructing a second one over a copied scope dict mutated the copy,
        # so none of these headers actually reached the client.
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()"
        )
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        # The CSP only matters for the HTML the API serves (its docs page); JSON
        # responses are unaffected, and sending it uniformly is simpler than
        # conditioning on content type.
        headers.setdefault(
            "Content-Security-Policy", self._settings.security.content_security_policy
        )
        if request.url.scheme == "https" and self._settings.security.hsts_max_age_seconds:
            headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={self._settings.security.hsts_max_age_seconds}; includeSubDomains",
            )
        return response


class BodySizeLimitMiddleware:
    """Rejects oversized request bodies.

    Implemented as raw ASGI rather than ``BaseHTTPMiddleware`` so the body is
    inspected as it streams: a ``BaseHTTPMiddleware`` implementation would have
    to buffer the whole request first, which is exactly what the limit exists to
    prevent.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        declared = headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self._max_bytes:
            await self._reject(send, int(declared))
            return

        received = 0
        limit_exceeded = False

        async def guarded_receive():  # type: ignore[no-untyped-def]
            nonlocal received, limit_exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    limit_exceeded = True
                    # Truncate the stream so the handler sees a short body and
                    # fails validation rather than consuming unbounded memory.
                    return {"type": "http.disconnect"}
            return message

        await self._app(scope, guarded_receive, send)

    async def _reject(self, send, size: int) -> None:  # type: ignore[no-untyped-def]
        response = JSONResponse(
            status_code=413,
            content={
                "code": ErrorCode.PAYLOAD_TOO_LARGE.value,
                "message": (
                    f"request body of {size} bytes exceeds the {self._max_bytes} byte limit"
                ),
                "request_id": _new_request_id(),
                "details": [],
                "context": {"limit_bytes": self._max_bytes},
            },
        )
        await response({"type": "http"}, _empty_receive, send)


async def _empty_receive():  # type: ignore[no-untyped-def]
    return {"type": "http.request", "body": b"", "more_body": False}


class AccessLogMiddleware(BaseHTTPMiddleware):
    """One structured log line per request."""

    def __init__(self, app: ASGIApp, *, excluded_paths: tuple[str, ...] = ()) -> None:
        super().__init__(app)
        self._excluded = excluded_paths

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in self._excluded:
            return await call_next(request)

        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            # The query string is omitted deliberately: it can contain a
            # subject id or a search term, and access logs are widely readable.
            log.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                status=status,
                duration_ms=round(duration_ms, 2),
            )
