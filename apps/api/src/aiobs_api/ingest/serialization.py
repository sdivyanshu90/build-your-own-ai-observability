"""Message encoding for the ingestion bus.

Rows cross the API-to-worker boundary as JSON. Two types need explicit
handling because JSON has no representation for them:

``Decimal``
    Encoded as a *string*, never a float. A cost that round-tripped through a
    JSON number would silently lose precision between the API and the worker --
    the one place where it must not.

``datetime``
    Encoded as epoch milliseconds. Unambiguous, compact and immune to the
    timezone-suffix parsing differences between languages.

The envelope carries an explicit ``schema_version`` so a consumer running older
code recognises a message it cannot parse and dead-letters it rather than
silently mis-reading fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, TypeVar

from ..storage.analytics.rows import (
    AgentStepRow,
    RetrievalDocumentRow,
    SpanEventRow,
    SpanRow,
)
from .normalizer import NormalizedSpan

__all__ = [
    "MESSAGE_SCHEMA_VERSION",
    "SpanMessage",
    "decode_span_message",
    "encode_span_message",
]

#: Bumped only when the meaning of an existing field changes. Additive changes
#: are backwards compatible because unknown keys are ignored on decode.
MESSAGE_SCHEMA_VERSION = "1.0"

T = TypeVar("T")


def _encode_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"__decimal__": format(value, "f")}
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return {"__datetime_ms__": int(aware.timestamp() * 1000)}
    if isinstance(value, dict):
        return {key: _encode_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_value(item) for item in value]
    return value


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict):
        if "__decimal__" in value:
            return Decimal(str(value["__decimal__"]))
        if "__datetime_ms__" in value:
            return datetime.fromtimestamp(int(value["__datetime_ms__"]) / 1000.0, tz=timezone.utc)
        return {key: _decode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_value(item) for item in value]
    return value


def _dataclass_to_dict(instance: Any) -> dict[str, Any]:
    return {
        field.name: _encode_value(getattr(instance, field.name))
        for field in dataclass_fields(instance)
    }


def _dict_to_dataclass(payload: Mapping[str, Any], row_type: type[T]) -> T:
    known = {field.name for field in dataclass_fields(row_type)}  # type: ignore[arg-type]
    # Unknown keys are dropped rather than raising: during a rolling deploy the
    # producer may be one version ahead of the consumer, and refusing the
    # message would dead-letter perfectly good telemetry.
    kwargs = {key: _decode_value(value) for key, value in payload.items() if key in known}
    return row_type(**kwargs)  # type: ignore[call-arg]


class SpanMessage:
    """Envelope carrying one normalised span and its derived rows."""

    __slots__ = ()

    @staticmethod
    def encode(normalized: NormalizedSpan) -> dict[str, Any]:
        return {
            "schema_version": MESSAGE_SCHEMA_VERSION,
            "span": _dataclass_to_dict(normalized.span),
            "events": [_dataclass_to_dict(row) for row in normalized.events],
            "retrieval_documents": [
                _dataclass_to_dict(row) for row in normalized.retrieval_documents
            ],
            "agent_steps": [_dataclass_to_dict(row) for row in normalized.agent_steps],
            "usage": normalized.usage.as_dict(),
        }

    @staticmethod
    def decode(payload: Mapping[str, Any]) -> NormalizedSpan:
        version = str(payload.get("schema_version", "0"))
        major = version.split(".", 1)[0]
        if major != MESSAGE_SCHEMA_VERSION.split(".", 1)[0]:
            raise ValueError(
                f"unsupported span message schema version {version!r}; "
                f"this consumer speaks {MESSAGE_SCHEMA_VERSION}"
            )
        span = _dict_to_dataclass(payload["span"], SpanRow)
        return NormalizedSpan(
            span=span,
            events=[_dict_to_dataclass(row, SpanEventRow) for row in payload.get("events", [])],
            retrieval_documents=[
                _dict_to_dataclass(row, RetrievalDocumentRow)
                for row in payload.get("retrieval_documents", [])
            ],
            agent_steps=[
                _dict_to_dataclass(row, AgentStepRow) for row in payload.get("agent_steps", [])
            ],
        )


def encode_span_message(normalized: NormalizedSpan) -> dict[str, Any]:
    return SpanMessage.encode(normalized)


def decode_span_message(payload: Mapping[str, Any]) -> NormalizedSpan:
    return SpanMessage.decode(payload)


def encode_rollup_message(
    *, organization_id: str, project_id: str, environment: str, trace_id: str
) -> dict[str, Any]:
    return {
        "schema_version": MESSAGE_SCHEMA_VERSION,
        "organization_id": organization_id,
        "project_id": project_id,
        "environment": environment,
        "trace_id": trace_id,
    }
