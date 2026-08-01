"""Domain exceptions.

The service layer raises these; a single FastAPI exception handler converts
them into the :class:`aiobs_schemas.errors.ErrorResponse` envelope. Handlers
never build error bodies by hand, so status codes and error codes cannot drift
between endpoints.

Nothing here imports FastAPI: the same exceptions are raised by the worker,
where there is no HTTP layer at all.
"""

from __future__ import annotations

from typing import Any

from aiobs_schemas.errors import STATUS_FOR_CODE, ErrorCode, ErrorDetail

__all__ = [
    "AiobsError",
    "AuthenticationError",
    "ConflictError",
    "DependencyUnavailableError",
    "GoneError",
    "ImmutableResourceError",
    "NotFoundError",
    "PermissionDeniedError",
    "PreconditionFailedError",
    "QuotaExceededError",
    "RateLimitedError",
    "TenantMismatchError",
    "ValidationFailedError",
]


class AiobsError(Exception):
    """Base class for every error the platform raises deliberately.

    Anything *not* deriving from this is an unexpected bug: the HTTP layer
    reports those as ``internal_error`` with no detail, and logs them with a
    stack trace. That split is what stops an accidental ``KeyError`` from
    leaking an internal identifier into an API response.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        details: list[ErrorDetail] | None = None,
        context: dict[str, Any] | None = None,
        retry_after_seconds: float | None = None,
        documentation_url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details or []
        self.context = context or {}
        self.retry_after_seconds = retry_after_seconds
        self.documentation_url = documentation_url

    @property
    def status_code(self) -> int:
        """HTTP status this error maps to."""
        return STATUS_FOR_CODE[self.code]

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code.value!r}, message={self.message!r})"


class ValidationFailedError(AiobsError):
    code = ErrorCode.VALIDATION_FAILED


class NotFoundError(AiobsError):
    code = ErrorCode.NOT_FOUND

    def __init__(self, resource: str, identifier: str | None = None, **kwargs: Any) -> None:
        # The message deliberately does not distinguish "does not exist" from
        # "exists in another tenant": that distinction is an enumeration oracle.
        message = f"{resource} not found"
        super().__init__(message, context={"resource": resource, "id": identifier}, **kwargs)


class ConflictError(AiobsError):
    code = ErrorCode.CONFLICT


class AlreadyExistsError(AiobsError):
    code = ErrorCode.ALREADY_EXISTS


class ImmutableResourceError(AiobsError):
    """Raised when something tries to mutate a published version.

    Immutability is the whole point of the version registries: if a published
    prompt could change, every trace that references it would silently start
    lying about what actually ran.
    """

    code = ErrorCode.IMMUTABLE_RESOURCE


class PreconditionFailedError(AiobsError):
    code = ErrorCode.PRECONDITION_FAILED


class GoneError(AiobsError):
    """The resource existed but its payload has passed its retention horizon."""

    code = ErrorCode.GONE


class AuthenticationError(AiobsError):
    code = ErrorCode.UNAUTHENTICATED


class PermissionDeniedError(AiobsError):
    code = ErrorCode.PERMISSION_DENIED

    def __init__(
        self,
        permission: str,
        *,
        resource: str | None = None,
        context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        # Merge rather than overwrite: callers add situational detail (the
        # project scope that was checked, for instance) and the base fields must
        # survive alongside it.
        super().__init__(
            f"permission {permission!r} is required",
            context={"permission": permission, "resource": resource, **(context or {})},
            **kwargs,
        )


class TenantMismatchError(AiobsError):
    """A request tried to reach across an organisation boundary.

    This is always logged at WARNING with the principal and both tenant ids: it
    is either a serious application bug or an attack, and both deserve an alert.
    """

    code = ErrorCode.TENANT_MISMATCH


class RateLimitedError(AiobsError):
    code = ErrorCode.RATE_LIMITED

    def __init__(
        self, retry_after_seconds: float, *, limit: int, window: str, **kwargs: Any
    ) -> None:
        super().__init__(
            f"rate limit exceeded: {limit} requests per {window}",
            retry_after_seconds=retry_after_seconds,
            context={"limit": limit, "window": window},
            **kwargs,
        )


class QuotaExceededError(AiobsError):
    code = ErrorCode.QUOTA_EXCEEDED


class DependencyUnavailableError(AiobsError):
    """A required backing service is unreachable or has tripped a circuit breaker."""

    code = ErrorCode.DEPENDENCY_UNAVAILABLE

    def __init__(self, dependency: str, *, cause: str | None = None, **kwargs: Any) -> None:
        super().__init__(
            f"dependency {dependency!r} is unavailable",
            context={"dependency": dependency, "cause": cause},
            retry_after_seconds=kwargs.pop("retry_after_seconds", 5.0),
            **kwargs,
        )


class TimeoutError_(AiobsError):
    code = ErrorCode.TIMEOUT


class IdempotencyConflictError(AiobsError):
    """The same idempotency key was replayed with a different request body."""

    code = ErrorCode.IDEMPOTENCY_KEY_REUSED


class IdempotencyInProgressError(AiobsError):
    """The original request carrying this idempotency key is still running."""

    code = ErrorCode.IDEMPOTENCY_IN_PROGRESS
