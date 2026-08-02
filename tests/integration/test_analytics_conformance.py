"""Analytics store conformance suite.

Every test here runs against **both** drivers. That is the mechanism that makes
a second implementation an asset rather than a divergence risk: SQLite and
ClickHouse are held to identical behaviour by one suite, so "it works locally
but not in production" becomes a test failure rather than an incident.

The ClickHouse parameterisation is skipped unless a server is reachable, so the
suite is useful on a laptop and complete in CI.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aiobs_api.core.query import CursorCodec, PageRequest, parse_filters, parse_sort
from aiobs_api.storage.analytics.protocol import (
    Aggregation,
    AnalyticsStore,
    MetricQuery,
    TimeInterval,
)
from aiobs_api.storage.analytics.rows import (
    AgentStepRow,
    AnalyticsScope,
    CostRecordRow,
    RetrievalDocumentRow,
    SpanEventRow,
    SpanRow,
    TraceRow,
)
from aiobs_api.storage.analytics.schemas import SPAN_SCHEMA, TRACE_SCHEMA
from aiobs_api.storage.analytics.sqlite_store import SqliteAnalyticsStore

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
BASE_NANO = int(NOW.timestamp() * 1e9)
ORG = "org_conformance"
PROJECT = "prj_conformance"
ENVIRONMENT = "development"

CLICKHOUSE_URL = os.environ.get("AIOBS_TEST_CLICKHOUSE_URL")


def _window() -> tuple[datetime, datetime]:
    return NOW - timedelta(hours=1), NOW + timedelta(hours=1)


async def _clickhouse_available(url: str) -> bool:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{url}/ping")
            return response.status_code == 200
    except Exception:
        return False


@pytest.fixture(params=["sqlite", "clickhouse"])
async def store(request: pytest.FixtureRequest, tmp_path) -> AsyncIterator[AnalyticsStore]:  # type: ignore[no-untyped-def]
    """Yield each analytics driver in turn."""
    codec = CursorCodec("conformance-secret")

    if request.param == "sqlite":
        instance: AnalyticsStore = SqliteAnalyticsStore(tmp_path / "analytics.db", codec)
        await instance.start()
        try:
            yield instance
        finally:
            await instance.close()
        return

    if not CLICKHOUSE_URL:
        pytest.skip("set AIOBS_TEST_CLICKHOUSE_URL to run the ClickHouse conformance suite")
    if not await _clickhouse_available(CLICKHOUSE_URL):
        pytest.skip(f"ClickHouse at {CLICKHOUSE_URL} is not reachable")

    from aiobs_api.storage.analytics.clickhouse_store import ClickHouseAnalyticsStore

    database = f"aiobs_test_{os.getpid()}"
    instance = ClickHouseAnalyticsStore(
        url=CLICKHOUSE_URL,
        database=database,
        username=os.environ.get("AIOBS_TEST_CLICKHOUSE_USER", "default"),
        password=os.environ.get("AIOBS_TEST_CLICKHOUSE_PASSWORD", ""),
        cursor_codec=codec,
    )
    await instance.start()
    try:
        yield instance
    finally:
        try:
            await instance._execute(f"DROP DATABASE IF EXISTS {database}", {})
        finally:
            await instance.close()


@pytest.fixture
def scope() -> AnalyticsScope:
    return AnalyticsScope(organization_id=ORG, project_id=PROJECT, environment=ENVIRONMENT)


def make_span(
    index: int,
    *,
    trace_id: str | None = None,
    organization_id: str = ORG,
    model: str = "mock-model-v1",
    status: str = "ok",
    duration_ms: float = 10.0,
    category: str = "chat_completion",
    tokens: int = 100,
    tags: list[str] | None = None,
    attributes: dict | None = None,
    parent: str | None = None,
    version: int = 1,
) -> SpanRow:
    start = BASE_NANO + index * 1_000_000_000
    return SpanRow(
        organization_id=organization_id,
        project_id=PROJECT,
        environment=ENVIRONMENT,
        trace_id=trace_id or f"{index:032x}",
        span_id=f"{index:016x}",
        parent_span_id=parent,
        name=f"span-{index}",
        kind="client",
        category=category,
        start_unix_nano=start,
        end_unix_nano=start + int(duration_ms * 1e6),
        duration_ns=int(duration_ms * 1e6),
        status=status,
        model=model,
        provider="mock",
        input_tokens=tokens,
        output_tokens=tokens // 2,
        total_tokens=tokens + tokens // 2,
        usage_source="provider",
        cost_total=Decimal("0.0015"),
        cost_currency="USD",
        cost_estimation_status="final",
        tags=tags if tags is not None else ["demo"],
        attributes=attributes or {"custom.key": f"value-{index % 3}"},
        ingested_at=NOW,
        ingest_version=version,
        content_hash=f"sha256:{index:064x}",
    )


def make_trace(index: int, *, organization_id: str = ORG, duration_ms: float = 100.0) -> TraceRow:
    start = BASE_NANO + index * 1_000_000_000
    return TraceRow(
        organization_id=organization_id,
        project_id=PROJECT,
        environment=ENVIRONMENT,
        trace_id=f"{index:032x}",
        name=f"trace-{index}",
        start_unix_nano=start,
        end_unix_nano=start + int(duration_ms * 1e6),
        duration_ns=int(duration_ms * 1e6),
        status="ok",
        span_count=3,
        total_tokens=150,
        total_cost=Decimal("0.0045"),
        cost_currency="USD",
        models=["mock-model-v1"],
        tags=["demo"],
        ingested_at=NOW,
        ingest_version=1,
        complete=True,
    )


class TestWritesAndReads:
    async def test_round_trips_a_span(self, store: AnalyticsStore, scope: AnalyticsScope) -> None:
        await store.insert_spans([make_span(1)])
        spans = await store.get_spans(scope, f"{1:032x}")
        assert len(spans) == 1
        stored = spans[0]
        assert stored.name == "span-1"
        assert stored.model == "mock-model-v1"
        assert stored.input_tokens == 100
        assert stored.cost_total == Decimal("0.0015")
        assert stored.tags == ["demo"]
        assert stored.attributes["custom.key"] == "value-1"

    async def test_insert_is_idempotent(self, store: AnalyticsStore, scope: AnalyticsScope) -> None:
        """At-least-once delivery means the same span arrives twice; the store
        must collapse it rather than double-count."""
        span = make_span(1)
        await store.insert_spans([span])
        await store.insert_spans([span])
        start, end = _window()
        assert await store.count_spans(scope, start=start, end=end) == 1

    async def test_a_newer_version_replaces_an_older_one(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        await store.insert_spans([make_span(1, status="unset", version=1)])
        await store.insert_spans([make_span(1, status="error", version=2)])
        spans = await store.get_spans(scope, f"{1:032x}")
        assert len(spans) == 1
        assert spans[0].status == "error"

    async def test_round_trips_derived_rows(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        trace_id = f"{7:032x}"
        span_id = f"{7:016x}"
        await store.insert_spans([make_span(7)])
        await store.insert_span_events(
            [
                SpanEventRow(
                    organization_id=ORG,
                    project_id=PROJECT,
                    environment=ENVIRONMENT,
                    trace_id=trace_id,
                    span_id=span_id,
                    time_unix_nano=BASE_NANO,
                    name="aiobs.first_token",
                    sequence=0,
                    attributes={"index": 0},
                    ingested_at=NOW,
                )
            ]
        )
        await store.insert_retrieval_documents(
            [
                RetrievalDocumentRow(
                    organization_id=ORG,
                    project_id=PROJECT,
                    environment=ENVIRONMENT,
                    trace_id=trace_id,
                    span_id=span_id,
                    time_unix_nano=BASE_NANO,
                    document_id="doc-1",
                    rank=0,
                    score=0.9,
                    rerank_rank=1,
                    selected=True,
                    token_count=40,
                    ingested_at=NOW,
                )
            ]
        )
        await store.insert_agent_steps(
            [
                AgentStepRow(
                    organization_id=ORG,
                    project_id=PROJECT,
                    environment=ENVIRONMENT,
                    trace_id=trace_id,
                    span_id=span_id,
                    agent_id="agent-1",
                    step_number=1,
                    start_unix_nano=BASE_NANO,
                    step_type="decision",
                    ingested_at=NOW,
                )
            ]
        )
        await store.insert_cost_records(
            [
                CostRecordRow(
                    organization_id=ORG,
                    project_id=PROJECT,
                    environment=ENVIRONMENT,
                    trace_id=trace_id,
                    span_id=span_id,
                    time_unix_nano=BASE_NANO,
                    provider="mock",
                    model="mock-model-v1",
                    currency="USD",
                    total=Decimal("0.0015"),
                    formula="100/1000000*1.00",
                    ingested_at=NOW,
                )
            ]
        )

        assert len(await store.get_span_events(scope, trace_id)) == 1
        documents = await store.get_retrieval_documents(scope, trace_id)
        assert documents[0].rank_delta == -1  # promoted by reranking
        assert len(await store.get_agent_steps(scope, trace_id)) == 1
        costs = await store.get_cost_records(scope, trace_id)
        assert costs[0].total == Decimal("0.0015")
        assert costs[0].formula == "100/1000000*1.00"


class TestTenantIsolation:
    async def test_another_tenants_spans_are_invisible(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        await store.insert_spans([make_span(1), make_span(2, organization_id="org_someone_else")])
        start, end = _window()
        assert await store.count_spans(scope, start=start, end=end) == 1
        assert await store.get_spans(scope, f"{2:032x}") == []

    async def test_another_tenants_trace_cannot_be_fetched_by_id(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        await store.upsert_traces([make_trace(3, organization_id="org_someone_else")])
        assert await store.get_trace(scope, f"{3:032x}") is None


class TestSearchAndPagination:
    async def test_keyset_pagination_covers_every_row_exactly_once(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        await store.upsert_traces([make_trace(index) for index in range(25)])
        start, end = _window()

        seen: list[str] = []
        cursor: PageRequest | None = PageRequest(limit=7)
        codec = CursorCodec("conformance-secret")
        for _ in range(10):
            page = await store.search_traces(scope, start=start, end=end, page=cursor)
            seen.extend(item.trace_id for item in page.items)
            if not page.has_more or page.next_cursor is None:
                break
            from aiobs_api.core.query import revive_cursor_values

            cursor = PageRequest(
                limit=7, cursor=revive_cursor_values(codec.decode(page.next_cursor))
            )

        assert len(seen) == 25
        assert len(set(seen)) == 25  # no duplicates across page boundaries

    async def test_sorting_is_applied(self, store: AnalyticsStore, scope: AnalyticsScope) -> None:
        await store.upsert_traces(
            [make_trace(index, duration_ms=index * 10 + 1) for index in range(5)]
        )
        start, end = _window()
        page = await store.search_traces(
            scope,
            start=start,
            end=end,
            sort=parse_sort(TRACE_SCHEMA, "-duration_ms"),
            page=PageRequest(limit=10),
        )
        durations = [item.duration_ns for item in page.items]
        assert durations == sorted(durations, reverse=True)

    async def test_time_range_is_half_open(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        """Adjacent windows must tile without double counting."""
        await store.insert_spans([make_span(0), make_span(1)])
        boundary = NOW + timedelta(seconds=1)
        first = await store.count_spans(scope, start=NOW - timedelta(hours=1), end=boundary)
        second = await store.count_spans(scope, start=boundary, end=NOW + timedelta(hours=1))
        assert first == 1 and second == 1


class TestFilters:
    async def test_equality_filter(self, store: AnalyticsStore, scope: AnalyticsScope) -> None:
        await store.insert_spans([make_span(1, status="ok"), make_span(2, status="error")])
        start, end = _window()
        page = await store.search_spans(
            scope,
            start=start,
            end=end,
            filters=parse_filters(SPAN_SCHEMA, ["status:eq:error"]),
            page=PageRequest(limit=50),
        )
        assert [item.span_id for item in page.items] == [f"{2:016x}"]

    async def test_numeric_comparison_filter_converts_units(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        """The API speaks milliseconds; storage is nanoseconds."""
        await store.insert_spans([make_span(1, duration_ms=5), make_span(2, duration_ms=500)])
        start, end = _window()
        page = await store.search_spans(
            scope,
            start=start,
            end=end,
            filters=parse_filters(SPAN_SCHEMA, ["duration_ms:gte:100"]),
            page=PageRequest(limit=50),
        )
        assert [item.span_id for item in page.items] == [f"{2:016x}"]

    async def test_in_filter(self, store: AnalyticsStore, scope: AnalyticsScope) -> None:
        await store.insert_spans([make_span(index, model=f"model-{index}") for index in range(4)])
        start, end = _window()
        page = await store.search_spans(
            scope,
            start=start,
            end=end,
            filters=parse_filters(SPAN_SCHEMA, ["model:in:model-1|model-3"]),
            page=PageRequest(limit=50),
        )
        assert {item.model for item in page.items} == {"model-1", "model-3"}

    async def test_array_membership_filter(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        await store.insert_spans(
            [make_span(1, tags=["production", "beta"]), make_span(2, tags=["staging"])]
        )
        start, end = _window()
        page = await store.search_spans(
            scope,
            start=start,
            end=end,
            filters=parse_filters(SPAN_SCHEMA, ["tags:has:production"]),
            page=PageRequest(limit=50),
        )
        assert [item.span_id for item in page.items] == [f"{1:016x}"]

    async def test_map_subpath_filter(self, store: AnalyticsStore, scope: AnalyticsScope) -> None:
        await store.insert_spans(
            [
                make_span(1, attributes={"team.name": "search"}),
                make_span(2, attributes={"team.name": "billing"}),
            ]
        )
        start, end = _window()
        page = await store.search_spans(
            scope,
            start=start,
            end=end,
            filters=parse_filters(SPAN_SCHEMA, ["attributes.team.name:eq:billing"]),
            page=PageRequest(limit=50),
        )
        assert [item.span_id for item in page.items] == [f"{2:016x}"]

    async def test_contains_filter_escapes_wildcards(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        """A user's literal '%' must not become a wildcard."""
        await store.insert_spans([make_span(1, attributes={"note": "100% done"})])
        start, end = _window()
        page = await store.search_spans(
            scope,
            start=start,
            end=end,
            filters=parse_filters(SPAN_SCHEMA, ["attributes.note:contains:100%"]),
            page=PageRequest(limit=50),
        )
        assert len(page.items) == 1


class TestAggregates:
    async def test_percentiles_are_ordered_and_exact(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        # Durations 1..100 ms, so the expected order statistics are computable.
        await store.insert_spans([make_span(index, duration_ms=index + 1) for index in range(100)])
        start, end = _window()
        results = await store.percentiles(scope, start=start, end=end, column="duration_ns")
        assert len(results) == 1
        result = results[0]
        assert result.count == 100
        assert result.p50 is not None and result.p95 is not None and result.p99 is not None
        assert result.p50 <= result.p95 <= result.p99 <= (result.max or 0)
        # quantileExactLow / nearest-lower: index floor(0.5 * 99) = 49 -> 50ms.
        assert result.p50 == pytest.approx(50e6)

    async def test_percentiles_group(self, store: AnalyticsStore, scope: AnalyticsScope) -> None:
        await store.insert_spans(
            [make_span(index, model="fast", duration_ms=1) for index in range(5)]
            + [make_span(index + 10, model="slow", duration_ms=100) for index in range(5)]
        )
        start, end = _window()
        results = await store.percentiles(
            scope, start=start, end=end, column="duration_ns", group_by=["model"]
        )
        by_model = {result.keys[0]: result for result in results}
        assert by_model["fast"].p50 is not None
        assert by_model["slow"].p50 > by_model["fast"].p50  # type: ignore[operator]

    async def test_timeseries_buckets(self, store: AnalyticsStore, scope: AnalyticsScope) -> None:
        await store.insert_spans([make_span(index) for index in range(10)])
        start, end = _window()
        groups = await store.timeseries(
            MetricQuery(
                scope=scope,
                start=start,
                end=end,
                metric="total_tokens",
                aggregation=Aggregation.SUM,
                interval=TimeInterval.MINUTE,
                source="spans",
            )
        )
        assert groups
        total = sum(float(point.value or 0) for group in groups for point in group.points)
        assert total == 10 * 150

    async def test_aggregate_groups_and_limits(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        await store.insert_spans(
            [make_span(index, model=f"model-{index % 3}") for index in range(9)]
        )
        start, end = _window()
        groups = await store.aggregate(
            MetricQuery(
                scope=scope,
                start=start,
                end=end,
                metric=None,
                aggregation=Aggregation.COUNT,
                group_by=("model",),
                source="spans",
            )
        )
        assert {group.keys[0] for group in groups} == {"model-0", "model-1", "model-2"}
        assert all(group.total == 3 for group in groups)

    async def test_distinct_values(self, store: AnalyticsStore, scope: AnalyticsScope) -> None:
        await store.insert_spans(
            [make_span(index, model="a") for index in range(3)]
            + [make_span(index + 10, model="b") for index in range(1)]
        )
        start, end = _window()
        values = await store.distinct_values(scope, column="model", start=start, end=end)
        assert dict(values) == {"a": 3, "b": 1}

    async def test_rejects_a_non_aggregatable_column(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        from aiobs_api.core.errors import ValidationFailedError

        start, end = _window()
        with pytest.raises(ValidationFailedError):
            await store.percentiles(scope, start=start, end=end, column="name")


class TestRollupSupport:
    async def test_stale_rollups_are_detected(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        """The reconciliation path: a trace whose spans are newer than its
        roll-up must be reported for recomputation."""
        await store.insert_spans([make_span(1)])
        stale = await store.trace_ids_needing_rollup(since=NOW - timedelta(hours=1))
        assert any(row[3] == f"{1:032x}" for row in stale)

        await store.upsert_traces([make_trace(1)])
        # After the roll-up is written with a matching timestamp it is no
        # longer stale.
        remaining = await store.trace_ids_needing_rollup(since=NOW - timedelta(hours=1))
        assert all(row[3] != f"{1:032x}" for row in remaining)

    async def test_derived_rows_can_be_cleared_before_re_derivation(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        trace_id = f"{5:032x}"
        await store.insert_retrieval_documents(
            [
                RetrievalDocumentRow(
                    organization_id=ORG,
                    project_id=PROJECT,
                    environment=ENVIRONMENT,
                    trace_id=trace_id,
                    span_id=f"{5:016x}",
                    time_unix_nano=BASE_NANO,
                    document_id="doc",
                    rank=0,
                    ingested_at=NOW,
                )
            ]
        )
        await store.delete_trace_children(scope, trace_id)
        assert await store.get_retrieval_documents(scope, trace_id) == []


class TestRetention:
    async def test_deletes_rows_older_than_the_cutoff(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        old = make_span(1)
        old.start_unix_nano = int((NOW - timedelta(days=60)).timestamp() * 1e9)
        recent = make_span(2)
        await store.insert_spans([old, recent])

        result = await store.delete_expired(table="spans", cutoff=NOW - timedelta(days=30))
        assert result.rows_deleted >= 1

        start, end = _window()
        assert await store.count_spans(scope, start=start, end=end) == 1


class TestMoneyAggregation:
    """Money must survive aggregation exactly, on every driver.

    A float sum of 0.0015 a few hundred times produces a total like
    2.1456037299999977. Every driver in this suite must return the exact
    decimal instead, because these totals are what invoices are reconciled
    against.
    """

    async def test_sum_of_costs_is_exact(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        # 0.1 + 0.2 is the canonical float trap; three of them must total 0.6.
        amounts = [Decimal("0.1"), Decimal("0.2"), Decimal("0.3")]
        spans = []
        for index, amount in enumerate(amounts):
            span = make_span(index)
            span.cost_total = amount
            spans.append(span)
        await store.insert_spans(spans)

        start, end = _window()
        groups = await store.aggregate(
            MetricQuery(
                scope=scope,
                start=start,
                end=end,
                metric="cost_total",
                aggregation=Aggregation.SUM,
                source="spans",
            )
        )

        assert len(groups) == 1
        total = groups[0].total
        assert isinstance(total, Decimal)
        assert total == Decimal("0.6")
        # The exact string matters as much as the value: it is what the API
        # serialises and what a person reconciles against an invoice.
        assert Decimal(str(total)) == Decimal("0.6")

    async def test_many_small_amounts_do_not_accumulate_error(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        unit = Decimal("0.0000001")
        spans = []
        for index in range(200):
            span = make_span(index)
            span.cost_total = unit
            spans.append(span)
        await store.insert_spans(spans)

        start, end = _window()
        groups = await store.aggregate(
            MetricQuery(
                scope=scope,
                start=start,
                end=end,
                metric="cost_total",
                aggregation=Aggregation.SUM,
                source="spans",
            )
        )
        assert Decimal(str(groups[0].total)) == Decimal("0.0000200")

    async def test_null_costs_are_skipped_not_counted_as_zero_rows(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        priced = make_span(1)
        priced.cost_total = Decimal("1.25")
        unpriced = make_span(2)
        unpriced.cost_total = None
        await store.insert_spans([priced, unpriced])

        start, end = _window()
        groups = await store.aggregate(
            MetricQuery(
                scope=scope,
                start=start,
                end=end,
                metric="cost_total",
                aggregation=Aggregation.SUM,
                source="spans",
            )
        )
        # The total is the priced amount; the unpriced row still counts towards
        # the row count so its absence from the total is visible.
        assert Decimal(str(groups[0].total)) == Decimal("1.25")
        assert groups[0].count == 2

    async def test_groups_are_ranked_numerically_not_lexically(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        # "9" sorts above "10" as text; the ranking must be numeric.
        cheap = make_span(1, model="cheap-model")
        cheap.cost_total = Decimal("9")
        expensive = make_span(2, model="expensive-model")
        expensive.cost_total = Decimal("10")
        await store.insert_spans([cheap, expensive])

        start, end = _window()
        groups = await store.aggregate(
            MetricQuery(
                scope=scope,
                start=start,
                end=end,
                metric="cost_total",
                aggregation=Aggregation.SUM,
                group_by=("model",),
                source="spans",
            )
        )
        assert [group.keys[0] for group in groups] == ["expensive-model", "cheap-model"]

    async def test_timeseries_keeps_costs_exact(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        spans = []
        for index in range(3):
            span = make_span(index)
            span.cost_total = Decimal("0.1")
            spans.append(span)
        await store.insert_spans(spans)

        start, end = _window()
        groups = await store.timeseries(
            MetricQuery(
                scope=scope,
                start=start,
                end=end,
                metric="cost_total",
                aggregation=Aggregation.SUM,
                interval=TimeInterval.HOUR,
                source="spans",
            )
        )
        total = sum(
            (Decimal(str(point.value)) for group in groups for point in group.points),
            Decimal(0),
        )
        assert total == Decimal("0.3")


class TestKeysetCursorsOverMoney:
    async def test_paginating_by_cost_round_trips_the_cursor(
        self, store: AnalyticsStore, scope: AnalyticsScope
    ) -> None:
        """A cursor over a decimal sort key must survive encode/decode.

        Sorting by cost puts a Decimal in the keyset. Serialising it as a JSON
        number would round it, which can place the cursor on the wrong side of
        a row boundary and silently skip or repeat a trace.
        """
        traces = []
        for index in range(5):
            trace = make_trace(index)
            trace.total_cost = Decimal(f"0.{index}00000000000001")
            traces.append(trace)
        await store.upsert_traces(traces)

        start, end = _window()
        first = await store.search_traces(
            scope,
            start=start,
            end=end,
            filters=(),
            sort=parse_sort(TRACE_SCHEMA, "-cost"),
            page=PageRequest(limit=2),
        )
        assert len(first.items) == 2
        assert first.next_cursor

        from aiobs_api.core.query import revive_cursor_values

        codec = CursorCodec("conformance-secret")
        revived = revive_cursor_values(codec.decode(first.next_cursor))
        # The decimal sort key must come back as a Decimal, not a float.
        assert isinstance(revived["total_cost"], Decimal), revived

        second = await store.search_traces(
            scope,
            start=start,
            end=end,
            filters=(),
            sort=parse_sort(TRACE_SCHEMA, "-cost"),
            page=PageRequest(limit=2, cursor=revived),
        )
        seen = [item.trace_id for item in first.items] + [item.trace_id for item in second.items]
        assert len(seen) == len(set(seen)), "cursor pagination repeated a row"
        costs = [trace.total_cost for trace in [*first.items, *second.items] if trace.total_cost]
        assert costs == sorted(costs, reverse=True)
