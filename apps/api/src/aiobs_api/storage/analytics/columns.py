"""Physical column definitions and generic row (de)serialisation.

Each analytics table is described once, as an ordered tuple of
``(column_name, ColumnKind)``. Both drivers derive everything from these
descriptions: the ``CREATE TABLE`` statement, the ``INSERT`` column list, and
the mapping between a result row and its dataclass.

Doing it this way -- rather than hand-writing INSERT statements per driver --
is what keeps the ClickHouse and SQLite schemas provably in step. Adding a
column is a one-line change in one place; forgetting to add it to a driver is
not possible.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, TypeVar

from .rows import (
    AgentStepRow,
    CostRecordRow,
    RetrievalDocumentRow,
    SpanEventRow,
    SpanRow,
    TraceRow,
)

__all__ = [
    "AGENT_STEP_COLUMNS",
    "COST_RECORD_COLUMNS",
    "RETRIEVAL_DOCUMENT_COLUMNS",
    "SPAN_COLUMNS",
    "SPAN_EVENT_COLUMNS",
    "TABLE_COLUMNS",
    "TABLE_ROW_TYPES",
    "TRACE_COLUMNS",
    "ColumnKind",
    "decode_row",
]


class ColumnKind(str, Enum):
    """Logical type of a physical column, driving DDL and codecs."""

    STRING = "string"
    #: 64-bit signed integer, never null (use INT_NULL for optional counts).
    INT = "int"
    INT_NULL = "int_null"
    FLOAT_NULL = "float_null"
    BOOL = "bool"
    #: Exact decimal; stored as NUMERIC on ClickHouse and text on SQLite.
    DECIMAL_NULL = "decimal_null"
    #: Array of strings.
    STRING_ARRAY = "string_array"
    #: String -> string map. Non-string values are JSON-encoded by the caller.
    MAP = "map"
    #: Arbitrary JSON, stored as a string on both drivers.
    JSON = "json"
    #: Millisecond-precision timestamp.
    TIMESTAMP = "timestamp"
    #: Low-cardinality string; a hint that lets ClickHouse dictionary-encode.
    ENUM = "enum"


_K = ColumnKind

#: Columns present on every table: the tenancy predicate.
_SCOPE: tuple[tuple[str, ColumnKind], ...] = (
    ("organization_id", _K.STRING),
    ("project_id", _K.STRING),
    ("environment", _K.ENUM),
)

SPAN_COLUMNS: tuple[tuple[str, ColumnKind], ...] = (
    *_SCOPE,
    ("trace_id", _K.STRING),
    ("span_id", _K.STRING),
    ("parent_span_id", _K.STRING),
    ("name", _K.STRING),
    ("kind", _K.ENUM),
    ("category", _K.ENUM),
    ("start_unix_nano", _K.INT),
    ("end_unix_nano", _K.INT_NULL),
    ("duration_ns", _K.INT_NULL),
    ("status", _K.ENUM),
    ("status_message", _K.STRING),
    ("error_type", _K.STRING),
    ("error_message", _K.STRING),
    ("service_name", _K.ENUM),
    ("service_version", _K.STRING),
    ("service_instance_id", _K.STRING),
    ("sdk_name", _K.ENUM),
    ("sdk_version", _K.STRING),
    ("session_id", _K.STRING),
    ("subject_id", _K.STRING),
    ("release", _K.STRING),
    ("git_commit", _K.STRING),
    ("tags", _K.STRING_ARRAY),
    ("provider", _K.ENUM),
    ("model", _K.ENUM),
    ("model_family", _K.ENUM),
    ("prompt_name", _K.STRING),
    ("prompt_version_id", _K.STRING),
    ("model_config_id", _K.STRING),
    ("dataset_version_id", _K.STRING),
    ("knowledge_base_version", _K.STRING),
    ("experiment_run_id", _K.STRING),
    ("input_tokens", _K.INT_NULL),
    ("output_tokens", _K.INT_NULL),
    ("total_tokens", _K.INT_NULL),
    ("cached_input_tokens", _K.INT_NULL),
    ("cache_write_tokens", _K.INT_NULL),
    ("reasoning_tokens", _K.INT_NULL),
    ("usage_source", _K.ENUM),
    ("cost_total", _K.DECIMAL_NULL),
    ("cost_currency", _K.ENUM),
    ("cost_estimation_status", _K.ENUM),
    ("price_book_version", _K.STRING),
    ("time_to_first_token_ms", _K.FLOAT_NULL),
    ("queue_ms", _K.FLOAT_NULL),
    ("provider_ms", _K.FLOAT_NULL),
    ("agent_id", _K.STRING),
    ("tool_name", _K.STRING),
    ("tool_status", _K.ENUM),
    ("retriever_name", _K.STRING),
    ("retrieval_result_count", _K.INT_NULL),
    ("input_preview", _K.STRING),
    ("output_preview", _K.STRING),
    ("input_ref", _K.STRING),
    ("output_ref", _K.STRING),
    ("attributes", _K.MAP),
    ("links", _K.JSON),
    ("sampling_rate", _K.FLOAT_NULL),
    ("ingested_at", _K.TIMESTAMP),
    ("ingest_version", _K.INT),
    ("content_hash", _K.STRING),
    ("late_arrival", _K.BOOL),
)

TRACE_COLUMNS: tuple[tuple[str, ColumnKind], ...] = (
    *_SCOPE,
    ("trace_id", _K.STRING),
    ("name", _K.STRING),
    ("start_unix_nano", _K.INT),
    ("end_unix_nano", _K.INT_NULL),
    ("duration_ns", _K.INT_NULL),
    ("status", _K.ENUM),
    ("error_summary", _K.STRING),
    ("root_span_id", _K.STRING),
    ("span_count", _K.INT),
    ("error_count", _K.INT),
    ("session_id", _K.STRING),
    ("subject_id", _K.STRING),
    ("release", _K.STRING),
    ("git_commit", _K.STRING),
    ("tags", _K.STRING_ARRAY),
    ("total_input_tokens", _K.INT),
    ("total_output_tokens", _K.INT),
    ("total_tokens", _K.INT),
    ("total_cached_input_tokens", _K.INT),
    ("total_reasoning_tokens", _K.INT),
    ("usage_source", _K.ENUM),
    ("total_cost", _K.DECIMAL_NULL),
    ("cost_currency", _K.ENUM),
    ("cost_estimation_status", _K.ENUM),
    ("time_to_first_token_ms", _K.FLOAT_NULL),
    ("models", _K.STRING_ARRAY),
    ("providers", _K.STRING_ARRAY),
    ("prompt_version_ids", _K.STRING_ARRAY),
    ("model_config_ids", _K.STRING_ARRAY),
    ("dataset_version_ids", _K.STRING_ARRAY),
    ("service_names", _K.STRING_ARRAY),
    ("llm_call_count", _K.INT),
    ("retrieval_count", _K.INT),
    ("tool_call_count", _K.INT),
    ("agent_step_count", _K.INT),
    ("sdk_name", _K.ENUM),
    ("sdk_version", _K.STRING),
    ("sampling_rate", _K.FLOAT_NULL),
    ("ingested_at", _K.TIMESTAMP),
    ("ingest_version", _K.INT),
    ("complete", _K.BOOL),
)

SPAN_EVENT_COLUMNS: tuple[tuple[str, ColumnKind], ...] = (
    *_SCOPE,
    ("trace_id", _K.STRING),
    ("span_id", _K.STRING),
    ("time_unix_nano", _K.INT),
    ("name", _K.STRING),
    ("sequence", _K.INT),
    ("attributes", _K.MAP),
    ("ingested_at", _K.TIMESTAMP),
)

RETRIEVAL_DOCUMENT_COLUMNS: tuple[tuple[str, ColumnKind], ...] = (
    *_SCOPE,
    ("trace_id", _K.STRING),
    ("span_id", _K.STRING),
    ("time_unix_nano", _K.INT),
    ("document_id", _K.STRING),
    ("chunk_id", _K.STRING),
    ("rank", _K.INT),
    ("score", _K.FLOAT_NULL),
    ("rerank_score", _K.FLOAT_NULL),
    ("rerank_rank", _K.INT_NULL),
    ("selected", _K.BOOL),
    ("token_count", _K.INT_NULL),
    ("truncated", _K.BOOL),
    ("source", _K.STRING),
    ("title", _K.STRING),
    ("content_preview", _K.STRING),
    ("content_ref", _K.STRING),
    ("retriever_name", _K.STRING),
    ("knowledge_base_version", _K.STRING),
    ("embedding_model", _K.ENUM),
    ("search_type", _K.ENUM),
    ("query", _K.STRING),
    ("rewritten_query", _K.STRING),
    ("metadata", _K.MAP),
    ("ingested_at", _K.TIMESTAMP),
)

AGENT_STEP_COLUMNS: tuple[tuple[str, ColumnKind], ...] = (
    *_SCOPE,
    ("trace_id", _K.STRING),
    ("span_id", _K.STRING),
    ("agent_id", _K.STRING),
    ("agent_version", _K.STRING),
    ("goal", _K.STRING),
    ("step_number", _K.INT),
    ("parent_step", _K.INT_NULL),
    ("step_type", _K.ENUM),
    ("decision_summary", _K.STRING),
    ("tool_name", _K.STRING),
    ("tool_status", _K.ENUM),
    ("tool_result_ref", _K.STRING),
    ("handoff_target", _K.STRING),
    ("memory_read_keys", _K.STRING_ARRAY),
    ("memory_write_keys", _K.STRING_ARRAY),
    ("retry_of", _K.INT_NULL),
    ("branch_id", _K.STRING),
    ("loop_iteration", _K.INT_NULL),
    ("approval_required", _K.BOOL),
    ("approval_status", _K.ENUM),
    ("termination_reason", _K.ENUM),
    ("max_steps", _K.INT_NULL),
    ("start_unix_nano", _K.INT),
    ("duration_ns", _K.INT_NULL),
    ("input_tokens", _K.INT_NULL),
    ("output_tokens", _K.INT_NULL),
    ("cost_total", _K.DECIMAL_NULL),
    ("status", _K.ENUM),
    ("error_message", _K.STRING),
    ("ingested_at", _K.TIMESTAMP),
)

COST_RECORD_COLUMNS: tuple[tuple[str, ColumnKind], ...] = (
    *_SCOPE,
    ("trace_id", _K.STRING),
    ("span_id", _K.STRING),
    ("time_unix_nano", _K.INT),
    ("provider", _K.ENUM),
    ("model", _K.ENUM),
    ("currency", _K.ENUM),
    ("total", _K.DECIMAL_NULL),
    ("price_book_id", _K.STRING),
    ("price_book_version", _K.STRING),
    ("estimation_status", _K.ENUM),
    ("usage_source", _K.ENUM),
    ("components", _K.JSON),
    ("formula", _K.STRING),
    ("prompt_version_id", _K.STRING),
    ("model_config_id", _K.STRING),
    ("session_id", _K.STRING),
    ("subject_id", _K.STRING),
    ("ingested_at", _K.TIMESTAMP),
)


TABLE_COLUMNS: dict[str, tuple[tuple[str, ColumnKind], ...]] = {
    "spans": SPAN_COLUMNS,
    "traces": TRACE_COLUMNS,
    "span_events": SPAN_EVENT_COLUMNS,
    "retrieval_documents": RETRIEVAL_DOCUMENT_COLUMNS,
    "agent_steps": AGENT_STEP_COLUMNS,
    "cost_records": COST_RECORD_COLUMNS,
}

TABLE_ROW_TYPES: dict[str, type] = {
    "spans": SpanRow,
    "traces": TraceRow,
    "span_events": SpanEventRow,
    "retrieval_documents": RetrievalDocumentRow,
    "agent_steps": AgentStepRow,
    "cost_records": CostRecordRow,
}

#: Dataclass attribute names that differ from their physical column name.
#: Kept empty deliberately -- when they match, the codec is trivially correct.
COLUMN_ALIASES: dict[str, dict[str, str]] = {}


T = TypeVar("T")


def encode_row(
    row: Any,
    columns: Sequence[tuple[str, ColumnKind]],
    codecs: Mapping[ColumnKind, Callable[[Any], Any]],
) -> list[Any]:
    """Convert a row dataclass into a positional value list for INSERT."""
    values: list[Any] = []
    for name, kind in columns:
        raw = getattr(row, name)
        encoder = codecs.get(kind)
        values.append(encoder(raw) if encoder else raw)
    return values


def decode_row(
    mapping: Mapping[str, Any],
    row_type: type[T],
    columns: Sequence[tuple[str, ColumnKind]],
    codecs: Mapping[ColumnKind, Callable[[Any], Any]],
) -> T:
    """Rebuild a row dataclass from a database result mapping.

    Unknown columns are ignored rather than raising, so a driver that has been
    migrated ahead of the code (a rolling deploy) does not break readers.
    """
    known = {field.name for field in dataclass_fields(row_type)}  # type: ignore[arg-type]
    kwargs: dict[str, Any] = {}
    for name, kind in columns:
        if name not in known or name not in mapping:
            continue
        decoder = codecs.get(kind)
        raw = mapping[name]
        kwargs[name] = decoder(raw) if decoder else raw
    return row_type(**kwargs)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Shared value helpers
# ---------------------------------------------------------------------------


def json_dumps(value: Any) -> str:
    """Compact, deterministic JSON for storage."""
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def json_loads(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def stringify_map(value: Mapping[str, Any] | None) -> dict[str, str]:
    """Flatten an attribute map to string values.

    Columnar stores want one physical type per map. Encoding non-strings as
    JSON keeps the round-trip lossless (``true`` stays distinguishable from
    ``"true"``) while letting the column stay ``Map(String, String)``.
    """
    if not value:
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(item, str):
            result[str(key)] = item
        else:
            result[str(key)] = json_dumps(item)
    return result


def parse_map(value: Any) -> dict[str, Any]:
    """Inverse of :func:`stringify_map`."""
    raw = json_loads(value) if isinstance(value, str) else value
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in raw.items():
        if isinstance(item, str) and item[:1] in '[{-0123456789tfn"':
            try:
                result[key] = json.loads(item)
                continue
            except (TypeError, ValueError):
                pass
        result[key] = item
    return result


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def from_decimal(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None
