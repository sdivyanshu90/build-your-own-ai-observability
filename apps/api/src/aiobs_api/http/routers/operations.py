"""Exports, audit log, retention policy and dead-letter operations."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import select

from ...core.errors import NotFoundError
from ...core.logging import get_logger
from ...domain.rbac import Permission
from ...services.audit import AuditAction, AuditRecord
from ...services.exports import ExportRequest
from ...storage.bus.protocol import Topics
from ...storage.postgres.models import RetentionPolicy
from ..deps import PageDep, PrincipalDep, ServicesDep, TimeRangeDep
from ..schemas import (
    AuditEventOut,
    CursorPage,
    ExportCreate,
    ExportOut,
    RetentionPolicyIn,
    RetentionPolicyOut,
)

__all__ = ["router"]

log = get_logger(__name__)

#: Strong references to in-flight background tasks. See the comment at the one
#: call site: without this, the event loop is free to collect a running task.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

router = APIRouter(tags=["operations"])


def _export_out(job, download_url: str | None = None) -> ExportOut:  # type: ignore[no-untyped-def]
    return ExportOut(
        id=job.id,
        project_id=job.project_id,
        resource=job.resource,
        format=job.format,
        status=job.status,
        redacted=job.redacted,
        row_count=job.row_count,
        size_bytes=job.size_bytes,
        error_message=job.error_message,
        created_at=job.created_at,
        completed_at=job.completed_at,
        expires_at=job.expires_at,
        download_url=download_url,
    )


@router.post(
    "/exports",
    response_model=ExportOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request an export",
    description=(
        "Exports run asynchronously. Poll the job until `status` is "
        "`completed`, then fetch it from `/exports/{id}/download`. Requesting "
        "unredacted payloads requires the `trace:read_payloads` permission and "
        "is recorded in the audit log."
    ),
)
async def create_export(
    payload: ExportCreate,
    principal: PrincipalDep,
    services: ServicesDep,
    time_range: TimeRangeDep,
) -> ExportOut:
    job = await services.exports.create(
        principal=principal,
        request=ExportRequest(
            project_id=payload.project_id,
            resource=payload.resource,
            start=time_range.start,
            end=time_range.end,
            format=payload.format,
            environment=payload.environment,
            include_payloads=payload.include_payloads,
        ),
    )
    await services.audit.record(
        principal=principal,
        action=AuditAction.EXPORT_REQUESTED,
        resource_type="export_job",
        resource_id=job.id,
        project_id=payload.project_id,
        metadata={
            "resource": payload.resource,
            "format": payload.format,
            "include_payloads": payload.include_payloads,
        },
    )
    # Small exports finish in-process so the common case does not need a worker;
    # the job row is the source of truth either way, so a failure here simply
    # leaves the job queued for the worker to pick up.
    #
    # A reference is kept until the task completes: asyncio holds only a weak
    # reference to a running task, so a fire-and-forget `create_task` can be
    # garbage-collected mid-flight and the export silently never runs.
    task = asyncio.create_task(_run_export_safely(services, job.id, principal.organization_id))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return _export_out(job)


async def _run_export_safely(services, job_id: str, organization_id: str) -> None:  # type: ignore[no-untyped-def]
    try:
        await services.exports.run(job_id=job_id, organization_id=organization_id)
    except Exception as exc:
        log.warning("export.background_failed", job_id=job_id, error=str(exc))


@router.get("/exports", response_model=list[ExportOut], summary="List exports")
async def list_exports(principal: PrincipalDep, services: ServicesDep) -> list[ExportOut]:
    jobs = await services.exports.list(principal=principal)
    return [_export_out(job) for job in jobs]


@router.get("/exports/{job_id}", response_model=ExportOut, summary="Get an export job")
async def get_export(job_id: str, principal: PrincipalDep, services: ServicesDep) -> ExportOut:
    job = await services.exports.get(principal=principal, job_id=job_id)
    return _export_out(job)


@router.get(
    "/exports/{job_id}/download",
    summary="Download a completed export",
    description=(
        "Redirects to a short-lived signed URL where the object store supports "
        "one, otherwise streams the archive through the API."
    ),
)
async def download_export(job_id: str, principal: PrincipalDep, services: ServicesDep) -> Response:
    url, job = await services.exports.download_url(principal=principal, job_id=job_id)
    await services.audit.record(
        principal=principal,
        action=AuditAction.EXPORT_DOWNLOADED,
        resource_type="export_job",
        resource_id=job_id,
        project_id=job.project_id,
    )
    if url:
        return Response(status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": url})
    payload, job = await services.exports.read_bytes(principal=principal, job_id=job_id)
    media_type = {
        "jsonl": "application/x-ndjson",
        "json": "application/json",
        "csv": "text/csv",
    }[job.format]
    return Response(
        content=payload,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{job.resource}-{job.id}.{job.format}"'
        },
    )


@router.get(
    "/audit-events",
    response_model=CursorPage[AuditEventOut],
    summary="Search the audit log",
)
async def list_audit_events(
    principal: PrincipalDep,
    services: ServicesDep,
    page: PageDep,
    time_range: TimeRangeDep,
) -> CursorPage[AuditEventOut]:
    principal.require(Permission.AUDIT_READ)
    result = await services.audit.search(
        organization_id=principal.organization_id,
        page=page,
        start=time_range.start,
        end=time_range.end,
    )
    return CursorPage[AuditEventOut](
        items=[
            AuditEventOut(
                id=event.id,
                occurred_at=event.occurred_at,
                action=event.action,
                actor_id=event.actor_id,
                actor_type=event.actor_type,
                actor_label=event.actor_label,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                project_id=event.project_id,
                outcome=event.outcome,
                request_id=event.request_id,
                client_ip=event.client_ip,
                metadata=dict(event.metadata_json or {}),
            )
            for event in result.items
        ],
        has_more=result.has_more,
    )


@router.get(
    "/projects/{project_id}/retention",
    response_model=list[RetentionPolicyOut],
    summary="Get a project's retention policies",
)
async def get_retention(
    project_id: str, principal: PrincipalDep, services: ServicesDep
) -> list[RetentionPolicyOut]:
    principal.require(Permission.RETENTION_READ)
    principal.require_project(project_id)
    async with services.container.database.session_scope() as session:
        policies = list(
            (
                await session.execute(
                    select(RetentionPolicy).where(
                        RetentionPolicy.project_id == project_id,
                        RetentionPolicy.organization_id == principal.organization_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    return [
        RetentionPolicyOut(
            id=policy.id,
            project_id=policy.project_id,
            environment_id=policy.environment_id,
            raw_span_days=policy.raw_span_days,
            aggregate_days=policy.aggregate_days,
            payload_days=policy.payload_days,
            purge_on_expiry=policy.purge_on_expiry,
            updated_at=policy.updated_at,
        )
        for policy in policies
    ]


@router.put(
    "/projects/{project_id}/retention",
    response_model=RetentionPolicyOut,
    summary="Set a project's retention policy",
    description=(
        "Shortening retention deletes data on the next sweep and cannot be "
        "undone. The change is audited with both the old and new horizons."
    ),
)
async def set_retention(
    project_id: str,
    payload: RetentionPolicyIn,
    principal: PrincipalDep,
    services: ServicesDep,
) -> RetentionPolicyOut:
    principal.require(Permission.RETENTION_WRITE)
    principal.require_project(project_id)
    from aiobs_schemas.ids import IdPrefix, generate_id

    async with services.container.database.session_scope() as session:
        policy = (
            await session.execute(
                select(RetentionPolicy).where(
                    RetentionPolicy.project_id == project_id,
                    RetentionPolicy.environment_id == payload.environment_id,
                    RetentionPolicy.organization_id == principal.organization_id,
                )
            )
        ).scalar_one_or_none()
        before = (
            {
                "raw_span_days": policy.raw_span_days,
                "aggregate_days": policy.aggregate_days,
                "payload_days": policy.payload_days,
            }
            if policy
            else None
        )
        if policy is None:
            policy = RetentionPolicy(
                id=generate_id(IdPrefix.RETENTION_POLICY),
                organization_id=principal.organization_id,
                project_id=project_id,
                environment_id=payload.environment_id,
            )
            session.add(policy)
        policy.raw_span_days = payload.raw_span_days
        policy.aggregate_days = payload.aggregate_days
        policy.payload_days = payload.payload_days
        policy.purge_on_expiry = payload.purge_on_expiry
        policy.updated_by = principal.id
        # The audit record joins this transaction: if the policy change rolls
        # back, so does its audit entry.
        session.add(
            services.audit.build(
                principal=principal,
                record=AuditRecord(
                    action=AuditAction.RETENTION_POLICY_CHANGED,
                    resource_type="retention_policy",
                    resource_id=policy.id,
                    project_id=project_id,
                    metadata={"before": before, "after": payload.model_dump(mode="json")},
                ),
            )
        )
        await session.flush()
        session.expunge(policy)

    return RetentionPolicyOut(
        id=policy.id,
        project_id=policy.project_id,
        environment_id=policy.environment_id,
        raw_span_days=policy.raw_span_days,
        aggregate_days=policy.aggregate_days,
        payload_days=policy.payload_days,
        purge_on_expiry=policy.purge_on_expiry,
        updated_at=policy.updated_at,
    )


@router.get(
    "/operations/queue",
    summary="Ingestion queue depth",
    description="Consumer lag per topic. The primary 'is ingestion keeping up' signal.",
)
async def queue_status(principal: PrincipalDep, services: ServicesDep) -> dict[str, object]:
    principal.require(Permission.OPERATIONS_ADMIN)
    bus = services.container.bus
    group = services.container.settings.bus.consumer_group
    return {
        "consumer_group": group,
        "lag": {topic: await bus.consumer_lag(topic, group=group) for topic in Topics.ALL},
    }


@router.post(
    "/operations/dead-letters/{topic}/replay",
    summary="Replay dead-lettered messages",
    description=(
        "Re-publishes parked messages onto their original topic after the "
        "handler bug has been fixed. Handlers are idempotent, so replaying a "
        "message that partially succeeded is safe."
    ),
)
async def replay_dead_letters(
    topic: str,
    principal: PrincipalDep,
    services: ServicesDep,
    limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
) -> dict[str, object]:
    principal.require(Permission.OPERATIONS_ADMIN)
    if topic not in Topics.ALL:
        raise NotFoundError("topic", topic)
    group = services.container.settings.bus.consumer_group
    replayed = await services.container.bus.replay_dead_letters(topic, group=group, limit=limit)
    await services.audit.record(
        principal=principal,
        action=AuditAction.DEAD_LETTER_REPLAYED,
        resource_type="topic",
        resource_id=topic,
        metadata={"replayed": replayed},
    )
    return {"topic": topic, "replayed": replayed}
