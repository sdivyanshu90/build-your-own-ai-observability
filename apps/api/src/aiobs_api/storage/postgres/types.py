"""Custom SQLAlchemy column types.

Only one type lives here, and it exists to solve a real correctness problem:
**money must never round-trip through a binary float.**

PostgreSQL has ``NUMERIC``, which is exact. SQLite does not: SQLAlchemy's
``Numeric`` on SQLite is stored as ``REAL`` (an IEEE-754 double) and emits a
runtime warning about lost precision. Since the platform runs on SQLite in
development and CI, a price stored as ``0.000003`` would come back as
``0.0000029999999999999997`` there and as ``0.000003`` in production -- and the
cost tests would pass on one and fail on the other, or worse, pass on both while
computing different totals.

:class:`DecimalText` sidesteps that by storing an exact decimal *string* on
SQLite and a native ``NUMERIC`` on PostgreSQL, converting to
:class:`decimal.Decimal` on the way out in both cases. Prices are never ordered
or aggregated in SQL -- the cost engine fetches the applicable entries and does
the arithmetic in Python with an explicit ``Decimal`` context -- so losing SQL
numeric ordering on SQLite costs nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Dialect, Numeric, String, TypeDecorator

__all__ = ["DecimalText", "UtcDateTime", "format_decimal"]

#: Widest value the platform accepts: 20 integral digits and 18 fractional
#: digits comfortably covers both per-token prices (1e-9) and annual spend.
_PRECISION = 38
_SCALE = 18


def format_decimal(value: Decimal) -> str:
    """Render a Decimal in a stable, lossless, sortable-by-value-free form.

    ``normalize()`` would turn ``1.500`` into ``1.5`` and ``100`` into ``1E+2``;
    the latter is legal but surprising in a database column and breaks naive
    string equality in tests. Formatting with ``f`` notation keeps the text
    plain while preserving every significant digit.
    """
    if not value.is_finite():
        raise ValueError(f"non-finite Decimal cannot be stored: {value!r}")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -_SCALE:
        raise ValueError(
            f"Decimal {value} has more than {_SCALE} fractional digits and would "
            "lose precision on storage"
        )
    return format(value, "f")


class DecimalText(TypeDecorator[Decimal]):
    """Exact decimal storage on every supported dialect."""

    impl = String(64)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Numeric(_PRECISION, _SCALE, asdecimal=True))
        return dialect.type_descriptor(String(64))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        as_decimal = value if isinstance(value, Decimal) else Decimal(str(value))
        if dialect.name == "postgresql":
            return as_decimal
        return format_decimal(as_decimal)

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp that is always timezone-aware UTC in Python.

    PostgreSQL's ``TIMESTAMPTZ`` round-trips an aware datetime correctly.
    SQLite has no timezone concept at all and hands back a *naive* datetime,
    so the same code that works in production raises
    ``can't compare offset-naive and offset-aware datetimes`` in development --
    or, worse, silently compares wrong.

    Enforcing the invariant in the column type means every timestamp in the
    platform is aware UTC by construction, on every dialect, and a naive value
    is rejected at the boundary where it can still be attributed to a caller.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"expected a datetime, got {type(value).__name__}")
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime rejected: attach a timezone explicitly "
                "(datetime.now(timezone.utc), not datetime.now())"
            )
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            return value
        # SQLite returns naive values; they were written as UTC, so label them.
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
