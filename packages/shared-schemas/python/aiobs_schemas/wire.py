"""The native ingestion wire format.

The platform accepts telemetry over two paths that converge on one internal
model:

1. **OTLP** (``/v1/traces``) -- for anything already emitting OpenTelemetry.
2. **The native batch endpoint** (``/v1/ingest/spans``) -- a JSON format that is
   structurally a subset of OTLP with the encoding warts removed (hex ids
   instead of base64 bytes, ISO timestamps accepted alongside nanosecond
   integers, a flat attribute map instead of the ``AnyValue`` union).

The native format additionally accepts *typed sub-objects* (``usage``,
``retrieval``, ``agent_step``, ``lineage``) as an ergonomic alternative to
hand-writing semantic-convention attributes. Those sub-objects are **lowered**
into ``aiobs.*``/``gen_ai.*`` attributes by :func:`WireSpan.lowered_attributes`
before storage, so a span sent either way is indistinguishable afterwards. This
is why the platform can claim OTLP compatibility without giving up a pleasant
SDK surface.

Limits declared here are enforced at the API boundary. They exist to bound
memory use per request and to keep analytics-store cardinality sane; exceeding
them produces a typed, per-span rejection rather than a whole-batch failure.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from . import semconv
from .enums import SpanCategory, SpanKind, SpanStatus, UsageSource
from .ids import is_valid_span_id, is_valid_trace_id

__all__ = [
    "LIMITS",
    "AgentStepPayload",
    "AttributeValue",
    "IngestBatch",
    "IngestResponse",
    "Limits",
    "LineagePayload",
    "ResourceDescriptor",
    "RetrievalDocument",
    "RetrievalPayload",
    "SpanEvent",
    "SpanLink",
    "SpanRejection",
    "TokenUsage",
    "WireSpan",
]


class Limits:
    """Hard limits applied to a single ingest request.

    Rationale for each value is in ``docs/architecture/ingestion-pipeline.md``.
    They are class attributes rather than settings because SDKs must be able to
    pre-validate locally without contacting the server; the server may only
    tighten them per tenant, never loosen them.
    """

    MAX_SPANS_PER_BATCH: Final = 2_000
    MAX_ATTRIBUTES_PER_SPAN: Final = 256
    MAX_EVENTS_PER_SPAN: Final = 128
    MAX_LINKS_PER_SPAN: Final = 64
    MAX_ATTRIBUTE_KEY_LENGTH: Final = 256
    MAX_ATTRIBUTE_VALUE_LENGTH: Final = 32_768
    MAX_ARRAY_ELEMENTS: Final = 256
    MAX_NAME_LENGTH: Final = 512
    MAX_TAGS: Final = 64
    MAX_RETRIEVAL_DOCUMENTS: Final = 500
    #: Uncompressed request body ceiling. Larger payloads must be offloaded to
    #: object storage by the SDK and referenced via ``aiobs.*.ref``.
    MAX_BODY_BYTES: Final = 8 * 1024 * 1024
    #: Spans starting further in the future than this are clock-skew rejected.
    MAX_CLOCK_SKEW_FUTURE_SECONDS: Final = 300
    #: Spans older than this are accepted but flagged as late-arriving.
    MAX_BACKFILL_AGE_SECONDS: Final = 7 * 24 * 3600


LIMITS: Final = Limits()

#: Scalar and homogeneous-array attribute values, mirroring the OTLP AnyValue
#: subset that maps cleanly onto ClickHouse columns.
AttributeValue = str | int | float | bool | list[str] | list[int] | list[float] | None


def _to_unix_nano(value: int | float | str | datetime) -> int:
    """Coerce a timestamp into Unix nanoseconds.

    Accepts nanosecond integers (OTLP native), floating-point seconds (common in
    Python code), RFC 3339 strings (common in JavaScript), and ``datetime``
    objects. Naive datetimes are interpreted as UTC and flagged by the caller;
    guessing a local timezone would silently shift traces by hours.
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(moment.timestamp() * 1_000_000_000)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return int(moment.timestamp() * 1_000_000_000)
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid timestamp")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # Heuristic: values below 1e12 are seconds, otherwise already nanoseconds.
        return int(value * 1_000_000_000) if value < 1e12 else int(value)
    raise ValueError(f"cannot interpret {value!r} as a timestamp")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)


class TokenUsage(_Strict):
    """Normalised token accounting for one model call.

    ``raw`` preserves the provider's own usage object verbatim. Normalised
    fields are what dashboards aggregate; ``raw`` is what an engineer inspects
    when the normalisation looks wrong, and what a reconciliation job replays.
    Never discard it.
    """

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    audio_input_seconds: float | None = Field(default=None, ge=0)
    audio_output_seconds: float | None = Field(default=None, ge=0)
    image_input_count: int | None = Field(default=None, ge=0)
    image_output_count: int | None = Field(default=None, ge=0)
    source: UsageSource = UsageSource.PROVIDER
    raw: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _derive_total(self) -> TokenUsage:
        if self.total_tokens is None and (
            self.input_tokens is not None or self.output_tokens is not None
        ):
            object.__setattr__(
                self,
                "total_tokens",
                (self.input_tokens or 0) + (self.output_tokens or 0),
            )
        return self

    @property
    def is_empty(self) -> bool:
        """Whether no usage signal at all was supplied."""
        return all(
            getattr(self, name) is None
            for name in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cached_input_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "audio_input_seconds",
                "audio_output_seconds",
                "image_input_count",
                "image_output_count",
            )
        )


class RetrievalDocument(_Strict):
    """One document or chunk considered by a retrieval step."""

    document_id: str = Field(max_length=512)
    chunk_id: str | None = Field(default=None, max_length=512)
    rank: int = Field(ge=0)
    score: float | None = None
    rerank_score: float | None = None
    rerank_rank: int | None = Field(default=None, ge=0)
    source: str | None = Field(default=None, max_length=2048)
    title: str | None = Field(default=None, max_length=1024)
    #: Chunk text, or ``None`` when redaction replaced it with ``content_ref``.
    content: str | None = None
    content_ref: str | None = Field(default=None, max_length=1024)
    #: Whether this chunk survived context selection and reached the model.
    selected: bool = False
    token_count: int | None = Field(default=None, ge=0)
    truncated: bool = False
    metadata: dict[str, AttributeValue] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def _bound_content(cls, value: str | None) -> str | None:
        if value is not None and len(value) > Limits.MAX_ATTRIBUTE_VALUE_LENGTH:
            raise ValueError(
                f"document content exceeds {Limits.MAX_ATTRIBUTE_VALUE_LENGTH} characters; "
                "store it in object storage and set content_ref instead"
            )
        return value


class RetrievalPayload(_Strict):
    """Typed description of a retrieval stage, lowered onto ``aiobs.retrieval.*``."""

    query: str | None = None
    rewritten_query: str | None = None
    retriever_name: str | None = Field(default=None, max_length=256)
    retriever_version: str | None = Field(default=None, max_length=128)
    knowledge_base_version: str | None = Field(default=None, max_length=128)
    search_type: str | None = Field(default=None, max_length=64)
    filters: dict[str, AttributeValue] = Field(default_factory=dict)
    top_k: int | None = Field(default=None, ge=0)
    embedding_model: str | None = Field(default=None, max_length=256)
    embedding_dimensions: int | None = Field(default=None, ge=0)
    embedding_latency_ms: float | None = Field(default=None, ge=0)
    reranker_model: str | None = Field(default=None, max_length=256)
    reranker_latency_ms: float | None = Field(default=None, ge=0)
    retrieval_latency_ms: float | None = Field(default=None, ge=0)
    context_tokens: int | None = Field(default=None, ge=0)
    context_truncated: bool = False
    documents: list[RetrievalDocument] = Field(default_factory=list)

    @field_validator("documents")
    @classmethod
    def _bound_documents(cls, value: list[RetrievalDocument]) -> list[RetrievalDocument]:
        if len(value) > Limits.MAX_RETRIEVAL_DOCUMENTS:
            raise ValueError(
                f"at most {Limits.MAX_RETRIEVAL_DOCUMENTS} retrieval documents per span"
            )
        return value


class AgentStepPayload(_Strict):
    """Typed description of one agent trajectory step.

    Note the absence of any field for private reasoning traces. The platform
    records *observable actions* and a short ``decision_summary`` that the
    application chooses to publish. See ``docs/security/data-redaction.md``.
    """

    agent_id: str = Field(max_length=256)
    agent_version: str | None = Field(default=None, max_length=128)
    goal: str | None = None
    step_number: int = Field(ge=0)
    parent_step: int | None = Field(default=None, ge=0)
    step_type: str = Field(default="observation", max_length=64)
    decision_summary: str | None = Field(default=None, max_length=4096)
    tool_name: str | None = Field(default=None, max_length=256)
    tool_arguments: dict[str, Any] | None = None
    tool_result_ref: str | None = Field(default=None, max_length=1024)
    tool_status: str | None = Field(default=None, max_length=32)
    handoff_target: str | None = Field(default=None, max_length=256)
    memory_read_keys: list[str] = Field(default_factory=list)
    memory_write_keys: list[str] = Field(default_factory=list)
    retry_of: int | None = Field(default=None, ge=0)
    branch_id: str | None = Field(default=None, max_length=128)
    loop_iteration: int | None = Field(default=None, ge=0)
    approval_required: bool = False
    approval_status: str | None = Field(default=None, max_length=32)
    termination_reason: str | None = Field(default=None, max_length=64)
    max_steps: int | None = Field(default=None, ge=0)


class LineagePayload(_Strict):
    """Links a span to the immutable versions that produced it."""

    prompt_name: str | None = Field(default=None, max_length=256)
    prompt_version_id: str | None = Field(default=None, max_length=64)
    prompt_version_label: str | None = Field(default=None, max_length=128)
    prompt_hash: str | None = Field(default=None, max_length=80)
    prompt_variables: dict[str, Any] | None = None
    model_config_id: str | None = Field(default=None, max_length=64)
    model_config_hash: str | None = Field(default=None, max_length=80)
    dataset_name: str | None = Field(default=None, max_length=256)
    dataset_version_id: str | None = Field(default=None, max_length=64)
    dataset_record_id: str | None = Field(default=None, max_length=256)
    knowledge_base_version: str | None = Field(default=None, max_length=128)
    experiment_id: str | None = Field(default=None, max_length=64)
    experiment_run_id: str | None = Field(default=None, max_length=64)
    release: str | None = Field(default=None, max_length=128)
    git_commit: str | None = Field(default=None, max_length=64)


class SpanEvent(_Strict):
    """A timestamped point-in-time occurrence inside a span."""

    name: str = Field(max_length=Limits.MAX_NAME_LENGTH)
    time_unix_nano: int = Field(ge=0)
    attributes: dict[str, AttributeValue] = Field(default_factory=dict)

    @field_validator("time_unix_nano", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> int:
        return _to_unix_nano(value)


class SpanLink(_Strict):
    """A non-parental relationship to another span.

    Links express fan-in (a batch consumer linking to each producer), retries
    (a retry span linking to the attempt it replaces) and agent sub-graphs,
    none of which the single-parent tree can represent.
    """

    trace_id: str
    span_id: str
    attributes: dict[str, AttributeValue] = Field(default_factory=dict)

    @field_validator("trace_id")
    @classmethod
    def _check_trace_id(cls, value: str) -> str:
        normalised = value.strip().lower()
        if not is_valid_trace_id(normalised):
            raise ValueError(f"invalid link trace id {value!r}")
        return normalised

    @field_validator("span_id")
    @classmethod
    def _check_span_id(cls, value: str) -> str:
        normalised = value.strip().lower()
        if not is_valid_span_id(normalised):
            raise ValueError(f"invalid link span id {value!r}")
        return normalised


class ResourceDescriptor(_Strict):
    """Identity of the process that produced a batch of spans.

    Mirrors the OTLP ``Resource`` message. ``environment`` here is advisory: the
    server resolves the authoritative environment from the API key, because a
    client must never be able to write into an environment it was not issued
    credentials for.
    """

    service_name: str = Field(max_length=256)
    service_version: str | None = Field(default=None, max_length=128)
    service_instance_id: str | None = Field(default=None, max_length=256)
    environment: str | None = Field(default=None, max_length=64)
    sdk_name: str | None = Field(default=None, max_length=128)
    sdk_version: str | None = Field(default=None, max_length=64)
    sdk_language: str | None = Field(default=None, max_length=64)
    attributes: dict[str, AttributeValue] = Field(default_factory=dict)


class WireSpan(_Strict):
    """A single span in the native ingest format."""

    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    name: str = Field(max_length=Limits.MAX_NAME_LENGTH)
    kind: SpanKind = SpanKind.INTERNAL
    category: SpanCategory = SpanCategory.CUSTOM
    start_time_unix_nano: int
    end_time_unix_nano: int | None = None
    status: SpanStatus = SpanStatus.UNSET
    status_message: str | None = Field(default=None, max_length=4096)
    attributes: dict[str, AttributeValue] = Field(default_factory=dict)
    events: list[SpanEvent] = Field(default_factory=list)
    links: list[SpanLink] = Field(default_factory=list)

    # Typed sub-objects, lowered onto semantic-convention attributes.
    usage: TokenUsage | None = None
    retrieval: RetrievalPayload | None = None
    agent_step: AgentStepPayload | None = None
    lineage: LineagePayload | None = None

    # Trace-level fields, only meaningful on the root span but accepted anywhere
    # so that a client which never sees the root can still label the trace.
    trace_name: str | None = Field(default=None, max_length=Limits.MAX_NAME_LENGTH)
    session_id: str | None = Field(default=None, max_length=256)
    subject_id: str | None = Field(default=None, max_length=256)
    tags: list[str] = Field(default_factory=list)

    @field_validator("trace_id")
    @classmethod
    def _check_trace_id(cls, value: str) -> str:
        normalised = value.strip().lower()
        if not is_valid_trace_id(normalised):
            raise ValueError(
                f"invalid trace id {value!r}: expected 32 lowercase hex characters, not all zero"
            )
        return normalised

    @field_validator("span_id")
    @classmethod
    def _check_span_id(cls, value: str) -> str:
        normalised = value.strip().lower()
        if not is_valid_span_id(normalised):
            raise ValueError(
                f"invalid span id {value!r}: expected 16 lowercase hex characters, not all zero"
            )
        return normalised

    @field_validator("parent_span_id")
    @classmethod
    def _check_parent_span_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalised = value.strip().lower()
        if normalised in {"", "0" * 16}:
            # OTLP encodes "no parent" as all-zero bytes; normalise to None so
            # root detection is a single `IS NULL` check everywhere downstream.
            return None
        if not is_valid_span_id(normalised):
            raise ValueError(f"invalid parent span id {value!r}")
        return normalised

    @field_validator("start_time_unix_nano", mode="before")
    @classmethod
    def _coerce_start(cls, value: Any) -> int:
        return _to_unix_nano(value)

    @field_validator("end_time_unix_nano", mode="before")
    @classmethod
    def _coerce_end(cls, value: Any) -> int | None:
        return None if value is None else _to_unix_nano(value)

    @field_validator("tags")
    @classmethod
    def _bound_tags(cls, value: list[str]) -> list[str]:
        if len(value) > Limits.MAX_TAGS:
            raise ValueError(f"at most {Limits.MAX_TAGS} tags per span")
        return value

    @field_validator("attributes")
    @classmethod
    def _bound_attributes(cls, value: dict[str, AttributeValue]) -> dict[str, AttributeValue]:
        if len(value) > Limits.MAX_ATTRIBUTES_PER_SPAN:
            raise ValueError(f"at most {Limits.MAX_ATTRIBUTES_PER_SPAN} attributes per span")
        for key, item in value.items():
            if len(key) > Limits.MAX_ATTRIBUTE_KEY_LENGTH:
                raise ValueError(f"attribute key {key[:40]!r}... exceeds the length limit")
            if isinstance(item, str) and len(item) > Limits.MAX_ATTRIBUTE_VALUE_LENGTH:
                raise ValueError(f"attribute {key!r} exceeds the value length limit")
            if isinstance(item, list) and len(item) > Limits.MAX_ARRAY_ELEMENTS:
                raise ValueError(f"attribute {key!r} exceeds the array element limit")
        return value

    @field_validator("events")
    @classmethod
    def _bound_events(cls, value: list[SpanEvent]) -> list[SpanEvent]:
        if len(value) > Limits.MAX_EVENTS_PER_SPAN:
            raise ValueError(f"at most {Limits.MAX_EVENTS_PER_SPAN} events per span")
        return value

    @field_validator("links")
    @classmethod
    def _bound_links(cls, value: list[SpanLink]) -> list[SpanLink]:
        if len(value) > Limits.MAX_LINKS_PER_SPAN:
            raise ValueError(f"at most {Limits.MAX_LINKS_PER_SPAN} links per span")
        return value

    @model_validator(mode="after")
    def _check_interval(self) -> WireSpan:
        if (
            self.end_time_unix_nano is not None
            and self.end_time_unix_nano < self.start_time_unix_nano
        ):
            raise ValueError(
                "end_time_unix_nano precedes start_time_unix_nano; "
                "a span cannot finish before it starts"
            )
        if self.parent_span_id is not None and self.parent_span_id == self.span_id:
            raise ValueError("a span cannot be its own parent")
        return self

    @property
    def duration_ns(self) -> int | None:
        """Span duration in nanoseconds, or ``None`` while the span is open."""
        if self.end_time_unix_nano is None:
            return None
        return self.end_time_unix_nano - self.start_time_unix_nano

    def lowered_attributes(self) -> dict[str, AttributeValue]:
        """Return attributes with all typed sub-objects lowered onto semconv keys.

        Explicit ``attributes`` win over lowered values: if a caller sets
        ``gen_ai.usage.input_tokens`` by hand *and* passes ``usage``, the hand
        written value is authoritative. That ordering matters for OTLP producers
        that already emit correct conventions.
        """
        import json as _json

        lowered: dict[str, AttributeValue] = {}

        if self.category is not SpanCategory.CUSTOM:
            lowered[semconv.SPAN_CATEGORY] = self.category.value
        if self.trace_name:
            lowered[semconv.TRACE_NAME] = self.trace_name
        if self.session_id:
            lowered[semconv.SESSION_ID] = self.session_id
        if self.subject_id:
            lowered[semconv.SUBJECT_ID] = self.subject_id
        if self.tags:
            lowered[semconv.TAGS] = list(self.tags)

        if self.usage is not None:
            usage = self.usage
            pairs: tuple[tuple[str, AttributeValue], ...] = (
                (semconv.USAGE_INPUT_TOKENS, usage.input_tokens),
                (semconv.USAGE_OUTPUT_TOKENS, usage.output_tokens),
                (semconv.USAGE_TOTAL_TOKENS, usage.total_tokens),
                (semconv.USAGE_CACHED_INPUT_TOKENS, usage.cached_input_tokens),
                (semconv.USAGE_CACHE_WRITE_TOKENS, usage.cache_write_tokens),
                (semconv.USAGE_REASONING_TOKENS, usage.reasoning_tokens),
                (semconv.USAGE_AUDIO_INPUT_SECONDS, usage.audio_input_seconds),
                (semconv.USAGE_AUDIO_OUTPUT_SECONDS, usage.audio_output_seconds),
                (semconv.USAGE_IMAGE_INPUT_COUNT, usage.image_input_count),
                (semconv.USAGE_IMAGE_OUTPUT_COUNT, usage.image_output_count),
                (semconv.USAGE_SOURCE, usage.source.value),
                # Mirror onto the upstream OTel keys so third-party backends and
                # collector processors see conventional usage too.
                (semconv.GEN_AI_USAGE_INPUT_TOKENS, usage.input_tokens),
                (semconv.GEN_AI_USAGE_OUTPUT_TOKENS, usage.output_tokens),
            )
            for key, item in pairs:
                if item is not None:
                    lowered[key] = item
            if usage.raw is not None:
                lowered[semconv.USAGE_RAW] = _json.dumps(usage.raw, separators=(",", ":"))

        if self.retrieval is not None:
            retrieval = self.retrieval
            simple: tuple[tuple[str, AttributeValue], ...] = (
                (semconv.RETRIEVAL_QUERY, retrieval.query),
                (semconv.RETRIEVAL_REWRITTEN_QUERY, retrieval.rewritten_query),
                (semconv.RETRIEVAL_RETRIEVER_NAME, retrieval.retriever_name),
                (semconv.RETRIEVAL_RETRIEVER_VERSION, retrieval.retriever_version),
                (semconv.KNOWLEDGE_BASE_VERSION, retrieval.knowledge_base_version),
                (semconv.RETRIEVAL_SEARCH_TYPE, retrieval.search_type),
                (semconv.RETRIEVAL_TOP_K, retrieval.top_k),
                (semconv.RETRIEVAL_EMBEDDING_MODEL, retrieval.embedding_model),
                (semconv.RETRIEVAL_EMBEDDING_DIMENSIONS, retrieval.embedding_dimensions),
                (semconv.RETRIEVAL_EMBEDDING_LATENCY_MS, retrieval.embedding_latency_ms),
                (semconv.RETRIEVAL_RERANKER_MODEL, retrieval.reranker_model),
                (semconv.RETRIEVAL_RERANKER_LATENCY_MS, retrieval.reranker_latency_ms),
                (semconv.RETRIEVAL_LATENCY_MS, retrieval.retrieval_latency_ms),
                (semconv.RETRIEVAL_CONTEXT_TOKENS, retrieval.context_tokens),
            )
            for key, item in simple:
                if item is not None:
                    lowered[key] = item
            lowered[semconv.RETRIEVAL_RESULT_COUNT] = len(retrieval.documents)
            lowered[semconv.RETRIEVAL_CONTEXT_TRUNCATED] = retrieval.context_truncated
            if retrieval.filters:
                lowered[semconv.RETRIEVAL_FILTERS] = _json.dumps(
                    retrieval.filters, separators=(",", ":"), sort_keys=True
                )
            if retrieval.documents:
                lowered[semconv.RETRIEVAL_DOCUMENTS] = _json.dumps(
                    [document.model_dump(mode="json") for document in retrieval.documents],
                    separators=(",", ":"),
                )

        if self.agent_step is not None:
            step = self.agent_step
            simple = (
                (semconv.AGENT_ID, step.agent_id),
                (semconv.AGENT_VERSION, step.agent_version),
                (semconv.AGENT_GOAL, step.goal),
                (semconv.AGENT_STEP_NUMBER, step.step_number),
                (semconv.AGENT_STEP_PARENT, step.parent_step),
                (semconv.AGENT_STEP_TYPE, step.step_type),
                (semconv.AGENT_DECISION_SUMMARY, step.decision_summary),
                (semconv.AGENT_TOOL_NAME, step.tool_name),
                (semconv.AGENT_TOOL_RESULT_REF, step.tool_result_ref),
                (semconv.AGENT_TOOL_STATUS, step.tool_status),
                (semconv.AGENT_HANDOFF_TARGET, step.handoff_target),
                (semconv.AGENT_RETRY_OF, step.retry_of),
                (semconv.AGENT_BRANCH_ID, step.branch_id),
                (semconv.AGENT_LOOP_ITERATION, step.loop_iteration),
                (semconv.AGENT_APPROVAL_STATUS, step.approval_status),
                (semconv.AGENT_TERMINATION_REASON, step.termination_reason),
                (semconv.AGENT_MAX_STEPS, step.max_steps),
            )
            for key, item in simple:
                if item is not None:
                    lowered[key] = item
            lowered[semconv.AGENT_APPROVAL_REQUIRED] = step.approval_required
            if step.tool_name:
                lowered[semconv.GEN_AI_TOOL_NAME] = step.tool_name
            if step.tool_arguments is not None:
                lowered[semconv.AGENT_TOOL_ARGUMENTS] = _json.dumps(
                    step.tool_arguments, separators=(",", ":"), sort_keys=True
                )
            if step.memory_read_keys:
                lowered[semconv.AGENT_MEMORY_READ_KEYS] = list(step.memory_read_keys)
            if step.memory_write_keys:
                lowered[semconv.AGENT_MEMORY_WRITE_KEYS] = list(step.memory_write_keys)

        if self.lineage is not None:
            lineage = self.lineage
            simple = (
                (semconv.PROMPT_NAME, lineage.prompt_name),
                (semconv.PROMPT_VERSION_ID, lineage.prompt_version_id),
                (semconv.PROMPT_VERSION_LABEL, lineage.prompt_version_label),
                (semconv.PROMPT_HASH, lineage.prompt_hash),
                (semconv.MODEL_CONFIG_ID, lineage.model_config_id),
                (semconv.MODEL_CONFIG_HASH, lineage.model_config_hash),
                (semconv.DATASET_NAME, lineage.dataset_name),
                (semconv.DATASET_VERSION_ID, lineage.dataset_version_id),
                (semconv.DATASET_RECORD_ID, lineage.dataset_record_id),
                (semconv.KNOWLEDGE_BASE_VERSION, lineage.knowledge_base_version),
                (semconv.EXPERIMENT_ID, lineage.experiment_id),
                (semconv.EXPERIMENT_RUN_ID, lineage.experiment_run_id),
                (semconv.RELEASE, lineage.release),
                (semconv.GIT_COMMIT, lineage.git_commit),
            )
            for key, item in simple:
                if item is not None:
                    lowered[key] = item
            if lineage.prompt_variables is not None:
                lowered[semconv.PROMPT_VARIABLES] = _json.dumps(
                    lineage.prompt_variables, separators=(",", ":"), sort_keys=True
                )

        lowered.update(self.attributes)
        return lowered


class IngestBatch(_Strict):
    """A batch of spans submitted to ``POST /v1/ingest/spans``."""

    resource: ResourceDescriptor
    spans: list[WireSpan]
    #: Client-chosen key making the whole batch retry-safe. Replaying a batch
    #: with the same key returns the original result without re-applying it.
    idempotency_key: str | None = Field(default=None, max_length=128)
    #: Head sampling rate the client applied, used to scale aggregate estimates.
    sampling_rate: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("spans")
    @classmethod
    def _bound_spans(cls, value: list[WireSpan]) -> list[WireSpan]:
        if not value:
            raise ValueError("a batch must contain at least one span")
        if len(value) > Limits.MAX_SPANS_PER_BATCH:
            raise ValueError(f"at most {Limits.MAX_SPANS_PER_BATCH} spans per batch")
        return value


class SpanRejection(_Strict):
    """Why one span in an otherwise-valid batch was not accepted.

    Partial failure is a first-class outcome: one malformed span must never
    discard the 1,999 valid spans it was batched with, and the client needs
    enough detail to fix the bug without guessing.
    """

    span_id: str | None
    index: int = Field(ge=0)
    code: Literal[
        "invalid_span",
        "invalid_trace_id",
        "invalid_span_id",
        "clock_skew",
        "too_old",
        "quota_exceeded",
        "payload_too_large",
        "duplicate",
        "internal_error",
    ]
    message: str


class IngestResponse(_Strict):
    """Result of an ingest request."""

    accepted: int = Field(ge=0)
    rejected: int = Field(ge=0)
    duplicates: int = Field(ge=0)
    batch_id: str
    #: True when this response was replayed from an idempotency record.
    replayed: bool = False
    rejections: list[SpanRejection] = Field(default_factory=list)


#: Convenience alias used by FastAPI route signatures.
IngestBatchBody = Annotated[IngestBatch, Field(description="Batch of spans to ingest")]
