"""Span processing: the worker half of the telemetry pipeline.

Consumes spans from the bus, enriches them with cost, writes them to the
analytics store, and recomputes the affected trace roll-ups.

Every step is idempotent, because at-least-once delivery guarantees this code
will see the same span twice:

* cost is a pure function of ``(usage, provider, model, event time, price book
  version)`` -- recomputing yields the same number;
* analytics writes collapse on the natural key;
* roll-ups are recomputed from scratch, never accumulated.

Failure handling distinguishes three cases, because conflating them is how a
pipeline either loses data or wedges itself:

``permanent``
    Malformed message, unknown schema version. Dead-lettered immediately;
    retrying cannot help.
``transient``
    Analytics store unavailable, timeout. Retried with jittered backoff up to
    the configured limit, then dead-lettered.
``unknown``
    Any other exception. Treated as transient but logged with a stack trace,
    because an unexpected error that is actually permanent will exhaust its
    retries and land in the DLQ where a human sees it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..core.config import Settings
from ..core.errors import DependencyUnavailableError
from ..core.logging import get_logger
from ..core.timeutil import Clock
from ..domain.cost import CostCalculator
from ..domain.usage import CacheConvention, NormalizedUsage
from ..ingest.normalizer import NormalizedSpan
from ..ingest.rollup import build_trace_rollup
from ..ingest.serialization import decode_span_message, encode_rollup_message
from ..storage.analytics.protocol import AnalyticsStore
from ..storage.analytics.rows import AnalyticsScope, CostRecordRow, SpanRow
from ..storage.bus.protocol import BusMessageEnvelope, EventBus, Topics
from .pricing import PricingService

__all__ = ["ProcessingOutcome", "SpanProcessor", "TraceRollupProcessor"]

log = get_logger(__name__)


class PermanentProcessingError(Exception):
    """The message can never succeed; dead-letter it now."""


@dataclass(slots=True)
class ProcessingOutcome:
    """Counters from one processed batch, surfaced as worker metrics."""

    spans_written: int = 0
    events_written: int = 0
    retrieval_documents_written: int = 0
    agent_steps_written: int = 0
    cost_records_written: int = 0
    traces_touched: int = 0
    permanent_failures: int = 0
    transient_failures: int = 0

    def merge(self, other: ProcessingOutcome) -> None:
        self.spans_written += other.spans_written
        self.events_written += other.events_written
        self.retrieval_documents_written += other.retrieval_documents_written
        self.agent_steps_written += other.agent_steps_written
        self.cost_records_written += other.cost_records_written
        self.traces_touched += other.traces_touched
        self.permanent_failures += other.permanent_failures
        self.transient_failures += other.transient_failures


class SpanProcessor:
    """Enriches and persists spans."""

    def __init__(
        self,
        *,
        settings: Settings,
        analytics: AnalyticsStore,
        pricing: PricingService,
        clock: Clock,
        bus: EventBus | None = None,
    ) -> None:
        self._settings = settings
        self._analytics = analytics
        self._pricing = pricing
        self._clock = clock
        self._bus = bus

    async def process_batch(
        self, messages: Sequence[BusMessageEnvelope]
    ) -> tuple[ProcessingOutcome, list[BusMessageEnvelope], list[BusMessageEnvelope]]:
        """Process a batch, returning ``(outcome, permanent_failures, retryable)``.

        Decoding failures are separated *before* any write, so one malformed
        message cannot prevent its well-formed neighbours from being persisted.
        """
        outcome = ProcessingOutcome()
        permanent: list[BusMessageEnvelope] = []
        decoded: list[tuple[BusMessageEnvelope, NormalizedSpan]] = []

        for message in messages:
            try:
                decoded.append((message, decode_span_message(message.payload)))
            except (ValueError, KeyError, TypeError) as exc:
                log.warning(
                    "processing.permanent_decode_failure",
                    error=str(exc),
                    partition_key=message.partition_key,
                )
                permanent.append(message)
                outcome.permanent_failures += 1

        if not decoded:
            return outcome, permanent, []

        try:
            touched = await self._persist(decoded, outcome)
            await self._request_rollups(touched)
        except DependencyUnavailableError as exc:
            log.warning("processing.transient_failure", error=str(exc))
            outcome.transient_failures += len(decoded)
            return outcome, permanent, [message for message, _ in decoded]

        return outcome, permanent, []

    async def _request_rollups(self, traces: set[tuple[str, str, str, str]]) -> None:
        """Ask for a roll-up recomputation for every trace just written.

        Published *after* the spans are durably in the analytics store, so the
        roll-up consumer is guaranteed to see them. One message per trace per
        batch; the roll-up consumer collapses duplicates within its own batch.
        """
        if self._bus is None or not traces:
            return
        await self._bus.publish_batch(
            [
                BusMessageEnvelope(
                    topic=Topics.TRACE_ROLLUP,
                    partition_key=trace_id,
                    payload=encode_rollup_message(
                        organization_id=organization_id,
                        project_id=project_id,
                        environment=environment,
                        trace_id=trace_id,
                    ),
                )
                for organization_id, project_id, environment, trace_id in traces
            ]
        )

    async def _persist(
        self,
        decoded: Sequence[tuple[BusMessageEnvelope, NormalizedSpan]],
        outcome: ProcessingOutcome,
    ) -> set[tuple[str, str, str, str]]:
        spans: list[SpanRow] = []
        events = []
        documents = []
        steps = []
        costs: list[CostRecordRow] = []
        traces: set[tuple[str, str, str, str]] = set()

        # Group by organisation so each tenant's price book is loaded once,
        # not once per span.
        calculators: dict[str, CostCalculator] = {}

        for _, item in decoded:
            span = item.span
            organization_id = span.organization_id
            calculator = calculators.get(organization_id)
            if calculator is None:
                calculator = await self._pricing.calculator_for(organization_id)
                calculators[organization_id] = calculator

            cost_row = self._apply_cost(span, calculator)
            if cost_row is not None:
                costs.append(cost_row)

            spans.append(span)
            events.extend(item.events)
            documents.extend(item.retrieval_documents)
            for step in item.agent_steps:
                step.cost_total = span.cost_total
                steps.append(step)
            traces.add((span.organization_id, span.project_id, span.environment, span.trace_id))

        outcome.spans_written += await self._analytics.insert_spans(spans)
        if events:
            outcome.events_written += await self._analytics.insert_span_events(events)
        if documents:
            outcome.retrieval_documents_written += await self._analytics.insert_retrieval_documents(
                documents
            )
        if steps:
            outcome.agent_steps_written += await self._analytics.insert_agent_steps(steps)
        if costs:
            outcome.cost_records_written += await self._analytics.insert_cost_records(costs)
        outcome.traces_touched += len(traces)
        return traces

    def _apply_cost(self, span: SpanRow, calculator: CostCalculator) -> CostRecordRow | None:
        """Cost a span in place and return its audit record.

        A span is costed when it carries a provider, a model *and* usage --
        not merely when its category says "model call". An agent-decision span
        that invoked a model is a real, billable call and would otherwise be
        silently free; conversely an orchestration span reports no usage of its
        own, so it is skipped without needing a category check.

        Double counting would require a parent span to duplicate its child's
        reported usage, which is a client-side bug rather than something the
        cost engine can detect -- and the per-span cost records make it visible
        when it happens.
        """
        if not span.provider or not span.model:
            return None

        usage = NormalizedUsage(
            input_tokens=span.input_tokens,
            output_tokens=span.output_tokens,
            total_tokens=span.total_tokens,
            cached_input_tokens=span.cached_input_tokens,
            cache_write_tokens=span.cache_write_tokens,
            reasoning_tokens=span.reasoning_tokens,
            source=_usage_source(span.usage_source),
            # Provider adapters declare the convention; when absent, assume
            # exclusive so cached tokens are never double-charged.
            cache_convention=CacheConvention.EXCLUSIVE,
        )
        if usage.is_missing:
            span.cost_estimation_status = "unpriced"
            return None

        at = _event_time(span)
        breakdown = calculator.compute(
            provider=span.provider or "", model=span.model or "", usage=usage, at=at
        )
        span.cost_total = breakdown.total if breakdown.components else None
        span.cost_currency = breakdown.currency
        span.cost_estimation_status = breakdown.estimation_status.value
        span.price_book_version = breakdown.price_book_version

        if not breakdown.components:
            log.debug(
                "processing.unpriced_span",
                provider=span.provider,
                model=span.model,
                categories=list(breakdown.unpriced_categories),
            )
            return None

        return CostRecordRow(
            organization_id=span.organization_id,
            project_id=span.project_id,
            environment=span.environment,
            trace_id=span.trace_id,
            span_id=span.span_id,
            time_unix_nano=span.start_unix_nano,
            provider=span.provider,
            model=span.model,
            currency=breakdown.currency,
            total=breakdown.total,
            price_book_id=breakdown.price_book_id,
            price_book_version=breakdown.price_book_version,
            estimation_status=breakdown.estimation_status.value,
            usage_source=usage.source.value,
            components=[component.as_dict() for component in breakdown.components],
            formula=breakdown.formula,
            prompt_version_id=span.prompt_version_id,
            model_config_id=span.model_config_id,
            session_id=span.session_id,
            subject_id=span.subject_id,
            ingested_at=span.ingested_at,
        )


class TraceRollupProcessor:
    """Recomputes trace roll-ups for traces whose spans changed."""

    def __init__(self, *, analytics: AnalyticsStore, clock: Clock) -> None:
        self._analytics = analytics
        self._clock = clock

    async def recompute(
        self, *, organization_id: str, project_id: str, environment: str, trace_id: str
    ) -> bool:
        """Rebuild one trace's roll-up from its spans. Returns whether it changed."""
        scope = AnalyticsScope(
            organization_id=organization_id, project_id=project_id, environment=environment
        )
        spans = await self._analytics.get_spans(scope, trace_id)
        if not spans:
            return False
        previous = await self._analytics.get_trace(scope, trace_id)
        rollup = build_trace_rollup(spans, clock=self._clock, previous=previous)
        if rollup is None:
            return False
        await self._analytics.upsert_traces([rollup])
        return True

    async def process_batch(
        self, messages: Sequence[BusMessageEnvelope]
    ) -> tuple[ProcessingOutcome, list[BusMessageEnvelope], list[BusMessageEnvelope]]:
        outcome = ProcessingOutcome()
        permanent: list[BusMessageEnvelope] = []
        retryable: list[BusMessageEnvelope] = []

        # Collapse duplicate roll-up requests within one batch: ten spans of a
        # trace produce ten messages but only one recomputation is needed.
        unique: dict[str, tuple[str, str, str, str]] = {}
        for message in messages:
            payload = message.payload
            trace_id = str(payload.get("trace_id") or "")
            if not trace_id:
                permanent.append(message)
                outcome.permanent_failures += 1
                continue
            unique[trace_id] = (
                str(payload.get("organization_id") or ""),
                str(payload.get("project_id") or ""),
                str(payload.get("environment") or ""),
                trace_id,
            )

        for organization_id, project_id, environment, trace_id in unique.values():
            try:
                if await self.recompute(
                    organization_id=organization_id,
                    project_id=project_id,
                    environment=environment,
                    trace_id=trace_id,
                ):
                    outcome.traces_touched += 1
            except DependencyUnavailableError:
                outcome.transient_failures += 1
                retryable.extend(
                    message
                    for message in messages
                    if str(message.payload.get("trace_id")) == trace_id
                )
        return outcome, permanent, retryable

    async def reconcile(self, *, since: timedelta, limit: int = 500) -> int:
        """Catch roll-ups missed by the eager path.

        The eager recomputation can be lost to a crash between the span write
        and the roll-up message. This sweep finds traces whose newest span is
        younger than their roll-up and fixes them, which is what makes
        "eventually consistent" a bounded promise rather than a hope.
        """
        cutoff = self._clock.now() - since
        stale = await self._analytics.trace_ids_needing_rollup(since=cutoff, limit=limit)
        repaired = 0
        for organization_id, project_id, environment, trace_id in stale:
            try:
                if await self.recompute(
                    organization_id=organization_id,
                    project_id=project_id,
                    environment=environment,
                    trace_id=trace_id,
                ):
                    repaired += 1
            except DependencyUnavailableError as exc:
                log.warning("rollup.reconcile_failed", trace_id=trace_id, error=str(exc))
                break
        if repaired:
            log.info("rollup.reconciled", traces=repaired)
        return repaired


def _usage_source(value: str):  # type: ignore[no-untyped-def]
    from aiobs_schemas.enums import UsageSource

    try:
        return UsageSource(value)
    except ValueError:
        return UsageSource.MISSING


def _event_time(span: SpanRow) -> datetime:
    from ..core.timeutil import unix_nano_to_datetime

    return unix_nano_to_datetime(span.start_unix_nano)
