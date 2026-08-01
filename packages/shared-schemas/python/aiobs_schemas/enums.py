"""Closed vocabularies shared by the SDKs, the ingestion pipeline and the UI.

These are deliberately *closed* enums: unknown values are rejected at the
ingestion boundary and mapped to an explicit ``unknown``/``custom`` member
rather than being silently stored. That keeps the analytics tables low
cardinality and makes dashboards meaningful.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "AgentStepType",
    "AgentTerminationReason",
    "ApprovalStatus",
    "CostEstimationStatus",
    "GuardrailOutcome",
    "ReleaseStage",
    "SamplingDecision",
    "SearchType",
    "SpanCategory",
    "SpanKind",
    "SpanStatus",
    "ToolStatus",
    "TraceStatus",
    "UsageSource",
]


class SpanKind(str, Enum):
    """OpenTelemetry span kind."""

    INTERNAL = "internal"
    SERVER = "server"
    CLIENT = "client"
    PRODUCER = "producer"
    CONSUMER = "consumer"

    @classmethod
    def from_otlp(cls, value: int) -> SpanKind:
        """Map an OTLP ``SpanKind`` enum value onto this enum."""
        return {
            0: cls.INTERNAL,
            1: cls.INTERNAL,
            2: cls.SERVER,
            3: cls.CLIENT,
            4: cls.PRODUCER,
            5: cls.CONSUMER,
        }.get(value, cls.INTERNAL)


class SpanStatus(str, Enum):
    """OpenTelemetry span status."""

    UNSET = "unset"
    OK = "ok"
    ERROR = "error"

    @classmethod
    def from_otlp(cls, value: int) -> SpanStatus:
        return {0: cls.UNSET, 1: cls.OK, 2: cls.ERROR}.get(value, cls.UNSET)


class TraceStatus(str, Enum):
    """Roll-up status of a whole logical AI request."""

    OK = "ok"
    ERROR = "error"
    #: Root span has not been observed yet, or an end time is still missing.
    INCOMPLETE = "incomplete"


class SpanCategory(str, Enum):
    """Platform span taxonomy, carried in ``aiobs.span.category``.

    The category drives which analytics projections a span feeds (retrieval
    documents, agent steps, cost records) and how the UI renders it.
    """

    LLM_GENERATION = "llm_generation"
    CHAT_COMPLETION = "chat_completion"
    EMBEDDING = "embedding"
    RETRIEVAL = "retrieval"
    RERANK = "rerank"
    PROMPT_RENDER = "prompt_render"
    GUARDRAIL = "guardrail"
    TOOL_CALL = "tool_call"
    AGENT_DECISION = "agent_decision"
    AGENT_HANDOFF = "agent_handoff"
    WORKFLOW_STEP = "workflow_step"
    DB_QUERY = "db_query"
    HTTP_REQUEST = "http_request"
    QUEUE_OPERATION = "queue_operation"
    CUSTOM = "custom"

    @classmethod
    def coerce(cls, value: str | None) -> SpanCategory:
        """Best-effort mapping of an arbitrary string onto a category."""
        if not value:
            return cls.CUSTOM
        normalised = value.strip().lower().replace("-", "_").replace(" ", "_")
        try:
            return cls(normalised)
        except ValueError:
            return cls.CUSTOM

    @property
    def is_model_call(self) -> bool:
        """Whether the category represents a billable model invocation."""
        return self in {
            SpanCategory.LLM_GENERATION,
            SpanCategory.CHAT_COMPLETION,
            SpanCategory.EMBEDDING,
            SpanCategory.RERANK,
        }


class UsageSource(str, Enum):
    """Provenance of token usage numbers -- never conflate these."""

    #: Reported verbatim by the model provider.
    PROVIDER = "provider"
    #: Computed locally by a tokeniser or heuristic.
    ESTIMATED = "estimated"
    #: Provider numbers corrected by a later reconciliation job.
    RECONCILED = "reconciled"
    #: No usage information available.
    MISSING = "missing"


class CostEstimationStatus(str, Enum):
    """Whether a cost figure can be trusted for billing-grade reporting."""

    #: Derived from provider-reported usage against a matching price entry.
    FINAL = "final"
    #: Derived from estimated usage, or from a fallback price entry.
    ESTIMATED = "estimated"
    #: No price entry matched; cost is unknown, not zero.
    UNPRICED = "unpriced"


class SearchType(str, Enum):
    VECTOR = "vector"
    KEYWORD = "keyword"
    HYBRID = "hybrid"
    GRAPH = "graph"
    CUSTOM = "custom"

    @classmethod
    def coerce(cls, value: str | None) -> SearchType:
        if not value:
            return cls.CUSTOM
        try:
            return cls(value.strip().lower())
        except ValueError:
            return cls.CUSTOM


class AgentStepType(str, Enum):
    DECISION = "decision"
    TOOL_CALL = "tool_call"
    HANDOFF = "handoff"
    MEMORY = "memory"
    APPROVAL = "approval"
    OBSERVATION = "observation"
    TERMINATE = "terminate"

    @classmethod
    def coerce(cls, value: str | None) -> AgentStepType:
        if not value:
            return cls.OBSERVATION
        try:
            return cls(value.strip().lower())
        except ValueError:
            return cls.OBSERVATION


class ToolStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    MALFORMED = "malformed"
    SKIPPED = "skipped"

    @classmethod
    def coerce(cls, value: str | None) -> ToolStatus:
        if not value:
            return cls.OK
        try:
            return cls(value.strip().lower())
        except ValueError:
            return cls.ERROR


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    NOT_REQUIRED = "not_required"


class AgentTerminationReason(str, Enum):
    COMPLETED = "completed"
    MAX_STEPS = "max_steps"
    LOOP_DETECTED = "loop_detected"
    ERROR = "error"
    TOOL_FAILURE = "tool_failure"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_TIMEOUT = "approval_timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: str | None) -> AgentTerminationReason:
        if not value:
            return cls.UNKNOWN
        try:
            return cls(value.strip().lower())
        except ValueError:
            return cls.UNKNOWN


class GuardrailOutcome(str, Enum):
    PASS = "pass"
    BLOCK = "block"
    FLAG = "flag"


class SamplingDecision(str, Enum):
    DROP = "drop"
    RECORD_ONLY = "record_only"
    RECORD_AND_SAMPLE = "record_and_sample"


class ReleaseStage(str, Enum):
    """Lifecycle stage of a registry version."""

    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"
