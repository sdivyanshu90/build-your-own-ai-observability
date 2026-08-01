"""Async engine and session lifecycle for the metadata store.

The engine is created once per process and disposed on shutdown. Sessions are
per-request (or per-unit-of-work in the worker) and are always used through
:func:`session_scope`, which guarantees exactly one of commit or rollback.

Two dialect-specific details are handled here rather than leaking into callers:

* **SQLite needs foreign keys switched on.** They are off by default, which
  would quietly disable every ``ON DELETE CASCADE`` in the schema and let the
  test suite pass on constraints production would reject.
* **PostgreSQL needs a statement timeout.** Without one, a pathological
  analytics-adjacent query can pin a connection indefinitely and exhaust the
  pool. The timeout is set per connection at checkout.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from ...core.config import DatabaseSettings
from ...core.errors import DependencyUnavailableError
from ...core.logging import get_logger

__all__ = ["Database", "create_engine_from_settings"]

log = get_logger(__name__)


def create_engine_from_settings(settings: DatabaseSettings) -> AsyncEngine:
    """Build the async engine for ``settings``, applying dialect-specific tuning."""
    kwargs: dict[str, Any] = {
        "echo": settings.echo,
        "pool_pre_ping": True,
        "future": True,
    }

    if settings.is_sqlite:
        # SQLite's file locking makes a connection pool actively harmful for
        # writes; a single connection serialised by the driver is both simpler
        # and faster here. NullPool also means test databases are released
        # promptly instead of being pinned by an idle pooled connection.
        kwargs["poolclass"] = NullPool
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        _ensure_sqlite_directory(settings.url)
    else:
        kwargs.update(
            pool_size=settings.pool_size,
            max_overflow=settings.max_overflow,
            pool_timeout=settings.pool_timeout_seconds,
            pool_recycle=settings.pool_recycle_seconds,
            connect_args={
                "server_settings": {
                    "application_name": "aiobs",
                    "statement_timeout": str(settings.statement_timeout_ms),
                    # Bound the time a transaction may sit idle holding locks.
                    "idle_in_transaction_session_timeout": "60000",
                },
                "timeout": settings.pool_timeout_seconds,
            },
        )

    engine = create_async_engine(settings.url, **kwargs)

    if settings.is_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            # Foreign keys are OFF by default in SQLite. Without this the
            # schema's cascades are decorative.
            cursor.execute("PRAGMA foreign_keys=ON")
            # WAL lets readers proceed during a write, which the ingest tests
            # depend on.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine


def _ensure_sqlite_directory(url: str) -> None:
    """Create the parent directory for a SQLite file DSN if it is missing."""
    _, _, path_part = url.partition("///")
    if not path_part or path_part.startswith(":memory:"):
        return
    path = Path(path_part.split("?", 1)[0])
    path.parent.mkdir(parents=True, exist_ok=True)


class Database:
    """Owns the engine and hands out sessions.

    Constructed once during application startup and shared. It is deliberately
    not a global: tests construct their own instance against a scratch database,
    and the worker constructs one with different pool sizing.
    """

    __slots__ = ("_engine", "_session_factory", "_settings")

    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings
        self._engine = create_engine_from_settings(settings)
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,  # objects stay usable after commit
            autoflush=False,  # flushes are explicit; surprise flushes hide bugs
            class_=AsyncSession,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def settings(self) -> DatabaseSettings:
        return self._settings

    def session(self) -> AsyncSession:
        """Return a new session. Prefer :meth:`session_scope` unless you need
        manual control over the transaction boundary."""
        return self._session_factory()

    @asynccontextmanager
    async def session_scope(self) -> AsyncIterator[AsyncSession]:
        """Run a unit of work, committing on success and rolling back on error.

        Every write path in the platform goes through this, which is what makes
        the outbox pattern sound: the domain change and its outbox row share
        one transaction, so they commit or vanish together.
        """
        session = self._session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def check_health(self, timeout: float = 3.0) -> None:
        """Raise :class:`DependencyUnavailableError` if the database is unreachable."""

        async def _ping() -> None:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))

        try:
            await asyncio.wait_for(_ping(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise DependencyUnavailableError("postgres", cause="health check timed out") from exc
        except (SQLAlchemyError, DBAPIError, OSError) as exc:
            raise DependencyUnavailableError("postgres", cause=type(exc).__name__) from exc

    async def create_all(self) -> None:
        """Create every table from the ORM metadata.

        Used by tests and by the ``--create-schema`` bootstrap path only.
        Production schema changes go through Alembic, because ``create_all``
        cannot express a migration and silently ignores drift.
        """
        from .models import Base

        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        """Close every pooled connection. Called during graceful shutdown."""
        await self._engine.dispose()
        log.info("database.disposed")
