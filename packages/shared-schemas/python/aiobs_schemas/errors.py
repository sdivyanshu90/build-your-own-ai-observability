"""The platform's typed error envelope.

Every non-2xx response from the API has exactly this shape. Clients therefore
never have to parse prose, and the SDKs can decide whether to retry from the
machine-readable ``code`` alone rather than from the HTTP status, which is too
coarse (a 429 from a per-tenant quota needs different handling to a 429 from a
burst limiter).

The envelope is intentionally small and stable; adding a field is a minor
version bump, changing the meaning of ``code`` is a major one.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["STATUS_FOR_CODE", "ErrorCode", "ErrorDetail", "ErrorResponse"]


class ErrorCode(str, Enum):
    """Stable, machine-readable error identifiers."""

    # --- request shape -----------------------------------------------------
    VALIDATION_FAILED = "validation_failed"
    MALFORMED_REQUEST = "malformed_request"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    INVALID_FILTER = "invalid_filter"
    INVALID_SORT = "invalid_sort"
    INVALID_CURSOR = "invalid_cursor"

    # --- authentication / authorisation ------------------------------------
    UNAUTHENTICATED = "unauthenticated"
    INVALID_CREDENTIALS = "invalid_credentials"
    API_KEY_EXPIRED = "api_key_expired"
    API_KEY_REVOKED = "api_key_revoked"
    TOKEN_EXPIRED = "token_expired"
    PERMISSION_DENIED = "permission_denied"
    TENANT_MISMATCH = "tenant_mismatch"

    # --- resources ---------------------------------------------------------
    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    CONFLICT = "conflict"
    IMMUTABLE_RESOURCE = "immutable_resource"
    PRECONDITION_FAILED = "precondition_failed"
    GONE = "gone"

    # --- quotas / limits ---------------------------------------------------
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"

    # --- idempotency -------------------------------------------------------
    IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
    IDEMPOTENCY_IN_PROGRESS = "idempotency_in_progress"

    # --- server ------------------------------------------------------------
    INTERNAL_ERROR = "internal_error"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    TIMEOUT = "timeout"
    NOT_IMPLEMENTED = "not_implemented"


#: Canonical HTTP status for each error code. Kept beside the codes so the API
#: and the contract tests cannot drift apart.
STATUS_FOR_CODE: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.MALFORMED_REQUEST: 400,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: 415,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.INVALID_FILTER: 400,
    ErrorCode.INVALID_SORT: 400,
    ErrorCode.INVALID_CURSOR: 400,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.INVALID_CREDENTIALS: 401,
    ErrorCode.API_KEY_EXPIRED: 401,
    ErrorCode.API_KEY_REVOKED: 401,
    ErrorCode.TOKEN_EXPIRED: 401,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.TENANT_MISMATCH: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.ALREADY_EXISTS: 409,
    ErrorCode.CONFLICT: 409,
    ErrorCode.IMMUTABLE_RESOURCE: 409,
    ErrorCode.PRECONDITION_FAILED: 412,
    ErrorCode.GONE: 410,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.QUOTA_EXCEEDED: 429,
    ErrorCode.IDEMPOTENCY_KEY_REUSED: 409,
    ErrorCode.IDEMPOTENCY_IN_PROGRESS: 409,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.NOT_IMPLEMENTED: 501,
}

#: Codes for which a client SDK should retry with backoff. Anything else is a
#: bug in the caller and retrying only amplifies load.
RETRYABLE_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.RATE_LIMITED,
        ErrorCode.INTERNAL_ERROR,
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        ErrorCode.TIMEOUT,
        ErrorCode.IDEMPOTENCY_IN_PROGRESS,
    }
)


class ErrorDetail(BaseModel):
    """One field-level problem, following the shape of RFC 9457 extensions."""

    model_config = ConfigDict(extra="forbid")

    #: JSON Pointer-ish location, e.g. ``spans.3.start_time_unix_nano``.
    location: str
    message: str
    #: Optional machine-readable sub-code, e.g. ``greater_than_equal``.
    reason: str | None = None


class ErrorResponse(BaseModel):
    """The body of every non-2xx API response."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str = Field(description="Human readable summary; never parse this.")
    #: Correlates the response with server logs and traces.
    request_id: str
    details: list[ErrorDetail] = Field(default_factory=list)
    #: Present on 429/503 responses; seconds the client should wait.
    retry_after_seconds: float | None = None
    #: Free-form, non-sensitive context (resource ids, limits that were hit).
    context: dict[str, Any] = Field(default_factory=dict)
    #: Link to the documentation section describing this failure.
    documentation_url: str | None = None
