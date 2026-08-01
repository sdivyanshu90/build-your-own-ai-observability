"""Span normalisation: wire format in, storage rows out.

This is where a span stops being someone else's data and becomes ours. Both
ingestion paths -- OTLP and the native batch endpoint -- converge here, which is
what makes them behave identically.

The normaliser is a **pure function of its inputs**. It performs no I/O, so it
is trivially testable and can run inside the API process (for validation) and
again inside the worker (for enrichment) without coordination.

Canonical decisions made here, each of which has a documented rationale in
``docs/architecture/ingestion-pipeline.md``:

* **Clock skew.** A span starting more than ``max_clock_skew_future_seconds``
  ahead of the server is rejected: it would sort into the future forever and
  poison every "last hour" query. A span *older* than the backfill window is
  accepted but flagged ``late_arrival``, because rejecting genuine backfill
  loses data an operator deliberately sent.
* **Missing end time.** Kept as an open span with ``duration_ns = None``, not
  as a zero-duration span. Zero would silently drag every latency percentile
  down; ``None`` is excluded from percentile calculations by construction.
* **Unknown attributes.** Preserved in the attribute map, never dropped. The
  long tail is where application-specific debugging value lives.
* **Server-authoritative tenancy.** ``organization_id``, ``project_id`` and
  ``environment`` come from the authenticated credential, never from the
  payload. A client that sets ``aiobs.tenant.id`` is ignored -- otherwise the
  attribute would be a tenant-hopping primitive.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aiobs_schemas import semconv
from aiobs_schemas.canonical import content_hash
from aiobs_schemas.enums import SpanCategory, SpanKind, SpanStatus, UsageSource
from aiobs_schemas.wire import ResourceDescriptor, WireSpan

from ..core.timeutil import (
    Clock,
    datetime_to_unix_nano,
    is_plausible_unix_nano,
)
from ..domain.redaction import Redactor
from ..domain.usage import CacheConvention, NormalizedUsage, estimate_tokens
from ..storage.analytics.rows import (
    AgentStepRow,
    RetrievalDocumentRow,
    SpanEventRow,
    SpanRow,
)

__all__ = [
    "IngestScope",
    "NormalizationError",
    "NormalizedSpan",
    "SpanNormalizer",
]


@dataclass(frozen=True, slots=True)
class IngestScope:
    """Server-authoritative destination for a batch of spans.

    Resolved from the API key, never from the payload. This is the single most
    important line of defence in the ingestion path: it makes cross-tenant
    writes structurally impossible rather than merely validated against.
    """

    organization_id: str
    project_id: str
    environment: str
    environment_id: str
    api_key_id: str | None = None
    sampling_rate: float = 1.0
    #: Payloads are stored only when the environment permits it.
    store_payloads: bool = True


class NormalizationError(ValueError):
    """A span could not be normalised. Carries a machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class NormalizedSpan:
    """A span plus every derived row it produces."""

    span: SpanRow
    events: list[SpanEventRow] = field(default_factory=list)
    retrieval_documents: list[RetrievalDocumentRow] = field(default_factory=list)
    agent_steps: list[AgentStepRow] = field(default_factory=list)
    usage: NormalizedUsage = field(default_factory=NormalizedUsage)
    #: Payloads to offload to object storage: ``(kind, bytes)`` keyed by field.
    payloads: dict[str, bytes] = field(default_factory=dict)

    @property
    def trace_id(self) -> str:
        return self.span.trace_id


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any, limit: int = 1024) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text[:limit]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value][:64]
    if isinstance(value, str):
        return [value]
    return []


def _json_or_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return None


class SpanNormalizer:
    """Converts wire spans into analytics rows."""

    __slots__ = ("_clock", "_max_backfill_ns", "_max_future_skew_ns", "_preview_chars", "_redactor")

    def __init__(
        self,
        *,
        clock: Clock,
        redactor: Redactor,
        max_clock_skew_future_seconds: int = 300,
        max_backfill_age_seconds: int = 7 * 86_400,
        preview_chars: int = 2_048,
    ) -> None:
        self._clock = clock
        self._redactor = redactor
        self._max_future_skew_ns = max_clock_skew_future_seconds * 1_000_000_000
        self._max_backfill_ns = max_backfill_age_seconds * 1_000_000_000
        self._preview_chars = preview_chars

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------

    def normalize(
        self,
        wire: WireSpan,
        resource: ResourceDescriptor,
        scope: IngestScope,
        *,
        ingest_version: int | None = None,
    ) -> NormalizedSpan:
        """Normalise one wire span. Raises :class:`NormalizationError` on rejection."""
        now = self._clock.now()
        now_nano = datetime_to_unix_nano(now)

        start_nano = self._validate_timestamp(wire.start_time_unix_nano, now_nano)
        end_nano = (
            None
            if wire.end_time_unix_nano is None
            else self._validate_end(wire.end_time_unix_nano, start_nano)
        )
        late = start_nano < now_nano - self._max_backfill_ns

        # Two views of the attributes exist, and the distinction is important.
        #
        # `raw` is what the client sent. The structured payloads that become
        # retrieval-document and agent-step rows live here, and several of them
        # are registered as sensitive -- so extracting the derived rows from the
        # *redacted* map would silently produce zero documents and zero steps.
        # Those rows apply their own field-level redaction as they are built.
        #
        # `stored` is the redacted map that lands in the span's attribute
        # column, which is what an operator browses.
        raw = dict(wire.lowered_attributes())
        redaction = self._redactor.redact_attributes(raw)
        stored = dict(redaction.value)
        if redaction.redacted_keys:
            stored[semconv.REDACTED_KEYS] = sorted(set(redaction.redacted_keys))[:64]

        category = self._resolve_category(wire, raw)
        usage = self._extract_usage(raw)
        span_row = self._build_span_row(
            wire=wire,
            resource=resource,
            scope=scope,
            attributes=stored,
            raw_attributes=raw,
            category=category,
            usage=usage,
            start_nano=start_nano,
            end_nano=end_nano,
            late=late,
            ingested_at=now,
            ingest_version=ingest_version if ingest_version is not None else now_nano,
        )

        result = NormalizedSpan(span=span_row, usage=usage)
        result.events = self._build_events(wire, scope, span_row)
        result.retrieval_documents = self._build_retrieval_documents(raw, scope, span_row, now)
        result.agent_steps = self._build_agent_steps(raw, scope, span_row, now)
        if scope.store_payloads:
            result.payloads = self._collect_payloads(raw)
        return result

    # ------------------------------------------------------------------
    # timestamps
    # ------------------------------------------------------------------

    def _validate_timestamp(self, value: int, now_nano: int) -> int:
        if not is_plausible_unix_nano(value):
            raise NormalizationError(
                "invalid_span",
                f"start_time_unix_nano {value} is outside the plausible range; "
                "the value is probably in seconds or milliseconds, not nanoseconds",
            )
        if value > now_nano + self._max_future_skew_ns:
            drift_seconds = (value - now_nano) / 1e9
            raise NormalizationError(
                "clock_skew",
                f"span starts {drift_seconds:.0f}s in the future; "
                "check the producer's clock synchronisation",
            )
        return value

    def _validate_end(self, value: int, start_nano: int) -> int:
        if not is_plausible_unix_nano(value):
            raise NormalizationError(
                "invalid_span", f"end_time_unix_nano {value} is outside the plausible range"
            )
        if value < start_nano:
            raise NormalizationError(
                "invalid_span", "end_time_unix_nano precedes start_time_unix_nano"
            )
        return value

    # ------------------------------------------------------------------
    # classification
    # ------------------------------------------------------------------

    def _resolve_category(self, wire: WireSpan, attributes: Mapping[str, Any]) -> SpanCategory:
        """Determine the span category, inferring it for third-party OTLP spans.

        A span produced by a generic OpenTelemetry instrumentation will not set
        ``aiobs.span.category``, but it usually sets enough conventional
        attributes to classify. Inferring here means LangChain or vendor
        instrumentation lights up the AI-specific views without any change on
        the producer's side.
        """
        explicit = attributes.get(semconv.SPAN_CATEGORY)
        if isinstance(explicit, str):
            category = SpanCategory.coerce(explicit)
            if category is not SpanCategory.CUSTOM:
                return category
        if wire.category is not SpanCategory.CUSTOM:
            return wire.category

        operation = str(attributes.get(semconv.GEN_AI_OPERATION_NAME) or "").lower()
        if operation in {"chat", "chat.completions", "generate_content"}:
            return SpanCategory.CHAT_COMPLETION
        if operation in {"embeddings", "embedding"}:
            return SpanCategory.EMBEDDING
        if operation in {"text_completion", "completion"}:
            return SpanCategory.LLM_GENERATION
        if operation == "execute_tool" or semconv.GEN_AI_TOOL_NAME in attributes:
            return SpanCategory.TOOL_CALL
        if semconv.RETRIEVAL_QUERY in attributes or semconv.RETRIEVAL_DOCUMENTS in attributes:
            return SpanCategory.RETRIEVAL
        if semconv.RETRIEVAL_RERANKER_MODEL in attributes:
            return SpanCategory.RERANK
        if semconv.AGENT_STEP_NUMBER in attributes:
            return SpanCategory.AGENT_DECISION
        if semconv.GUARDRAIL_NAME in attributes:
            return SpanCategory.GUARDRAIL
        if semconv.GEN_AI_REQUEST_MODEL in attributes:
            return SpanCategory.LLM_GENERATION
        if semconv.DB_SYSTEM in attributes:
            return SpanCategory.DB_QUERY
        if semconv.HTTP_REQUEST_METHOD in attributes:
            return SpanCategory.HTTP_REQUEST
        if semconv.MESSAGING_SYSTEM in attributes:
            return SpanCategory.QUEUE_OPERATION
        return SpanCategory.CUSTOM

    # ------------------------------------------------------------------
    # usage
    # ------------------------------------------------------------------

    def _extract_usage(self, attributes: Mapping[str, Any]) -> NormalizedUsage:
        """Read usage from attributes, preferring platform keys over OTel ones.

        Both namespaces are checked because a span may arrive from our SDK
        (``aiobs.usage.*``), from upstream OTel instrumentation
        (``gen_ai.usage.*``), or from both.
        """
        input_tokens = _as_int(
            attributes.get(semconv.USAGE_INPUT_TOKENS)
            if semconv.USAGE_INPUT_TOKENS in attributes
            else attributes.get(semconv.GEN_AI_USAGE_INPUT_TOKENS)
        )
        output_tokens = _as_int(
            attributes.get(semconv.USAGE_OUTPUT_TOKENS)
            if semconv.USAGE_OUTPUT_TOKENS in attributes
            else attributes.get(semconv.GEN_AI_USAGE_OUTPUT_TOKENS)
        )
        raw = _json_or_none(attributes.get(semconv.USAGE_RAW))
        source_text = str(attributes.get(semconv.USAGE_SOURCE) or "").lower()
        try:
            source = UsageSource(source_text) if source_text else UsageSource.PROVIDER
        except ValueError:
            source = UsageSource.PROVIDER

        convention = CacheConvention.UNKNOWN
        if isinstance(raw, dict):
            declared = str(raw.get("cache_convention") or "").lower()
            if declared in {"inclusive", "exclusive"}:
                convention = CacheConvention(declared)

        usage = NormalizedUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=_as_int(attributes.get(semconv.USAGE_TOTAL_TOKENS)),
            cached_input_tokens=_as_int(attributes.get(semconv.USAGE_CACHED_INPUT_TOKENS)),
            cache_write_tokens=_as_int(attributes.get(semconv.USAGE_CACHE_WRITE_TOKENS)),
            reasoning_tokens=_as_int(attributes.get(semconv.USAGE_REASONING_TOKENS)),
            audio_input_seconds=_as_float(attributes.get(semconv.USAGE_AUDIO_INPUT_SECONDS)),
            audio_output_seconds=_as_float(attributes.get(semconv.USAGE_AUDIO_OUTPUT_SECONDS)),
            image_input_count=_as_int(attributes.get(semconv.USAGE_IMAGE_INPUT_COUNT)),
            image_output_count=_as_int(attributes.get(semconv.USAGE_IMAGE_OUTPUT_COUNT)),
            source=source,
            cache_convention=convention,
            raw=raw if isinstance(raw, dict) else None,
        )
        if usage.is_missing:
            return self._estimate_usage(attributes)
        return usage

    def _estimate_usage(self, attributes: Mapping[str, Any]) -> NormalizedUsage:
        """Fall back to a character-ratio estimate, clearly marked as such.

        Only attempted when a payload is present. An estimate is better than a
        blank for spotting a runaway prompt, but it is always tagged ESTIMATED
        so it never masquerades as a billing-grade number.
        """
        prompt = attributes.get(semconv.INPUT_VALUE)
        completion = attributes.get(semconv.OUTPUT_VALUE)
        if not isinstance(prompt, str) and not isinstance(completion, str):
            return NormalizedUsage(source=UsageSource.MISSING)
        return NormalizedUsage(
            input_tokens=estimate_tokens(prompt if isinstance(prompt, str) else None),
            output_tokens=estimate_tokens(completion if isinstance(completion, str) else None),
            source=UsageSource.ESTIMATED,
            cache_convention=CacheConvention.UNKNOWN,
        )

    # ------------------------------------------------------------------
    # row construction
    # ------------------------------------------------------------------

    def _build_span_row(
        self,
        *,
        wire: WireSpan,
        resource: ResourceDescriptor,
        scope: IngestScope,
        attributes: dict[str, Any],
        raw_attributes: Mapping[str, Any],
        category: SpanCategory,
        usage: NormalizedUsage,
        start_nano: int,
        end_nano: int | None,
        late: bool,
        ingested_at: datetime,
        ingest_version: int,
    ) -> SpanRow:
        error_type = _as_str(attributes.get(semconv.EXCEPTION_TYPE), 256)
        error_message = _as_str(attributes.get(semconv.EXCEPTION_MESSAGE), 2_048)
        if not error_type and wire.status is SpanStatus.ERROR:
            error_type = "error"
        if not error_message and wire.status_message:
            error_message = wire.status_message[:2_048]

        model = _as_str(
            attributes.get(semconv.GEN_AI_RESPONSE_MODEL)
            or attributes.get(semconv.GEN_AI_REQUEST_MODEL),
            256,
        )
        # Previews come from the raw payload and apply their own redaction, so
        # the trace list shows a scrubbed excerpt rather than "[redacted]".
        input_preview, output_preview = self._previews(raw_attributes)
        subject_id = _as_str(raw_attributes.get(semconv.SUBJECT_ID), 256)

        return SpanRow(
            organization_id=scope.organization_id,
            project_id=scope.project_id,
            environment=scope.environment,
            trace_id=wire.trace_id,
            span_id=wire.span_id,
            parent_span_id=wire.parent_span_id,
            name=wire.name[:512],
            kind=(wire.kind or SpanKind.INTERNAL).value,
            category=category.value,
            start_unix_nano=start_nano,
            end_unix_nano=end_nano,
            duration_ns=None if end_nano is None else end_nano - start_nano,
            status=(wire.status or SpanStatus.UNSET).value,
            status_message=_as_str(wire.status_message, 2_048),
            error_type=error_type,
            error_message=error_message,
            service_name=resource.service_name[:256],
            service_version=_as_str(resource.service_version, 128),
            service_instance_id=_as_str(resource.service_instance_id, 256),
            sdk_name=_as_str(resource.sdk_name, 128),
            sdk_version=_as_str(resource.sdk_version, 64),
            session_id=_as_str(attributes.get(semconv.SESSION_ID), 256),
            subject_id=subject_id,
            release=_as_str(attributes.get(semconv.RELEASE), 128),
            git_commit=_as_str(attributes.get(semconv.GIT_COMMIT), 64),
            tags=_as_list(attributes.get(semconv.TAGS)),
            provider=_as_str(attributes.get(semconv.GEN_AI_SYSTEM), 64),
            model=model,
            model_family=_as_str(attributes.get(semconv.MODEL_FAMILY), 64),
            prompt_name=_as_str(attributes.get(semconv.PROMPT_NAME), 256),
            prompt_version_id=_as_str(attributes.get(semconv.PROMPT_VERSION_ID), 64),
            model_config_id=_as_str(attributes.get(semconv.MODEL_CONFIG_ID), 64),
            dataset_version_id=_as_str(attributes.get(semconv.DATASET_VERSION_ID), 64),
            knowledge_base_version=_as_str(attributes.get(semconv.KNOWLEDGE_BASE_VERSION), 128),
            experiment_run_id=_as_str(attributes.get(semconv.EXPERIMENT_RUN_ID), 64),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.effective_total_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            usage_source=usage.source.value,
            # Cost is filled in by the worker: it needs the price book, which
            # requires I/O the normaliser deliberately does not perform.
            cost_total=None,
            cost_currency="",
            cost_estimation_status="unpriced",
            time_to_first_token_ms=_as_float(
                attributes.get(semconv.LATENCY_TIME_TO_FIRST_TOKEN_MS)
            ),
            queue_ms=_as_float(attributes.get(semconv.LATENCY_QUEUE_MS)),
            provider_ms=_as_float(attributes.get(semconv.LATENCY_PROVIDER_MS)),
            agent_id=_as_str(attributes.get(semconv.AGENT_ID), 256),
            tool_name=_as_str(
                attributes.get(semconv.AGENT_TOOL_NAME) or attributes.get(semconv.GEN_AI_TOOL_NAME),
                256,
            ),
            tool_status=_as_str(attributes.get(semconv.AGENT_TOOL_STATUS), 32),
            retriever_name=_as_str(attributes.get(semconv.RETRIEVAL_RETRIEVER_NAME), 256),
            retrieval_result_count=_as_int(attributes.get(semconv.RETRIEVAL_RESULT_COUNT)),
            input_preview=input_preview,
            output_preview=output_preview,
            input_ref=_as_str(attributes.get(semconv.INPUT_REF), 1_024),
            output_ref=_as_str(attributes.get(semconv.OUTPUT_REF), 1_024),
            attributes=self._long_tail_attributes(attributes),
            links=[
                {"trace_id": link.trace_id, "span_id": link.span_id, "attributes": link.attributes}
                for link in wire.links
            ],
            sampling_rate=scope.sampling_rate,
            ingested_at=ingested_at,
            ingest_version=ingest_version,
            content_hash=self._content_hash(wire, scope),
            late_arrival=late,
        )

    def _previews(self, attributes: Mapping[str, Any]) -> tuple[str, str]:
        """Short, redacted excerpts stored inline for the trace list."""
        raw_input = attributes.get(semconv.INPUT_VALUE)
        raw_output = attributes.get(semconv.OUTPUT_VALUE)
        input_text, _ = self._redactor.redact_payload(
            raw_input if isinstance(raw_input, str) else None
        )
        output_text, _ = self._redactor.redact_payload(
            raw_output if isinstance(raw_output, str) else None
        )
        return (
            (input_text or "")[: self._preview_chars],
            (output_text or "")[: self._preview_chars],
        )

    #: Promoted attributes are already stored in their own columns; keeping a
    #: second copy in the map would inflate every row for no query benefit.
    _PROMOTED_KEYS = frozenset(
        {
            semconv.SPAN_CATEGORY,
            semconv.SESSION_ID,
            semconv.SUBJECT_ID,
            semconv.RELEASE,
            semconv.GIT_COMMIT,
            semconv.TAGS,
            semconv.GEN_AI_SYSTEM,
            semconv.GEN_AI_REQUEST_MODEL,
            semconv.GEN_AI_RESPONSE_MODEL,
            semconv.MODEL_FAMILY,
            semconv.PROMPT_NAME,
            semconv.PROMPT_VERSION_ID,
            semconv.MODEL_CONFIG_ID,
            semconv.DATASET_VERSION_ID,
            semconv.KNOWLEDGE_BASE_VERSION,
            semconv.EXPERIMENT_RUN_ID,
            semconv.USAGE_INPUT_TOKENS,
            semconv.USAGE_OUTPUT_TOKENS,
            semconv.USAGE_TOTAL_TOKENS,
            semconv.USAGE_CACHED_INPUT_TOKENS,
            semconv.USAGE_CACHE_WRITE_TOKENS,
            semconv.USAGE_REASONING_TOKENS,
            semconv.USAGE_SOURCE,
            semconv.GEN_AI_USAGE_INPUT_TOKENS,
            semconv.GEN_AI_USAGE_OUTPUT_TOKENS,
            semconv.AGENT_ID,
            semconv.AGENT_TOOL_NAME,
            semconv.GEN_AI_TOOL_NAME,
            semconv.AGENT_TOOL_STATUS,
            semconv.RETRIEVAL_RETRIEVER_NAME,
            semconv.RETRIEVAL_RESULT_COUNT,
            semconv.RETRIEVAL_DOCUMENTS,
            semconv.INPUT_VALUE,
            semconv.OUTPUT_VALUE,
            semconv.INPUT_REF,
            semconv.OUTPUT_REF,
            semconv.LATENCY_TIME_TO_FIRST_TOKEN_MS,
            semconv.LATENCY_QUEUE_MS,
            semconv.LATENCY_PROVIDER_MS,
            semconv.TRACE_NAME,
        }
    )

    def _long_tail_attributes(self, attributes: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in attributes.items() if key not in self._PROMOTED_KEYS}

    def _content_hash(self, wire: WireSpan, scope: IngestScope) -> str:
        """Stable identity of this observation, used for de-duplication.

        Covers the fields that make two deliveries the *same* span. Notably it
        excludes ingestion metadata, so a replay hashes identically and is
        recognised as a duplicate rather than a new observation.
        """
        return content_hash(
            {
                "organization_id": scope.organization_id,
                "project_id": scope.project_id,
                "trace_id": wire.trace_id,
                "span_id": wire.span_id,
                "start": wire.start_time_unix_nano,
                "end": wire.end_time_unix_nano,
                "name": wire.name,
                "status": wire.status.value,
            }
        )

    # ------------------------------------------------------------------
    # derived rows
    # ------------------------------------------------------------------

    def _build_events(
        self, wire: WireSpan, scope: IngestScope, span: SpanRow
    ) -> list[SpanEventRow]:
        rows: list[SpanEventRow] = []
        for index, event in enumerate(wire.events):
            redacted = self._redactor.redact_attributes(event.attributes)
            rows.append(
                SpanEventRow(
                    organization_id=scope.organization_id,
                    project_id=scope.project_id,
                    environment=scope.environment,
                    trace_id=wire.trace_id,
                    span_id=wire.span_id,
                    time_unix_nano=event.time_unix_nano,
                    name=event.name[:512],
                    sequence=index,
                    attributes=dict(redacted.value),
                    ingested_at=span.ingested_at,
                )
            )
        return rows

    def _build_retrieval_documents(
        self,
        attributes: Mapping[str, Any],
        scope: IngestScope,
        span: SpanRow,
        now: datetime,
    ) -> list[RetrievalDocumentRow]:
        payload = _json_or_none(attributes.get(semconv.RETRIEVAL_DOCUMENTS))
        if not isinstance(payload, list):
            return []
        query, _ = self._redactor.redact_payload(
            _as_str(attributes.get(semconv.RETRIEVAL_QUERY), 4_096) or None
        )
        rewritten, _ = self._redactor.redact_payload(
            _as_str(attributes.get(semconv.RETRIEVAL_REWRITTEN_QUERY), 4_096) or None
        )
        retriever = _as_str(attributes.get(semconv.RETRIEVAL_RETRIEVER_NAME), 256)
        kb_version = _as_str(attributes.get(semconv.KNOWLEDGE_BASE_VERSION), 128)
        embedding_model = _as_str(attributes.get(semconv.RETRIEVAL_EMBEDDING_MODEL), 256)
        search_type = _as_str(attributes.get(semconv.RETRIEVAL_SEARCH_TYPE), 64)

        rows: list[RetrievalDocumentRow] = []
        for index, document in enumerate(payload[:500]):
            if not isinstance(document, dict):
                continue
            content, _ = self._redactor.redact_payload(
                document.get("content") if isinstance(document.get("content"), str) else None
            )
            rows.append(
                RetrievalDocumentRow(
                    organization_id=scope.organization_id,
                    project_id=scope.project_id,
                    environment=scope.environment,
                    trace_id=span.trace_id,
                    span_id=span.span_id,
                    time_unix_nano=span.start_unix_nano,
                    document_id=_as_str(document.get("document_id"), 512) or f"doc-{index}",
                    chunk_id=_as_str(document.get("chunk_id"), 512),
                    rank=_as_int(document.get("rank")) or index,
                    score=_as_float(document.get("score")),
                    rerank_score=_as_float(document.get("rerank_score")),
                    rerank_rank=_as_int(document.get("rerank_rank")),
                    selected=bool(document.get("selected")),
                    token_count=_as_int(document.get("token_count")),
                    truncated=bool(document.get("truncated")),
                    source=_as_str(document.get("source"), 2_048),
                    title=_as_str(document.get("title"), 1_024),
                    content_preview=(content or "")[: self._preview_chars],
                    content_ref=_as_str(document.get("content_ref"), 1_024),
                    retriever_name=retriever,
                    knowledge_base_version=kb_version,
                    embedding_model=embedding_model,
                    search_type=search_type,
                    query=(query or "")[:4_096],
                    rewritten_query=(rewritten or "")[:4_096],
                    metadata=self._redactor.redact_attributes(document.get("metadata") or {}).value,
                    ingested_at=now,
                )
            )
        return rows

    def _build_agent_steps(
        self,
        attributes: Mapping[str, Any],
        scope: IngestScope,
        span: SpanRow,
        now: datetime,
    ) -> list[AgentStepRow]:
        step_number = _as_int(attributes.get(semconv.AGENT_STEP_NUMBER))
        agent_id = _as_str(attributes.get(semconv.AGENT_ID), 256)
        if step_number is None or not agent_id:
            return []
        summary, _ = self._redactor.redact_payload(
            _as_str(attributes.get(semconv.AGENT_DECISION_SUMMARY), 4_096) or None
        )
        goal, _ = self._redactor.redact_payload(
            _as_str(attributes.get(semconv.AGENT_GOAL), 2_048) or None
        )
        return [
            AgentStepRow(
                organization_id=scope.organization_id,
                project_id=scope.project_id,
                environment=scope.environment,
                trace_id=span.trace_id,
                span_id=span.span_id,
                agent_id=agent_id,
                step_number=step_number,
                start_unix_nano=span.start_unix_nano,
                duration_ns=span.duration_ns,
                agent_version=_as_str(attributes.get(semconv.AGENT_VERSION), 128),
                goal=(goal or "")[:2_048],
                parent_step=_as_int(attributes.get(semconv.AGENT_STEP_PARENT)),
                step_type=_as_str(attributes.get(semconv.AGENT_STEP_TYPE), 64) or "observation",
                decision_summary=(summary or "")[:4_096],
                tool_name=span.tool_name,
                tool_status=span.tool_status,
                tool_result_ref=_as_str(attributes.get(semconv.AGENT_TOOL_RESULT_REF), 1_024),
                handoff_target=_as_str(attributes.get(semconv.AGENT_HANDOFF_TARGET), 256),
                memory_read_keys=_as_list(attributes.get(semconv.AGENT_MEMORY_READ_KEYS)),
                memory_write_keys=_as_list(attributes.get(semconv.AGENT_MEMORY_WRITE_KEYS)),
                retry_of=_as_int(attributes.get(semconv.AGENT_RETRY_OF)),
                branch_id=_as_str(attributes.get(semconv.AGENT_BRANCH_ID), 128),
                loop_iteration=_as_int(attributes.get(semconv.AGENT_LOOP_ITERATION)),
                approval_required=bool(attributes.get(semconv.AGENT_APPROVAL_REQUIRED)),
                approval_status=_as_str(attributes.get(semconv.AGENT_APPROVAL_STATUS), 32),
                termination_reason=_as_str(attributes.get(semconv.AGENT_TERMINATION_REASON), 64),
                max_steps=_as_int(attributes.get(semconv.AGENT_MAX_STEPS)),
                input_tokens=span.input_tokens,
                output_tokens=span.output_tokens,
                cost_total=None,
                status=span.status,
                error_message=span.error_message,
                ingested_at=now,
            )
        ]

    def _collect_payloads(self, attributes: Mapping[str, Any]) -> dict[str, bytes]:
        """Payloads large enough to belong in object storage rather than a column."""
        payloads: dict[str, bytes] = {}
        for field_name, key in (
            ("input", semconv.INPUT_VALUE),
            ("output", semconv.OUTPUT_VALUE),
        ):
            value = attributes.get(key)
            if isinstance(value, str) and len(value) > self._preview_chars:
                payloads[field_name] = value.encode("utf-8")
        return payloads


def normalize_batch(
    normalizer: SpanNormalizer,
    spans: Sequence[WireSpan],
    resource: ResourceDescriptor,
    scope: IngestScope,
) -> tuple[list[NormalizedSpan], list[tuple[int, str, str]]]:
    """Normalise a batch, returning results and ``(index, code, message)`` failures.

    One bad span never discards the batch. That is not politeness: an SDK
    batching 2,000 spans would otherwise lose 1,999 good ones because of a
    single malformed timestamp, and the resulting data loss would be invisible.
    """
    results: list[NormalizedSpan] = []
    failures: list[tuple[int, str, str]] = []
    for index, wire in enumerate(spans):
        try:
            results.append(normalizer.normalize(wire, resource, scope))
        except NormalizationError as exc:
            failures.append((index, exc.code, exc.message))
        except Exception as exc:
            failures.append((index, "internal_error", f"{type(exc).__name__}: {exc}"))
    return results, failures
