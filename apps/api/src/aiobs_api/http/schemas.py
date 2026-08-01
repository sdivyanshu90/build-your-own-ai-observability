"""Request and response models for the REST API.

Response models are declared explicitly rather than serialising ORM or row
objects directly. That costs a mapping function per resource and buys three
things: the OpenAPI schema is accurate, an internal column added tomorrow does
not silently appear in a public response, and money is rendered as a string
instead of a lossy JSON number.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from ..core.timeutil import unix_nano_to_datetime

__all__ = [
    "AgentStepOut",
    "ApiKeyCreated",
    "ApiKeyOut",
    "CursorPage",
    "LoginRequest",
    "PromptVersionOut",
    "SpanOut",
    "TokenResponse",
    "TraceDetailOut",
    "TraceOut",
]

T = TypeVar("T")


class _Model(BaseModel):
    # 'model_' is a protected namespace in Pydantic v2, and the domain genuinely
    # uses model_config_id / model_versions. Clearing the namespace is safer
    # than renaming domain concepts to satisfy a library default.
    model_config = ConfigDict(protected_namespaces=(), extra="forbid")


class CursorPage(_Model, Generic[T]):
    """Uniform pagination envelope.

    No total count: see :class:`aiobs_api.core.query.Page` for why.
    """

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


class LoginRequest(_Model):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)
    organization_id: str | None = Field(
        default=None,
        description="Required when the user belongs to more than one organization.",
    )


class TokenResponse(_Model):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    organization_id: str
    role: str


class RefreshRequest(_Model):
    refresh_token: str


class UserOut(_Model):
    id: str
    email: str
    display_name: str
    is_active: bool
    last_login_at: datetime | None = None


class MembershipOut(_Model):
    user: UserOut
    role: str
    project_scope: list[str] = Field(default_factory=list)
    created_at: datetime


class OrganizationOut(_Model):
    id: str
    slug: str
    name: str
    role: str | None = None
    max_spans_per_day: int
    max_projects: int
    created_at: datetime


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------


class EnvironmentOut(_Model):
    id: str
    name: str
    is_production: bool
    settings: dict[str, Any] = Field(default_factory=dict)


class ProjectOut(_Model):
    id: str
    slug: str
    name: str
    description: str | None = None
    default_sampling_rate: float
    environments: list[EnvironmentOut] = Field(default_factory=list)
    created_at: datetime


class ProjectCreate(_Model):
    name: str = Field(min_length=1, max_length=256)
    slug: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=2_000)


# ---------------------------------------------------------------------------
# api keys
# ---------------------------------------------------------------------------


class ApiKeyCreate(_Model):
    name: str = Field(min_length=1, max_length=256)
    project_id: str
    environment_id: str
    scopes: list[str] = Field(default_factory=lambda: ["ingest"])
    expires_in_days: int | None = Field(default=None, ge=1, le=3_650)


class ApiKeyOut(_Model):
    id: str
    name: str
    prefix: str
    project_id: str
    environment_id: str
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


class ApiKeyCreated(ApiKeyOut):
    """Returned once, at creation. ``secret`` is unrecoverable afterwards."""

    secret: str = Field(description="Shown exactly once. Store it now; it cannot be retrieved.")


# ---------------------------------------------------------------------------
# traces
# ---------------------------------------------------------------------------


class TraceOut(_Model):
    trace_id: str
    name: str
    environment: str
    status: str
    start_time: datetime
    end_time: datetime | None = None
    duration_ms: float | None = None
    span_count: int
    error_count: int
    session_id: str = ""
    subject_id: str = ""
    release: str = ""
    tags: list[str] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    usage_source: str = "missing"
    cost: str | None = Field(default=None, description="Decimal string; never a float.")
    cost_currency: str = ""
    cost_status: str = "unpriced"
    time_to_first_token_ms: float | None = None
    models: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    prompt_version_ids: list[str] = Field(default_factory=list)
    model_config_ids: list[str] = Field(default_factory=list)
    dataset_version_ids: list[str] = Field(default_factory=list)
    service_names: list[str] = Field(default_factory=list)
    llm_call_count: int = 0
    retrieval_count: int = 0
    tool_call_count: int = 0
    agent_step_count: int = 0
    complete: bool = False

    @classmethod
    def from_row(cls, row: Any) -> TraceOut:
        return cls(
            trace_id=row.trace_id,
            name=row.name,
            environment=row.environment,
            status=row.status,
            start_time=unix_nano_to_datetime(row.start_unix_nano),
            end_time=(
                None if row.end_unix_nano is None else unix_nano_to_datetime(row.end_unix_nano)
            ),
            duration_ms=None if row.duration_ns is None else row.duration_ns / 1e6,
            span_count=row.span_count,
            error_count=row.error_count,
            session_id=row.session_id,
            subject_id=row.subject_id,
            release=row.release,
            tags=list(row.tags),
            input_tokens=row.total_input_tokens,
            output_tokens=row.total_output_tokens,
            total_tokens=row.total_tokens,
            cached_input_tokens=row.total_cached_input_tokens,
            usage_source=row.usage_source,
            cost=None if row.total_cost is None else str(row.total_cost),
            cost_currency=row.cost_currency,
            cost_status=row.cost_estimation_status,
            time_to_first_token_ms=row.time_to_first_token_ms,
            models=list(row.models),
            providers=list(row.providers),
            prompt_version_ids=list(row.prompt_version_ids),
            model_config_ids=list(row.model_config_ids),
            dataset_version_ids=list(row.dataset_version_ids),
            service_names=list(row.service_names),
            llm_call_count=row.llm_call_count,
            retrieval_count=row.retrieval_count,
            tool_call_count=row.tool_call_count,
            agent_step_count=row.agent_step_count,
            complete=row.complete,
        )


class SpanOut(_Model):
    span_id: str
    trace_id: str
    parent_span_id: str | None = None
    name: str
    kind: str
    category: str
    status: str
    status_message: str = ""
    error_type: str = ""
    error_message: str = ""
    start_time: datetime
    end_time: datetime | None = None
    duration_ms: float | None = None
    self_time_ms: float | None = None
    on_critical_path: bool = False
    service_name: str = ""
    service_version: str = ""
    provider: str = ""
    model: str = ""
    prompt_name: str = ""
    prompt_version_id: str = ""
    model_config_id: str = ""
    dataset_version_id: str = ""
    knowledge_base_version: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    usage_source: str = "missing"
    cost: str | None = None
    cost_currency: str = ""
    cost_status: str = "unpriced"
    time_to_first_token_ms: float | None = None
    agent_id: str = ""
    tool_name: str = ""
    tool_status: str = ""
    retriever_name: str = ""
    input_preview: str = ""
    output_preview: str = ""
    input_ref: str = ""
    output_ref: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    links: list[dict[str, Any]] = Field(default_factory=list)
    late_arrival: bool = False

    @classmethod
    def from_row(
        cls,
        row: Any,
        *,
        self_time_ns: int | None = None,
        on_critical_path: bool = False,
    ) -> SpanOut:
        return cls(
            span_id=row.span_id,
            trace_id=row.trace_id,
            # The analytics column stores "" for a root span (columnar stores
            # dislike nullable strings); the API contract is null.
            parent_span_id=row.parent_span_id or None,
            name=row.name,
            kind=row.kind,
            category=row.category,
            status=row.status,
            status_message=row.status_message,
            error_type=row.error_type,
            error_message=row.error_message,
            start_time=unix_nano_to_datetime(row.start_unix_nano),
            end_time=(
                None if row.end_unix_nano is None else unix_nano_to_datetime(row.end_unix_nano)
            ),
            duration_ms=row.duration_ms,
            self_time_ms=None if self_time_ns is None else self_time_ns / 1e6,
            on_critical_path=on_critical_path,
            service_name=row.service_name,
            service_version=row.service_version,
            provider=row.provider,
            model=row.model,
            prompt_name=row.prompt_name,
            prompt_version_id=row.prompt_version_id,
            model_config_id=row.model_config_id,
            dataset_version_id=row.dataset_version_id,
            knowledge_base_version=row.knowledge_base_version,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            total_tokens=row.total_tokens,
            cached_input_tokens=row.cached_input_tokens,
            reasoning_tokens=row.reasoning_tokens,
            usage_source=row.usage_source,
            cost=None if row.cost_total is None else str(row.cost_total),
            cost_currency=row.cost_currency,
            cost_status=row.cost_estimation_status,
            time_to_first_token_ms=row.time_to_first_token_ms,
            agent_id=row.agent_id,
            tool_name=row.tool_name,
            tool_status=row.tool_status,
            retriever_name=row.retriever_name,
            input_preview=row.input_preview,
            output_preview=row.output_preview,
            input_ref=row.input_ref,
            output_ref=row.output_ref,
            attributes=dict(row.attributes),
            links=list(row.links),
            late_arrival=row.late_arrival,
        )


class SpanEventOut(_Model):
    span_id: str
    name: str
    time: datetime
    sequence: int
    attributes: dict[str, Any] = Field(default_factory=dict)


class CostRecordOut(_Model):
    span_id: str
    provider: str
    model: str
    currency: str
    total: str
    price_book_version: str
    estimation_status: str
    usage_source: str
    components: list[dict[str, Any]] = Field(default_factory=list)
    formula: str = ""


class TraceDetailOut(_Model):
    trace: TraceOut
    spans: list[SpanOut]
    events: list[SpanEventOut]
    cost_records: list[CostRecordOut]
    critical_path: list[str]
    children: dict[str, list[str]]
    orphan_span_ids: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    retry_groups: dict[str, list[str]] = Field(default_factory=dict)


class RetrievalDocumentOut(_Model):
    document_id: str
    chunk_id: str = ""
    rank: int
    score: float | None = None
    rerank_score: float | None = None
    rerank_rank: int | None = None
    rank_delta: int | None = None
    selected: bool = False
    token_count: int | None = None
    truncated: bool = False
    source: str = ""
    title: str = ""
    content_preview: str = ""
    content_ref: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalStageOut(_Model):
    span_id: str
    span_name: str
    query: str
    rewritten_query: str
    retriever_name: str
    knowledge_base_version: str
    embedding_model: str
    search_type: str
    latency_ms: float | None = None
    embedding_latency_ms: float | None = None
    reranker_latency_ms: float | None = None
    reranker_model: str = ""
    stages: list[dict[str, Any]] = Field(default_factory=list)
    documents: list[RetrievalDocumentOut] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class AgentStepOut(_Model):
    span_id: str
    agent_id: str
    agent_version: str = ""
    step_number: int
    parent_step: int | None = None
    step_type: str
    decision_summary: str = ""
    tool_name: str = ""
    tool_status: str = ""
    handoff_target: str = ""
    retry_of: int | None = None
    branch_id: str = ""
    loop_iteration: int | None = None
    approval_required: bool = False
    approval_status: str = ""
    termination_reason: str = ""
    duration_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: str | None = None
    status: str = "unset"
    error_message: str = ""


class TrajectoryOut(_Model):
    graph: dict[str, Any]
    steps: list[AgentStepOut]


# ---------------------------------------------------------------------------
# registries
# ---------------------------------------------------------------------------


class PromptCreate(_Model):
    project_id: str
    name: str = Field(min_length=1, max_length=256)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class PromptOut(_Model):
    id: str
    project_id: str
    name: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime


class PromptVersionCreate(_Model):
    messages: list[dict[str, Any]] = Field(min_length=1)
    variable_schema: dict[str, Any] = Field(default_factory=dict)
    default_variables: dict[str, Any] = Field(default_factory=dict)
    template_engine: str = "fstring"
    commit_message: str | None = Field(default=None, max_length=2_000)
    label: str | None = Field(default=None, max_length=128)
    publish: bool = True


class PromptVersionOut(_Model):
    id: str
    prompt_id: str
    version_number: int
    label: str
    content_hash: str
    messages: list[dict[str, Any]]
    variable_schema: dict[str, Any] = Field(default_factory=dict)
    default_variables: dict[str, Any] = Field(default_factory=dict)
    template_engine: str
    release_stage: str
    parent_version_id: str | None = None
    commit_message: str | None = None
    created_at: datetime
    published_at: datetime | None = None


class PromptAliasOut(_Model):
    name: str
    version_id: str
    previous_version_id: str | None = None
    promoted_at: datetime


class AliasPromoteRequest(_Model):
    alias: str = Field(min_length=1, max_length=64)
    version_id: str


class ModelVersionCreate(_Model):
    provider: str = Field(min_length=1, max_length=64)
    model_identifier: str = Field(min_length=1, max_length=256)
    project_id: str | None = None
    family: str | None = None
    endpoint_kind: str = "chat"
    config: dict[str, Any] = Field(default_factory=dict)


class ModelOut(_Model):
    id: str
    provider: str
    model_identifier: str
    family: str | None = None
    endpoint_kind: str
    created_at: datetime


class ModelVersionOut(_Model):
    id: str
    model_id: str
    version_number: int
    label: str
    config_hash: str
    deployment_name: str | None = None
    region: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    max_tokens: int | None = None
    stop_sequences: list[str] = Field(default_factory=list)
    seed: int | None = None
    tool_config: dict[str, Any] = Field(default_factory=dict)
    response_format: dict[str, Any] = Field(default_factory=dict)
    safety_settings: dict[str, Any] = Field(default_factory=dict)
    provider_extras: dict[str, Any] = Field(default_factory=dict)
    system_fingerprint: str | None = None
    created_at: datetime


class DatasetCreate(_Model):
    project_id: str
    name: str = Field(min_length=1, max_length=256)
    description: str | None = None
    license: str | None = None
    contains_sensitive_data: bool = True
    tags: list[str] = Field(default_factory=list)


class DatasetOut(_Model):
    id: str
    project_id: str
    name: str
    description: str | None = None
    license: str | None = None
    contains_sensitive_data: bool
    tags: list[str] = Field(default_factory=list)
    created_at: datetime


class DatasetVersionOut(_Model):
    id: str
    dataset_id: str
    version_number: int
    label: str
    dataset_hash: str
    row_count: int
    total_bytes: int
    record_schema: dict[str, Any] = Field(default_factory=dict)
    splits: dict[str, Any] = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)
    quality_summary: dict[str, Any] = Field(default_factory=dict)
    parent_version_id: str | None = None
    created_at: datetime
    payload_deleted_at: datetime | None = None


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------


class PriceEntryIn(_Model):
    provider: str
    model_identifier: str
    usage_category: str
    unit_price: Decimal
    effective_from: datetime
    unit_quantity: int = 1_000_000
    currency: str = Field(default="USD", min_length=3, max_length=3)
    effective_to: datetime | None = None
    tier_min_units: int = 0
    tier_max_units: int | None = None
    discount_rate: Decimal | None = None
    source_url: str | None = None
    notes: str | None = None


class PriceBookCreate(_Model):
    version: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=256)
    description: str | None = None
    source: str | None = None
    currency: str = Field(default="USD", min_length=3, max_length=3)
    scope: str = Field(
        default="organization",
        description="'organization' for negotiated rates, 'public' for the platform list.",
    )
    entries: list[PriceEntryIn] = Field(default_factory=list)


class PriceBookOut(_Model):
    id: str
    organization_id: str | None = None
    version: str
    name: str
    description: str | None = None
    currency: str
    is_active: bool
    published_at: datetime
    frozen_at: datetime | None = None
    entry_count: int = 0


class PriceEntryOut(_Model):
    id: str
    provider: str
    model_identifier: str
    usage_category: str
    unit_quantity: int
    unit_price: str
    currency: str
    effective_from: datetime
    effective_to: datetime | None = None
    tier_min_units: int
    tier_max_units: int | None = None
    discount_rate: str | None = None
    source_url: str | None = None


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------


class RetentionPolicyIn(_Model):
    raw_span_days: int = Field(ge=1, le=3_650)
    aggregate_days: int = Field(ge=1, le=3_650)
    payload_days: int = Field(ge=1, le=3_650)
    environment_id: str | None = None
    purge_on_expiry: bool = True


class RetentionPolicyOut(_Model):
    id: str
    project_id: str
    environment_id: str | None = None
    raw_span_days: int
    aggregate_days: int
    payload_days: int
    purge_on_expiry: bool
    updated_at: datetime


class AuditEventOut(_Model):
    id: str
    occurred_at: datetime
    action: str
    actor_id: str | None = None
    actor_type: str
    actor_label: str | None = None
    resource_type: str
    resource_id: str | None = None
    project_id: str | None = None
    outcome: str
    request_id: str | None = None
    client_ip: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExportCreate(_Model):
    project_id: str
    resource: str = Field(pattern="^(traces|spans|costs)$")
    format: str = Field(default="jsonl", pattern="^(jsonl|csv|json)$")
    environment: str | None = None
    include_payloads: bool = False


class ExportOut(_Model):
    id: str
    project_id: str
    resource: str
    format: str
    status: str
    redacted: bool
    row_count: int
    size_bytes: int
    error_message: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    expires_at: datetime | None = None
    download_url: str | None = None


class HealthOut(_Model):
    status: str
    version: str
    git_commit: str
    environment: str
    checks: dict[str, str] = Field(default_factory=dict)
    configuration: dict[str, Any] = Field(default_factory=dict)
