"""Shared schemas for the AI Observability Platform.

This package is the contract boundary between the SDKs, the ingestion API, the
worker and the analytics stores. It contains *no* I/O and *no* framework
dependencies beyond Pydantic, so it can be imported by a customer application
without dragging in a database driver.

Modules
-------
:mod:`aiobs_schemas.semconv`
    Registry of every span attribute the platform understands.
:mod:`aiobs_schemas.enums`
    Closed vocabularies (span kinds, categories, usage provenance, ...).
:mod:`aiobs_schemas.canonical`
    RFC 8785 canonical JSON and SHA-256 content addressing.
:mod:`aiobs_schemas.ids`
    Trace/span id validation and prefixed, time-sortable resource identifiers.
:mod:`aiobs_schemas.wire`
    The native ingestion wire format and its limits.
:mod:`aiobs_schemas.errors`
    The typed error envelope returned by every API endpoint.
"""

from __future__ import annotations

from . import canonical, enums, errors, ids, semconv, wire
from .canonical import canonical_json, canonical_json_str, content_hash, verify_hash
from .enums import (
    AgentStepType,
    AgentTerminationReason,
    ApprovalStatus,
    CostEstimationStatus,
    GuardrailOutcome,
    ReleaseStage,
    SamplingDecision,
    SearchType,
    SpanCategory,
    SpanKind,
    SpanStatus,
    ToolStatus,
    TraceStatus,
    UsageSource,
)
from .errors import ErrorCode, ErrorDetail, ErrorResponse
from .ids import IdPrefix, generate_id, generate_span_id, generate_trace_id, version_id
from .wire import (
    LIMITS,
    AgentStepPayload,
    IngestBatch,
    IngestResponse,
    LineagePayload,
    ResourceDescriptor,
    RetrievalDocument,
    RetrievalPayload,
    SpanEvent,
    SpanLink,
    SpanRejection,
    TokenUsage,
    WireSpan,
)

__version__ = "0.1.0"

#: Wire format version. Bumped when the meaning of an existing field changes;
#: additive changes do not bump it. Producers send it in the
#: ``X-AIOBS-Schema-Version`` header and the API rejects unknown majors.
SCHEMA_VERSION = "1.0"

__all__ = [
    "LIMITS",
    "SCHEMA_VERSION",
    "AgentStepPayload",
    "AgentStepType",
    "AgentTerminationReason",
    "ApprovalStatus",
    "CostEstimationStatus",
    "ErrorCode",
    "ErrorDetail",
    "ErrorResponse",
    "GuardrailOutcome",
    "IdPrefix",
    "IngestBatch",
    "IngestResponse",
    "LineagePayload",
    "ReleaseStage",
    "ResourceDescriptor",
    "RetrievalDocument",
    "RetrievalPayload",
    "SamplingDecision",
    "SearchType",
    "SpanCategory",
    "SpanEvent",
    "SpanKind",
    "SpanLink",
    "SpanRejection",
    "SpanStatus",
    "TokenUsage",
    "ToolStatus",
    "TraceStatus",
    "UsageSource",
    "WireSpan",
    "__version__",
    "canonical",
    "canonical_json",
    "canonical_json_str",
    "content_hash",
    "enums",
    "errors",
    "generate_id",
    "generate_span_id",
    "generate_trace_id",
    "ids",
    "semconv",
    "verify_hash",
    "version_id",
    "wire",
]
