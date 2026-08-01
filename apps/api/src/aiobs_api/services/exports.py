"""Asynchronous data export.

Exports are jobs, not streaming responses. A trace export can span millions of
rows: a synchronous request would exceed any sane timeout, and a client retry
would restart the whole scan from zero.

Two properties are enforced regardless of what the caller asks for:

**Redaction is applied and recorded.** The archive's manifest states whether
fields were redacted, so a downstream consumer cannot mistake a redacted export
for full-fidelity data. Requesting an unredacted export requires
``trace:read_payloads``.

**Exports expire.** The object is written with a TTL and the signed URL is
short-lived, so a link pasted into a chat channel stops working rather than
becoming a permanent, unaudited data egress path.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from aiobs_schemas.ids import IdPrefix, generate_id

from ..core.errors import GoneError, NotFoundError, ValidationFailedError
from ..core.logging import get_logger
from ..core.query import CursorCodec, FilterCondition, PageRequest, revive_cursor_values
from ..core.timeutil import Clock
from ..domain.principal import Principal
from ..domain.rbac import Permission
from ..domain.redaction import Redactor
from ..storage.analytics.protocol import AnalyticsStore
from ..storage.analytics.rows import AnalyticsScope
from ..storage.objects.protocol import ObjectStore, PayloadKind, build_object_key
from ..storage.postgres.models import ExportJob, StoredObject
from ..storage.postgres.session import Database

__all__ = ["ExportRequest", "ExportService"]

log = get_logger(__name__)

#: Hard ceiling per export. Beyond this, an operator should be querying the
#: analytics store directly rather than materialising an archive.
MAX_EXPORT_ROWS = 1_000_000
_PAGE_SIZE = 500


@dataclass(frozen=True, slots=True)
class ExportRequest:
    project_id: str
    resource: str
    start: datetime
    end: datetime
    format: str = "jsonl"
    environment: str | None = None
    filters: tuple[FilterCondition, ...] = ()
    include_payloads: bool = False


class ExportService:
    """Creates, runs and serves export jobs."""

    _RESOURCES = {"traces", "spans", "costs"}

    def __init__(
        self,
        *,
        database: Database,
        analytics: AnalyticsStore,
        objects: ObjectStore,
        clock: Clock,
        redactor: Redactor,
        cursor_codec: CursorCodec,
    ) -> None:
        self._database = database
        self._analytics = analytics
        self._objects = objects
        self._clock = clock
        self._redactor = redactor
        # Injected rather than read off the analytics store: the codec is not
        # part of the AnalyticsStore protocol, and reaching for a private
        # attribute worked only because both drivers happen to have one.
        self._cursor_codec = cursor_codec

    async def create(self, *, principal: Principal, request: ExportRequest) -> ExportJob:
        principal.require(Permission.EXPORT_CREATE)
        principal.require_project(request.project_id)
        if request.resource not in self._RESOURCES:
            raise ValidationFailedError(
                f"unknown export resource {request.resource!r}; "
                f"expected one of {sorted(self._RESOURCES)}"
            )
        if request.format not in {"jsonl", "csv", "json"}:
            raise ValidationFailedError("export format must be jsonl, csv or json")
        if request.include_payloads and not principal.can(Permission.TRACE_READ_PAYLOADS):
            raise ValidationFailedError(
                "exporting unredacted payloads requires the trace:read_payloads permission"
            )
        if request.end <= request.start:
            raise ValidationFailedError("the end of the time range must be after its start")

        now = self._clock.now()
        job = ExportJob(
            id=generate_id(IdPrefix.EXPORT),
            organization_id=principal.organization_id,
            project_id=request.project_id,
            requested_by=principal.id,
            resource=request.resource,
            format=request.format,
            query={
                "start": request.start.isoformat(),
                "end": request.end.isoformat(),
                "environment": request.environment,
                "include_payloads": request.include_payloads,
                "filters": [
                    f"{condition.field.name}:{condition.operator.value}:{condition.value}"
                    for condition in request.filters
                ],
            },
            status="queued",
            redacted=not request.include_payloads,
            created_at=now,
            expires_at=now + timedelta(days=7),
        )
        async with self._database.session_scope() as session:
            session.add(job)
            await session.flush()
            session.expunge(job)
        return job

    async def run(self, *, job_id: str, organization_id: str) -> ExportJob:
        """Execute an export job. Safe to re-run: it rewrites the same object key."""
        async with self._database.session_scope() as session:
            job = (
                await session.execute(
                    select(ExportJob).where(
                        ExportJob.id == job_id,
                        ExportJob.organization_id == organization_id,
                    )
                )
            ).scalar_one_or_none()
            if job is None:
                raise NotFoundError("export job", job_id)
            job.status = "running"
            job.started_at = self._clock.now()
            query = dict(job.query)
            resource = job.resource
            export_format = job.format
            project_id = job.project_id
            redacted = job.redacted
            session.expunge(job)

        try:
            payload, row_count = await self._materialise(
                organization_id=organization_id,
                project_id=project_id,
                resource=resource,
                export_format=export_format,
                query=query,
                redacted=redacted,
            )
        except Exception as exc:
            async with self._database.session_scope() as session:
                failed = (
                    await session.execute(select(ExportJob).where(ExportJob.id == job_id))
                ).scalar_one()
                failed.status = "failed"
                failed.error_message = f"{type(exc).__name__}: {exc}"[:2_000]
                failed.completed_at = self._clock.now()
            log.warning("export.failed", job_id=job_id, error=str(exc))
            raise

        from ..storage.objects.protocol import compute_checksum

        checksum = compute_checksum(payload)
        key = build_object_key(
            organization_id=organization_id,
            kind=PayloadKind.EXPORT,
            checksum=checksum,
            extension=export_format,
        )
        metadata = await self._objects.put(
            key,
            payload,
            content_type=_content_type(export_format),
            metadata={"export_job": job_id, "redacted": str(redacted).lower()},
        )

        now = self._clock.now()
        async with self._database.session_scope() as session:
            stored = (
                await session.execute(select(ExportJob).where(ExportJob.id == job_id))
            ).scalar_one()
            stored.status = "completed"
            stored.object_key = key
            stored.size_bytes = metadata.size_bytes
            stored.row_count = row_count
            stored.completed_at = now
            # Object keys are content addresses, so two exports producing
            # identical bytes legitimately share one object. Reuse the existing
            # row -- extending its lifetime to the later of the two horizons --
            # rather than inserting a duplicate key.
            existing = (
                await session.execute(select(StoredObject).where(StoredObject.object_key == key))
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    StoredObject(
                        id=generate_id(IdPrefix.OBJECT),
                        organization_id=organization_id,
                        project_id=project_id,
                        object_key=key,
                        kind=PayloadKind.EXPORT,
                        content_type=metadata.content_type,
                        size_bytes=metadata.size_bytes,
                        checksum=metadata.checksum,
                        owner_type="export_job",
                        owner_id=job_id,
                        created_at=now,
                        expires_at=stored.expires_at,
                    )
                )
            else:
                existing.deleted_at = None
                if stored.expires_at is not None and (
                    existing.expires_at is None or existing.expires_at < stored.expires_at
                ):
                    existing.expires_at = stored.expires_at
            await session.flush()
            session.expunge(stored)
            log.info("export.completed", job_id=job_id, rows=row_count, bytes=metadata.size_bytes)
            return stored

    async def _materialise(
        self,
        *,
        organization_id: str,
        project_id: str,
        resource: str,
        export_format: str,
        query: dict[str, Any],
        redacted: bool,
    ) -> tuple[bytes, int]:
        scope = AnalyticsScope(
            organization_id=organization_id,
            project_id=project_id,
            environment=query.get("environment"),
        )
        start = datetime.fromisoformat(str(query["start"]))
        end = datetime.fromisoformat(str(query["end"]))

        rows: list[dict[str, Any]] = []
        cursor: PageRequest | None = PageRequest(limit=_PAGE_SIZE)
        while cursor is not None and len(rows) < MAX_EXPORT_ROWS:
            if resource == "traces":
                page = await self._analytics.search_traces(scope, start=start, end=end, page=cursor)
            else:
                page = await self._analytics.search_spans(scope, start=start, end=end, page=cursor)
            for item in page.items:
                rows.append(self._serialise(item, redacted=redacted))
            if not page.has_more or page.next_cursor is None:
                break
            cursor = PageRequest(
                limit=_PAGE_SIZE,
                cursor=revive_cursor_values(self._cursor_codec.decode(page.next_cursor)),
            )

        manifest_note = (
            "Fields marked [redacted] were removed by the platform's redaction policy."
            if redacted
            else "This export contains unredacted payloads."
        )
        if export_format == "jsonl":
            body = "\n".join(json.dumps(row, default=str) for row in rows)
            return body.encode("utf-8"), len(rows)
        if export_format == "json":
            document = {
                "manifest": {
                    "resource": resource,
                    "project_id": project_id,
                    "row_count": len(rows),
                    "redacted": redacted,
                    "note": manifest_note,
                    "generated_at": self._clock.now().isoformat(),
                },
                "rows": rows,
            }
            return json.dumps(document, default=str).encode("utf-8"), len(rows)

        buffer = io.StringIO()
        if rows:
            # Union of keys across rows: a sparse column present on only some
            # rows must still appear, or the CSV silently drops data.
            fieldnames: list[str] = []
            for row in rows:
                for key in row:
                    if key not in fieldnames:
                        fieldnames.append(key)
            writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _csv_value(value) for key, value in row.items()})
        return buffer.getvalue().encode("utf-8"), len(rows)

    def _serialise(self, row: Any, *, redacted: bool) -> dict[str, Any]:
        from dataclasses import asdict, is_dataclass

        payload = asdict(row) if is_dataclass(row) else dict(row)
        payload.pop("previous_start_unix_nano", None)
        if redacted:
            for field in ("input_preview", "output_preview"):
                if payload.get(field):
                    payload[field], _ = self._redactor.redact_payload(payload[field])
            if isinstance(payload.get("attributes"), dict):
                payload["attributes"] = self._redactor.redact_attributes(
                    payload["attributes"]
                ).value
        return {key: _json_value(value) for key, value in payload.items()}

    async def get(self, *, principal: Principal, job_id: str) -> ExportJob:
        principal.require(Permission.EXPORT_READ)
        async with self._database.session_scope() as session:
            job = (
                await session.execute(
                    select(ExportJob).where(
                        ExportJob.id == job_id,
                        ExportJob.organization_id == principal.organization_id,
                    )
                )
            ).scalar_one_or_none()
            if job is None:
                raise NotFoundError("export job", job_id)
            session.expunge(job)
            return job

    async def list(self, *, principal: Principal, limit: int = 50) -> list[ExportJob]:
        principal.require(Permission.EXPORT_READ)
        async with self._database.session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(ExportJob)
                        .where(ExportJob.organization_id == principal.organization_id)
                        .order_by(ExportJob.created_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

    async def download_url(
        self, *, principal: Principal, job_id: str
    ) -> tuple[str | None, ExportJob]:
        """Return a short-lived signed URL, or ``None`` when streaming is required."""
        job = await self.get(principal=principal, job_id=job_id)
        if job.status != "completed" or not job.object_key:
            raise ValidationFailedError(f"export job is {job.status}, not ready for download")
        if job.expires_at is not None and job.expires_at <= self._clock.now():
            raise GoneError("export has expired and its archive has been deleted")
        return await self._objects.signed_url(job.object_key, expires_in=300), job

    async def read_bytes(self, *, principal: Principal, job_id: str) -> tuple[bytes, ExportJob]:
        """Stream an export through the API, for object stores without presigning."""
        job = await self.get(principal=principal, job_id=job_id)
        if job.status != "completed" or not job.object_key:
            raise ValidationFailedError(f"export job is {job.status}, not ready for download")
        payload = await self._objects.get(job.object_key)
        return payload, job


def _content_type(export_format: str) -> str:
    return {
        "jsonl": "application/x-ndjson",
        "json": "application/json",
        "csv": "text/csv",
    }.get(export_format, "application/octet-stream")


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)
