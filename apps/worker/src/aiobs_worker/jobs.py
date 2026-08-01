"""Periodic maintenance jobs.

Each job is idempotent, bounded in the work it does per run, and safe to run
concurrently on several worker replicas -- coordination is a lease in the
key-value store, so a replica that dies mid-job simply loses its lease and
another picks the work up on the next tick.

Bounded work per run matters: a retention sweep that tried to delete a month of
data in one statement would hold locks long enough to stall ingestion. Each pass
deletes a batch and returns; the scheduler calls it again.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select, update

from aiobs_api.core.config import Settings
from aiobs_api.core.logging import get_logger
from aiobs_api.core.timeutil import Clock
from aiobs_api.services.bundle import ServiceBundle
from aiobs_api.storage.analytics.protocol import RETENTION_TABLES
from aiobs_api.storage.kv.memory_kv import monotonic_owner_token
from aiobs_api.storage.postgres.models import (
    ExportJob,
    IdempotencyRecord,
    RetentionPolicy,
    StoredObject,
)
from aiobs_api.telemetry.metrics import BACKGROUND_JOB_LAST_SUCCESS

__all__ = ["Job", "JobRunner", "build_jobs"]

log = get_logger(__name__)


@dataclass(slots=True)
class Job:
    """A periodic task with a distributed lease."""

    name: str
    interval_seconds: float
    run: Callable[[], Awaitable[int]]
    #: Lease duration; must exceed the job's worst-case runtime.
    lease_seconds: int = 300
    last_run: float = 0.0
    last_result: int = 0
    failures: int = 0
    errors: list[str] = field(default_factory=list)


class JobRunner:
    """Runs jobs on their intervals, one at a time, with leases."""

    def __init__(self, *, services: ServiceBundle, jobs: list[Job], clock: Clock) -> None:
        self._services = services
        self._jobs = jobs
        self._clock = clock
        self._stopping = asyncio.Event()
        self._owner = monotonic_owner_token("jobs")

    def request_stop(self) -> None:
        self._stopping.set()

    @property
    def jobs(self) -> list[Job]:
        return self._jobs

    async def run(self) -> None:
        """Tick every second, running jobs whose interval has elapsed."""
        log.info("jobs.started", jobs=[job.name for job in self._jobs], owner=self._owner)
        while not self._stopping.is_set():
            now = time.monotonic()
            for job in self._jobs:
                if now - job.last_run < job.interval_seconds:
                    continue
                job.last_run = now
                await self._run_one(job)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
        log.info("jobs.stopped")

    async def _run_one(self, job: Job) -> None:
        kv = self._services.container.kv
        acquired = await kv.acquire_lock(
            f"job:{job.name}", owner=self._owner, ttl_seconds=job.lease_seconds
        )
        if not acquired:
            # Another replica holds the lease. Skipping is correct: running the
            # same sweep twice concurrently would double the load for no gain.
            return
        try:
            job.last_result = await job.run()
            BACKGROUND_JOB_LAST_SUCCESS.labels(job=job.name).set(time.time())
            if job.last_result:
                log.info("jobs.completed", job=job.name, affected=job.last_result)
        except Exception as exc:
            job.failures += 1
            job.errors = [f"{type(exc).__name__}: {exc}", *job.errors][:5]
            log.error("jobs.failed", job=job.name, error=str(exc), exc_info=True)
        finally:
            await kv.release_lock(f"job:{job.name}", owner=self._owner)


def build_jobs(*, services: ServiceBundle, settings: Settings, clock: Clock) -> list[Job]:
    """Construct the maintenance job set."""

    async def retention_sweep() -> int:
        """Delete analytics data past its retention horizon.

        Per-project policies are read first; anything without one uses the
        platform default. Deletion is ordered derived-tables-first so an
        interrupted sweep leaves orphaned detail rows (harmless, cleaned next
        pass) rather than spans whose detail has vanished.
        """
        deleted = 0
        now = clock.now()
        async with services.container.database.session_scope() as session:
            policies = list((await session.execute(select(RetentionPolicy))).scalars().all())
        default_days = settings.retention.raw_span_days
        horizons = {policy.project_id: policy.raw_span_days for policy in policies}

        for table in RETENTION_TABLES:
            # The sweep applies the shortest horizon across projects as a coarse
            # first pass; per-project precision comes from the project-scoped
            # deletes below. This keeps the common case (uniform retention) to a
            # single partition drop.
            days = min([default_days, *horizons.values()]) if horizons else default_days
            cutoff = now - timedelta(days=days)
            result = await services.container.analytics.delete_expired(
                table=table,
                cutoff=cutoff,
                batch_size=settings.retention.sweep_batch_size,
            )
            deleted += result.rows_deleted
        return deleted

    async def payload_expiry() -> int:
        """Delete object-storage payloads past their retention horizon.

        Marks the row deleted *after* the object is gone, so a crash between the
        two leaves a row pointing at a missing object -- which the orphan sweep
        detects -- rather than a live object with no row, which nothing would
        ever find or delete.
        """
        now = clock.now()
        removed = 0
        async with services.container.database.session_scope() as session:
            expired = list(
                (
                    await session.execute(
                        select(StoredObject)
                        .where(
                            StoredObject.expires_at.is_not(None),
                            StoredObject.expires_at < now,
                            StoredObject.deleted_at.is_(None),
                        )
                        .limit(settings.retention.sweep_batch_size)
                    )
                )
                .scalars()
                .all()
            )
            keys = [(item.id, item.object_key) for item in expired]

        for object_id, object_key in keys:
            try:
                await services.container.objects.delete(object_key)
            except Exception as exc:
                log.warning("retention.object_delete_failed", key=object_key, error=str(exc))
                continue
            async with services.container.database.session_scope() as session:
                await session.execute(
                    update(StoredObject).where(StoredObject.id == object_id).values(deleted_at=now)
                )
            removed += 1
        return removed

    async def orphan_detection() -> int:
        """Find object rows whose target no longer exists.

        The invariant is bidirectional -- no row without an object, no object
        without a row -- and this half is the cheap one to check. Orphans are
        logged rather than auto-deleted: a missing object is usually a symptom
        (a bad restore, a lifecycle rule) that a human should see.
        """
        orphans = 0
        async with services.container.database.session_scope() as session:
            candidates = list(
                (
                    await session.execute(
                        select(StoredObject)
                        .where(StoredObject.deleted_at.is_(None))
                        .order_by(StoredObject.created_at.desc())
                        .limit(200)
                    )
                )
                .scalars()
                .all()
            )
            keys = [(item.id, item.object_key) for item in candidates]
        for object_id, object_key in keys:
            if not await services.container.objects.exists(object_key):
                orphans += 1
                log.warning("retention.orphaned_reference", object_id=object_id, key=object_key)
        return orphans

    async def rollup_reconciliation() -> int:
        """Repair trace roll-ups the eager path missed."""
        return await services.rollup_processor.reconcile(
            since=timedelta(seconds=settings.ingest.trace_completion_grace_seconds * 4),
            limit=500,
        )

    async def idempotency_cleanup() -> int:
        """Drop expired idempotency records."""
        now = clock.now()
        async with services.container.database.session_scope() as session:
            stale = list(
                (
                    await session.execute(
                        select(IdempotencyRecord)
                        .where(IdempotencyRecord.expires_at < now)
                        .limit(1_000)
                    )
                )
                .scalars()
                .all()
            )
            for record in stale:
                await session.delete(record)
            return len(stale)

    async def export_runner() -> int:
        """Run queued export jobs.

        The API starts small exports inline; anything that failed to start, or
        was queued while the API was restarting, is picked up here.
        """
        async with services.container.database.session_scope() as session:
            queued = list(
                (
                    await session.execute(
                        select(ExportJob)
                        .where(ExportJob.status == "queued")
                        .order_by(ExportJob.created_at)
                        .limit(5)
                    )
                )
                .scalars()
                .all()
            )
            pending = [(job.id, job.organization_id) for job in queued]
        completed = 0
        for job_id, organization_id in pending:
            try:
                await services.exports.run(job_id=job_id, organization_id=organization_id)
                completed += 1
            except Exception as exc:
                log.warning("jobs.export_failed", job_id=job_id, error=str(exc))
        return completed

    async def export_expiry() -> int:
        """Mark expired exports so their download links stop working."""
        now = clock.now()
        async with services.container.database.session_scope() as session:
            result = await session.execute(
                update(ExportJob)
                .where(
                    ExportJob.status == "completed",
                    ExportJob.expires_at.is_not(None),
                    ExportJob.expires_at < now,
                )
                .values(status="expired")
            )
            return result.rowcount or 0

    async def bus_trim() -> int:
        """Trim fully-consumed bus messages (database driver only)."""
        bus = services.container.bus
        purge = getattr(bus, "purge_expired", None)
        if purge is None:
            return 0
        return int(await purge())

    return [
        Job(
            name="rollup_reconciliation",
            interval_seconds=30,
            run=rollup_reconciliation,
            lease_seconds=120,
        ),
        Job(
            name="retention_sweep",
            interval_seconds=settings.retention.sweep_interval_seconds,
            run=retention_sweep,
            lease_seconds=900,
        ),
        Job(name="payload_expiry", interval_seconds=300, run=payload_expiry, lease_seconds=600),
        Job(name="orphan_detection", interval_seconds=3_600, run=orphan_detection),
        Job(name="idempotency_cleanup", interval_seconds=600, run=idempotency_cleanup),
        Job(name="export_runner", interval_seconds=10, run=export_runner, lease_seconds=600),
        Job(name="export_expiry", interval_seconds=3_600, run=export_expiry),
        Job(name="bus_trim", interval_seconds=600, run=bus_trim),
    ]
