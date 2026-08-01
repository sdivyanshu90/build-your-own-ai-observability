"""The analytics store interface.

Everything the platform asks of its high-volume store is declared here, as an
abstract base class rather than a ``typing.Protocol``, because the two concrete
drivers share a substantial SQL-building base class and inheritance expresses
that honestly.

Two drivers implement it:

``ClickHouseAnalyticsStore``
    Production. Columnar, partitioned by day, ordered for the trace-explorer
    access pattern.

``SqliteAnalyticsStore``
    Development, CI and unit tests. Same semantics, no daemon.

They are held to identical behaviour by a shared conformance suite
(``tests/integration/analytics/test_conformance.py``) that runs every test
against both. That suite is the reason a second implementation is a
maintainable asset rather than a divergence risk -- see ADR-0013.

Every read method takes an :class:`AnalyticsScope`. There is no way to express
a query that spans tenants.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from ...core.query import FilterCondition, Page, PageRequest, SortTerm
from .rows import (
    AgentStepRow,
    AnalyticsScope,
    CostRecordRow,
    RetrievalDocumentRow,
    SpanEventRow,
    SpanRow,
    TraceRow,
)

__all__ = [
    "Aggregation",
    "AnalyticsStore",
    "GroupedMetric",
    "MetricPoint",
    "MetricQuery",
    "PercentileResult",
    "TimeInterval",
]


class TimeInterval(str, Enum):
    """Bucket width for time-series queries."""

    MINUTE = "1m"
    FIVE_MINUTES = "5m"
    FIFTEEN_MINUTES = "15m"
    HOUR = "1h"
    SIX_HOURS = "6h"
    DAY = "1d"
    WEEK = "7d"

    @property
    def seconds(self) -> int:
        return {
            TimeInterval.MINUTE: 60,
            TimeInterval.FIVE_MINUTES: 300,
            TimeInterval.FIFTEEN_MINUTES: 900,
            TimeInterval.HOUR: 3_600,
            TimeInterval.SIX_HOURS: 21_600,
            TimeInterval.DAY: 86_400,
            TimeInterval.WEEK: 604_800,
        }[self]

    @classmethod
    def auto(cls, start: datetime, end: datetime, target_buckets: int = 60) -> TimeInterval:
        """Pick the coarsest interval that still yields ~``target_buckets`` points.

        Choosing automatically stops the UI from asking for a minute-resolution
        series over 90 days, which would return 130,000 points that no chart can
        render and no browser should receive.
        """
        span_seconds = max((end - start).total_seconds(), 1.0)
        ideal = span_seconds / target_buckets
        for interval in cls:
            if interval.seconds >= ideal:
                return interval
        return cls.WEEK


class Aggregation(str, Enum):
    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    P50 = "p50"
    P75 = "p75"
    P90 = "p90"
    P95 = "p95"
    P99 = "p99"
    UNIQUE = "unique"

    @property
    def quantile(self) -> float | None:
        return {
            Aggregation.P50: 0.50,
            Aggregation.P75: 0.75,
            Aggregation.P90: 0.90,
            Aggregation.P95: 0.95,
            Aggregation.P99: 0.99,
        }.get(self)


@dataclass(frozen=True, slots=True)
class MetricQuery:
    """A dashboard query: what to measure, over what, sliced how."""

    scope: AnalyticsScope
    start: datetime
    end: datetime
    #: Physical column to aggregate. ``None`` for ``count``.
    metric: str | None
    aggregation: Aggregation
    interval: TimeInterval | None = None
    #: Column names to group by, e.g. ``("model",)`` or ``("model", "status")``.
    group_by: tuple[str, ...] = ()
    filters: tuple[FilterCondition, ...] = ()
    #: Keep the top N groups by aggregate value; the rest collapse into
    #: ``__other__`` so a high-cardinality dimension cannot blow up the response.
    limit_groups: int = 20
    #: Which physical table to read.
    source: str = "spans"


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """One (bucket, value) pair in a time series."""

    bucket: datetime
    value: float | Decimal | None
    count: int = 0


@dataclass(frozen=True, slots=True)
class GroupedMetric:
    """A time series (or single value) for one combination of group-by keys."""

    keys: tuple[str, ...]
    points: tuple[MetricPoint, ...] = ()
    total: float | Decimal | None = None
    count: int = 0


@dataclass(frozen=True, slots=True)
class PercentileResult:
    """Latency distribution for one group."""

    keys: tuple[str, ...]
    count: int
    p50: float | None = None
    p75: float | None = None
    p90: float | None = None
    p95: float | None = None
    p99: float | None = None
    avg: float | None = None
    max: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "keys": list(self.keys),
            "count": self.count,
            "p50": self.p50,
            "p75": self.p75,
            "p90": self.p90,
            "p95": self.p95,
            "p99": self.p99,
            "avg": self.avg,
            "max": self.max,
        }


@dataclass(slots=True)
class RetentionResult:
    """Outcome of one retention sweep pass."""

    table: str
    rows_deleted: int
    cutoff: datetime
    exhausted: bool = field(default=True)


class AnalyticsStore(ABC):
    """Read/write interface to the high-volume trace store."""

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    async def start(self) -> None:
        """Open connections and verify the schema is present."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources. Must be safe to call twice."""

    @abstractmethod
    async def check_health(self) -> None:
        """Raise ``DependencyUnavailableError`` when the store is not usable."""

    @abstractmethod
    async def migrate(self) -> None:
        """Create or update the analytics schema.

        Idempotent: every statement is ``CREATE ... IF NOT EXISTS``. The
        analytics schema is managed here rather than by Alembic because
        ClickHouse DDL is not something Alembic models, and because the schema
        must be creatable by a worker starting cold in a fresh cluster.
        """

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    @abstractmethod
    async def insert_spans(self, rows: Sequence[SpanRow]) -> int:
        """Insert spans. Must be idempotent with respect to (trace_id, span_id)."""

    @abstractmethod
    async def upsert_traces(self, rows: Sequence[TraceRow]) -> int:
        """Insert or replace trace roll-ups."""

    @abstractmethod
    async def insert_span_events(self, rows: Sequence[SpanEventRow]) -> int: ...

    @abstractmethod
    async def insert_retrieval_documents(self, rows: Sequence[RetrievalDocumentRow]) -> int: ...

    @abstractmethod
    async def insert_agent_steps(self, rows: Sequence[AgentStepRow]) -> int: ...

    @abstractmethod
    async def insert_cost_records(self, rows: Sequence[CostRecordRow]) -> int: ...

    @abstractmethod
    async def delete_trace_children(self, scope: AnalyticsScope, trace_id: str) -> None:
        """Remove derived rows for a trace before re-deriving them.

        Called when a trace is recomputed after late-arriving spans, so that
        retrieval documents and agent steps do not accumulate duplicates.
        """

    # ------------------------------------------------------------------
    # trace reads
    # ------------------------------------------------------------------

    @abstractmethod
    async def search_traces(
        self,
        scope: AnalyticsScope,
        *,
        start: datetime,
        end: datetime,
        filters: Sequence[FilterCondition] = (),
        sort: Sequence[SortTerm] = (),
        page: PageRequest | None = None,
        text_query: str | None = None,
    ) -> Page[TraceRow]:
        """Keyset-paginated trace search."""

    @abstractmethod
    async def get_trace(self, scope: AnalyticsScope, trace_id: str) -> TraceRow | None: ...

    @abstractmethod
    async def get_traces(self, scope: AnalyticsScope, trace_ids: Sequence[str]) -> list[TraceRow]:
        """Batch fetch, used by trace comparison."""

    @abstractmethod
    async def get_spans(
        self, scope: AnalyticsScope, trace_id: str, *, limit: int = 10_000
    ) -> list[SpanRow]: ...

    @abstractmethod
    async def get_span(
        self, scope: AnalyticsScope, trace_id: str, span_id: str
    ) -> SpanRow | None: ...

    @abstractmethod
    async def get_span_events(
        self, scope: AnalyticsScope, trace_id: str, span_id: str | None = None
    ) -> list[SpanEventRow]: ...

    @abstractmethod
    async def get_retrieval_documents(
        self, scope: AnalyticsScope, trace_id: str, span_id: str | None = None
    ) -> list[RetrievalDocumentRow]: ...

    @abstractmethod
    async def get_agent_steps(self, scope: AnalyticsScope, trace_id: str) -> list[AgentStepRow]: ...

    @abstractmethod
    async def get_cost_records(
        self, scope: AnalyticsScope, trace_id: str
    ) -> list[CostRecordRow]: ...

    @abstractmethod
    async def search_spans(
        self,
        scope: AnalyticsScope,
        *,
        start: datetime,
        end: datetime,
        filters: Sequence[FilterCondition] = (),
        sort: Sequence[SortTerm] = (),
        page: PageRequest | None = None,
    ) -> Page[SpanRow]:
        """Span-level search, used by the span explorer and by exports."""

    # ------------------------------------------------------------------
    # aggregates
    # ------------------------------------------------------------------

    @abstractmethod
    async def timeseries(self, query: MetricQuery) -> list[GroupedMetric]:
        """Bucketed time series, one entry per group-by combination."""

    @abstractmethod
    async def aggregate(self, query: MetricQuery) -> list[GroupedMetric]:
        """Single aggregate value per group, no time bucketing."""

    @abstractmethod
    async def percentiles(
        self,
        scope: AnalyticsScope,
        *,
        start: datetime,
        end: datetime,
        column: str = "duration_ns",
        group_by: Sequence[str] = (),
        filters: Sequence[FilterCondition] = (),
        source: str = "spans",
        limit_groups: int = 20,
    ) -> list[PercentileResult]:
        """Latency percentiles computed in the store, never in application memory.

        Pulling a million durations into Python to call ``statistics.quantiles``
        would move gigabytes over the wire to compute five numbers. ClickHouse
        computes them with a t-digest during the scan; the SQLite driver uses an
        exact ordered selection, which is slower but correct and fine at
        development volumes.
        """

    @abstractmethod
    async def distinct_values(
        self,
        scope: AnalyticsScope,
        *,
        column: str,
        start: datetime,
        end: datetime,
        prefix: str | None = None,
        limit: int = 100,
    ) -> list[tuple[str, int]]:
        """Distinct values and their counts, for filter autocomplete."""

    @abstractmethod
    async def count_spans(self, scope: AnalyticsScope, *, start: datetime, end: datetime) -> int:
        """Span count in a window, used for quota enforcement."""

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------

    @abstractmethod
    async def delete_expired(
        self, *, table: str, cutoff: datetime, batch_size: int = 10_000
    ) -> RetentionResult:
        """Delete rows older than ``cutoff`` from ``table``, in bounded batches."""

    @abstractmethod
    async def trace_ids_needing_rollup(
        self, *, since: datetime, limit: int = 1_000
    ) -> list[tuple[str, str, str, str]]:
        """Return ``(org, project, environment, trace_id)`` tuples whose roll-up
        is stale because spans arrived after the roll-up was last computed."""


#: Tables the retention sweep manages, in deletion order. Derived tables are
#: cleared before spans so a crash mid-sweep leaves orphaned derived rows
#: (harmless, cleaned next pass) rather than spans whose details have vanished.
RETENTION_TABLES: tuple[str, ...] = (
    "retrieval_documents",
    "agent_steps",
    "span_events",
    "cost_records",
    "spans",
    "traces",
)
