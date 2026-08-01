"""Queryable field definitions for the analytics tables.

These :class:`~aiobs_api.core.query.ResourceSchema` objects are the *only* place
that maps a user-facing filter name onto a physical column. The API routers use
them to parse query strings; the analytics store uses the parsed result to build
SQL. Because both sides read the same definition, a field cannot be filterable
in the API but missing from the table, and no user-supplied string ever reaches
a query builder as an identifier.
"""

from __future__ import annotations

from ...core.query import FieldSpec, FieldType, ResourceSchema, SortDirection, build_schema

__all__ = [
    "AGENT_STEP_SCHEMA",
    "COST_SCHEMA",
    "RETRIEVAL_SCHEMA",
    "SCHEMAS_BY_SOURCE",
    "SPAN_SCHEMA",
    "TRACE_SCHEMA",
    "schema_for",
]

_S = FieldType.STRING
_I = FieldType.INTEGER
_N = FieldType.NUMBER
_B = FieldType.BOOLEAN
_T = FieldType.TIMESTAMP
_A = FieldType.STRING_ARRAY
_M = FieldType.MAP


def _f(
    name: str,
    type_: FieldType,
    column: str | None = None,
    *,
    sortable: bool = False,
    filterable: bool = True,
    allowed: frozenset[str] | None = None,
    subpath: bool = False,
    description: str = "",
) -> FieldSpec:
    return FieldSpec(
        name=name,
        type=type_,
        column=column or name,
        sortable=sortable,
        filterable=filterable,
        allowed_values=allowed,
        supports_subpath=subpath,
        description=description,
    )


_STATUS_VALUES = frozenset({"ok", "error", "incomplete", "unset"})
_CATEGORY_VALUES = frozenset(
    {
        "llm_generation",
        "chat_completion",
        "embedding",
        "retrieval",
        "rerank",
        "prompt_render",
        "guardrail",
        "tool_call",
        "agent_decision",
        "agent_handoff",
        "workflow_step",
        "db_query",
        "http_request",
        "queue_operation",
        "custom",
    }
)
_USAGE_SOURCE_VALUES = frozenset({"provider", "estimated", "reconciled", "missing"})
_COST_STATUS_VALUES = frozenset({"final", "estimated", "unpriced"})


TRACE_SCHEMA: ResourceSchema = build_schema(
    "traces",
    [
        _f("trace_id", _S, sortable=True, description="W3C trace identifier"),
        _f("name", _S, sortable=True, description="Logical request name"),
        _f("status", _S, allowed=_STATUS_VALUES, description="Roll-up status"),
        _f("start_time", _T, "start_unix_nano", sortable=True, description="Trace start"),
        _f("end_time", _T, "end_unix_nano", sortable=True, description="Trace end"),
        _f("duration_ms", _N, "duration_ns", sortable=True, description="Total duration"),
        _f("span_count", _I, sortable=True),
        _f("error_count", _I, sortable=True),
        _f("session_id", _S),
        _f("subject_id", _S, description="Pseudonymous end-user identifier"),
        _f("release", _S),
        _f("git_commit", _S),
        _f("tags", _A),
        _f("environment", _S),
        _f("input_tokens", _I, "total_input_tokens", sortable=True),
        _f("output_tokens", _I, "total_output_tokens", sortable=True),
        _f("total_tokens", _I, sortable=True),
        _f("cached_input_tokens", _I, "total_cached_input_tokens", sortable=True),
        _f("usage_source", _S, allowed=_USAGE_SOURCE_VALUES),
        _f("cost", _N, "total_cost", sortable=True, description="Total cost in cost_currency"),
        _f("cost_currency", _S),
        _f("cost_status", _S, "cost_estimation_status", allowed=_COST_STATUS_VALUES),
        _f("time_to_first_token_ms", _N, sortable=True),
        _f("model", _A, "models", description="Any model used in the trace"),
        _f("provider", _A, "providers"),
        _f("prompt_version_id", _A, "prompt_version_ids"),
        _f("model_config_id", _A, "model_config_ids"),
        _f("dataset_version_id", _A, "dataset_version_ids"),
        _f("service_name", _A, "service_names"),
        _f("llm_call_count", _I, sortable=True),
        _f("retrieval_count", _I, sortable=True),
        _f("tool_call_count", _I, sortable=True),
        _f("agent_step_count", _I, sortable=True),
        _f("sdk_name", _S),
        _f("sdk_version", _S),
        _f("complete", _B),
    ],
    default_sort=[("start_time", SortDirection.DESC)],
    tiebreaker="trace_id",
)


SPAN_SCHEMA: ResourceSchema = build_schema(
    "spans",
    [
        _f("trace_id", _S, sortable=True),
        _f("span_id", _S, sortable=True),
        _f("parent_span_id", _S),
        _f("name", _S, sortable=True),
        _f("kind", _S),
        _f("category", _S, allowed=_CATEGORY_VALUES),
        _f("status", _S, allowed=_STATUS_VALUES),
        _f("start_time", _T, "start_unix_nano", sortable=True),
        _f("duration_ms", _N, "duration_ns", sortable=True),
        _f("service_name", _S),
        _f("service_version", _S),
        _f("environment", _S),
        _f("session_id", _S),
        _f("subject_id", _S),
        _f("release", _S),
        _f("tags", _A),
        _f("provider", _S),
        _f("model", _S),
        _f("model_family", _S),
        _f("prompt_name", _S),
        _f("prompt_version_id", _S),
        _f("model_config_id", _S),
        _f("dataset_version_id", _S),
        _f("knowledge_base_version", _S),
        _f("experiment_run_id", _S),
        _f("input_tokens", _I, sortable=True),
        _f("output_tokens", _I, sortable=True),
        _f("total_tokens", _I, sortable=True),
        _f("cached_input_tokens", _I),
        _f("reasoning_tokens", _I),
        _f("usage_source", _S, allowed=_USAGE_SOURCE_VALUES),
        _f("cost", _N, "cost_total", sortable=True),
        _f("cost_status", _S, "cost_estimation_status", allowed=_COST_STATUS_VALUES),
        _f("time_to_first_token_ms", _N, sortable=True),
        _f("agent_id", _S),
        _f("tool_name", _S),
        _f("tool_status", _S),
        _f("retriever_name", _S),
        _f("error_type", _S),
        _f("late_arrival", _B),
        _f(
            "attributes",
            _M,
            "attributes",
            subpath=True,
            description="Long-tail attributes; filtering here scans more data than a promoted column",
        ),
    ],
    default_sort=[("start_time", SortDirection.DESC)],
    tiebreaker="span_id",
)


RETRIEVAL_SCHEMA: ResourceSchema = build_schema(
    "retrieval_documents",
    [
        _f("trace_id", _S),
        _f("span_id", _S),
        _f("document_id", _S, sortable=True),
        _f("chunk_id", _S),
        _f("rank", _I, sortable=True),
        _f("score", _N, sortable=True),
        _f("rerank_score", _N, sortable=True),
        _f("rerank_rank", _I, sortable=True),
        _f("selected", _B),
        _f("truncated", _B),
        _f("token_count", _I, sortable=True),
        _f("retriever_name", _S),
        _f("knowledge_base_version", _S),
        _f("embedding_model", _S),
        _f("search_type", _S),
        _f("source", _S),
        _f("start_time", _T, "time_unix_nano", sortable=True),
    ],
    default_sort=[("rank", SortDirection.ASC)],
    tiebreaker="document_id",
)


AGENT_STEP_SCHEMA: ResourceSchema = build_schema(
    "agent_steps",
    [
        _f("trace_id", _S),
        _f("span_id", _S),
        _f("agent_id", _S),
        _f("agent_version", _S),
        _f("step_number", _I, sortable=True),
        _f("parent_step", _I),
        _f("step_type", _S),
        _f("tool_name", _S),
        _f("tool_status", _S),
        _f("handoff_target", _S),
        _f("branch_id", _S),
        _f("retry_of", _I),
        _f("loop_iteration", _I),
        _f("approval_required", _B),
        _f("approval_status", _S),
        _f("termination_reason", _S),
        _f("status", _S),
        _f("duration_ms", _N, "duration_ns", sortable=True),
        _f("start_time", _T, "start_unix_nano", sortable=True),
        _f("input_tokens", _I),
        _f("output_tokens", _I),
        _f("cost", _N, "cost_total"),
    ],
    default_sort=[("step_number", SortDirection.ASC)],
    tiebreaker="span_id",
)


COST_SCHEMA: ResourceSchema = build_schema(
    "cost_records",
    [
        _f("trace_id", _S),
        _f("span_id", _S),
        _f("provider", _S),
        _f("model", _S),
        _f("currency", _S),
        _f("total", _N, sortable=True),
        _f("price_book_version", _S),
        _f("estimation_status", _S, allowed=_COST_STATUS_VALUES),
        _f("usage_source", _S, allowed=_USAGE_SOURCE_VALUES),
        _f("prompt_version_id", _S),
        _f("model_config_id", _S),
        _f("session_id", _S),
        _f("subject_id", _S),
        _f("start_time", _T, "time_unix_nano", sortable=True),
    ],
    default_sort=[("start_time", SortDirection.DESC)],
    tiebreaker="span_id",
)


SCHEMAS_BY_SOURCE: dict[str, ResourceSchema] = {
    "traces": TRACE_SCHEMA,
    "spans": SPAN_SCHEMA,
    "retrieval_documents": RETRIEVAL_SCHEMA,
    "agent_steps": AGENT_STEP_SCHEMA,
    "cost_records": COST_SCHEMA,
}


def schema_for(source: str) -> ResourceSchema:
    """Return the schema for a physical table, raising on an unknown source."""
    try:
        return SCHEMAS_BY_SOURCE[source]
    except KeyError as exc:
        raise KeyError(
            f"unknown analytics source {source!r}; known: {sorted(SCHEMAS_BY_SOURCE)}"
        ) from exc


def aggregatable_columns(source: str) -> frozenset[str]:
    """Numeric columns that may be aggregated for ``source``.

    Restricting aggregation to a known set keeps a metric query from being a
    vector for scanning a column it has no business touching, and gives a clear
    error instead of a confusing SQL failure.
    """
    schema = schema_for(source)
    return frozenset(
        field.column
        for field in schema.fields
        if field.type in {FieldType.INTEGER, FieldType.NUMBER}
    )
