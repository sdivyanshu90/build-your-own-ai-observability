"""ClickHouse analytics driver -- the production trace store.

Schema decisions, and why
-------------------------

**Engine: ``ReplacingMergeTree(ingest_version)``.** Ingestion is at-least-once.
The alternative to de-duplicating in the storage engine is de-duplicating in the
application, which means a read-before-write on every span -- a random read per
row, which is precisely what a columnar store is worst at. ``ReplacingMergeTree``
collapses rows sharing a sorting key during background merges, keeping the one
with the highest ``ingest_version``. Trace-scoped reads add ``FINAL`` so a user
never sees a transient duplicate; wide aggregate scans do not, because ``FINAL``
over a whole partition is expensive and the worker's content-hash de-duplication
already makes duplicates rare.

**Partitioning: by day of event time.** Retention becomes ``DROP PARTITION``,
which is a metadata operation, instead of a ``DELETE`` that rewrites parts.
Time-range queries -- essentially all of them -- prune whole days without
reading. Daily (not monthly) because a 30-day default retention with monthly
partitions would keep up to 60 days of data.

**Sorting key: ``(organization_id, project_id, environment, start_unix_nano,
trace_id, span_id)``.** Ordered by selectivity: the tenant predicate is on every
query and is the most selective, then the project, then time. Because the
sorting key is also the primary index, this layout means the common query --
"one project's spans in the last hour" -- reads a contiguous run of granules.
Putting ``trace_id`` first would optimise trace-detail lookups at the cost of
every list and dashboard query; the bloom-filter skip index on ``trace_id``
recovers the former without sacrificing the latter.

**Timestamps as ``Int64`` nanoseconds.** ``DateTime64(9)`` would be idiomatic,
but the whole platform speaks Unix nanoseconds (OTLP does too), and keeping one
integer representation end-to-end removes a class of precision and timezone
bugs. Human-facing conversion happens once, at the API boundary.

**Skip indexes over more sorting keys.** A ``bloom_filter`` on ``trace_id`` and
``session_id`` and a ``set`` index on ``model`` let point lookups and common
filters skip granules without a second table.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ...core.errors import DependencyUnavailableError
from ...core.logging import get_logger
from ...core.timeutil import datetime_to_unix_nano
from .columns import (
    TABLE_COLUMNS,
    ColumnKind,
    encode_row,
    json_dumps,
    json_loads,
    parse_map,
    stringify_map,
    to_datetime,
    to_decimal,
)
from .protocol import RetentionResult
from .rows import AnalyticsScope, TraceRow
from .sqlbase import TIME_COLUMN, SqlAnalyticsStore

__all__ = ["ClickHouseAnalyticsStore"]

log = get_logger(__name__)

_CLICKHOUSE_TYPES: dict[ColumnKind, str] = {
    ColumnKind.STRING: "String",
    # LowCardinality dictionary-encodes columns with few distinct values, which
    # is most of the AI metadata: providers, models, statuses, environments.
    ColumnKind.ENUM: "LowCardinality(String)",
    ColumnKind.JSON: "String",
    ColumnKind.MAP: "Map(String, String)",
    ColumnKind.STRING_ARRAY: "Array(String)",
    ColumnKind.INT: "Int64",
    ColumnKind.INT_NULL: "Nullable(Int64)",
    ColumnKind.FLOAT_NULL: "Nullable(Float64)",
    ColumnKind.BOOL: "UInt8",
    ColumnKind.DECIMAL_NULL: "Nullable(Decimal(38, 18))",
    ColumnKind.TIMESTAMP: "Nullable(DateTime64(3, 'UTC'))",
}

_ORDER_BY: dict[str, str] = {
    "spans": "(organization_id, project_id, environment, start_unix_nano, trace_id, span_id)",
    "traces": "(organization_id, project_id, environment, start_unix_nano, trace_id)",
    "span_events": "(organization_id, project_id, trace_id, span_id, time_unix_nano, sequence)",
    "retrieval_documents": "(organization_id, project_id, trace_id, span_id, rank)",
    "agent_steps": "(organization_id, project_id, trace_id, span_id, step_number)",
    "cost_records": "(organization_id, project_id, environment, time_unix_nano, trace_id, span_id)",
}

_VERSION_COLUMN: dict[str, str] = {
    "spans": "ingest_version",
    "traces": "ingest_version",
}

#: ``(name, expression, type, granularity)`` skip indexes per table.
_SKIP_INDEXES: dict[str, tuple[tuple[str, str, str, int], ...]] = {
    "spans": (
        ("idx_trace_id", "trace_id", "bloom_filter(0.01)", 1),
        ("idx_session", "session_id", "bloom_filter(0.01)", 4),
        ("idx_model", "model", "set(200)", 4),
        ("idx_status", "status", "set(8)", 4),
        ("idx_prompt_version", "prompt_version_id", "bloom_filter(0.01)", 4),
    ),
    "traces": (
        ("idx_trace_id", "trace_id", "bloom_filter(0.01)", 1),
        ("idx_session", "session_id", "bloom_filter(0.01)", 4),
        ("idx_subject", "subject_id", "bloom_filter(0.01)", 4),
    ),
    "cost_records": (("idx_trace_id", "trace_id", "bloom_filter(0.01)", 1),),
}


def _partition_expression(table: str) -> str:
    column = TIME_COLUMN[table]
    return f"toYYYYMMDD(toDateTime(intDiv({column}, 1000000000)))"


_ENCODERS: dict[ColumnKind, Callable[[Any], Any]] = {
    ColumnKind.STRING: lambda v: "" if v is None else str(v),
    ColumnKind.ENUM: lambda v: "" if v is None else str(v),
    ColumnKind.INT: lambda v: 0 if v is None else int(v),
    ColumnKind.INT_NULL: lambda v: None if v is None else int(v),
    ColumnKind.FLOAT_NULL: lambda v: None if v is None else float(v),
    ColumnKind.BOOL: lambda v: 1 if v else 0,
    ColumnKind.DECIMAL_NULL: lambda v: None if v is None else Decimal(str(v)),
    ColumnKind.STRING_ARRAY: lambda v: [str(item) for item in (v or [])],
    ColumnKind.MAP: stringify_map,
    ColumnKind.JSON: lambda v: json_dumps(v if v is not None else []),
    ColumnKind.TIMESTAMP: lambda v: v,
}

_DECODERS: dict[ColumnKind, Callable[[Any], Any]] = {
    ColumnKind.STRING: lambda v: "" if v is None else str(v),
    ColumnKind.ENUM: lambda v: "" if v is None else str(v),
    ColumnKind.INT: lambda v: 0 if v is None else int(v),
    ColumnKind.INT_NULL: lambda v: None if v is None else int(v),
    ColumnKind.FLOAT_NULL: lambda v: None if v is None else float(v),
    ColumnKind.BOOL: bool,
    ColumnKind.DECIMAL_NULL: to_decimal,
    ColumnKind.STRING_ARRAY: lambda v: list(v or []),
    ColumnKind.MAP: parse_map,
    ColumnKind.JSON: lambda v: json_loads(v) or [],
    ColumnKind.TIMESTAMP: to_datetime,
}


class ClickHouseAnalyticsStore(SqlAnalyticsStore):
    """ClickHouse implementation of :class:`AnalyticsStore`."""

    driver_name = "clickhouse"

    def __init__(
        self,
        *,
        url: str,
        database: str,
        username: str,
        password: str,
        cursor_codec: Any,
        connect_timeout: float = 5.0,
        query_timeout: float = 30.0,
        max_result_rows: int = 50_000,
    ) -> None:
        self._url = url
        self._database = database
        self._username = username
        self._password = password
        self._connect_timeout = connect_timeout
        self._query_timeout = query_timeout
        self._max_result_rows = max_result_rows
        self._client: Any = None
        self._cursor_codec = cursor_codec

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._client is not None:
            return
        try:
            import clickhouse_connect
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise DependencyUnavailableError(
                "clickhouse", cause="clickhouse-connect is not installed"
            ) from exc

        from urllib.parse import urlparse

        parsed = urlparse(self._url)
        try:
            self._client = await clickhouse_connect.get_async_client(
                host=parsed.hostname or "localhost",
                port=parsed.port or (8443 if parsed.scheme == "https" else 8123),
                secure=parsed.scheme == "https",
                username=self._username,
                password=self._password,
                database="default",
                connect_timeout=int(self._connect_timeout),
                send_receive_timeout=int(self._query_timeout),
                settings={
                    "max_result_rows": self._max_result_rows,
                    # Truncate rather than fail when a query would exceed the
                    # row cap: a dashboard showing partial data with a warning
                    # beats a dashboard showing an error.
                    "result_overflow_mode": "break",
                    "max_execution_time": int(self._query_timeout),
                },
            )
        except Exception as exc:
            raise DependencyUnavailableError("clickhouse", cause=str(exc)) from exc
        await self.migrate()

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            finally:
                self._client = None

    async def check_health(self) -> None:
        try:
            await self._fetch("SELECT 1 AS ok", {})
        except Exception as exc:
            raise DependencyUnavailableError("clickhouse", cause=str(exc)) from exc

    def _require_client(self) -> Any:
        if self._client is None:
            raise DependencyUnavailableError(
                "clickhouse", cause="store used before start() was awaited"
            )
        return self._client

    async def migrate(self) -> None:
        """Create the database, tables and materialised views if absent."""
        client = self._require_client()
        await client.command(f"CREATE DATABASE IF NOT EXISTS {self._database}")
        for table, columns in TABLE_COLUMNS.items():
            definitions = ",\n  ".join(
                f"`{name}` {_CLICKHOUSE_TYPES[kind]}" for name, kind in columns
            )
            indexes = "".join(
                f",\n  INDEX {name} {expression} TYPE {index_type} GRANULARITY {granularity}"
                for name, expression, index_type, granularity in _SKIP_INDEXES.get(table, ())
            )
            version = _VERSION_COLUMN.get(table)
            engine = f"ReplacingMergeTree({version})" if version else "ReplacingMergeTree()"
            await client.command(
                f"CREATE TABLE IF NOT EXISTS {self._database}.{table} (\n  {definitions}{indexes}\n) "
                f"ENGINE = {engine} "
                f"PARTITION BY {_partition_expression(table)} "
                f"ORDER BY {_ORDER_BY[table]} "
                # Sparse index granularity: 8192 is ClickHouse's default and is
                # right for these row widths; a smaller value would bloat the
                # primary index for marginal skip gains.
                f"SETTINGS index_granularity = 8192, "
                f"ttl_only_drop_parts = 1"
            )
        await self._create_materialised_views()

    async def _create_materialised_views(self) -> None:
        """Pre-aggregate the dashboard's hottest query.

        The overview dashboard asks the same question constantly: request count,
        error count, token totals and latency distribution per five-minute
        bucket per project. Recomputing that from raw spans on every page load
        scans the whole retention window; an ``AggregatingMergeTree`` view
        computes it once at insert time and turns the dashboard into a read of
        a few thousand rows.

        ``quantilesState`` stores a mergeable t-digest, so percentiles can be
        rolled up across buckets correctly -- averaging percentiles, the obvious
        wrong alternative, is meaningless.
        """
        client = self._require_client()
        await client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {self._database}.metrics_5m (
              organization_id String,
              project_id String,
              environment LowCardinality(String),
              bucket DateTime,
              category LowCardinality(String),
              model LowCardinality(String),
              status LowCardinality(String),
              span_count AggregateFunction(count),
              error_count AggregateFunction(sum, UInt64),
              input_tokens AggregateFunction(sum, Int64),
              output_tokens AggregateFunction(sum, Int64),
              total_tokens AggregateFunction(sum, Int64),
              cost_total AggregateFunction(sum, Decimal(38, 18)),
              duration_quantiles AggregateFunction(quantiles(0.5, 0.75, 0.9, 0.95, 0.99), Int64)
            ) ENGINE = AggregatingMergeTree()
            PARTITION BY toYYYYMMDD(bucket)
            ORDER BY (organization_id, project_id, environment, bucket, category, model, status)
            """
        )
        await client.command(
            f"""
            CREATE MATERIALIZED VIEW IF NOT EXISTS {self._database}.metrics_5m_mv
            TO {self._database}.metrics_5m AS
            SELECT
              organization_id,
              project_id,
              environment,
              toStartOfFiveMinute(toDateTime(intDiv(start_unix_nano, 1000000000))) AS bucket,
              category,
              model,
              status,
              countState() AS span_count,
              sumState(toUInt64(status = 'error')) AS error_count,
              sumState(ifNull(input_tokens, 0)) AS input_tokens,
              sumState(ifNull(output_tokens, 0)) AS output_tokens,
              sumState(ifNull(total_tokens, 0)) AS total_tokens,
              sumState(ifNull(cost_total, toDecimal128(0, 18))) AS cost_total,
              quantilesState(0.5, 0.75, 0.9, 0.95, 0.99)(ifNull(duration_ns, 0)) AS duration_quantiles
            FROM {self._database}.spans
            GROUP BY organization_id, project_id, environment, bucket, category, model, status
            """
        )

    # ------------------------------------------------------------------
    # dialect primitives
    # ------------------------------------------------------------------

    def _param(self, name: str) -> str:
        # clickhouse-connect binds %(name)s client-side with correct quoting and
        # escaping for the value's Python type.
        return f"%({name})s"

    def _array_contains(self, column: str, placeholder: str) -> str:
        return f"has({column}, {placeholder})"

    def _map_value(self, column: str, key_placeholder: str) -> str:
        return f"{column}[{key_placeholder}]"

    def _bool_literal(self, value: bool) -> str:
        return "1" if value else "0"

    def _like_escape(self) -> str:
        # ClickHouse treats backslash as the LIKE escape character already and
        # rejects an explicit ESCAPE clause with a syntax error.
        return ""

    def _table(self, table: str, *, final: bool = False) -> str:
        qualified = f"{self._database}.{table}"
        # The traces roll-up is always read de-duplicated: it is small relative
        # to spans, and a duplicate row there is directly visible in the UI.
        if final or table == "traces":
            return f"{qualified} FINAL"
        return qualified

    @property
    def _decoders(self) -> Mapping[ColumnKind, Callable[[Any], Any]]:
        return _DECODERS

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------

    async def _fetch(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        client = self._require_client()
        try:
            result = await client.query(sql, parameters=dict(params))
        except Exception as exc:
            log.warning("clickhouse.query_failed", error=str(exc), sql=sql[:400])
            raise DependencyUnavailableError("clickhouse", cause=str(exc)) from exc
        names = result.column_names
        # strict: the driver guarantees one value per named column, and a
        # mismatch would silently drop a column rather than fail.
        return [dict(zip(names, row, strict=True)) for row in result.result_rows]

    async def _execute(self, sql: str, params: Mapping[str, Any]) -> None:
        client = self._require_client()
        try:
            await client.command(sql, parameters=dict(params))
        except Exception as exc:
            raise DependencyUnavailableError("clickhouse", cause=str(exc)) from exc

    async def _insert(self, table: str, rows: Sequence[Any]) -> int:
        if not rows:
            return 0
        client = self._require_client()
        columns = TABLE_COLUMNS[table]
        names = [name for name, _ in columns]
        data = [encode_row(row, columns, _ENCODERS) for row in rows]
        try:
            await client.insert(
                table,
                data,
                column_names=names,
                database=self._database,
                settings={
                    # Insert deduplication on the client-supplied block hash is
                    # a second safety net behind ReplacingMergeTree: an
                    # identical retried block is dropped by the server.
                    "insert_deduplicate": 1,
                    "async_insert": 1,
                    "wait_for_async_insert": 1,
                },
            )
        except Exception as exc:
            raise DependencyUnavailableError("clickhouse", cause=str(exc)) from exc
        return len(rows)

    async def upsert_traces(self, rows: Sequence[TraceRow]) -> int:
        """Write trace roll-ups, retiring rows whose sorting-key position moved.

        A late span can pull a trace's start time earlier. Because
        ``start_unix_nano`` is part of the sorting key, the new roll-up lands in
        a different position and ``ReplacingMergeTree`` would keep both. The
        superseded row is removed explicitly with a lightweight delete, which is
        rare enough (only when a trace's earliest span arrives late) that the
        mutation cost is irrelevant.
        """
        moved = [
            row
            for row in rows
            if row.previous_start_unix_nano is not None
            and row.previous_start_unix_nano != row.start_unix_nano
        ]
        for row in moved:
            await self._execute(
                f"DELETE FROM {self._database}.traces "
                "WHERE organization_id = %(org)s AND trace_id = %(trace)s "
                "AND start_unix_nano = %(previous)s",
                {
                    "org": row.organization_id,
                    "trace": row.trace_id,
                    "previous": row.previous_start_unix_nano,
                },
            )
        return await self._insert("traces", rows)

    # ------------------------------------------------------------------
    # percentiles
    # ------------------------------------------------------------------

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
        keys = list(group_by)
        selected_keys = "".join(f"{key}, " for key in keys)
        group_clause = f"GROUP BY {', '.join(keys)} " if keys else ""
        limit_clause = f"LIMIT {int(limit_groups)}" if keys else ""
        level_list = ", ".join(str(level) for level in levels)
        # quantilesExactLow matches the SQLite driver's order-statistic
        # definition exactly, which is what lets one conformance suite assert
        # identical numbers from both stores. Deployments that need the speed of
        # an approximate digest can switch to quantilesTDigest and accept ~1%
        # error -- documented in docs/concepts/latency-analysis.md.
        aliases = ", ".join(
            f"quantiles_array[{index + 1}] AS p{int(level * 100)}"
            for index, level in enumerate(levels)
        )
        sql = (
            f"SELECT {selected_keys}n, {aliases}, avg_value, max_value FROM ("
            f"  SELECT {selected_keys}count() AS n, "
            f"    quantilesExactLow({level_list})({column}) AS quantiles_array, "
            f"    avg({column}) AS avg_value, max({column}) AS max_value "
            f"  FROM {self._table(table)} WHERE {where} AND {column} IS NOT NULL "
            f"  {group_clause}ORDER BY n DESC {limit_clause}"
            f")"
        )
        return await self._fetch(sql, params)

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------

    async def delete_trace_children(self, scope: AnalyticsScope, trace_id: str) -> None:
        for table in ("retrieval_documents", "agent_steps", "span_events", "cost_records"):
            await self._execute(
                f"DELETE FROM {self._database}.{table} "
                "WHERE organization_id = %(org)s AND trace_id = %(trace)s",
                {"org": scope.organization_id, "trace": trace_id},
            )

    async def delete_expired(
        self, *, table: str, cutoff: datetime, batch_size: int = 10_000
    ) -> RetentionResult:
        """Drop whole partitions older than ``cutoff``.

        Dropping a partition is a metadata operation: it unlinks part
        directories rather than rewriting them, so retention costs nothing
        proportional to the data volume. This is the single biggest reason the
        table is partitioned by day.
        """
        if table not in TABLE_COLUMNS:
            raise KeyError(f"unknown analytics table {table!r}")
        boundary = int(cutoff.astimezone(timezone.utc).strftime("%Y%m%d"))
        partitions = await self._fetch(
            "SELECT DISTINCT partition_id, sum(rows) AS rows FROM system.parts "
            "WHERE database = %(db)s AND table = %(table)s AND active "
            "AND toInt64(partition_id) < %(boundary)s "
            "GROUP BY partition_id ORDER BY partition_id LIMIT 50",
            {"db": self._database, "table": table, "boundary": boundary},
        )
        deleted = 0
        for row in partitions:
            partition_id = str(row["partition_id"])
            await self._execute(
                f"ALTER TABLE {self._database}.{table} DROP PARTITION %(partition)s",
                {"partition": partition_id},
            )
            deleted += int(row.get("rows") or 0)
        return RetentionResult(
            table=table, rows_deleted=deleted, cutoff=cutoff, exhausted=len(partitions) < 50
        )

    async def trace_ids_needing_rollup(
        self, *, since: datetime, limit: int = 1_000
    ) -> list[tuple[str, str, str, str]]:
        since_nano = datetime_to_unix_nano(since)
        sql = (
            "SELECT s.organization_id AS organization_id, s.project_id AS project_id, "
            "       s.environment AS environment, s.trace_id AS trace_id "
            f"FROM {self._database}.spans AS s "
            f"LEFT JOIN {self._database}.traces AS t "
            "  ON t.organization_id = s.organization_id AND t.trace_id = s.trace_id "
            "WHERE s.ingested_at >= %(since)s "
            "  AND (t.trace_id = '' OR t.ingested_at < s.ingested_at) "
            "GROUP BY organization_id, project_id, environment, trace_id "
            "LIMIT %(limit)s"
        )
        rows = await self._fetch(
            sql,
            {
                "since": datetime.fromtimestamp(since_nano / 1e9, tz=timezone.utc),
                "limit": int(limit),
            },
        )
        return [
            (
                str(row["organization_id"]),
                str(row["project_id"]),
                str(row["environment"]),
                str(row["trace_id"]),
            )
            for row in rows
        ]
