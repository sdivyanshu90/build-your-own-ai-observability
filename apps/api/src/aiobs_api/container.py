"""Composition root.

Every concrete storage driver is chosen here, once, from configuration. No
module below this one imports a driver directly -- they depend on the abstract
interfaces. That is what makes the SQLite/ClickHouse and memory/Redis
substitutions possible without touching a line of business logic, and what
keeps the service layer testable with real fakes rather than patched imports.

Startup and shutdown are explicit and ordered:

* **Start**: relational database, then analytics, object store, key-value,
  event bus. Each is health-checked; a failure aborts startup rather than
  producing a process that serves 500s.
* **Stop**: reverse order, with a grace period, so in-flight work drains before
  its dependencies disappear.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .core.config import (
    AnalyticsDriver,
    BusDriver,
    KeyValueDriver,
    ObjectStoreDriver,
    Settings,
)
from .core.errors import DependencyUnavailableError
from .core.logging import get_logger
from .core.query import CursorCodec
from .core.timeutil import Clock, SystemClock
from .storage.analytics.protocol import AnalyticsStore
from .storage.bus.protocol import EventBus
from .storage.kv.protocol import KeyValueStore
from .storage.objects.protocol import ObjectStore
from .storage.postgres.session import Database

__all__ = ["Container", "build_container"]

log = get_logger(__name__)


@dataclass(slots=True)
class HealthReport:
    """Per-dependency health, as reported by ``/ready``."""

    healthy: bool
    checks: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {"healthy": self.healthy, "checks": dict(self.checks)}


class Container:
    """Owns every long-lived resource in the process."""

    __slots__ = (
        "_started",
        "analytics",
        "bus",
        "clock",
        "cursor_codec",
        "database",
        "kv",
        "objects",
        "settings",
    )

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        analytics: AnalyticsStore,
        objects: ObjectStore,
        kv: KeyValueStore,
        bus: EventBus,
        cursor_codec: CursorCodec,
        clock: Clock,
    ) -> None:
        self.settings = settings
        self.database = database
        self.analytics = analytics
        self.objects = objects
        self.kv = kv
        self.bus = bus
        self.cursor_codec = cursor_codec
        self.clock = clock
        self._started = False

    async def start(self) -> None:
        """Open every dependency, failing fast if any is unusable."""
        if self._started:
            return
        problems = self.settings.validate_for_runtime()
        if problems:
            raise DependencyUnavailableError(
                "configuration",
                cause="; ".join(problems),
            )
        await self.database.check_health()
        await self.analytics.start()
        await self.objects.start()
        await self.kv.start()
        await self.bus.start()
        self._started = True
        log.info("container.started", **self.settings.describe())

    async def stop(self) -> None:
        """Close dependencies in reverse order, tolerating individual failures.

        A failure closing one resource must not prevent the others from
        closing: a leaked ClickHouse connection because Redis was already gone
        is a worse outcome than a logged warning.
        """
        for name, closer in (
            ("bus", self.bus.close),
            ("kv", self.kv.close),
            ("objects", self.objects.close),
            ("analytics", self.analytics.close),
            ("database", self.database.dispose),
        ):
            try:
                await asyncio.wait_for(closer(), timeout=10.0)
            except Exception as exc:
                log.warning("container.close_failed", dependency=name, error=str(exc))
        self._started = False
        log.info("container.stopped")

    async def health(self) -> HealthReport:
        """Probe every dependency concurrently for the readiness endpoint."""
        checks: dict[str, str] = {}

        async def probe(name: str, coroutine: Any) -> None:
            try:
                await asyncio.wait_for(coroutine, timeout=3.0)
                checks[name] = "ok"
            except asyncio.TimeoutError:
                checks[name] = "timeout"
            except Exception as exc:
                checks[name] = f"error: {type(exc).__name__}"

        await asyncio.gather(
            probe("database", self.database.check_health()),
            probe("analytics", self.analytics.check_health()),
            probe("object_store", self.objects.check_health()),
            probe("key_value", self.kv.check_health()),
            probe("event_bus", self.bus.check_health()),
        )
        healthy = all(status == "ok" for status in checks.values())
        return HealthReport(healthy=healthy, checks=checks)


def build_container(settings: Settings, *, clock: Clock | None = None) -> Container:
    """Construct the container for ``settings`` without opening connections."""
    active_clock = clock or SystemClock()
    database = Database(settings.database)
    # Cursors are signed with a key derived from the JWT secret so that a
    # cursor issued by one replica validates on another without extra config.
    cursor_codec = CursorCodec(settings.auth.jwt_secret.get_secret_value() + ":cursor")

    analytics = _build_analytics(settings, cursor_codec)
    objects = _build_objects(settings)
    kv = _build_kv(settings, active_clock)
    bus = _build_bus(settings, database, active_clock)

    return Container(
        settings=settings,
        database=database,
        analytics=analytics,
        objects=objects,
        kv=kv,
        bus=bus,
        cursor_codec=cursor_codec,
        clock=active_clock,
    )


def _build_analytics(settings: Settings, cursor_codec: CursorCodec) -> AnalyticsStore:
    if settings.analytics.driver is AnalyticsDriver.CLICKHOUSE:
        from .storage.analytics.clickhouse_store import ClickHouseAnalyticsStore

        return ClickHouseAnalyticsStore(
            url=settings.analytics.url,
            database=settings.analytics.database,
            username=settings.analytics.username,
            password=settings.analytics.password.get_secret_value(),
            cursor_codec=cursor_codec,
            connect_timeout=settings.analytics.connect_timeout_seconds,
            query_timeout=settings.analytics.query_timeout_seconds,
            max_result_rows=settings.analytics.max_result_rows,
        )
    from .storage.analytics.sqlite_store import SqliteAnalyticsStore

    return SqliteAnalyticsStore(settings.analytics.sqlite_path, cursor_codec)


def _build_objects(settings: Settings) -> ObjectStore:
    if settings.objects.driver is ObjectStoreDriver.S3:
        from .storage.objects.s3_store import S3ObjectStore

        access_key = settings.objects.access_key_id
        secret_key = settings.objects.secret_access_key
        return S3ObjectStore(
            bucket=settings.objects.bucket,
            region=settings.objects.region,
            endpoint_url=settings.objects.endpoint_url,
            access_key_id=access_key.get_secret_value() if access_key else None,
            secret_access_key=secret_key.get_secret_value() if secret_key else None,
            max_object_bytes=settings.objects.max_object_bytes,
            # Auto-creation is a MinIO convenience and must never run against a
            # real account, where buckets carry policy that Terraform owns.
            create_bucket=not settings.environment.is_production_like,
        )
    from .storage.objects.filesystem_store import FilesystemObjectStore

    return FilesystemObjectStore(
        settings.objects.root_path, max_object_bytes=settings.objects.max_object_bytes
    )


def _build_kv(settings: Settings, clock: Clock) -> KeyValueStore:
    if settings.kv.driver is KeyValueDriver.REDIS:
        from .storage.kv.redis_kv import RedisKeyValueStore

        return RedisKeyValueStore(
            settings.kv.url, connect_timeout=settings.kv.connect_timeout_seconds
        )
    from .storage.kv.memory_kv import InMemoryKeyValueStore

    return InMemoryKeyValueStore(clock=clock)


def _build_bus(settings: Settings, database: Database, clock: Clock) -> EventBus:
    if settings.bus.driver is BusDriver.KAFKA:
        from .storage.bus.kafka_bus import KafkaEventBus

        return KafkaEventBus(
            brokers=settings.bus.brokers,
            topic_prefix=settings.bus.topic_prefix,
            compression=settings.bus.compression,
            client_id=settings.service_name,
            max_delivery_attempts=settings.bus.max_delivery_attempts,
        )
    from .storage.bus.database_bus import DatabaseEventBus

    return DatabaseEventBus(
        database,
        topic_prefix=settings.bus.topic_prefix,
        clock=clock,
        owner=settings.service_name,
    )
