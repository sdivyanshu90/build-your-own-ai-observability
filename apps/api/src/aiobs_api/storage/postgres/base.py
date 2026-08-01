"""SQLAlchemy foundations for the relational metadata store.

Portability note
----------------
The models here run on PostgreSQL in production and on SQLite in development,
CI and unit tests. That constrains a few choices:

* identifiers are ``String(40)`` rather than native ``UUID`` -- the platform
  uses prefixed ULIDs anyway (see :mod:`aiobs_schemas.ids`), which are strings;
* structured columns use ``JSON`` with a ``JSONB`` variant, so PostgreSQL gets
  the indexable binary form while SQLite still works;
* enums are stored as ``String`` with a ``CHECK`` constraint rather than native
  ``ENUM`` types, because altering a PostgreSQL enum requires a migration lock
  and SQLite has no enum at all;
* every timestamp is ``UtcDateTime`` and always written as UTC.

The tenancy invariant -- *every row belongs to exactly one organisation* -- is
expressed structurally by :class:`TenantScopedMixin`, and enforced at query time
by the repository base class. Two layers, because a single missed ``WHERE``
clause is a cross-tenant data leak.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    MetaData,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .types import UtcDateTime

__all__ = [
    "Base",
    "JSONColumn",
    "TenantScopedMixin",
    "TimestampMixin",
    "naming_convention",
    "utc_now_column",
]

#: Deterministic constraint names. Without this, Alembic autogenerate emits
#: database-assigned names that differ between PostgreSQL and SQLite, and
#: downgrade scripts cannot find the constraint they need to drop.
naming_convention = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: JSON on SQLite, JSONB on PostgreSQL. JSONB is worth the variant: it supports
#: GIN indexes, which the audit-log and prompt-variable searches depend on.
JSONColumn = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    """Declarative base carrying the shared metadata and type map."""

    metadata = MetaData(naming_convention=naming_convention)

    type_annotation_map = {
        dict[str, Any]: JSONColumn,
        list[str]: JSONColumn,
        list[dict[str, Any]]: JSONColumn,
    }

    def as_dict(self) -> dict[str, Any]:
        """Shallow column dictionary, for logging and test assertions."""
        return {column.name: getattr(self, column.name) for column in self.__table__.columns}

    def __repr__(self) -> str:
        identifier = getattr(self, "id", None)
        return f"<{type(self).__name__} id={identifier!r}>"


def utc_now_column() -> Mapped[datetime]:
    """A server-defaulted, timezone-aware creation timestamp."""
    return mapped_column(
        UtcDateTime,
        nullable=False,
        server_default=func.now(),
        index=True,
    )


class TimestampMixin:
    """``created_at`` / ``updated_at`` maintained by the database.

    Server-side defaults rather than Python defaults, so that rows written by a
    migration or by ``psql`` are timestamped consistently with rows written by
    the application.
    """

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TenantScopedMixin:
    """Marks a table as belonging to exactly one organisation.

    Every tenant-scoped table gets an ``organization_id`` foreign key with
    ``ON DELETE CASCADE`` and a leading index, because *every* query against
    these tables filters on it. Repositories refuse to build a query for a
    tenant-scoped model without an organisation predicate -- see
    :class:`aiobs_api.storage.postgres.repository.TenantRepository`.
    """

    @property
    def __tenant_column__(self) -> str:
        return "organization_id"

    organization_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def id_column(prefix_hint: str) -> Mapped[str]:
    """Primary key column for a prefixed ULID.

    ``prefix_hint`` documents the expected :class:`aiobs_schemas.ids.IdPrefix`
    and is enforced by a CHECK constraint so that a project id can never be
    written into an api-key column.
    """
    return mapped_column(
        String(40),
        primary_key=True,
        comment=f"prefixed ULID, expected prefix {prefix_hint!r}",
    )


def prefix_check(table: str, column: str, prefix: str) -> CheckConstraint:
    """CHECK constraint asserting an id column carries the right type prefix."""
    return CheckConstraint(
        f"{column} LIKE '{prefix}\\_%' ESCAPE '\\'",
        name=f"{table}_{column}_prefix",
    )


def tenant_index(table: str, *columns: str) -> Index:
    """Composite index with ``organization_id`` first.

    Leading with the tenant column means a single index serves both the
    per-tenant listing query and the tenant-isolation predicate, and keeps one
    tenant's rows physically clustered away from another's.
    """
    return Index(f"ix_{table}_org_{'_'.join(columns)}", "organization_id", *columns)
