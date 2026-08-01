"""Per-request ambient context.

A handful of values -- the request id, the authenticated principal, the
resolved tenant -- are needed almost everywhere: in log lines, in audit
records, in the tenant predicate of every query. Threading them through every
function signature would be noise, and stashing them on a global would break
under concurrency.

:class:`contextvars.ContextVar` gives the right semantics: values are scoped to
the current task, inherited by child tasks, and isolated between concurrent
requests. The middleware sets them once per request and resets them on the way
out.

Reading a value that was never set is a programming error, not a missing
optional, so the accessors distinguish "absent" from "empty" explicitly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "RequestContext",
    "current_context",
    "current_request_id",
    "get_context",
    "set_context",
    "use_context",
]


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Immutable snapshot of who is making the current request, and how."""

    request_id: str
    #: Populated once authentication succeeds; ``None`` on public endpoints.
    principal_id: str | None = None
    principal_type: str | None = None  # 'user' | 'api_key' | 'service_account'
    organization_id: str | None = None
    project_id: str | None = None
    environment_id: str | None = None
    #: W3C traceparent of the *inbound* request, if the caller supplied one.
    trace_id: str | None = None
    span_id: str | None = None
    #: Client-supplied idempotency key for mutating requests.
    idempotency_key: str | None = None
    route: str | None = None
    method: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def with_principal(
        self,
        *,
        principal_id: str,
        principal_type: str,
        organization_id: str,
    ) -> RequestContext:
        """Return a copy with authentication results filled in."""
        return RequestContext(
            request_id=self.request_id,
            principal_id=principal_id,
            principal_type=principal_type,
            organization_id=organization_id,
            project_id=self.project_id,
            environment_id=self.environment_id,
            trace_id=self.trace_id,
            span_id=self.span_id,
            idempotency_key=self.idempotency_key,
            route=self.route,
            method=self.method,
            client_ip=self.client_ip,
            user_agent=self.user_agent,
            extra=dict(self.extra),
        )

    def with_scope(
        self,
        *,
        project_id: str | None = None,
        environment_id: str | None = None,
    ) -> RequestContext:
        """Return a copy narrowed to a project/environment."""
        return RequestContext(
            request_id=self.request_id,
            principal_id=self.principal_id,
            principal_type=self.principal_type,
            organization_id=self.organization_id,
            project_id=project_id or self.project_id,
            environment_id=environment_id or self.environment_id,
            trace_id=self.trace_id,
            span_id=self.span_id,
            idempotency_key=self.idempotency_key,
            route=self.route,
            method=self.method,
            client_ip=self.client_ip,
            user_agent=self.user_agent,
            extra=dict(self.extra),
        )

    def as_log_fields(self) -> dict[str, Any]:
        """Fields merged into every structured log record for this request."""
        fields: dict[str, Any] = {"request_id": self.request_id}
        for name in (
            "principal_id",
            "principal_type",
            "organization_id",
            "project_id",
            "trace_id",
            "route",
            "method",
        ):
            value = getattr(self, name)
            if value is not None:
                fields[name] = value
        return fields


_context: ContextVar[RequestContext | None] = ContextVar("aiobs_request_context", default=None)


def get_context() -> RequestContext | None:
    """Return the current request context, or ``None`` outside a request."""
    return _context.get()


def current_context() -> RequestContext:
    """Return the current request context, raising if there is none.

    Use this in code that structurally cannot run outside a request (audit
    writers, tenant predicates). The exception is loud on purpose: a silent
    ``None`` here would produce an audit record with no actor.
    """
    context = _context.get()
    if context is None:
        raise LookupError(
            "no request context is active; this code path must run inside a "
            "request or an explicit use_context() block"
        )
    return context


def current_request_id() -> str | None:
    """Return the current request id if one is set."""
    context = _context.get()
    return context.request_id if context else None


def set_context(context: RequestContext) -> Token[RequestContext | None]:
    """Install ``context`` and return the token needed to restore the previous one."""
    return _context.set(context)


def reset_context(token: Token[RequestContext | None]) -> None:
    """Restore the context that was active before the matching :func:`set_context`."""
    _context.reset(token)


@contextmanager
def use_context(context: RequestContext) -> Iterator[RequestContext]:
    """Scope ``context`` to a block.

    Used by the worker, which has no HTTP middleware but still wants every log
    line and audit record to carry a correlation id.
    """
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)
