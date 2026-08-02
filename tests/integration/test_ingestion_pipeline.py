"""The ingestion pipeline end to end, through real storage.

Covers the cases that break naive pipelines: duplicates, out-of-order arrival,
missing parents, clock skew, partial batches, and the crash-between-write-and-
acknowledge window.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from aiobs_api.core.timeutil import FrozenClock, datetime_to_unix_nano
from aiobs_api.services.bundle import ServiceBundle
from aiobs_api.storage.analytics.rows import AnalyticsScope
from aiobs_api.storage.bus.protocol import Topics
from aiobs_schemas.ids import generate_span_id, generate_trace_id
from aiobs_schemas.wire import (
    AgentStepPayload,
    IngestBatch,
    LineagePayload,
    ResourceDescriptor,
    RetrievalDocument,
    RetrievalPayload,
    SpanEvent,
    TokenUsage,
    WireSpan,
)


def resource() -> ResourceDescriptor:
    return ResourceDescriptor(
        service_name="test-service",
        service_version="1.0.0",
        environment="development",
        sdk_name="aiobs-python",
        sdk_version="0.1.0",
    )


def wire_span(
    clock: FrozenClock,
    *,
    trace_id: str,
    span_id: str | None = None,
    parent: str | None = None,
    name: str = "generate",
    category: str = "chat_completion",
    offset_seconds: float = 0.0,
    duration_ms: float = 10.0,
    status: str = "ok",
    usage: TokenUsage | None = None,
    **extra: object,
) -> WireSpan:
    start = datetime_to_unix_nano(clock.now() + timedelta(seconds=offset_seconds))
    return WireSpan(
        trace_id=trace_id,
        span_id=span_id or generate_span_id(),
        parent_span_id=parent,
        name=name,
        kind="client",
        category=category,
        start_time_unix_nano=start,
        end_time_unix_nano=start + int(duration_ms * 1e6),
        status=status,
        usage=usage,
        attributes={"gen_ai.system": "mock", "gen_ai.request.model": "mock-model-v1"},
        **extra,  # type: ignore[arg-type]
    )


async def drain(services: ServiceBundle) -> dict[str, int]:
    """Run the worker consumers once, synchronously, and report the counters."""
    group = services.container.settings.bus.consumer_group
    bus = services.container.bus
    counters = {"spans": 0, "traces": 0}

    async for batch in bus.consume(Topics.SPANS, group=group, max_records=500):
        outcome, permanent, retryable = await services.span_processor.process_batch(batch)
        counters["spans"] += outcome.spans_written
        await bus.commit(
            [m for m in batch if m not in permanent and m not in retryable], group=group
        )
        break

    async for batch in bus.consume(Topics.TRACE_ROLLUP, group=group, max_records=500):
        outcome, permanent, retryable = await services.rollup_processor.process_batch(batch)
        counters["traces"] += outcome.traces_touched
        await bus.commit(
            [m for m in batch if m not in permanent and m not in retryable], group=group
        )
        break

    return counters


class TestHappyPath:
    async def test_a_span_flows_from_ingest_to_storage(
        self, services: ServiceBundle, ingest_principal, scope: AnalyticsScope, price_book, clock
    ) -> None:
        trace_id = generate_trace_id()
        root = wire_span(clock, trace_id=trace_id, name="request", category="workflow_step")
        child = wire_span(
            clock,
            trace_id=trace_id,
            parent=root.span_id,
            usage=TokenUsage(input_tokens=1_000, output_tokens=500),
        )

        result = await services.ingestion.ingest(
            principal=ingest_principal,
            batch=IngestBatch(resource=resource(), spans=[root, child]),
            source="native_json",
            payload_bytes=1_000,
        )
        assert result.response.accepted == 2
        assert result.response.rejected == 0

        await drain(services)

        spans = await services.container.analytics.get_spans(scope, trace_id)
        assert len(spans) == 2

        trace = await services.container.analytics.get_trace(scope, trace_id)
        assert trace is not None
        assert trace.name == "request"
        assert trace.span_count == 2
        assert trace.total_tokens == 1_500
        # 1000/1e6 * 1.00 + 500/1e6 * 2.00 = 0.002
        assert trace.total_cost == Decimal("0.002")
        assert trace.status == "ok"
        assert trace.complete is True

    async def test_derived_rows_are_produced(
        self, services: ServiceBundle, ingest_principal, scope: AnalyticsScope, price_book, clock
    ) -> None:
        trace_id = generate_trace_id()
        spans = [
            wire_span(
                clock,
                trace_id=trace_id,
                name="retrieve",
                category="retrieval",
                retrieval=RetrievalPayload(
                    query="how do refunds work?",
                    retriever_name="pgvector",
                    documents=[
                        RetrievalDocument(
                            document_id=f"doc-{index}",
                            rank=index,
                            score=0.9 - index * 0.1,
                            rerank_rank=2 - index,
                            selected=index < 2,
                            token_count=30,
                            content="refund policy text",
                        )
                        for index in range(3)
                    ],
                ),
            ),
            wire_span(
                clock,
                trace_id=trace_id,
                name="decide",
                category="agent_decision",
                agent_step=AgentStepPayload(
                    agent_id="agent-1", step_number=1, step_type="decision"
                ),
            ),
        ]
        spans[0].events.append(
            SpanEvent(name="aiobs.first_token", time_unix_nano=spans[0].start_time_unix_nano)
        )

        await services.ingestion.ingest(
            principal=ingest_principal,
            batch=IngestBatch(resource=resource(), spans=spans),
            source="native_json",
            payload_bytes=1_000,
        )
        await drain(services)

        documents = await services.container.analytics.get_retrieval_documents(scope, trace_id)
        assert len(documents) == 3
        assert sum(1 for item in documents if item.selected) == 2
        assert documents[0].query == "how do refunds work?"

        steps = await services.container.analytics.get_agent_steps(scope, trace_id)
        assert len(steps) == 1
        events = await services.container.analytics.get_span_events(scope, trace_id)
        assert len(events) == 1


class TestIdempotency:
    async def test_duplicate_spans_are_dropped(
        self, services: ServiceBundle, ingest_principal, scope: AnalyticsScope, clock
    ) -> None:
        trace_id = generate_trace_id()
        span = wire_span(clock, trace_id=trace_id)
        batch = IngestBatch(resource=resource(), spans=[span])

        first = await services.ingestion.ingest(
            principal=ingest_principal, batch=batch, source="native_json", payload_bytes=100
        )
        second = await services.ingestion.ingest(
            principal=ingest_principal, batch=batch, source="native_json", payload_bytes=100
        )

        assert first.response.accepted == 1 and first.response.duplicates == 0
        assert second.response.accepted == 0 and second.response.duplicates == 1

        await drain(services)
        assert len(await services.container.analytics.get_spans(scope, trace_id)) == 1

    async def test_idempotency_key_replays_the_original_response(
        self, services: ServiceBundle, ingest_principal, clock
    ) -> None:
        batch = IngestBatch(
            resource=resource(),
            spans=[wire_span(clock, trace_id=generate_trace_id())],
            idempotency_key="deploy-42",
        )
        first = await services.ingestion.ingest(
            principal=ingest_principal, batch=batch, source="native_json", payload_bytes=100
        )
        replay = await services.ingestion.ingest(
            principal=ingest_principal, batch=batch, source="native_json", payload_bytes=100
        )
        assert replay.response.replayed is True
        assert replay.response.batch_id == first.response.batch_id

    async def test_processing_the_same_message_twice_does_not_double_count(
        self, services: ServiceBundle, ingest_principal, scope: AnalyticsScope, price_book, clock
    ) -> None:
        """The crash-after-write-before-ack window: the batch is redelivered."""
        trace_id = generate_trace_id()
        await services.ingestion.ingest(
            principal=ingest_principal,
            batch=IngestBatch(
                resource=resource(),
                spans=[
                    wire_span(
                        clock,
                        trace_id=trace_id,
                        usage=TokenUsage(input_tokens=1_000, output_tokens=500),
                    )
                ],
            ),
            source="native_json",
            payload_bytes=100,
        )

        bus = services.container.bus
        group = services.container.settings.bus.consumer_group
        async for batch in bus.consume(Topics.SPANS, group=group, max_records=100):
            # Process twice without committing in between: exactly what a crash
            # between the write and the acknowledgement produces.
            await services.span_processor.process_batch(batch)
            await services.span_processor.process_batch(batch)
            await bus.commit(batch, group=group)
            break

        await services.rollup_processor.recompute(
            organization_id=scope.organization_id,
            project_id=scope.project_id or "",
            environment=scope.environment or "",
            trace_id=trace_id,
        )

        spans = await services.container.analytics.get_spans(scope, trace_id)
        assert len(spans) == 1
        trace = await services.container.analytics.get_trace(scope, trace_id)
        assert trace is not None
        assert trace.total_tokens == 1_500  # not 3,000
        assert trace.total_cost == Decimal("0.002")  # not 0.004


class TestOrderingAndCompleteness:
    async def test_a_child_arriving_before_its_parent_is_reconciled(
        self, services: ServiceBundle, ingest_principal, scope: AnalyticsScope, clock
    ) -> None:
        trace_id = generate_trace_id()
        root_id = generate_span_id()
        child = wire_span(clock, trace_id=trace_id, parent=root_id, name="child")

        await services.ingestion.ingest(
            principal=ingest_principal,
            batch=IngestBatch(resource=resource(), spans=[child]),
            source="native_json",
            payload_bytes=100,
        )
        await drain(services)

        trace = await services.container.analytics.get_trace(scope, trace_id)
        assert trace is not None
        # No root yet: the trace is explicitly incomplete rather than "ok".
        assert trace.status == "incomplete"
        assert trace.complete is False

        root = wire_span(
            clock, trace_id=trace_id, span_id=root_id, name="request", category="workflow_step"
        )
        await services.ingestion.ingest(
            principal=ingest_principal,
            batch=IngestBatch(resource=resource(), spans=[root]),
            source="native_json",
            payload_bytes=100,
        )
        await drain(services)

        trace = await services.container.analytics.get_trace(scope, trace_id)
        assert trace is not None
        assert trace.status == "ok"
        assert trace.complete is True
        assert trace.name == "request"
        assert trace.span_count == 2

    async def test_a_late_span_that_moves_the_start_time_is_handled(
        self, services: ServiceBundle, ingest_principal, scope: AnalyticsScope, clock
    ) -> None:
        trace_id = generate_trace_id()
        await services.ingestion.ingest(
            principal=ingest_principal,
            batch=IngestBatch(
                resource=resource(), spans=[wire_span(clock, trace_id=trace_id, offset_seconds=10)]
            ),
            source="native_json",
            payload_bytes=100,
        )
        await drain(services)
        first = await services.container.analytics.get_trace(scope, trace_id)
        assert first is not None

        # An earlier span arrives afterwards and pulls the trace start back.
        await services.ingestion.ingest(
            principal=ingest_principal,
            batch=IngestBatch(
                resource=resource(),
                spans=[wire_span(clock, trace_id=trace_id, offset_seconds=0, name="earlier")],
            ),
            source="native_json",
            payload_bytes=100,
        )
        await drain(services)

        traces = await services.container.analytics.get_traces(scope, [trace_id])
        assert len(traces) == 1  # exactly one roll-up, not two
        assert traces[0].start_unix_nano < first.start_unix_nano
        assert traces[0].span_count == 2


class TestValidationAndPartialFailure:
    async def test_a_future_timestamp_is_rejected_for_clock_skew(
        self, services: ServiceBundle, ingest_principal, clock
    ) -> None:
        span = wire_span(clock, trace_id=generate_trace_id(), offset_seconds=3_600)
        result = await services.ingestion.ingest(
            principal=ingest_principal,
            batch=IngestBatch(resource=resource(), spans=[span]),
            source="native_json",
            payload_bytes=100,
        )
        assert result.response.accepted == 0
        assert result.response.rejections[0].code == "clock_skew"

    async def test_one_bad_span_does_not_discard_the_batch(
        self, services: ServiceBundle, ingest_principal, clock
    ) -> None:
        good = [wire_span(clock, trace_id=generate_trace_id()) for _ in range(4)]
        bad = wire_span(clock, trace_id=generate_trace_id(), offset_seconds=3_600)
        result = await services.ingestion.ingest(
            principal=ingest_principal,
            batch=IngestBatch(resource=resource(), spans=[*good, bad]),
            source="native_json",
            payload_bytes=100,
        )
        assert result.response.accepted == 4
        assert result.response.rejected == 1
        assert result.response.rejections[0].index == 4

    async def test_an_old_span_is_accepted_and_flagged(
        self, services: ServiceBundle, ingest_principal, scope: AnalyticsScope, clock
    ) -> None:
        """Deliberate backfill must not be discarded, but must be visible."""
        trace_id = generate_trace_id()
        span = wire_span(clock, trace_id=trace_id, offset_seconds=-8 * 86_400)
        result = await services.ingestion.ingest(
            principal=ingest_principal,
            batch=IngestBatch(resource=resource(), spans=[span]),
            source="native_json",
            payload_bytes=100,
        )
        assert result.response.accepted == 1
        await drain(services)
        stored = await services.container.analytics.get_spans(scope, trace_id)
        assert stored[0].late_arrival is True


class TestTenancy:
    async def test_the_payload_cannot_choose_its_tenant(
        self, services: ServiceBundle, ingest_principal, scope: AnalyticsScope, clock
    ) -> None:
        """A client setting aiobs.tenant.id must not redirect its telemetry."""
        trace_id = generate_trace_id()
        span = wire_span(clock, trace_id=trace_id)
        span.attributes["aiobs.tenant.id"] = "org_victim"
        span.attributes["aiobs.project.id"] = "prj_victim"

        await services.ingestion.ingest(
            principal=ingest_principal,
            batch=IngestBatch(resource=resource(), spans=[span]),
            source="native_json",
            payload_bytes=100,
        )
        await drain(services)

        stored = await services.container.analytics.get_spans(scope, trace_id)
        assert len(stored) == 1
        assert stored[0].organization_id == scope.organization_id
        assert stored[0].project_id == scope.project_id

    async def test_the_resource_cannot_choose_its_environment(
        self, services: ServiceBundle, ingest_principal, scope: AnalyticsScope, clock
    ) -> None:
        """A staging key must not be able to write into production."""
        trace_id = generate_trace_id()
        descriptor = resource()
        object.__setattr__(descriptor, "environment", "production")

        await services.ingestion.ingest(
            principal=ingest_principal,
            batch=IngestBatch(resource=descriptor, spans=[wire_span(clock, trace_id=trace_id)]),
            source="native_json",
            payload_bytes=100,
        )
        await drain(services)

        stored = await services.container.analytics.get_spans(scope, trace_id)
        assert stored[0].environment == "development"


class TestLineage:
    async def test_lineage_is_preserved_through_the_pipeline(
        self, services: ServiceBundle, ingest_principal, scope: AnalyticsScope, clock
    ) -> None:
        trace_id = generate_trace_id()
        span = wire_span(
            clock,
            trace_id=trace_id,
            lineage=LineagePayload(
                prompt_name="support-reply",
                prompt_version_id="pmv_ABC",
                model_config_id="mdv_DEF",
                dataset_version_id="dsv_GHI",
                knowledge_base_version="kb-2026-07",
            ),
        )
        await services.ingestion.ingest(
            principal=ingest_principal,
            batch=IngestBatch(resource=resource(), spans=[span]),
            source="native_json",
            payload_bytes=100,
        )
        await drain(services)

        stored = (await services.container.analytics.get_spans(scope, trace_id))[0]
        assert stored.prompt_version_id == "pmv_ABC"
        assert stored.model_config_id == "mdv_DEF"
        assert stored.dataset_version_id == "dsv_GHI"

        trace = await services.container.analytics.get_trace(scope, trace_id)
        assert trace is not None
        assert "pmv_ABC" in trace.prompt_version_ids
        assert "dsv_GHI" in trace.dataset_version_ids
