"""OTLP decoding.

Accepting OTLP is what makes the platform usable without adopting its SDK: any
existing OpenTelemetry pipeline, vendor instrumentation or Collector can point
at ``/v1/traces`` and the AI-specific views light up from the ``gen_ai.*``
conventions alone.

Both encodings the OTLP/HTTP specification defines are supported:

``application/x-protobuf``
    The default for every official OTel SDK exporter. Decoded with the
    generated ``opentelemetry-proto`` messages.

``application/json``
    The Protobuf-JSON mapping, with the specification's one deviation: trace and
    span ids are **hex strings**, not base64. Some clients follow the generic
    protobuf-JSON rule and send base64 anyway, so both are accepted -- rejecting
    a technically-non-conformant but unambiguous id would break real producers
    for no safety benefit.

Decoding is total: a malformed span is skipped with a recorded reason rather
than failing the request, matching the partial-success semantics OTLP defines.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aiobs_schemas import semconv
from aiobs_schemas.enums import SpanKind, SpanStatus
from aiobs_schemas.wire import (
    AttributeValue,
    ResourceDescriptor,
    SpanEvent,
    SpanLink,
    WireSpan,
)

__all__ = [
    "OtlpDecodeError",
    "OtlpDecodeResult",
    "decode_otlp_json",
    "decode_otlp_protobuf",
    "protobuf_available",
]


class OtlpDecodeError(ValueError):
    """The payload could not be decoded at all (as opposed to one bad span)."""


@dataclass(slots=True)
class OtlpDecodeResult:
    """Decoded resource/span groups plus per-span rejection reasons."""

    groups: list[tuple[ResourceDescriptor, list[WireSpan]]] = field(default_factory=list)
    rejected: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def span_count(self) -> int:
        return sum(len(spans) for _, spans in self.groups)


def _decode_id(value: Any, expected_bytes: int) -> str:
    """Return a lowercase hex id from hex text, base64 text or raw bytes."""
    if value in (None, "", b""):
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    text = str(value).strip()
    expected_hex = expected_bytes * 2
    if len(text) == expected_hex:
        try:
            int(text, 16)
            return text.lower()
        except ValueError:
            pass
    # Fall back to base64, which is what a generic protobuf-JSON encoder emits.
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OtlpDecodeError(f"identifier {text!r} is neither hex nor base64") from exc
    if len(raw) != expected_bytes:
        raise OtlpDecodeError(f"identifier decodes to {len(raw)} bytes, expected {expected_bytes}")
    return raw.hex()


def _any_value(value: Mapping[str, Any] | None) -> AttributeValue:
    """Convert an OTLP ``AnyValue`` JSON object into a plain Python value."""
    if not value:
        return None
    if "stringValue" in value:
        return str(value["stringValue"])
    if "string_value" in value:
        return str(value["string_value"])
    if "boolValue" in value or "bool_value" in value:
        return bool(value.get("boolValue", value.get("bool_value")))
    if "intValue" in value or "int_value" in value:
        # int64 is encoded as a *string* in protobuf-JSON to survive JavaScript's
        # 53-bit number precision.
        raw = value.get("intValue", value.get("int_value"))
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    if "doubleValue" in value or "double_value" in value:
        try:
            return float(value.get("doubleValue", value.get("double_value")))
        except (TypeError, ValueError):
            return None
    if "arrayValue" in value or "array_value" in value:
        container = value.get("arrayValue", value.get("array_value")) or {}
        items = [_any_value(item) for item in container.get("values", [])]
        # Storage columns are homogeneous arrays; a mixed array is stringified
        # rather than dropped so no information is lost.
        if all(isinstance(item, str) for item in items):
            return [item for item in items if isinstance(item, str)]
        if all(isinstance(item, int) and not isinstance(item, bool) for item in items):
            return [item for item in items if isinstance(item, int)]
        if all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in items):
            return [float(item) for item in items if isinstance(item, (int, float))]
        return [str(item) for item in items]
    if "kvlistValue" in value or "kvlist_value" in value:
        import json

        container = value.get("kvlistValue", value.get("kvlist_value")) or {}
        return json.dumps(_attributes(container.get("values", [])), separators=(",", ":"))
    if "bytesValue" in value or "bytes_value" in value:
        return str(value.get("bytesValue", value.get("bytes_value")))
    return None


def _attributes(items: Iterable[Mapping[str, Any]] | None) -> dict[str, AttributeValue]:
    result: dict[str, AttributeValue] = {}
    for item in items or []:
        key = item.get("key")
        if not key:
            continue
        value = _any_value(item.get("value"))
        if value is not None:
            result[str(key)] = value
    return result


def _timestamp(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _resource_from(attributes: Mapping[str, AttributeValue], scope_name: str) -> ResourceDescriptor:
    def text(key: str) -> str | None:
        value = attributes.get(key)
        return str(value) if isinstance(value, (str, int, float)) else None

    return ResourceDescriptor(
        service_name=text(semconv.SERVICE_NAME) or "unknown_service",
        service_version=text(semconv.SERVICE_VERSION),
        service_instance_id=text(semconv.SERVICE_INSTANCE_ID),
        environment=text(semconv.DEPLOYMENT_ENVIRONMENT) or text("deployment.environment"),
        sdk_name=text(semconv.TELEMETRY_SDK_NAME) or scope_name or None,
        sdk_version=text(semconv.TELEMETRY_SDK_VERSION),
        sdk_language=text(semconv.TELEMETRY_SDK_LANGUAGE),
        attributes={
            key: value
            for key, value in attributes.items()
            if key
            not in {
                semconv.SERVICE_NAME,
                semconv.SERVICE_VERSION,
                semconv.SERVICE_INSTANCE_ID,
                semconv.TELEMETRY_SDK_NAME,
                semconv.TELEMETRY_SDK_VERSION,
                semconv.TELEMETRY_SDK_LANGUAGE,
            }
        },
    )


def decode_otlp_json(payload: Mapping[str, Any]) -> OtlpDecodeResult:
    """Decode an OTLP/HTTP JSON ``ExportTraceServiceRequest``."""
    result = OtlpDecodeResult()
    resource_spans = payload.get("resourceSpans") or payload.get("resource_spans")
    if not isinstance(resource_spans, list):
        raise OtlpDecodeError(
            "payload is missing 'resourceSpans'; this does not look like an "
            "OTLP ExportTraceServiceRequest"
        )

    index = -1
    for resource_span in resource_spans:
        if not isinstance(resource_span, Mapping):
            continue
        resource_attributes = _attributes((resource_span.get("resource") or {}).get("attributes"))
        scope_spans = (
            resource_span.get("scopeSpans")
            or resource_span.get("scope_spans")
            or resource_span.get("instrumentationLibrarySpans")
            or []
        )
        spans: list[WireSpan] = []
        scope_name = ""
        for scope_span in scope_spans:
            if not isinstance(scope_span, Mapping):
                continue
            scope = scope_span.get("scope") or scope_span.get("instrumentationLibrary") or {}
            scope_name = str(scope.get("name") or scope_name)
            for raw_span in scope_span.get("spans") or []:
                index += 1
                try:
                    spans.append(_span_from_json(raw_span))
                except (OtlpDecodeError, ValueError) as exc:
                    result.rejected.append((index, "invalid_span", str(exc)))
        if spans:
            result.groups.append((_resource_from(resource_attributes, scope_name), spans))
    return result


def _span_from_json(raw: Mapping[str, Any]) -> WireSpan:
    trace_id = _decode_id(raw.get("traceId") or raw.get("trace_id"), 16)
    span_id = _decode_id(raw.get("spanId") or raw.get("span_id"), 8)
    parent_id = _decode_id(raw.get("parentSpanId") or raw.get("parent_span_id"), 8)
    status = raw.get("status") or {}
    attributes = _attributes(raw.get("attributes"))

    events: list[SpanEvent] = []
    for raw_event in raw.get("events") or []:
        if not isinstance(raw_event, Mapping):
            continue
        events.append(
            SpanEvent(
                name=str(raw_event.get("name") or "event")[:512],
                time_unix_nano=_timestamp(
                    raw_event.get("timeUnixNano") or raw_event.get("time_unix_nano")
                ),
                attributes=_attributes(raw_event.get("attributes")),
            )
        )

    links: list[SpanLink] = []
    for raw_link in raw.get("links") or []:
        if not isinstance(raw_link, Mapping):
            continue
        try:
            links.append(
                SpanLink(
                    trace_id=_decode_id(raw_link.get("traceId") or raw_link.get("trace_id"), 16),
                    span_id=_decode_id(raw_link.get("spanId") or raw_link.get("span_id"), 8),
                    attributes=_attributes(raw_link.get("attributes")),
                )
            )
        except (OtlpDecodeError, ValueError):
            # A malformed link is dropped; it must not invalidate the span.
            continue

    return WireSpan(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_id or None,
        name=str(raw.get("name") or "span")[:512],
        kind=SpanKind.from_otlp(int(raw.get("kind") or 0)),
        start_time_unix_nano=_timestamp(
            raw.get("startTimeUnixNano") or raw.get("start_time_unix_nano")
        ),
        end_time_unix_nano=(
            _timestamp(raw.get("endTimeUnixNano") or raw.get("end_time_unix_nano")) or None
        ),
        status=SpanStatus.from_otlp(int(status.get("code") or 0)),
        status_message=str(status.get("message") or "")[:4096] or None,
        attributes=attributes,
        events=events[:128],
        links=links[:64],
    )


def protobuf_available() -> bool:
    """Whether the OTLP protobuf messages can be imported."""
    try:
        import opentelemetry.proto.trace.v1.trace_pb2  # noqa: F401
    except ImportError:
        return False
    return True


def decode_otlp_protobuf(body: bytes) -> OtlpDecodeResult:
    """Decode a binary OTLP ``ExportTraceServiceRequest``."""
    try:
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise OtlpDecodeError(
            "protobuf OTLP support requires the opentelemetry-proto package"
        ) from exc

    request = ExportTraceServiceRequest()
    try:
        request.ParseFromString(body)
    except Exception as exc:
        raise OtlpDecodeError(f"malformed OTLP protobuf payload: {exc}") from exc

    result = OtlpDecodeResult()
    index = -1
    for resource_span in request.resource_spans:
        resource_attributes = _protobuf_attributes(resource_span.resource.attributes)
        spans: list[WireSpan] = []
        scope_name = ""
        for scope_span in resource_span.scope_spans:
            scope_name = scope_span.scope.name or scope_name
            for proto_span in scope_span.spans:
                index += 1
                try:
                    spans.append(_span_from_protobuf(proto_span))
                except (OtlpDecodeError, ValueError) as exc:
                    result.rejected.append((index, "invalid_span", str(exc)))
        if spans:
            result.groups.append((_resource_from(resource_attributes, scope_name), spans))
    return result


def _protobuf_any_value(value: Any) -> AttributeValue:
    kind = value.WhichOneof("value")
    if kind == "string_value":
        return value.string_value
    if kind == "bool_value":
        return value.bool_value
    if kind == "int_value":
        return int(value.int_value)
    if kind == "double_value":
        return float(value.double_value)
    if kind == "array_value":
        items = [_protobuf_any_value(item) for item in value.array_value.values]
        if all(isinstance(item, str) for item in items):
            return [item for item in items if isinstance(item, str)]
        if all(isinstance(item, int) and not isinstance(item, bool) for item in items):
            return [item for item in items if isinstance(item, int)]
        if all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in items):
            return [float(item) for item in items if isinstance(item, (int, float))]
        return [str(item) for item in items]
    if kind == "kvlist_value":
        import json

        return json.dumps(_protobuf_attributes(value.kvlist_value.values), separators=(",", ":"))
    if kind == "bytes_value":
        return base64.b64encode(value.bytes_value).decode("ascii")
    return None


def _protobuf_attributes(items: Sequence[Any]) -> dict[str, AttributeValue]:
    result: dict[str, AttributeValue] = {}
    for item in items:
        value = _protobuf_any_value(item.value)
        if value is not None:
            result[item.key] = value
    return result


def _span_from_protobuf(proto: Any) -> WireSpan:
    events = [
        SpanEvent(
            name=(event.name or "event")[:512],
            time_unix_nano=int(event.time_unix_nano),
            attributes=_protobuf_attributes(event.attributes),
        )
        for event in proto.events
    ][:128]

    links: list[SpanLink] = []
    for link in proto.links:
        try:
            links.append(
                SpanLink(
                    trace_id=link.trace_id.hex(),
                    span_id=link.span_id.hex(),
                    attributes=_protobuf_attributes(link.attributes),
                )
            )
        except ValueError:
            continue

    return WireSpan(
        trace_id=proto.trace_id.hex(),
        span_id=proto.span_id.hex(),
        parent_span_id=proto.parent_span_id.hex() or None,
        name=(proto.name or "span")[:512],
        kind=SpanKind.from_otlp(int(proto.kind)),
        start_time_unix_nano=int(proto.start_time_unix_nano),
        end_time_unix_nano=int(proto.end_time_unix_nano) or None,
        status=SpanStatus.from_otlp(int(proto.status.code)),
        status_message=(proto.status.message or "")[:4096] or None,
        attributes=_protobuf_attributes(proto.attributes),
        events=events,
        links=links[:64],
    )
