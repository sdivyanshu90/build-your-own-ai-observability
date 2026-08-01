"""FastAPI dependencies.

Authentication, authorisation scoping and pagination parsing live here so that
route handlers stay free of plumbing and, more importantly, so that *forgetting*
the plumbing is impossible: a handler that needs a principal declares it, and a
handler that does not declare one has no way to reach tenant data.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import Depends, Header, Query, Request

from aiobs_schemas.errors import ErrorCode

from ..core.context import get_context, set_context
from ..core.errors import AuthenticationError, ValidationFailedError
from ..core.query import (
    CursorCodec,
    FilterCondition,
    PageRequest,
    ResourceSchema,
    SortTerm,
    parse_filters,
    parse_sort,
)
from ..core.timeutil import parse_rfc3339
from ..domain.principal import Principal
from ..domain.rbac import Permission
from ..services.bundle import ServiceBundle

__all__ = [
    "TimeRange",
    "get_principal",
    "get_services",
    "optional_principal",
    "require_permission",
    "time_range",
]


def get_services(request: Request) -> ServiceBundle:
    """Return the process-wide service bundle."""
    services: ServiceBundle = request.app.state.services
    return services


ServicesDep = Annotated[ServiceBundle, Depends(get_services)]


def _extract_credential(authorization: str | None, api_key_header: str | None) -> str | None:
    """Pull the credential out of ``Authorization`` or ``X-API-Key``.

    Both are supported because OTLP exporters differ: the OpenTelemetry SDKs
    send arbitrary headers via ``OTEL_EXPORTER_OTLP_HEADERS``, and some
    deployments find a dedicated header easier to route than a bearer token.
    """
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value.strip()
        if not value:
            # Some clients send the raw key with no scheme.
            return authorization.strip()
    if api_key_header:
        return api_key_header.strip()
    return None


async def optional_principal(
    request: Request,
    services: ServicesDep,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> Principal | None:
    """Resolve a principal if a credential is present, else ``None``."""
    credential = _extract_credential(authorization, x_api_key)
    if not credential:
        return None
    principal = await services.auth.resolve_credential(credential)
    context = get_context()
    if context is not None:
        set_context(
            context.with_principal(
                principal_id=principal.id,
                principal_type=principal.type.value,
                organization_id=principal.organization_id,
            )
        )
    request.state.principal = principal
    return principal


async def get_principal(
    principal: Annotated[Principal | None, Depends(optional_principal)],
) -> Principal:
    """Require an authenticated principal."""
    if principal is None:
        raise AuthenticationError(
            "authentication required: supply an Authorization bearer token or X-API-Key header",
            code=ErrorCode.UNAUTHENTICATED,
        )
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require_permission(permission: Permission) -> Callable[..., Any]:
    """Build a dependency asserting ``permission``.

    Used on routes whose handler would otherwise have to remember the check.
    Handlers still call ``principal.require(...)`` for resource-specific rules;
    this covers the coarse gate.
    """

    async def dependency(principal: PrincipalDep) -> Principal:
        principal.require(permission)
        return principal

    return dependency


#: Maximum window a single query may span. Longer ranges belong in an export.
MAX_TIME_RANGE = timedelta(days=400)
DEFAULT_TIME_RANGE = timedelta(hours=1)


class TimeRange:
    """A validated ``[start, end)`` window."""

    __slots__ = ("end", "start")

    def __init__(self, start: datetime, end: datetime) -> None:
        if end <= start:
            raise ValidationFailedError("'end' must be after 'start'")
        if (end - start) > MAX_TIME_RANGE:
            raise ValidationFailedError(
                f"time range exceeds the maximum of {MAX_TIME_RANGE.days} days"
            )
        self.start = start
        self.end = end

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


def time_range(
    start: Annotated[
        str | None, Query(description="RFC 3339 start of the window (inclusive)")
    ] = None,
    end: Annotated[str | None, Query(description="RFC 3339 end of the window (exclusive)")] = None,
) -> TimeRange:
    """Parse the ``start``/``end`` query parameters, defaulting to the last hour."""
    now = datetime.now(timezone.utc)
    parsed_end = parse_rfc3339(end) if end else now
    parsed_start = parse_rfc3339(start) if start else parsed_end - DEFAULT_TIME_RANGE
    return TimeRange(parsed_start, parsed_end)


TimeRangeDep = Annotated[TimeRange, Depends(time_range)]


def pagination(
    services: ServicesDep,
    limit: Annotated[int | None, Query(ge=1, le=500, description="Page size")] = None,
    cursor: Annotated[str | None, Query(description="Opaque cursor from a previous page")] = None,
) -> PageRequest:
    """Parse pagination parameters into a validated :class:`PageRequest`."""
    codec: CursorCodec = services.container.cursor_codec
    return PageRequest.build(codec, limit=limit, cursor=cursor)


PageDep = Annotated[PageRequest, Depends(pagination)]


def query_parser(
    schema: ResourceSchema,
) -> Callable[..., tuple[tuple[FilterCondition, ...], tuple[SortTerm, ...]]]:
    """Build a dependency that parses ``filter`` and ``sort`` for one resource."""

    def dependency(
        filter: Annotated[
            list[str] | None,
            Query(description="Repeatable '<field>:<operator>:<value>' filter"),
        ] = None,
        sort: Annotated[
            str | None, Query(description="Comma-separated sort, '-' prefix for descending")
        ] = None,
    ) -> tuple[tuple[FilterCondition, ...], tuple[SortTerm, ...]]:
        return parse_filters(schema, filter or []), parse_sort(schema, sort)

    return dependency


def idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str | None:
    """Read and bound the client's idempotency key."""
    if idempotency_key is None:
        return None
    value = idempotency_key.strip()
    if len(value) > 128:
        raise ValidationFailedError("Idempotency-Key must be at most 128 characters")
    return value or None


IdempotencyKeyDep = Annotated[str | None, Depends(idempotency_key)]
