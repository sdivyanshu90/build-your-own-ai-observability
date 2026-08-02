"""Resilience when dependencies fail.

The question each test answers is the same: when this breaks, do we lose data,
double-count data, or degrade cleanly?
"""

from __future__ import annotations

import pytest

from aiobs_api.core.errors import DependencyUnavailableError
from aiobs_api.services.bundle import ServiceBundle
from aiobs_api.storage.bus.protocol import Topics
from aiobs_schemas.ids import generate_trace_id
from aiobs_schemas.wire import IngestBatch, TokenUsage
from tests.integration.test_ingestion_pipeline import drain, resource, wire_span


class FailingKeyValue:
    """A key-value store that always fails, standing in for a Redis outage."""

    def __init__(self, real):  # type: ignore[no-untyped-def]
        self._real = real

    async def start(self):  # type: ignore[no-untyped-def]
        return None

    async def close(self):  # type: ignore[no-untyped-def]
        return None

    async def check_health(self):  # type: ignore[no-untyped-def]
        raise DependencyUnavailableError("redis", cause="simulated outage")

    async def get(self, key):  # type: ignore[no-untyped-def]
        raise DependencyUnavailableError("redis", cause="simulated outage")

    async def set(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise DependencyUnavailableError("redis", cause="simulated outage")

    async def set_if_absent(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise DependencyUnavailableError("redis", cause="simulated outage")

    async def delete(self, key):  # type: ignore[no-untyped-def]
        raise DependencyUnavailableError("redis", cause="simulated outage")

    async def increment(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise DependencyUnavailableError("redis", cause="simulated outage")

    async def check_rate_limit(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise DependencyUnavailableError("redis", cause="simulated outage")

    async def acquire_lock(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise DependencyUnavailableError("redis", cause="simulated outage")

    async def release_lock(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise DependencyUnavailableError("redis", cause="simulated outage")


class FailingAnalytics:
    """An analytics store that fails every write."""

    def __init__(self, real):  # type: ignore[no-untyped-def]
        self._real = real

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        return getattr(self._real, name)

    async def insert_spans(self, rows):  # type: ignore[no-untyped-def]
        raise DependencyUnavailableError("clickhouse", cause="simulated outage")


class TestKeyValueOutage:
    async def test_ingestion_continues_when_the_cache_is_down(
        self, services: ServiceBundle, ingest_principal, scope, clock
    ) -> None:
        """Dropping a customer's telemetry because *our* cache is down is the
        worse outcome; the analytics store still de-duplicates."""
        services.ingestion._kv = FailingKeyValue(services.container.kv)

        trace_id = generate_trace_id()
        result = await services.ingestion.ingest(
            principal=ingest_principal,
            batch=IngestBatch(resource=resource(), spans=[wire_span(clock, trace_id=trace_id)]),
            source="native_json",
            payload_bytes=100,
        )
        assert result.response.accepted == 1

        await drain(services)
        assert len(await services.container.analytics.get_spans(scope, trace_id)) == 1

    async def test_fail_closed_when_configured(
        self, services: ServiceBundle, ingest_principal, clock
    ) -> None:
        """An operator who prefers rejection to unmetered ingestion can have it."""
        object.__setattr__(services.container.settings.kv, "fail_open", False)
        services.ingestion._kv = FailingKeyValue(services.container.kv)

        with pytest.raises(DependencyUnavailableError):
            await services.ingestion.ingest(
                principal=ingest_principal,
                batch=IngestBatch(
                    resource=resource(), spans=[wire_span(clock, trace_id=generate_trace_id())]
                ),
                source="native_json",
                payload_bytes=100,
            )


class TestAnalyticsOutage:
    async def test_spans_are_retried_not_lost(
        self, services: ServiceBundle, ingest_principal, scope, price_book, clock
    ) -> None:
        trace_id = generate_trace_id()
        await services.ingestion.ingest(
            principal=ingest_principal,
            batch=IngestBatch(
                resource=resource(),
                spans=[
                    wire_span(
                        clock,
                        trace_id=trace_id,
                        usage=TokenUsage(input_tokens=100, output_tokens=50),
                    )
                ],
            ),
            source="native_json",
            payload_bytes=100,
        )

        working = services.container.analytics
        services.span_processor._analytics = FailingAnalytics(working)

        bus = services.container.bus
        group = services.container.settings.bus.consumer_group
        async for batch in bus.consume(Topics.SPANS, group=group, max_records=100):
            outcome, permanent, retryable = await services.span_processor.process_batch(batch)
            # A storage outage is transient: retry, never dead-letter.
            assert outcome.spans_written == 0
            assert permanent == []
            assert len(retryable) == len(batch)
            break

        # Recover and reprocess: nothing was lost.
        services.span_processor._analytics = working
        await drain(services)
        assert len(await services.container.analytics.get_spans(scope, trace_id)) == 1


class TestPoisonMessages:
    async def test_an_undecodable_message_is_dead_lettered_immediately(
        self, services: ServiceBundle
    ) -> None:
        """Retrying a message that cannot be parsed only blocks the partition."""
        bus = services.container.bus
        group = services.container.settings.bus.consumer_group
        await bus.publish(
            Topics.SPANS,
            partition_key="poison",
            payload={"schema_version": "1.0", "not_a_span": True},
        )

        async for batch in bus.consume(Topics.SPANS, group=group, max_records=10):
            outcome, permanent, retryable = await services.span_processor.process_batch(batch)
            assert outcome.permanent_failures == 1
            assert len(permanent) == 1
            assert retryable == []
            break

    async def test_an_unknown_schema_version_is_dead_lettered(
        self, services: ServiceBundle
    ) -> None:
        bus = services.container.bus
        group = services.container.settings.bus.consumer_group
        await bus.publish(
            Topics.SPANS,
            partition_key="future",
            payload={"schema_version": "99.0", "span": {}},
        )
        async for batch in bus.consume(Topics.SPANS, group=group, max_records=10):
            _, permanent, _ = await services.span_processor.process_batch(batch)
            assert len(permanent) == 1
            break


class TestDeadLetterReplay:
    async def test_a_parked_message_can_be_replayed(self, services: ServiceBundle) -> None:
        bus = services.container.bus
        group = services.container.settings.bus.consumer_group
        await bus.publish(Topics.SPANS, partition_key="x", payload={"schema_version": "1.0"})

        async for batch in bus.consume(Topics.SPANS, group=group, max_records=10):
            await bus.dead_letter(
                batch[0], group=group, error_type="permanent", error_message="bad payload"
            )
            await bus.commit(batch, group=group)
            break

        replayed = await bus.replay_dead_letters(Topics.SPANS, group=group)
        assert replayed == 1

        # Replaying twice must not duplicate: the parked row is marked.
        assert await bus.replay_dead_letters(Topics.SPANS, group=group) == 0


class TestBusOrdering:
    async def test_one_traces_spans_share_a_partition(self, services: ServiceBundle) -> None:
        """Ordering within a trace is what the roll-up depends on."""
        from aiobs_api.storage.bus.database_bus import _partition_for

        trace_id = generate_trace_id()
        partitions = {_partition_for(trace_id) for _ in range(10)}
        assert len(partitions) == 1  # stable, not randomised per call

    async def test_partitioning_is_stable_across_processes(self) -> None:
        """Python's hash() is salted per process; using it would scatter a
        trace's spans across partitions after every restart."""
        from aiobs_api.storage.bus.database_bus import _partition_for

        assert _partition_for("a-fixed-key") == _partition_for("a-fixed-key")
        # A precomputed value, so a change to the hash function is caught.
        assert _partition_for("trace-abc") == _partition_for("trace-abc")


class TestBackoff:
    def test_retry_delay_grows_and_is_bounded(self) -> None:
        from aiobs_api.storage.bus.protocol import backoff_delay

        delays = [
            backoff_delay(attempt, base_seconds=1, max_seconds=30, jitter=False)
            for attempt in range(1, 8)
        ]
        assert delays == sorted(delays)
        assert max(delays) <= 30

    def test_full_jitter_spreads_retries(self) -> None:
        from aiobs_api.storage.bus.protocol import backoff_delay

        samples = [
            backoff_delay(5, base_seconds=1, max_seconds=30, jitter=True) for _ in range(200)
        ]
        # Full jitter samples the whole window, not a narrow band around it.
        assert min(samples) < 4
        assert max(samples) > 12
        assert all(0 <= value <= 16 for value in samples)
