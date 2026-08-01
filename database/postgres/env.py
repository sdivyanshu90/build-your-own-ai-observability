"""Alembic environment.

Runs migrations against the async engine the application itself uses, so a
migration cannot succeed against a driver or DSN that production does not use.

``render_as_batch`` is enabled for SQLite. SQLite cannot ``ALTER COLUMN``;
Alembic's batch mode emulates it by recreating the table. Without this, any
migration touching a column would work on PostgreSQL and fail in CI.
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Make the application package importable when Alembic is run from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (
    _REPO_ROOT / "apps" / "api" / "src",
    _REPO_ROOT / "packages" / "shared-schemas" / "python",
):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from aiobs_api.storage.postgres.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("AIOBS_DATABASE__URL")
    if url:
        return url
    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured
    # Match the application's development default so `make migrate` works with
    # no environment set at all.
    return "sqlite+aiosqlite:///./.aiobs/metadata.db"


def _render_item(type_, obj, autogen_context):  # type: ignore[no-untyped-def]
    """Render custom column types without importing application code.

    A migration must remain runnable years later, after the application has been
    refactored. Emitting ``aiobs_api.storage.postgres.types.DecimalText()`` into
    a migration would make every historical revision break the day that module
    moves. Instead the *physical* type is rendered, with the same dialect
    variance the custom type implements.
    """
    import sqlalchemy as sa

    from aiobs_api.storage.postgres.types import DecimalText, UtcDateTime

    if type_ != "type":
        return False
    if isinstance(obj, UtcDateTime):
        autogen_context.imports.add("import sqlalchemy as sa")
        return "sa.DateTime(timezone=True)"
    if isinstance(obj, DecimalText):
        autogen_context.imports.add("import sqlalchemy as sa")
        return "sa.Numeric(precision=38, scale=18).with_variant(sa.String(64), 'sqlite')"
    if isinstance(obj, sa.JSON):
        autogen_context.imports.add("import sqlalchemy as sa")
        autogen_context.imports.add("from sqlalchemy.dialects import postgresql")
        return "sa.JSON().with_variant(postgresql.JSONB(), 'postgresql')"
    return False


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Detect type changes as well as added/removed columns; without it a
        # widened VARCHAR silently drifts between the model and the database.
        compare_type=True,
        compare_server_default=True,
        render_as_batch=connection.dialect.name == "sqlite",
        # Keep Alembic's own bookkeeping table out of autogenerate diffs.
        include_object=_include_object,
        render_item=_render_item,
    )


def _include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    if type_ == "table" and name in {"alembic_version"}:
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting.

    Used by `make migrate-sql` so a DBA can review exactly what will run
    against production before it does.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    try:
        async with connectable.connect() as connection:
            if connection.dialect.name == "sqlite":
                await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            await connection.run_sync(do_run_migrations)
            # SQLAlchemy 2.0 does not autocommit. Without this the version-table
            # update is rolled back when the connection closes, leaving a
            # database whose schema has changed but whose recorded revision has
            # not -- the worst possible migration outcome.
            await connection.commit()
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
