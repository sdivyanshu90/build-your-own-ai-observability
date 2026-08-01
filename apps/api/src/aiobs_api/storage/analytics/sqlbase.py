"""SQL construction shared by the ClickHouse and SQLite analytics drivers.

The two stores differ in dialect, not in semantics. Everything semantic --
which columns exist, how a filter maps to a predicate, how keyset pagination
composes, how time buckets are computed -- lives here and is written once.
Each driver supplies a handful of dialect primitives and its own execution and
bulk-insert path.

The alternative (two hand-written query layers) is how conformance suites start
passing while the implementations quietly diverge on the third operator nobody
tested.

**No user input ever becomes SQL text.** Column names come from
:mod:`aiobs_api.storage.analytics.schemas`, operators come from a closed enum,
and every value is bound as a parameter.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ...core.errors import ValidationFailedError
from ...core.query import (
    FilterCondition,
    FilterOperator,
    Page,
    PageRequest,
    SortDirection,
    SortTerm,
)
from ...core.timeutil import datetime_to_unix_nano, unix_nano_to_datetime
from .columns import TABLE_COLUMNS, TABLE_ROW_TYPES, ColumnKind, decode_row
from .protocol import (
    Aggregation,
    AnalyticsStore,
    GroupedMetric,
    MetricPoint,
    MetricQuery,
    PercentileResult,
)
from .rows import (
    AgentStepRow,
    AnalyticsScope,
    CostRecordRow,
    RetrievalDocumentRow,
    SpanEventRow,
    SpanRow,
    TraceRow,
)
from .schemas import aggregatable_columns, schema_for

__all__ = ["SqlAnalyticsStore"]

#: Column carrying the row's event time, per table. Every range scan filters on
#: it, and both drivers physically order/partition by it.
TIME_COLUMN: dict[str, str] = {
    "spans": "start_unix_nano",
    "traces": "start_unix_nano",
    "span_events": "time_unix_nano",
    "retrieval_documents": "time_unix_nano",
    "agent_steps": "start_unix_nano",
    "cost_records": "time_unix_nano",
}

#: Sentinel substituted for NULL in ordered comparisons so that keyset
#: pagination has a total order. Chosen as INT64_MIN so nulls sort first
#: ascending, which matches both dialects' default NULLS FIRST behaviour.
_NULL_SENTINEL = -9_223_372_036_854_775_808

_SCOPE_COLUMNS = ("organization_id", "project_id", "environment")


class SqlAnalyticsStore(AnalyticsStore):
    """Dialect-agnostic query construction over the analytics tables."""

    #: Set by subclasses; used in error messages and metrics labels.
    driver_name: str = "sql"

    # ------------------------------------------------------------------
    # dialect primitives -- implemented per driver
    # ------------------------------------------------------------------

    @abstractmethod
    def _param(self, name: str) -> str:
        """Render a bound-parameter placeholder for ``name``."""

    @abstractmethod
    def _array_contains(self, column: str, placeholder: str) -> str:
        """Predicate: string array ``column`` contains the bound value."""

    @abstractmethod
    def _map_value(self, column: str, key_placeholder: str) -> str:
        """Expression yielding the value stored at a map key."""

    @abstractmethod
    def _bool_literal(self, value: bool) -> str:
        """Render a boolean constant."""

    def _like_escape(self) -> str:
        """Trailing clause that declares the LIKE escape character.

        SQLite requires an explicit ``ESCAPE`` clause. ClickHouse already treats
        backslash as the escape character in LIKE patterns and rejects the
        clause as a syntax error, so it returns an empty string. The *pattern*
        escaping is identical either way.
        """
        return " ESCAPE '\\'"

    def _metric_order_expression(self) -> str:
        """Expression used to rank groups by their aggregate value.

        Kept separate from the selected value because a driver may return an
        exact decimal as text, which sorts lexicographically ("9" > "10"). The
        *ordering* only has to be numeric; the *value* has to be exact.
        """
        return "value"

    def _decimal_sum(self, column: str) -> str:
        """Exact SUM over a money column.

        The default is a plain ``SUM``, which is correct on any engine with a
        native decimal type. A driver whose storage is not decimal must override
        this -- summing money through a float is the one thing the cost engine
        exists to prevent, and a total like ``2.1456037299999977`` is how it
        announces itself.
        """
        return f"SUM(COALESCE({column}, 0))"

    @abstractmethod
    async def _fetch(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Run a read query and return rows as dictionaries."""

    @abstractmethod
    async def _execute(self, sql: str, params: Mapping[str, Any]) -> None:
        """Run a statement with no result set."""

    @abstractmethod
    async def _insert(self, table: str, rows: Sequence[Any]) -> int:
        """Bulk-insert dataclass rows into ``table``."""

    @property
    @abstractmethod
    def _decoders(self) -> Mapping[ColumnKind, Callable[[Any], Any]]:
        """Per-kind decoders converting result values back to Python."""

    @abstractmethod
    async def _percentile_rows(
        self,
        *,
        table: str,
        column: str,
        group_by: Sequence[str],
        where: str,
        params: dict[str, Any],
        levels: Sequence[float],
        limit_groups: int,
    ) -> list[dict[str, Any]]:
        """Driver-specific percentile computation.

        Kept out of the shared builder because ClickHouse computes all
        quantiles in a single aggregate function while SQLite needs a windowed
        CTE; the *definition* (exact lower order statistic) is identical and is
        asserted by the conformance suite.
        """

    # ------------------------------------------------------------------
    # parameter accumulation
    # ------------------------------------------------------------------

    class _Params:
        """Accumulates bound parameters and hands out unique names."""

        __slots__ = ("_counter", "values")

        def __init__(self) -> None:
            self.values: dict[str, Any] = {}
            self._counter = 0

        def add(self, value: Any) -> str:
            self._counter += 1
            name = f"p{self._counter}"
            self.values[name] = value
            return name

    # ------------------------------------------------------------------
    # WHERE construction
    # ------------------------------------------------------------------

    def _scope_predicate(self, scope: AnalyticsScope, params: _Params) -> list[str]:
        """Mandatory tenancy predicate. Always first, always present."""
        clauses = [f"organization_id = {self._param(params.add(scope.organization_id))}"]
        if scope.project_id:
            clauses.append(f"project_id = {self._param(params.add(scope.project_id))}")
        if scope.environment:
            clauses.append(f"environment = {self._param(params.add(scope.environment))}")
        return clauses

    def _time_predicate(
        self, table: str, start: datetime, end: datetime, params: _Params
    ) -> list[str]:
        column = TIME_COLUMN[table]
        low = self._param(params.add(datetime_to_unix_nano(start)))
        high = self._param(params.add(datetime_to_unix_nano(end)))
        # Half-open interval: adjacent windows tile without double counting.
        return [f"{column} >= {low}", f"{column} < {high}"]

    def _filter_predicate(self, condition: FilterCondition, params: _Params) -> str:
        column = condition.column
        operator = condition.operator
        value = condition.value

        if condition.subpath is not None:
            key = self._param(params.add(condition.subpath))
            column = self._map_value(column, key)

        # Duration filters are expressed in milliseconds by the API but stored
        # in nanoseconds; convert here so the physical unit never leaks out.
        scale = 1_000_000 if condition.field.name.endswith("_ms") and column.endswith("_ns") else 1

        def bind(raw: Any) -> str:
            if isinstance(raw, datetime):
                return self._param(params.add(datetime_to_unix_nano(raw)))
            if scale != 1 and isinstance(raw, (int, float)):
                return self._param(params.add(int(raw * scale)))
            if isinstance(raw, Decimal):
                return self._param(params.add(str(raw)))
            return self._param(params.add(raw))

        if operator is FilterOperator.EQ:
            return f"{column} = {bind(value)}"
        if operator is FilterOperator.NE:
            return f"{column} != {bind(value)}"
        if operator is FilterOperator.GT:
            return f"{column} > {bind(value)}"
        if operator is FilterOperator.GTE:
            return f"{column} >= {bind(value)}"
        if operator is FilterOperator.LT:
            return f"{column} < {bind(value)}"
        if operator is FilterOperator.LTE:
            return f"{column} <= {bind(value)}"
        if operator is FilterOperator.IN:
            placeholders = ", ".join(bind(item) for item in value)
            return f"{column} IN ({placeholders})"
        if operator is FilterOperator.NOT_IN:
            placeholders = ", ".join(bind(item) for item in value)
            return f"{column} NOT IN ({placeholders})"
        if operator is FilterOperator.CONTAINS:
            return f"{column} LIKE {self._param(params.add(f'%{_escape_like(value)}%'))}{self._like_escape()}"
        if operator is FilterOperator.STARTS_WITH:
            return f"{column} LIKE {self._param(params.add(f'{_escape_like(value)}%'))}{self._like_escape()}"
        if operator is FilterOperator.ENDS_WITH:
            return f"{column} LIKE {self._param(params.add(f'%{_escape_like(value)}'))}{self._like_escape()}"
        if operator is FilterOperator.HAS:
            return self._array_contains(column, bind(value))
        if operator is FilterOperator.HAS_ANY:
            parts = [self._array_contains(column, bind(item)) for item in value]
            return "(" + " OR ".join(parts) + ")"
        if operator is FilterOperator.HAS_ALL:
            parts = [self._array_contains(column, bind(item)) for item in value]
            return "(" + " AND ".join(parts) + ")"
        if operator is FilterOperator.BETWEEN:
            low, high = value
            return f"{column} >= {bind(low)} AND {column} < {bind(high)}"
        if operator is FilterOperator.IS_NULL:
            return f"({column} IS NULL OR {column} = '')"
        if operator is FilterOperator.IS_NOT_NULL:
            return f"({column} IS NOT NULL AND {column} != '')"
        raise ValidationFailedError(f"operator {operator.value!r} is not implemented")

    def _build_where(
        self,
        table: str,
        scope: AnalyticsScope,
        params: _Params,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        filters: Sequence[FilterCondition] = (),
        text_query: str | None = None,
        extra: Sequence[str] = (),
    ) -> str:
        clauses = self._scope_predicate(scope, params)
        if start is not None and end is not None:
            clauses.extend(self._time_predicate(table, start, end, params))
        for condition in filters:
            clauses.append(self._filter_predicate(condition, params))
        if text_query:
            clauses.append(self._text_predicate(table, text_query, params))
        clauses.extend(extra)
        return " AND ".join(clauses)

    def _text_predicate(self, table: str, query: str, params: _Params) -> str:
        """Free-text search.

        A substring match on the name plus exact matches on the identifier
        columns. This is deliberately not an inverted index: the platform does
        not promise ranked full-text search over span payloads, and pretending
        to would be worse than a documented substring match.
        """
        needle = self._param(params.add(f"%{_escape_like(query)}%"))
        exact = self._param(params.add(query))
        parts = [f"name LIKE {needle}{self._like_escape()}", f"trace_id = {exact}"]
        columns = {name for name, _ in TABLE_COLUMNS[table]}
        if "session_id" in columns:
            parts.append(f"session_id = {exact}")
        if "subject_id" in columns:
            parts.append(f"subject_id = {exact}")
        return "(" + " OR ".join(parts) + ")"

    # ------------------------------------------------------------------
    # ORDER BY and keyset pagination
    # ------------------------------------------------------------------

    def _null_safe(self, column: str, kind: ColumnKind) -> str:
        """Wrap a nullable ordered column so comparisons have a total order."""
        if kind in {ColumnKind.INT_NULL, ColumnKind.FLOAT_NULL, ColumnKind.DECIMAL_NULL}:
            return f"COALESCE({column}, {_NULL_SENTINEL})"
        return column

    def _order_value(self, kind: ColumnKind, value: Any) -> Any:
        """Adapt a cursor value for binding against :meth:`_null_safe`.

        The two must agree: if a driver orders a column by a projection, the
        cursor value has to be projected the same way or the page boundary
        lands in the wrong place. The default is identity, which is right for
        any engine that can order and bind the stored type directly.
        """
        return value

    def _column_kinds(self, table: str) -> dict[str, ColumnKind]:
        return dict(TABLE_COLUMNS[table])

    def _build_order_by(self, table: str, sort: Sequence[SortTerm]) -> tuple[str, list[str]]:
        """Return the ORDER BY clause and the ordered list of key columns.

        The schema's tiebreaker is always appended. Without it, two rows with
        equal sort values could be returned on both sides of a page boundary,
        or neither -- keyset pagination requires a strict total order.
        """
        schema = schema_for(table)
        kinds = self._column_kinds(table)
        terms = list(sort) or list(schema.default_sort)
        columns = [term.field.column for term in terms]
        directions = [term.direction for term in terms]
        if schema.tiebreaker not in columns:
            columns.append(schema.tiebreaker)
            directions.append(directions[-1] if directions else SortDirection.DESC)

        rendered = ", ".join(
            f"{self._null_safe(column, kinds.get(column, ColumnKind.STRING))} "
            f"{'DESC' if direction is SortDirection.DESC else 'ASC'}"
            for column, direction in zip(columns, directions, strict=True)
        )
        return rendered, columns

    def _keyset_predicate(
        self,
        table: str,
        columns: Sequence[str],
        sort: Sequence[SortTerm],
        cursor: Mapping[str, Any],
        params: _Params,
    ) -> str:
        """Build the ``(a,b,c) > (v1,v2,v3)`` predicate as nested comparisons.

        Row-value comparison syntax is not portable across both engines, so the
        expanded form is emitted instead. It is equivalent and every engine
        plans it against the ordering key.
        """
        schema = schema_for(table)
        kinds = self._column_kinds(table)
        directions: list[SortDirection] = [term.direction for term in sort] or [
            term.direction for term in schema.default_sort
        ]
        while len(directions) < len(columns):
            directions.append(directions[-1] if directions else SortDirection.DESC)

        def build(index: int) -> str:
            column = columns[index]
            kind = kinds.get(column, ColumnKind.STRING)
            expression = self._null_safe(column, kind)
            raw = cursor.get(column)
            if isinstance(raw, datetime):
                raw = datetime_to_unix_nano(raw)
            if raw is None:
                raw = _NULL_SENTINEL if kind is not ColumnKind.STRING else ""
            placeholder = self._param(params.add(self._order_value(kind, raw)))
            comparison = "<" if directions[index] is SortDirection.DESC else ">"
            if index == len(columns) - 1:
                return f"{expression} {comparison} {placeholder}"
            return (
                f"({expression} {comparison} {placeholder} OR "
                f"({expression} = {placeholder} AND {build(index + 1)}))"
            )

        return build(0)

    def _cursor_for(self, table: str, columns: Sequence[str]) -> Callable[[Any], dict[str, Any]]:
        def extract(row: Any) -> dict[str, Any]:
            payload: dict[str, Any] = {}
            for column in columns:
                payload[column] = getattr(row, column, None)
            return payload

        return extract

    # ------------------------------------------------------------------
    # generic list query
    # ------------------------------------------------------------------

    async def _list(
        self,
        table: str,
        scope: AnalyticsScope,
        *,
        start: datetime | None,
        end: datetime | None,
        filters: Sequence[FilterCondition],
        sort: Sequence[SortTerm],
        page: PageRequest | None,
        text_query: str | None = None,
    ) -> Page[Any]:
        request = page or PageRequest()
        params = self._Params()
        order_by, key_columns = self._build_order_by(table, sort)
        extra: list[str] = []
        if request.cursor:
            extra.append(self._keyset_predicate(table, key_columns, sort, request.cursor, params))
        where = self._build_where(
            table,
            scope,
            params,
            start=start,
            end=end,
            filters=filters,
            text_query=text_query,
            extra=extra,
        )
        columns = ", ".join(name for name, _ in TABLE_COLUMNS[table])
        # Fetch one extra row: that is how has_more is known without a COUNT.
        sql = (
            f"SELECT {columns} FROM {self._table(table)} "
            f"WHERE {where} ORDER BY {order_by} LIMIT {int(request.limit) + 1}"
        )
        raw_rows = await self._fetch(sql, params.values)
        rows = [self._decode(table, row) for row in raw_rows]
        return Page.from_rows(
            rows,
            limit=request.limit,
            codec=self._cursor_codec,
            cursor_for=self._cursor_for(table, key_columns),
        )

    def _decode(self, table: str, mapping: Mapping[str, Any]) -> Any:
        return decode_row(mapping, TABLE_ROW_TYPES[table], TABLE_COLUMNS[table], self._decoders)

    def _table(self, table: str, *, final: bool = False) -> str:
        """Fully-qualified table source expression.

        ``final`` asks the driver for a de-duplicated view. ClickHouse honours
        it with the ``FINAL`` modifier (merge-on-read); SQLite ignores it
        because its unique index makes duplicates impossible in the first
        place. Trace-scoped reads always request it -- a trace detail page
        showing the same span twice would be a correctness bug -- while wide
        aggregate scans do not, because FINAL over a full partition is
        expensive and the ingest path already de-duplicates.
        """
        return table

    # ------------------------------------------------------------------
    # AnalyticsStore -- reads
    # ------------------------------------------------------------------

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
        return await self._list(
            "traces",
            scope,
            start=start,
            end=end,
            filters=filters,
            sort=sort,
            page=page,
            text_query=text_query,
        )

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
        return await self._list(
            "spans", scope, start=start, end=end, filters=filters, sort=sort, page=page
        )

    async def get_trace(self, scope: AnalyticsScope, trace_id: str) -> TraceRow | None:
        rows = await self.get_traces(scope, [trace_id])
        return rows[0] if rows else None

    async def get_traces(self, scope: AnalyticsScope, trace_ids: Sequence[str]) -> list[TraceRow]:
        if not trace_ids:
            return []
        params = self._Params()
        placeholders = ", ".join(self._param(params.add(tid)) for tid in trace_ids)
        where = self._build_where("traces", scope, params, extra=[f"trace_id IN ({placeholders})"])
        columns = ", ".join(name for name, _ in TABLE_COLUMNS["traces"])
        sql = (
            f"SELECT {columns} FROM {self._table('traces', final=True)} WHERE {where} "
            f"ORDER BY start_unix_nano DESC LIMIT {len(trace_ids)}"
        )
        return [self._decode("traces", row) for row in await self._fetch(sql, params.values)]

    async def get_spans(
        self, scope: AnalyticsScope, trace_id: str, *, limit: int = 10_000
    ) -> list[SpanRow]:
        params = self._Params()
        where = self._build_where(
            "spans",
            scope,
            params,
            extra=[f"trace_id = {self._param(params.add(trace_id))}"],
        )
        columns = ", ".join(name for name, _ in TABLE_COLUMNS["spans"])
        sql = (
            f"SELECT {columns} FROM {self._table('spans', final=True)} WHERE {where} "
            f"ORDER BY start_unix_nano ASC, span_id ASC LIMIT {int(limit)}"
        )
        return [self._decode("spans", row) for row in await self._fetch(sql, params.values)]

    async def get_span(self, scope: AnalyticsScope, trace_id: str, span_id: str) -> SpanRow | None:
        params = self._Params()
        where = self._build_where(
            "spans",
            scope,
            params,
            extra=[
                f"trace_id = {self._param(params.add(trace_id))}",
                f"span_id = {self._param(params.add(span_id))}",
            ],
        )
        columns = ", ".join(name for name, _ in TABLE_COLUMNS["spans"])
        sql = f"SELECT {columns} FROM {self._table('spans', final=True)} WHERE {where} LIMIT 1"
        rows = await self._fetch(sql, params.values)
        return self._decode("spans", rows[0]) if rows else None

    async def _children_of_trace(
        self,
        table: str,
        scope: AnalyticsScope,
        trace_id: str,
        span_id: str | None,
        order: str,
        limit: int = 20_000,
    ) -> list[Any]:
        params = self._Params()
        extra = [f"trace_id = {self._param(params.add(trace_id))}"]
        if span_id:
            extra.append(f"span_id = {self._param(params.add(span_id))}")
        where = self._build_where(table, scope, params, extra=extra)
        columns = ", ".join(name for name, _ in TABLE_COLUMNS[table])
        sql = (
            f"SELECT {columns} FROM {self._table(table, final=True)} WHERE {where} "
            f"ORDER BY {order} LIMIT {int(limit)}"
        )
        return [self._decode(table, row) for row in await self._fetch(sql, params.values)]

    async def get_span_events(
        self, scope: AnalyticsScope, trace_id: str, span_id: str | None = None
    ) -> list[SpanEventRow]:
        return await self._children_of_trace(
            "span_events", scope, trace_id, span_id, "time_unix_nano ASC, sequence ASC"
        )

    async def get_retrieval_documents(
        self, scope: AnalyticsScope, trace_id: str, span_id: str | None = None
    ) -> list[RetrievalDocumentRow]:
        return await self._children_of_trace(
            "retrieval_documents", scope, trace_id, span_id, "span_id ASC, rank ASC"
        )

    async def get_agent_steps(self, scope: AnalyticsScope, trace_id: str) -> list[AgentStepRow]:
        return await self._children_of_trace(
            "agent_steps", scope, trace_id, None, "step_number ASC, start_unix_nano ASC"
        )

    async def get_cost_records(self, scope: AnalyticsScope, trace_id: str) -> list[CostRecordRow]:
        return await self._children_of_trace(
            "cost_records", scope, trace_id, None, "time_unix_nano ASC"
        )

    # ------------------------------------------------------------------
    # AnalyticsStore -- aggregates
    # ------------------------------------------------------------------

    def _aggregate_expression(self, query: MetricQuery) -> str:
        aggregation = query.aggregation
        if aggregation is Aggregation.COUNT:
            return "COUNT(*)"
        if query.metric is None:
            raise ValidationFailedError(
                f"aggregation {aggregation.value!r} requires a metric column"
            )
        allowed = aggregatable_columns(query.source)
        if query.metric not in allowed:
            raise ValidationFailedError(
                f"column {query.metric!r} cannot be aggregated on {query.source!r}; "
                f"aggregatable: {sorted(allowed)}"
            )
        column = query.metric
        if aggregation is Aggregation.SUM:
            if _is_decimal_column(query.source, column):
                # Money. A plain SUM over a decimal is only exact if the engine
                # has a real decimal type; where it does not, the driver
                # supplies an exact alternative rather than letting the value
                # pass through a float.
                return self._decimal_sum(column)
            return f"SUM(COALESCE({column}, 0))"
        if aggregation is Aggregation.AVG:
            return f"AVG({column})"
        if aggregation is Aggregation.MIN:
            return f"MIN({column})"
        if aggregation is Aggregation.MAX:
            return f"MAX({column})"
        if aggregation is Aggregation.UNIQUE:
            return f"COUNT(DISTINCT {column})"
        raise ValidationFailedError(
            f"aggregation {aggregation.value!r} is not available here; "
            "use the percentiles endpoint for quantiles"
        )

    def _validate_group_by(self, source: str, group_by: Sequence[str]) -> list[str]:
        known = {name for name, _ in TABLE_COLUMNS[source]}
        for column in group_by:
            if column not in known:
                raise ValidationFailedError(
                    f"cannot group by {column!r} on {source!r}; unknown column"
                )
        return list(group_by)

    async def timeseries(self, query: MetricQuery) -> list[GroupedMetric]:
        interval = query.interval
        if interval is None:
            raise ValidationFailedError("timeseries queries require an interval")
        bucket_ns = interval.seconds * 1_000_000_000
        time_column = TIME_COLUMN[query.source]
        group_by = self._validate_group_by(query.source, query.group_by)

        params = self._Params()
        where = self._build_where(
            query.source,
            query.scope,
            params,
            start=query.start,
            end=query.end,
            filters=query.filters,
        )
        bucket_expression = f"({time_column} / {bucket_ns}) * {bucket_ns}"
        selected = [f"{bucket_expression} AS bucket", *group_by]
        aggregate = self._aggregate_expression(query)

        top_filter = ""
        if group_by:
            # Restrict to the top-N groups first; otherwise a high-cardinality
            # dimension returns thousands of series the client cannot use.
            top_groups = await self._top_groups(query)
            if top_groups:
                clauses = []
                for keys in top_groups:
                    parts = [
                        f"{column} = {self._param(params.add(value))}"
                        for column, value in zip(group_by, keys, strict=True)
                    ]
                    clauses.append("(" + " AND ".join(parts) + ")")
                top_filter = " AND (" + " OR ".join(clauses) + ")"

        sql = (
            f"SELECT {', '.join(selected)}, {aggregate} AS value, COUNT(*) AS n "
            f"FROM {self._table(query.source)} WHERE {where}{top_filter} "
            f"GROUP BY bucket{''.join(', ' + column for column in group_by)} "
            f"ORDER BY bucket ASC"
        )
        rows = await self._fetch(sql, params.values)

        exact = query.metric is not None and _is_decimal_column(query.source, query.metric)
        series: dict[tuple[str, ...], list[MetricPoint]] = {}
        for row in rows:
            keys = tuple(str(row.get(column) or "") for column in group_by)
            point = MetricPoint(
                bucket=unix_nano_to_datetime(int(row["bucket"])),
                value=_coerce_metric(row["value"], exact=exact),
                count=int(row.get("n") or 0),
            )
            series.setdefault(keys, []).append(point)

        return [
            GroupedMetric(
                keys=keys,
                points=tuple(points),
                total=_sum_points(points),
                count=sum(point.count for point in points),
            )
            for keys, points in series.items()
        ]

    async def _top_groups(self, query: MetricQuery) -> list[tuple[str, ...]]:
        """Return the highest-ranking group-by combinations for ``query``."""
        group_by = list(query.group_by)
        if not group_by:
            return []
        params = self._Params()
        where = self._build_where(
            query.source,
            query.scope,
            params,
            start=query.start,
            end=query.end,
            filters=query.filters,
        )
        aggregate = self._aggregate_expression(query)
        sql = (
            f"SELECT {', '.join(group_by)}, {aggregate} AS value "
            f"FROM {self._table(query.source)} WHERE {where} "
            f"GROUP BY {', '.join(group_by)} "
            f"ORDER BY {self._metric_order_expression()} DESC "
            f"LIMIT {int(query.limit_groups)}"
        )
        rows = await self._fetch(sql, params.values)
        return [tuple(str(row.get(column) or "") for column in group_by) for row in rows]

    async def aggregate(self, query: MetricQuery) -> list[GroupedMetric]:
        group_by = self._validate_group_by(query.source, query.group_by)
        params = self._Params()
        where = self._build_where(
            query.source,
            query.scope,
            params,
            start=query.start,
            end=query.end,
            filters=query.filters,
        )
        aggregate = self._aggregate_expression(query)
        selected = ", ".join([*group_by, f"{aggregate} AS value", "COUNT(*) AS n"])
        group_clause = f" GROUP BY {', '.join(group_by)}" if group_by else ""
        order_clause = f" ORDER BY {self._metric_order_expression()} DESC" if group_by else ""
        limit_clause = f" LIMIT {int(query.limit_groups)}" if group_by else ""
        sql = (
            f"SELECT {selected} FROM {self._table(query.source)} "
            f"WHERE {where}{group_clause}{order_clause}{limit_clause}"
        )
        rows = await self._fetch(sql, params.values)
        exact = query.metric is not None and _is_decimal_column(query.source, query.metric)
        return [
            GroupedMetric(
                keys=tuple(str(row.get(column) or "") for column in group_by),
                total=_coerce_metric(row["value"], exact=exact),
                count=int(row.get("n") or 0),
            )
            for row in rows
        ]

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
        allowed = aggregatable_columns(source)
        if column not in allowed:
            raise ValidationFailedError(
                f"column {column!r} is not a percentile-able metric on {source!r}"
            )
        keys = self._validate_group_by(source, group_by)
        params = self._Params()
        where = self._build_where(source, scope, params, start=start, end=end, filters=filters)
        levels = (0.50, 0.75, 0.90, 0.95, 0.99)
        rows = await self._percentile_rows(
            table=source,
            column=column,
            group_by=keys,
            where=where,
            params=params.values,
            levels=levels,
            limit_groups=limit_groups,
        )
        results: list[PercentileResult] = []
        for row in rows:
            results.append(
                PercentileResult(
                    keys=tuple(str(row.get(key) or "") for key in keys),
                    count=int(row.get("n") or 0),
                    p50=_as_float(row.get("p50")),
                    p75=_as_float(row.get("p75")),
                    p90=_as_float(row.get("p90")),
                    p95=_as_float(row.get("p95")),
                    p99=_as_float(row.get("p99")),
                    avg=_as_float(row.get("avg_value")),
                    max=_as_float(row.get("max_value")),
                )
            )
        return results

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
        known = {name for name, _ in TABLE_COLUMNS["spans"]}
        if column not in known:
            raise ValidationFailedError(f"unknown column {column!r} for distinct values")
        params = self._Params()
        extra = [f"{column} != ''"]
        if prefix:
            extra.append(
                f"{column} LIKE {self._param(params.add(f'{_escape_like(prefix)}%'))}{self._like_escape()}"
            )
        where = self._build_where("spans", scope, params, start=start, end=end, extra=extra)
        sql = (
            f"SELECT {column} AS value, COUNT(*) AS n FROM {self._table('spans')} "
            f"WHERE {where} GROUP BY {column} ORDER BY n DESC LIMIT {int(limit)}"
        )
        rows = await self._fetch(sql, params.values)
        return [(str(row["value"]), int(row["n"])) for row in rows]

    async def count_spans(self, scope: AnalyticsScope, *, start: datetime, end: datetime) -> int:
        params = self._Params()
        where = self._build_where("spans", scope, params, start=start, end=end)
        # final=True: the caller asked how many spans exist, not how many
        # un-merged parts happen to hold a copy of one.
        sql = f"SELECT COUNT(*) AS n FROM {self._table('spans', final=True)} WHERE {where}"
        rows = await self._fetch(sql, params.values)
        return int(rows[0]["n"]) if rows else 0

    # ------------------------------------------------------------------
    # AnalyticsStore -- writes shared across drivers
    # ------------------------------------------------------------------

    async def insert_spans(self, rows: Sequence[SpanRow]) -> int:
        return await self._insert("spans", rows)

    async def upsert_traces(self, rows: Sequence[TraceRow]) -> int:
        return await self._insert("traces", rows)

    async def insert_span_events(self, rows: Sequence[SpanEventRow]) -> int:
        return await self._insert("span_events", rows)

    async def insert_retrieval_documents(self, rows: Sequence[RetrievalDocumentRow]) -> int:
        return await self._insert("retrieval_documents", rows)

    async def insert_agent_steps(self, rows: Sequence[AgentStepRow]) -> int:
        return await self._insert("agent_steps", rows)

    async def insert_cost_records(self, rows: Sequence[CostRecordRow]) -> int:
        return await self._insert("cost_records", rows)

    # The cursor codec is injected by the factory so that cursors issued by one
    # replica validate on another.
    _cursor_codec: Any


def _escape_like(value: Any) -> str:
    """Escape LIKE wildcards so a user's ``%`` matches a literal percent sign."""
    text = str(value)
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


#: Column kinds that hold money and must never round-trip through a float.
_DECIMAL_KINDS = frozenset({ColumnKind.DECIMAL_NULL})


def _is_decimal_column(source: str, column: str) -> bool:
    """Whether ``column`` on ``source`` holds an exact decimal."""
    for name, kind in TABLE_COLUMNS.get(source, ()):
        if name == column:
            return kind in _DECIMAL_KINDS
    return False


def _coerce_metric(value: Any, *, exact: bool = False) -> float | Decimal | None:
    """Convert an aggregate result to a number.

    ``exact`` is set for money columns: the value stays a :class:`Decimal` all
    the way to the JSON encoder, which renders it as a string. Passing it
    through ``float`` here would undo the exact aggregation the driver just
    performed.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if exact:
        try:
            return Decimal(str(value))
        except (TypeError, ValueError, InvalidOperation):
            return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sum_points(points: Sequence[MetricPoint]) -> float | Decimal | None:
    """Total a series.

    Decimal points are summed as decimals; mixing them into a float would
    reintroduce the rounding the exact aggregation avoided.
    """
    values = [point.value for point in points if point.value is not None]
    if not values:
        return None
    if all(isinstance(value, Decimal) for value in values):
        return sum(values, Decimal(0))
    return sum(float(value) for value in values)
