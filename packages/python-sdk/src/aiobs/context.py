"""W3C Trace Context propagation.

Implements the ``traceparent``/``tracestate``/``baggage`` headers directly
rather than depending on the OpenTelemetry SDK. The reason is dependency
weight: an application that wants to trace its AI calls should not be forced to
adopt a whole telemetry framework, and the header formats are small, frozen
specifications.

Applications that *do* run OpenTelemetry are still first-class -- the SDK reads
an active OTel context when one is present (see :func:`current_otel_context`),
so spans nest correctly under existing instrumentation instead of starting a
detached trace.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from urllib.parse import quote, unquote

from .ids import generate_span_id, generate_trace_id, is_valid_span_id, is_valid_trace_id

__all__ = [
    "TRACEPARENT_HEADER",
    "TRACESTATE_HEADER",
    "SpanContext",
    "extract",
    "get_current_context",
    "inject",
    "set_current_context",
    "use_context",
]

TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"
BAGGAGE_HEADER = "baggage"

#: version-traceid-spanid-flags, all lowercase hex.
_TRACEPARENT_RE = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")

#: Sampled bit of the trace-flags field.
FLAG_SAMPLED = 0x01

#: The specification caps tracestate at 32 entries and 512 bytes per entry.
_MAX_TRACESTATE_ENTRIES = 32
_MAX_BAGGAGE_BYTES = 8192


@dataclass(frozen=True, slots=True)
class SpanContext:
    """The identity of a span, as propagated across process boundaries."""

    trace_id: str
    span_id: str
    #: Bitfield; only the sampled bit is defined by the specification today.
    trace_flags: int = FLAG_SAMPLED
    #: Vendor-specific state, preserved verbatim when we are not the vendor.
    trace_state: str = ""
    #: Application-defined key/value pairs propagated alongside the trace.
    baggage: dict[str, str] = field(default_factory=dict)
    #: True when this context came from an inbound header rather than being
    #: created locally. Remote parents are never re-sampled: the upstream
    #: sampling decision is authoritative, or a trace would be half-recorded.
    remote: bool = False

    @property
    def sampled(self) -> bool:
        return bool(self.trace_flags & FLAG_SAMPLED)

    @classmethod
    def new_root(cls, *, sampled: bool = True) -> SpanContext:
        return cls(
            trace_id=generate_trace_id(),
            span_id=generate_span_id(),
            trace_flags=FLAG_SAMPLED if sampled else 0x00,
        )

    def child(self) -> SpanContext:
        """A new span in the same trace, inheriting the sampling decision."""
        return SpanContext(
            trace_id=self.trace_id,
            span_id=generate_span_id(),
            trace_flags=self.trace_flags,
            trace_state=self.trace_state,
            baggage=dict(self.baggage),
        )

    def to_traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags:02x}"

    def with_baggage(self, **items: str) -> SpanContext:
        return replace(self, baggage={**self.baggage, **items})


_current: ContextVar[SpanContext | None] = ContextVar("aiobs_span_context", default=None)


def get_current_context() -> SpanContext | None:
    """The span context active in this task, if any."""
    return _current.get()


def set_current_context(context: SpanContext | None) -> Token[SpanContext | None]:
    return _current.set(context)


def reset_current_context(token: Token[SpanContext | None]) -> None:
    _current.reset(token)


class use_context:
    """Scope a span context to a block or a coroutine.

    Usable as both a context manager and a decorator, because propagating
    context across a manually-created task is a common need and
    ``asyncio.create_task`` copies the context at creation time.
    """

    __slots__ = ("_context", "_token")

    def __init__(self, context: SpanContext | None) -> None:
        self._context = context
        self._token: Token[SpanContext | None] | None = None

    def __enter__(self) -> SpanContext | None:
        self._token = _current.set(self._context)
        return self._context

    def __exit__(self, *_: object) -> None:
        if self._token is not None:
            _current.reset(self._token)
            self._token = None


def parse_traceparent(value: str | None) -> SpanContext | None:
    """Parse a ``traceparent`` header, returning ``None`` when unusable.

    Per the specification, an unparseable or future-version header must be
    ignored and a new trace started, rather than treated as an error -- a
    misbehaving upstream must not break the downstream service.
    """
    if not value:
        return None
    match = _TRACEPARENT_RE.match(value.strip().lower())
    if match is None:
        return None
    version, trace_id, span_id, flags = match.groups()
    if version == "ff":  # reserved as invalid
        return None
    if not is_valid_trace_id(trace_id) or not is_valid_span_id(span_id):
        return None
    return SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        trace_flags=int(flags, 16),
        remote=True,
    )


def parse_baggage(value: str | None) -> dict[str, str]:
    """Parse a W3C ``baggage`` header into a dictionary."""
    if not value or len(value.encode("utf-8")) > _MAX_BAGGAGE_BYTES:
        return {}
    result: dict[str, str] = {}
    for member in value.split(","):
        member = member.strip()
        if not member or "=" not in member:
            continue
        # Properties after ';' are permitted by the spec; we preserve only the
        # value, which is what applications actually read.
        key, _, raw = member.partition("=")
        cleaned = raw.split(";", 1)[0]
        try:
            result[unquote(key.strip())] = unquote(cleaned.strip())
        except Exception:
            continue
    return result


def format_baggage(items: Mapping[str, str]) -> str:
    encoded = [
        f"{quote(str(key), safe='')}={quote(str(value), safe='')}" for key, value in items.items()
    ]
    joined = ",".join(encoded)
    # Truncate rather than send an over-long header a proxy might reject.
    while len(joined.encode("utf-8")) > _MAX_BAGGAGE_BYTES and encoded:
        encoded.pop()
        joined = ",".join(encoded)
    return joined


def extract(headers: Mapping[str, str]) -> SpanContext | None:
    """Build a :class:`SpanContext` from inbound HTTP or message headers."""
    lowered = {str(key).lower(): value for key, value in headers.items()}
    context = parse_traceparent(lowered.get(TRACEPARENT_HEADER))
    if context is None:
        return None
    state = (lowered.get(TRACESTATE_HEADER) or "").strip()
    if state.count(",") + 1 > _MAX_TRACESTATE_ENTRIES:
        state = ",".join(state.split(",")[:_MAX_TRACESTATE_ENTRIES])
    baggage = parse_baggage(lowered.get(BAGGAGE_HEADER))
    return replace(context, trace_state=state, baggage=baggage)


def inject(
    context: SpanContext | None = None, carrier: dict[str, str] | None = None
) -> dict[str, str]:
    """Write the current (or given) context into a header carrier."""
    resolved = context or get_current_context()
    target = carrier if carrier is not None else {}
    if resolved is None:
        return target
    target[TRACEPARENT_HEADER] = resolved.to_traceparent()
    if resolved.trace_state:
        target[TRACESTATE_HEADER] = resolved.trace_state
    if resolved.baggage:
        target[BAGGAGE_HEADER] = format_baggage(resolved.baggage)
    return target


def current_otel_context() -> SpanContext | None:
    """Adopt an active OpenTelemetry span context, when one exists.

    Lets the SDK nest under existing instrumentation instead of starting a
    detached trace. Imported lazily and failing silently, so OpenTelemetry
    remains entirely optional.
    """
    try:
        from opentelemetry import trace as otel_trace
    except ImportError:
        return None
    try:
        span = otel_trace.get_current_span()
        span_context = span.get_span_context()
        if not span_context.is_valid:
            return None
        return SpanContext(
            trace_id=format(span_context.trace_id, "032x"),
            span_id=format(span_context.span_id, "016x"),
            trace_flags=int(span_context.trace_flags),
            remote=bool(span_context.is_remote),
        )
    except Exception:
        return None


def resolve_parent() -> SpanContext | None:
    """The parent for a new span: our own context first, then OpenTelemetry's."""
    return get_current_context() or current_otel_context()


def iter_headers(context: SpanContext | None = None) -> Iterator[tuple[str, str]]:
    """Yield propagation headers as pairs, for clients that want tuples."""
    yield from inject(context).items()
