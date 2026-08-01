"""SQLite analytics driver.

This is a *real* implementation of :class:`AnalyticsStore`, not a stub. It backs
local development, the unit and contract test suites, and CI runs that should
not need a ClickHouse container. It is held to identical behaviour by the
conformance suite that runs against both drivers.

What it deliberately does **not** try to be is a production analytics engine.
SQLite scans rows, not columns; there is no compression, no partition pruning
and no parallel aggregation. At a few million spans it is comfortable; at a few
hundred million it is not, which is exactly why
:meth:`Settings.validate_for_runtime` refuses to start a production process
configured to use it.

Idempotency is provided by ``ON CONFLICT ... DO UPDATE`` guarded on
``ingest_version``, which reproduces ClickHouse's ``ReplacingMergeTree``
semantics: a re-delivered span with an equal or newer version replaces the row,
never adds one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import aiosqlite

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
from .rows import AnalyticsScope
from .sqlbase import _NULL_SENTINEL, TIME_COLUMN, SqlAnalyticsStore

__all__ = ["SqliteAnalyticsStore"]

log = get_logger(__name__)

_SQLITE_TYPES: dict[ColumnKind, str] = {
    ColumnKind.STRING: "TEXT NOT NULL DEFAULT ''",
    ColumnKind.ENUM: "TEXT NOT NULL DEFAULT ''",
    ColumnKind.JSON: "TEXT NOT NULL DEFAULT '[]'",
    ColumnKind.MAP: "TEXT NOT NULL DEFAULT '{}'",
    ColumnKind.STRING_ARRAY: "TEXT NOT NULL DEFAULT '[]'",
    ColumnKind.INT: "INTEGER NOT NULL DEFAULT 0",
    ColumnKind.INT_NULL: "INTEGER",
    ColumnKind.FLOAT_NULL: "REAL",
    ColumnKind.BOOL: "INTEGER NOT NULL DEFAULT 0",
    ColumnKind.DECIMAL_NULL: "TEXT",
    ColumnKind.TIMESTAMP: "INTEGER",
}

#: Natural key per table, used both for the unique index and for upserts.
_NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "spans": ("organization_id", "trace_id", "span_id"),
    "traces": ("organization_id", "trace_id"),
    "span_events": ("organization_id", "trace_id", "span_id", "sequence"),
    "retrieval_documents": ("organization_id", "trace_id", "span_id", "rank"),
    "agent_steps": ("organization_id", "trace_id", "span_id", "step_number"),
    "cost_records": ("organization_id", "trace_id", "span_id"),
}

#: Secondary indexes matching the driver's real access patterns.
_INDEXES: dict[str, tuple[tuple[str, ...], ...]] = {
    "spans": (
        ("organization_id", "project_id", "environment", "start_unix_nano"),
        ("organization_id", "trace_id"),
        ("organization_id", "project_id", "model", "start_unix_nano"),
        ("ingested_at",),
    ),
    "traces": (
        ("organization_id", "project_id", "environment", "start_unix_nano"),
        ("organization_id", "session_id"),
        ("ingested_at",),
    ),
    "span_events": (("organization_id", "trace_id", "span_id"),),
    "retrieval_documents": (
        ("organization_id", "trace_id", "span_id"),
        ("organization_id", "project_id", "time_unix_nano"),
    ),
    "agent_steps": (
        ("organization_id", "trace_id"),
        ("organization_id", "project_id", "start_unix_nano"),
    ),
    "cost_records": (
        ("organization_id", "project_id", "time_unix_nano"),
        ("organization_id", "trace_id"),
    ),
}


class _DecimalSum:
    """Exact SUM for the TEXT columns SQLite stores money in.

    Registered as a user-defined aggregate so that ``GROUP BY`` works normally.
    The result is returned as text and decoded back into a ``Decimal``: routing
    it through SQLite's own numeric coercion is exactly the float rounding this
    exists to avoid.
    """

    __slots__ = ("total",)

    def __init__(self) -> None:
        self.total = Decimal(0)

    def step(self, value: Any) -> None:
        if value is None:
            return
        try:
            self.total += Decimal(str(value))
        except InvalidOperation:
            # A malformed stored value must not abort the whole query. It is
            # skipped, which makes the total a lower bound -- the same contract
            # as an unpriced span, rather than a 500 on a dashboard.
            return

    def finalize(self) -> str:
        return format(self.total, "f")


def _encode_timestamp(value: datetime | None) -> int | None:
    if value is None:
        return None
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return int(aware.timestamp() * 1000)


_ENCODERS: dict[ColumnKind, Callable[[Any], Any]] = {
    ColumnKind.STRING: lambda v: "" if v is None else str(v),
    ColumnKind.ENUM: lambda v: "" if v is None else str(v),
    ColumnKind.INT: lambda v: 0 if v is None else int(v),
    ColumnKind.INT_NULL: lambda v: None if v is None else int(v),
    ColumnKind.FLOAT_NULL: lambda v: None if v is None else float(v),
    ColumnKind.BOOL: lambda v: 1 if v else 0,
    ColumnKind.DECIMAL_NULL: lambda v: None if v is None else format(v, "f"),
    ColumnKind.STRING_ARRAY: lambda v: json_dumps(list(v or [])),
    ColumnKind.MAP: lambda v: json_dumps(stringify_map(v)),
    ColumnKind.JSON: lambda v: json_dumps(v if v is not None else []),
    ColumnKind.TIMESTAMP: _encode_timestamp,
}

_DECODERS: dict[ColumnKind, Callable[[Any], Any]] = {
    ColumnKind.STRING: lambda v: "" if v is None else str(v),
    ColumnKind.ENUM: lambda v: "" if v is None else str(v),
    ColumnKind.INT: lambda v: 0 if v is None else int(v),
    ColumnKind.INT_NULL: lambda v: None if v is None else int(v),
    ColumnKind.FLOAT_NULL: lambda v: None if v is None else float(v),
    ColumnKind.BOOL: bool,
    ColumnKind.DECIMAL_NULL: to_decimal,
    ColumnKind.STRING_ARRAY: lambda v: list(json_loads(v) or []),
    ColumnKind.MAP: parse_map,
    ColumnKind.JSON: lambda v: json_loads(v) or [],
    ColumnKind.TIMESTAMP: to_datetime,
}


class SqliteAnalyticsStore(SqlAnalyticsStore):
    """SQLite-backed implementation of the analytics store."""

    driver_name = "sqlite"

    def __init__(self, path: Path | str, cursor_codec: Any) -> None:
        self._path = str(path)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._cursor_codec = cursor_codec

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._connection is not None:
            return
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self._path, timeout=30)
        self._connection.row_factory = aiosqlite.Row
        # WAL keeps the ingest writer from blocking the API's readers, which is
        # the whole reason local development feels usable.
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA synchronous=NORMAL")
        await self._connection.execute("PRAGMA busy_timeout=30000")
        await self._connection.execute("PRAGMA foreign_keys=ON")
        await self._connection.commit()
        # SQLite has no decimal type, so SUM() over the TEXT column money is
        # stored in would coerce every value to a float. This aggregate keeps
        # the arithmetic in Decimal and returns the exact total as text.
        await self._register_decimal_sum()
        await self.migrate()

    async def _register_decimal_sum(self) -> None:
        """Install the exact-decimal SUM aggregate on the sqlite3 connection.

        aiosqlite proxies ``create_function`` but not ``create_aggregate``, so
        the registration is submitted to the connection's own worker thread via
        the same private executor aiosqlite uses for every other call. Doing it
        on this thread instead would violate sqlite3's single-thread rule for
        the connection object.
        """
        connection = self._require_connection()

        def register() -> None:
            connection._conn.create_aggregate("aiobs_decimal_sum", 1, _DecimalSum)

        await connection._execute(register)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def check_health(self) -> None:
        try:
            await self._fetch("SELECT 1 AS ok", {})
        except Exception as exc:
            raise DependencyUnavailableError("analytics-sqlite", cause=str(exc)) from exc

    async def migrate(self) -> None:
        connection = self._require_connection()
        for table, columns in TABLE_COLUMNS.items():
            definitions = ", ".join(f"{name} {_SQLITE_TYPES[kind]}" for name, kind in columns)
            await connection.execute(f"CREATE TABLE IF NOT EXISTS {table} ({definitions})")
            key = _NATURAL_KEYS[table]
            await connection.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_natural "
                f"ON {table} ({', '.join(key)})"
            )
            for index_columns in _INDEXES.get(table, ()):
                name = f"ix_{table}_{'_'.join(index_columns)}"[:60]
                await connection.execute(
                    f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({', '.join(index_columns)})"
                )
        await connection.commit()

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise DependencyUnavailableError(
                "analytics-sqlite", cause="store used before start() was awaited"
            )
        return self._connection

    # ------------------------------------------------------------------
    # dialect primitives
    # ------------------------------------------------------------------

    def _param(self, name: str) -> str:
        return f":{name}"

    def _decimal_sum(self, column: str) -> str:
        return f"aiobs_decimal_sum({column})"

    def _null_safe(self, column: str, kind: ColumnKind) -> str:
        if kind is ColumnKind.DECIMAL_NULL:
            # Money is stored as TEXT here, and TEXT orders lexicographically:
            # "9" would sort above "10". Ordering is projected through REAL,
            # which is safe -- the *values* returned to the caller are still
            # decoded from the exact text, and the schema tiebreaker keeps the
            # total order strict even if two amounts collapse to one float.
            return f"COALESCE(CAST({column} AS REAL), {_NULL_SENTINEL})"
        return super()._null_safe(column, kind)

    def _order_value(self, kind: ColumnKind, value: Any) -> Any:
        if kind is ColumnKind.DECIMAL_NULL and isinstance(value, Decimal):
            # Must match the projection in _null_safe, and sqlite3 cannot bind
            # a Decimal at all.
            return float(value)
        return value

    def _metric_order_expression(self) -> str:
        # aiobs_decimal_sum returns text; ordering it as text would rank "9"
        # above "10". Ranking is approximate by nature, so a float cast is fine
        # here even though the returned value must stay exact.
        return "CAST(value AS REAL)"

    def _array_contains(self, column: str, placeholder: str) -> str:
        # json_each explodes the stored JSON array; EXISTS keeps it a semi-join.
        return f"EXISTS (SELECT 1 FROM json_each({column}) AS _je WHERE _je.value = {placeholder})"

    def _map_value(self, column: str, key_placeholder: str) -> str:
        # The key is a bound parameter and is charset-restricted at parse time,
        # so the constructed JSON path cannot be escaped.
        return f"json_extract({column}, '$.\"' || {key_placeholder} || '\"')"

    def _bool_literal(self, value: bool) -> str:
        return "1" if value else "0"

    @property
    def _decoders(self) -> Mapping[ColumnKind, Callable[[Any], Any]]:
        return _DECODERS

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------

    async def _fetch(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        connection = self._require_connection()
        async with connection.execute(sql, dict(params)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def _execute(self, sql: str, params: Mapping[str, Any]) -> None:
        connection = self._require_connection()
        async with self._lock:
            await connection.execute(sql, dict(params))
            await connection.commit()

    async def _insert(self, table: str, rows: Sequence[Any]) -> int:
        if not rows:
            return 0
        connection = self._require_connection()
        columns = TABLE_COLUMNS[table]
        names = [name for name, _ in columns]
        key = set(_NATURAL_KEYS[table])
        placeholders = ", ".join("?" for _ in names)
        updates = ", ".join(f"{name} = excluded.{name}" for name in names if name not in key)
        # The version guard is what makes a replay a no-op rather than a
        # rewrite: an older duplicate cannot overwrite newer, enriched data.
        version_column = "ingest_version" if "ingest_version" in names else None
        conflict = (
            f"ON CONFLICT ({', '.join(_NATURAL_KEYS[table])}) DO UPDATE SET {updates}"
            if updates
            else f"ON CONFLICT ({', '.join(_NATURAL_KEYS[table])}) DO NOTHING"
        )
        if updates and version_column:
            conflict += f" WHERE excluded.{version_column} >= {table}.{version_column}"

        sql = f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders}) {conflict}"
        values = [encode_row(row, columns, _ENCODERS) for row in rows]
        async with self._lock:
            await connection.executemany(sql, values)
            await connection.commit()
        return len(rows)

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
        partition = f"PARTITION BY {', '.join(keys)} " if keys else ""
        group_clause = f"GROUP BY {', '.join(keys)} " if keys else ""
        limit_clause = f"LIMIT {int(limit_groups)}" if keys else ""

        # Exact lower order statistic: index floor(level * (n - 1)), 0-based.
        # This matches ClickHouse's quantileExactLow so the two drivers agree
        # to the digit, which the conformance suite asserts.
        percentile_selects = ", ".join(
            f"MAX(CASE WHEN rn = CAST({level} * (n - 1) AS INTEGER) + 1 THEN v END) "
            f"AS p{int(level * 100)}"
            for level in levels
        )

        sql = (
            f"WITH filtered AS ("
            f"  SELECT {selected_keys}{column} AS v FROM {table} "
            f"  WHERE {where} AND {column} IS NOT NULL"
            f"), ranked AS ("
            f"  SELECT {selected_keys}v, "
            f"    ROW_NUMBER() OVER ({partition}ORDER BY v) AS rn, "
            f"    COUNT(*) OVER ({partition}) AS n "
            f"  FROM filtered"
            f") "
            f"SELECT {selected_keys}MAX(n) AS n, {percentile_selects}, "
            f"AVG(v) AS avg_value, MAX(v) AS max_value "
            f"FROM ranked {group_clause}ORDER BY n DESC {limit_clause}"
        )
        return await self._fetch(sql, params)

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------

    async def delete_trace_children(self, scope: AnalyticsScope, trace_id: str) -> None:
        for table in ("retrieval_documents", "agent_steps", "span_events", "cost_records"):
            await self._execute(
                f"DELETE FROM {table} WHERE organization_id = :org AND trace_id = :trace",
                {"org": scope.organization_id, "trace": trace_id},
            )

    async def delete_expired(
        self, *, table: str, cutoff: datetime, batch_size: int = 10_000
    ) -> RetentionResult:
        if table not in TABLE_COLUMNS:
            raise KeyError(f"unknown analytics table {table!r}")
        time_column = TIME_COLUMN[table]
        connection = self._require_connection()
        cutoff_nano = datetime_to_unix_nano(cutoff)
        # rowid-bounded delete keeps the write lock short, which matters even
        # here: a multi-million-row DELETE would stall ingestion.
        sql = (
            f"DELETE FROM {table} WHERE rowid IN ("
            f"  SELECT rowid FROM {table} WHERE {time_column} < :cutoff LIMIT :batch)"
        )
        async with self._lock:
            cursor = await connection.execute(
                sql, {"cutoff": cutoff_nano, "batch": int(batch_size)}
            )
            deleted = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            await connection.commit()
        return RetentionResult(
            table=table,
            rows_deleted=deleted,
            cutoff=cutoff,
            exhausted=deleted < batch_size,
        )

    async def trace_ids_needing_rollup(
        self, *, since: datetime, limit: int = 1_000
    ) -> list[tuple[str, str, str, str]]:
        """Traces whose spans have been written more recently than their roll-up.

        This is the reconciliation path for late-arriving spans: the worker
        recomputes eagerly for traces it just touched, and this query catches
        anything a crash or a dead-letter replay left behind.
        """
        since_ms = _encode_timestamp(since) or 0
        sql = (
            "SELECT s.organization_id, s.project_id, s.environment, s.trace_id "
            "FROM spans AS s LEFT JOIN traces AS t "
            "  ON t.organization_id = s.organization_id AND t.trace_id = s.trace_id "
            "WHERE s.ingested_at >= :since "
            "  AND (t.trace_id IS NULL OR t.ingested_at < s.ingested_at) "
            "GROUP BY s.organization_id, s.project_id, s.environment, s.trace_id "
            "LIMIT :limit"
        )
        rows = await self._fetch(sql, {"since": since_ms, "limit": int(limit)})
        return [
            (
                str(row["organization_id"]),
                str(row["project_id"]),
                str(row["environment"]),
                str(row["trace_id"]),
            )
            for row in rows
        ]

    async def raw_query(
        self, sql: str, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Escape hatch for tests and the admin CLI. Never called from request paths."""
        return await self._fetch(sql, params or {})
