"""Dashboard and monitoring queries.

Thin, deliberately: the heavy lifting belongs in the analytics store, where the
data is. This layer picks bucket widths, validates that a requested dimension
exists, marks partial results, and computes period-over-period comparisons.

Two behaviours worth calling out:

**Partial-data marking.** A window whose most recent bucket is still filling
returns ``partial=True`` for that bucket. Without it, every dashboard shows a
cliff at the right-hand edge and someone opens an incident about a traffic drop
that is really a half-finished five-minute bucket.

**Comparison windows are equal length and immediately preceding.** "vs previous
period" over a 7-day window compares against the 7 days before it, not against
"last week" in a calendar sense. Calendar comparisons are surprising when the
window is not calendar-aligned.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ..core.errors import ValidationFailedError
from ..core.logging import get_logger
from ..core.query import FilterCondition
from ..core.timeutil import Clock
from ..domain.principal import Principal
from ..domain.rbac import Permission
from ..storage.analytics.protocol import (
    Aggregation,
    AnalyticsStore,
    GroupedMetric,
    MetricPoint,
    MetricQuery,
    PercentileResult,
    TimeInterval,
)
from ..storage.analytics.rows import AnalyticsScope
from ..storage.analytics.schemas import aggregatable_columns, schema_for

__all__ = ["DashboardSeries", "MetricsService", "OverviewSummary"]

log = get_logger(__name__)

#: Dimensions the dashboards may group by. Restricted to low-cardinality
#: columns: grouping by ``trace_id`` would return millions of series.
GROUPABLE_DIMENSIONS: frozenset[str] = frozenset(
    {
        "model",
        "provider",
        "category",
        "status",
        "environment",
        "service_name",
        "prompt_version_id",
        "model_config_id",
        "dataset_version_id",
        "tool_name",
        "tool_status",
        "retriever_name",
        "usage_source",
        "cost_estimation_status",
        "session_id",
        "subject_id",
        "release",
    }
)


@dataclass(slots=True)
class DashboardSeries:
    """A named metric plus its grouped time series."""

    metric: str
    aggregation: str
    interval: str
    groups: list[GroupedMetric]
    #: Bucket start times that may still be filling.
    partial_buckets: list[datetime]
    unit: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "aggregation": self.aggregation,
            "interval": self.interval,
            "unit": self.unit,
            "partial_buckets": [bucket.isoformat() for bucket in self.partial_buckets],
            "groups": [
                {
                    "keys": list(group.keys),
                    "total": _numeric(group.total),
                    "count": group.count,
                    "points": [
                        {
                            "bucket": point.bucket.isoformat(),
                            "value": _numeric(point.value),
                            "count": point.count,
                        }
                        for point in group.points
                    ],
                }
                for group in self.groups
            ],
        }


@dataclass(slots=True)
class OverviewSummary:
    """The headline numbers on the overview dashboard."""

    request_count: int
    error_count: int
    error_rate: float
    total_tokens: int
    input_tokens: int
    output_tokens: int
    total_cost: Decimal | None
    cost_currency: str
    latency: PercentileResult | None
    time_to_first_token: PercentileResult | None
    #: Same numbers for the immediately preceding window, when requested.
    previous: dict[str, Any] | None = None
    #: True when any contributing cost was estimated or unpriced.
    cost_is_partial: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_count": self.request_count,
            "error_count": self.error_count,
            "error_rate": round(self.error_rate, 6),
            "total_tokens": self.total_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_cost": None if self.total_cost is None else str(self.total_cost),
            "cost_currency": self.cost_currency,
            "cost_is_partial": self.cost_is_partial,
            "latency": self.latency.as_dict() if self.latency else None,
            "time_to_first_token": (
                self.time_to_first_token.as_dict() if self.time_to_first_token else None
            ),
            "previous": self.previous,
        }


class MetricsService:
    """Builds dashboard read models from the analytics store."""

    def __init__(self, *, analytics: AnalyticsStore, clock: Clock) -> None:
        self._analytics = analytics
        self._clock = clock

    def _scope(
        self, principal: Principal, project_id: str, environment: str | None
    ) -> AnalyticsScope:
        principal.require(Permission.METRICS_READ)
        principal.require_project(project_id)
        return AnalyticsScope(
            organization_id=principal.organization_id,
            project_id=project_id,
            environment=environment,
        )

    def _validate_dimensions(self, group_by: Sequence[str]) -> tuple[str, ...]:
        if len(group_by) > 2:
            raise ValidationFailedError(
                "at most two grouping dimensions are supported; more produces a "
                "series count no chart can render"
            )
        unknown = [dimension for dimension in group_by if dimension not in GROUPABLE_DIMENSIONS]
        if unknown:
            raise ValidationFailedError(
                f"cannot group by {unknown}; available dimensions: {sorted(GROUPABLE_DIMENSIONS)}"
            )
        return tuple(group_by)

    async def timeseries(
        self,
        *,
        principal: Principal,
        project_id: str,
        environment: str | None,
        start: datetime,
        end: datetime,
        metric: str | None,
        aggregation: Aggregation,
        interval: TimeInterval | None = None,
        group_by: Sequence[str] = (),
        filters: Sequence[FilterCondition] = (),
        source: str = "spans",
        limit_groups: int = 20,
    ) -> DashboardSeries:
        scope = self._scope(principal, project_id, environment)
        if end <= start:
            raise ValidationFailedError("the end of the time range must be after its start")
        resolved_interval = interval or TimeInterval.auto(start, end)
        dimensions = self._validate_dimensions(group_by)
        column, rescale = _resolve_metric(source, metric)

        groups = await self._analytics.timeseries(
            MetricQuery(
                scope=scope,
                start=start,
                end=end,
                metric=column,
                aggregation=aggregation,
                interval=resolved_interval,
                group_by=dimensions,
                filters=tuple(filters),
                limit_groups=limit_groups,
                source=source,
            )
        )
        return DashboardSeries(
            metric=metric or "count",
            aggregation=aggregation.value,
            interval=resolved_interval.value,
            groups=[_rescale_group(group, rescale) for group in groups],
            partial_buckets=self._partial_buckets(end, resolved_interval),
            unit=_unit_for(metric),
        )

    def _partial_buckets(self, end: datetime, interval: TimeInterval) -> list[datetime]:
        """Buckets overlapping 'now' that are still accumulating data."""
        now = self._clock.now()
        if end <= now:
            return []
        width = timedelta(seconds=interval.seconds)
        boundary = datetime.fromtimestamp(
            (now.timestamp() // interval.seconds) * interval.seconds, tz=now.tzinfo
        )
        return [boundary] if boundary + width > now else []

    async def overview(
        self,
        *,
        principal: Principal,
        project_id: str,
        environment: str | None,
        start: datetime,
        end: datetime,
        filters: Sequence[FilterCondition] = (),
        compare_previous: bool = False,
    ) -> OverviewSummary:
        """Headline numbers, optionally against the preceding equal-length window."""
        summary = await self._overview_window(
            principal=principal,
            project_id=project_id,
            environment=environment,
            start=start,
            end=end,
            filters=filters,
        )
        if compare_previous:
            window = end - start
            previous = await self._overview_window(
                principal=principal,
                project_id=project_id,
                environment=environment,
                start=start - window,
                end=start,
                filters=filters,
            )
            summary.previous = previous.as_dict()
        return summary

    async def _overview_window(
        self,
        *,
        principal: Principal,
        project_id: str,
        environment: str | None,
        start: datetime,
        end: datetime,
        filters: Sequence[FilterCondition],
    ) -> OverviewSummary:
        scope = self._scope(principal, project_id, environment)
        base: dict[str, Any] = {
            "scope": scope,
            "start": start,
            "end": end,
            "filters": tuple(filters),
            "source": "traces",
        }

        counts = await self._analytics.aggregate(
            MetricQuery(**base, metric=None, aggregation=Aggregation.COUNT)
        )
        errors = await self._analytics.aggregate(
            MetricQuery(**base, metric="error_count", aggregation=Aggregation.SUM)
        )
        tokens = await self._analytics.aggregate(
            MetricQuery(**base, metric="total_tokens", aggregation=Aggregation.SUM)
        )
        input_tokens = await self._analytics.aggregate(
            MetricQuery(**base, metric="total_input_tokens", aggregation=Aggregation.SUM)
        )
        output_tokens = await self._analytics.aggregate(
            MetricQuery(**base, metric="total_output_tokens", aggregation=Aggregation.SUM)
        )
        cost = await self._analytics.aggregate(
            MetricQuery(**base, metric="total_cost", aggregation=Aggregation.SUM)
        )
        latency = await self._analytics.percentiles(
            scope,
            start=start,
            end=end,
            column="duration_ns",
            filters=tuple(filters),
            source="traces",
        )
        ttft = await self._analytics.percentiles(
            scope,
            start=start,
            end=end,
            column="time_to_first_token_ms",
            filters=tuple(filters),
            source="traces",
        )
        cost_status = await self._analytics.aggregate(
            MetricQuery(
                **{**base, "group_by": ("cost_estimation_status",)},
                metric=None,
                aggregation=Aggregation.COUNT,
            )
        )

        request_count = int(_scalar(counts) or 0)
        error_count = int(_scalar(errors) or 0)
        cost_total = _scalar(cost)
        return OverviewSummary(
            request_count=request_count,
            error_count=error_count,
            error_rate=(error_count / request_count) if request_count else 0.0,
            total_tokens=int(_scalar(tokens) or 0),
            input_tokens=int(_scalar(input_tokens) or 0),
            output_tokens=int(_scalar(output_tokens) or 0),
            total_cost=Decimal(str(cost_total)) if cost_total is not None else None,
            cost_currency="USD",
            latency=_normalise_latency(latency[0]) if latency else None,
            time_to_first_token=ttft[0] if ttft else None,
            cost_is_partial=any(
                group.keys and group.keys[0] in {"estimated", "unpriced"} and group.count > 0
                for group in cost_status
            ),
        )

    async def latency_percentiles(
        self,
        *,
        principal: Principal,
        project_id: str,
        environment: str | None,
        start: datetime,
        end: datetime,
        group_by: Sequence[str] = (),
        filters: Sequence[FilterCondition] = (),
        source: str = "spans",
        column: str = "duration_ns",
    ) -> list[PercentileResult]:
        scope = self._scope(principal, project_id, environment)
        results = await self._analytics.percentiles(
            scope,
            start=start,
            end=end,
            column=column,
            group_by=self._validate_dimensions(group_by),
            filters=tuple(filters),
            source=source,
        )
        # Durations are stored in nanoseconds; the API speaks milliseconds.
        return [
            _normalise_latency(result) if column.endswith("_ns") else result for result in results
        ]

    async def cost_breakdown(
        self,
        *,
        principal: Principal,
        project_id: str,
        environment: str | None,
        start: datetime,
        end: datetime,
        group_by: Sequence[str] = ("model",),
        filters: Sequence[FilterCondition] = (),
    ) -> list[GroupedMetric]:
        principal.require(Permission.COST_READ)
        scope = self._scope(principal, project_id, environment)
        return await self._analytics.aggregate(
            MetricQuery(
                scope=scope,
                start=start,
                end=end,
                metric="total",
                aggregation=Aggregation.SUM,
                group_by=self._validate_dimensions(group_by),
                filters=tuple(filters),
                source="cost_records",
                limit_groups=50,
            )
        )

    async def distinct_values(
        self,
        *,
        principal: Principal,
        project_id: str,
        environment: str | None,
        column: str,
        start: datetime,
        end: datetime,
        prefix: str | None = None,
    ) -> list[tuple[str, int]]:
        """Filter autocomplete."""
        scope = self._scope(principal, project_id, environment)
        if column not in GROUPABLE_DIMENSIONS:
            raise ValidationFailedError(
                f"cannot enumerate values of {column!r}; available: {sorted(GROUPABLE_DIMENSIONS)}"
            )
        return await self._analytics.distinct_values(
            scope, column=column, start=start, end=end, prefix=prefix
        )


def _scalar(groups: Sequence[GroupedMetric]) -> float | Decimal | None:
    if not groups:
        return None
    return groups[0].total


def _numeric(value: float | Decimal | None) -> float | str | None:
    if value is None:
        return None
    # Decimals are rendered as strings so JSON does not round money.
    return str(value) if isinstance(value, Decimal) else float(value)


def _normalise_latency(result: PercentileResult) -> PercentileResult:
    """Convert a nanosecond percentile result into milliseconds."""

    def to_ms(value: float | None) -> float | None:
        return None if value is None else value / 1e6

    return PercentileResult(
        keys=result.keys,
        count=result.count,
        p50=to_ms(result.p50),
        p75=to_ms(result.p75),
        p90=to_ms(result.p90),
        p95=to_ms(result.p95),
        p99=to_ms(result.p99),
        avg=to_ms(result.avg),
        max=to_ms(result.max),
    )


def _resolve_metric(source: str, metric: str | None) -> tuple[str | None, Callable[[Any], Any]]:
    """Map a client-supplied metric name onto a physical column.

    Clients use the same logical field names everywhere else -- filters, sort,
    group-by -- so requiring the physical column name here (``duration_ns``
    rather than ``duration_ms``) would be an inconsistency that shows up as a
    422 on a perfectly reasonable request. Physical names keep working, so
    nothing that already used them breaks.
    """
    if metric is None:
        return None, _identity

    aggregatable = aggregatable_columns(source)
    field = schema_for(source).field_map().get(metric)

    if field is not None and field.column in aggregatable:
        return field.column, _scale_for(metric, field.column)
    if metric in aggregatable:
        return metric, _identity

    known = sorted(
        {
            candidate.name
            for candidate in schema_for(source).fields
            if candidate.column in aggregatable
        }
    )
    raise ValidationFailedError(
        f"metric {metric!r} is not aggregatable on {source!r}; aggregatable: {known}"
    )


def _identity(value: Any) -> Any:
    return value


def _scale_for(logical: str, column: str) -> Callable[[Any], Any]:
    """Converter from the stored unit to the unit the field name implies."""
    if logical.endswith("_ms") and column.endswith("_ns"):

        def to_ms(value: Any) -> Any:
            if value is None:
                return None
            # Decimal / float both divide cleanly; money never has a unit scale,
            # so this never touches a currency value.
            return float(value) / 1e6

        return to_ms
    return _identity


def _rescale_group(group: GroupedMetric, rescale: Callable[[Any], Any]) -> GroupedMetric:
    if rescale is _identity:
        return group
    return GroupedMetric(
        keys=group.keys,
        points=tuple(
            MetricPoint(bucket=point.bucket, value=rescale(point.value), count=point.count)
            for point in group.points
        ),
        total=rescale(group.total),
        count=group.count,
    )


def _unit_for(metric: str | None) -> str:
    if metric is None:
        return "requests"
    if metric.endswith("_ns"):
        return "ns"
    if metric.endswith("_ms"):
        return "ms"
    if "token" in metric:
        return "tokens"
    if "cost" in metric or metric == "total":
        return "currency"
    return ""
