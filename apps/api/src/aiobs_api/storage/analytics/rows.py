"""Row shapes for the analytics store.

These dataclasses are the physical projection of a span onto the columnar
tables. They are deliberately *flat*: a columnar store rewards wide, sparse
rows and punishes nested access, so every field the UI filters or aggregates on
is promoted to its own column instead of living inside the attribute map.

Which fields get promoted is a judgement call with real consequences:

* **Promoted** -- anything that appears in a dashboard ``GROUP BY``, a trace
  explorer filter, or a cost calculation. Reading a dedicated column touches
  one compressed stream; reading a map key decompresses the whole map.
* **Left in ``attributes``** -- long-tail, application-specific and
  high-cardinality keys. Promoting these would create thousands of mostly-null
  columns, which is how a columnar schema turns into a slow one.

The ``attributes`` map is still queryable (``filter=attributes.my.key:eq:x``),
just at a higher cost, and that cost asymmetry is documented so users understand
why filtering on ``model`` is instant and filtering on a custom attribute is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

__all__ = [
    "AgentStepRow",
    "AnalyticsScope",
    "CostRecordRow",
    "RetrievalDocumentRow",
    "SpanEventRow",
    "SpanRow",
    "TraceRow",
]


@dataclass(frozen=True, slots=True)
class AnalyticsScope:
    """Mandatory tenancy predicate for every analytics query.

    There is no query method on the store that does not take one of these. That
    is the single mechanical guarantee that a trace belonging to one tenant
    cannot surface in another's results: a caller physically cannot express the
    unscoped query.
    """

    organization_id: str
    project_id: str | None = None
    environment: str | None = None

    def __post_init__(self) -> None:
        if not self.organization_id:
            raise ValueError("AnalyticsScope requires a non-empty organization_id")


@dataclass(slots=True)
class SpanRow:
    """One span as stored in the analytics table."""

    # --- identity and tenancy ---------------------------------------------
    organization_id: str
    project_id: str
    environment: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: str
    category: str

    # --- timing ------------------------------------------------------------
    start_unix_nano: int
    end_unix_nano: int | None = None
    duration_ns: int | None = None

    # --- outcome -----------------------------------------------------------
    status: str = "unset"
    status_message: str = ""
    error_type: str = ""
    error_message: str = ""

    # --- origin ------------------------------------------------------------
    service_name: str = ""
    service_version: str = ""
    service_instance_id: str = ""
    sdk_name: str = ""
    sdk_version: str = ""

    # --- trace level (denormalised onto every span for cheap filtering) ----
    session_id: str = ""
    subject_id: str = ""
    release: str = ""
    git_commit: str = ""
    tags: list[str] = field(default_factory=list)

    # --- model ---------------------------------------------------------
    provider: str = ""
    model: str = ""
    model_family: str = ""

    # --- lineage -----------------------------------------------------------
    prompt_name: str = ""
    prompt_version_id: str = ""
    model_config_id: str = ""
    dataset_version_id: str = ""
    knowledge_base_version: str = ""
    experiment_run_id: str = ""

    # --- usage -------------------------------------------------------------
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    usage_source: str = "missing"

    # --- cost --------------------------------------------------------------
    cost_total: Decimal | None = None
    cost_currency: str = ""
    cost_estimation_status: str = "unpriced"
    price_book_version: str = ""

    # --- latency breakdown --------------------------------------------------
    time_to_first_token_ms: float | None = None
    queue_ms: float | None = None
    provider_ms: float | None = None

    # --- agent / retrieval quick filters ------------------------------------
    agent_id: str = ""
    tool_name: str = ""
    tool_status: str = ""
    retriever_name: str = ""
    retrieval_result_count: int | None = None

    # --- payloads ------------------------------------------------------------
    input_preview: str = ""
    output_preview: str = ""
    input_ref: str = ""
    output_ref: str = ""

    # --- long tail -----------------------------------------------------------
    attributes: dict[str, Any] = field(default_factory=dict)
    #: Serialised span links; too rare to justify their own table.
    links: list[dict[str, Any]] = field(default_factory=list)

    # --- ingestion bookkeeping -----------------------------------------------
    sampling_rate: float = 1.0
    ingested_at: datetime | None = None
    #: Monotonic version used by ReplacingMergeTree to collapse duplicates. A
    #: re-ingested span with a higher version wins; an identical replay loses.
    ingest_version: int = 0
    #: Hash of the normalised span content; two rows with the same hash are the
    #: same observation and must never be counted twice.
    content_hash: str = ""
    #: True when the span arrived outside the normal ingestion window.
    late_arrival: bool = False

    @property
    def is_root(self) -> bool:
        return not self.parent_span_id

    @property
    def duration_ms(self) -> float | None:
        return None if self.duration_ns is None else self.duration_ns / 1_000_000


@dataclass(slots=True)
class TraceRow:
    """Roll-up of a whole logical AI request.

    Recomputed from that trace's spans whenever any of them is (re-)ingested,
    rather than incrementally updated. Recomputation is idempotent, which means
    a duplicate delivery, a late-arriving span and a replay from the dead-letter
    queue all converge on the same numbers -- an incremental ``+=`` would
    double-count on every one of those paths.
    """

    organization_id: str
    project_id: str
    environment: str
    trace_id: str
    name: str
    start_unix_nano: int
    end_unix_nano: int | None = None
    duration_ns: int | None = None
    status: str = "incomplete"
    error_summary: str = ""
    root_span_id: str = ""
    span_count: int = 0
    error_count: int = 0

    session_id: str = ""
    subject_id: str = ""
    release: str = ""
    git_commit: str = ""
    tags: list[str] = field(default_factory=list)

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cached_input_tokens: int = 0
    total_reasoning_tokens: int = 0
    usage_source: str = "missing"

    total_cost: Decimal | None = None
    cost_currency: str = ""
    cost_estimation_status: str = "unpriced"

    time_to_first_token_ms: float | None = None

    models: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    prompt_version_ids: list[str] = field(default_factory=list)
    model_config_ids: list[str] = field(default_factory=list)
    dataset_version_ids: list[str] = field(default_factory=list)
    service_names: list[str] = field(default_factory=list)

    llm_call_count: int = 0
    retrieval_count: int = 0
    tool_call_count: int = 0
    agent_step_count: int = 0

    sdk_name: str = ""
    sdk_version: str = ""
    sampling_rate: float = 1.0
    ingested_at: datetime | None = None
    ingest_version: int = 0
    #: True until a root span has been observed and every span has an end time.
    complete: bool = False

    #: Transient, never stored. When a late-arriving span moves a trace's start
    #: time, the previous roll-up occupies a different position in ClickHouse's
    #: sorting key and would survive as a duplicate; the driver uses this to
    #: delete the superseded row. SQLite's natural key is (org, trace_id), so it
    #: overwrites in place and ignores the field.
    previous_start_unix_nano: int | None = None


@dataclass(slots=True)
class SpanEventRow:
    """A timestamped event inside a span."""

    organization_id: str
    project_id: str
    environment: str
    trace_id: str
    span_id: str
    time_unix_nano: int
    name: str
    sequence: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    ingested_at: datetime | None = None


@dataclass(slots=True)
class RetrievalDocumentRow:
    """One retrieved document/chunk, exploded out of its retrieval span.

    Stored in its own table rather than inside the span's attributes because
    the retrieval diagnostics -- score distributions, rank movement, unused
    chunks -- are aggregate questions across documents, and answering them from
    a JSON blob would mean parsing every span in the range.
    """

    organization_id: str
    project_id: str
    environment: str
    trace_id: str
    span_id: str
    time_unix_nano: int
    document_id: str
    rank: int
    chunk_id: str = ""
    score: float | None = None
    rerank_score: float | None = None
    rerank_rank: int | None = None
    selected: bool = False
    token_count: int | None = None
    truncated: bool = False
    source: str = ""
    title: str = ""
    content_preview: str = ""
    content_ref: str = ""
    retriever_name: str = ""
    knowledge_base_version: str = ""
    embedding_model: str = ""
    search_type: str = ""
    query: str = ""
    rewritten_query: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    ingested_at: datetime | None = None

    @property
    def rank_delta(self) -> int | None:
        """Positions gained (positive) or lost (negative) during reranking."""
        if self.rerank_rank is None:
            return None
        return self.rank - self.rerank_rank


@dataclass(slots=True)
class AgentStepRow:
    """One step of an agent trajectory."""

    organization_id: str
    project_id: str
    environment: str
    trace_id: str
    span_id: str
    agent_id: str
    step_number: int
    start_unix_nano: int
    duration_ns: int | None = None
    agent_version: str = ""
    goal: str = ""
    parent_step: int | None = None
    step_type: str = "observation"
    decision_summary: str = ""
    tool_name: str = ""
    tool_status: str = ""
    tool_result_ref: str = ""
    handoff_target: str = ""
    memory_read_keys: list[str] = field(default_factory=list)
    memory_write_keys: list[str] = field(default_factory=list)
    retry_of: int | None = None
    branch_id: str = ""
    loop_iteration: int | None = None
    approval_required: bool = False
    approval_status: str = ""
    termination_reason: str = ""
    max_steps: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_total: Decimal | None = None
    status: str = "unset"
    error_message: str = ""
    ingested_at: datetime | None = None


@dataclass(slots=True)
class CostRecordRow:
    """An auditable cost calculation for one span.

    Every field needed to *re-derive* the number is stored: the usage inputs,
    the price-book version, the per-component breakdown and the formula. A cost
    figure you cannot reconstruct six months later is not auditable, and the
    first question anyone asks about an unexpected bill is "how was this
    computed?".
    """

    organization_id: str
    project_id: str
    environment: str
    trace_id: str
    span_id: str
    time_unix_nano: int
    provider: str
    model: str
    currency: str
    total: Decimal
    price_book_id: str = ""
    price_book_version: str = ""
    estimation_status: str = "final"
    usage_source: str = "provider"
    #: [{"category": "input_tokens", "quantity": 1200, "unit_quantity": 1000000,
    #:   "unit_price": "3.00", "amount": "0.0036"}]
    components: list[dict[str, Any]] = field(default_factory=list)
    #: Human readable derivation, e.g. "1200/1000000*3.00 + 340/1000000*15.00".
    formula: str = ""
    prompt_version_id: str = ""
    model_config_id: str = ""
    session_id: str = ""
    subject_id: str = ""
    ingested_at: datetime | None = None
