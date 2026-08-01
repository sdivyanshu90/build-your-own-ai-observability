"""Exception handlers.

Every error response is built here, from :class:`AiobsError` or from a
framework exception, so the envelope is identical across the whole API and no
handler can invent its own shape.

The dividing line that matters: anything deriving from :class:`AiobsError` is a
*deliberate* outcome and its message is safe to return. Anything else is a bug,
and its message could contain an internal identifier, a file path or a fragment
of a query -- so it is logged with a stack trace and reported to the client as
an opaque ``internal_error`` carrying only the request id.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from aiobs_schemas.errors import STATUS_FOR_CODE, ErrorCode, ErrorDetail, ErrorResponse

from ..core.context import current_request_id
from ..core.errors import AiobsError, TenantMismatchError
from ..core.logging import get_logger

__all__ = ["install_exception_handlers"]

log = get_logger(__name__)

_DOCS_BASE = "https://github.com/aiobs/ai-observability-platform/blob/main/docs/api/errors.md"


def _request_id(request: Request | None) -> str:
    """Resolve the request id from the request first, then the context.

    ``request.state`` is authoritative because it is set by the outermost
    middleware and travels with the request object; the contextvar can be lost
    across an ASGI task boundary, and an error response with no correlation id
    is an error report nobody can act on.
    """
    if request is not None:
        candidate = getattr(request.state, "request_id", None)
        if candidate:
            return str(candidate)
    return current_request_id() or "unknown"


def _envelope(
    *,
    code: ErrorCode,
    message: str,
    request: Request | None = None,
    details: list[ErrorDetail] | None = None,
    context: dict[str, Any] | None = None,
    retry_after: float | None = None,
) -> ErrorResponse:
    return ErrorResponse(
        code=code,
        message=message,
        request_id=_request_id(request),
        details=details or [],
        context=context or {},
        retry_after_seconds=retry_after,
        documentation_url=f"{_DOCS_BASE}#{code.value}",
    )


def _response(envelope: ErrorResponse, status_code: int) -> JSONResponse:
    headers: dict[str, str] = {}
    if envelope.retry_after_seconds is not None:
        headers["Retry-After"] = str(max(int(envelope.retry_after_seconds), 1))
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Register handlers for domain, framework and unexpected exceptions."""

    @app.exception_handler(AiobsError)
    async def handle_domain_error(request: Request, exc: AiobsError) -> JSONResponse:
        if isinstance(exc, TenantMismatchError):
            # Cross-tenant access is either a serious bug or an attack. It is
            # logged at WARNING with both organisation ids so it can be alerted
            # on, regardless of how routine the endpoint is.
            log.warning("authz.tenant_mismatch", **exc.context)
        return _response(
            _envelope(
                code=exc.code,
                message=exc.message,
                request=request,
                details=exc.details,
                context=exc.context,
                retry_after=exc.retry_after_seconds,
            ),
            exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            ErrorDetail(
                location=".".join(str(part) for part in error.get("loc", ())[1:]) or "body",
                message=str(error.get("msg", "invalid value")),
                reason=str(error.get("type")) if error.get("type") else None,
            )
            for error in exc.errors()[:50]
        ]
        return _response(
            _envelope(
                code=ErrorCode.VALIDATION_FAILED,
                message="request validation failed",
                request=request,
                details=details,
            ),
            STATUS_FOR_CODE[ErrorCode.VALIDATION_FAILED],
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            400: ErrorCode.MALFORMED_REQUEST,
            401: ErrorCode.UNAUTHENTICATED,
            403: ErrorCode.PERMISSION_DENIED,
            404: ErrorCode.NOT_FOUND,
            405: ErrorCode.MALFORMED_REQUEST,
            409: ErrorCode.CONFLICT,
            413: ErrorCode.PAYLOAD_TOO_LARGE,
            415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            429: ErrorCode.RATE_LIMITED,
        }.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        return _response(
            _envelope(code=code, message=str(exc.detail), request=request),
            exc.status_code,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # exc_info is what makes this debuggable; the response deliberately
        # carries nothing but the request id, which correlates to this log line.
        log.error("http.unhandled_exception", error=str(exc), exc_info=True)
        return _response(
            _envelope(
                code=ErrorCode.INTERNAL_ERROR,
                message="an internal error occurred; quote the request id when reporting it",
                request=request,
            ),
            500,
        )
