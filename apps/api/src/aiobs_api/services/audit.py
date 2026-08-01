"""Audit logging.

Audit events are written in the *same transaction* as the change they describe.
That is the difference between an audit log and a best-effort log: if the change
commits, the record exists; if the record cannot be written, the change does not
happen. A fire-and-forget audit write would silently lose exactly the events an
investigator needs -- the ones during an incident.

Denied attempts are recorded too. "Who tried and failed" is usually more
interesting than "who succeeded".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aiobs_schemas.ids import IdPrefix, generate_id

from ..core.context import get_context
from ..core.logging import get_logger
from ..core.query import FilterCondition, FilterOperator, Page, PageRequest
from ..core.timeutil import Clock
from ..domain.principal import Principal
from ..domain.redaction import Redactor
from ..storage.postgres.models import AuditEvent
from ..storage.postgres.session import Database

__all__ = ["AuditAction", "AuditService"]

log = get_logger(__name__)


class AuditAction:
    """Dotted action names. Kept as constants so queries and alerts are stable."""

    LOGIN_SUCCEEDED = "auth.login.succeeded"
    LOGIN_FAILED = "auth.login.failed"
    LOGOUT = "auth.logout"
    TOKEN_REFRESHED = "auth.token.refreshed"
    PASSWORD_CHANGED = "auth.password.changed"

    API_KEY_CREATED = "api_key.created"
    API_KEY_REVOKED = "api_key.revoked"
    SERVICE_ACCOUNT_CREATED = "service_account.created"
    SERVICE_ACCOUNT_REVOKED = "service_account.revoked"

    MEMBER_INVITED = "member.invited"
    MEMBER_ROLE_CHANGED = "member.role_changed"
    MEMBER_REMOVED = "member.removed"

    PROJECT_CREATED = "project.created"
    PROJECT_UPDATED = "project.updated"
    PROJECT_DELETED = "project.deleted"
    ENVIRONMENT_CREATED = "environment.created"

    PROMPT_CREATED = "prompt.created"
    PROMPT_VERSION_PUBLISHED = "prompt_version.published"
    PROMPT_ALIAS_PROMOTED = "prompt_alias.promoted"

    MODEL_VERSION_CREATED = "model_version.created"

    DATASET_CREATED = "dataset.created"
    DATASET_VERSION_CREATED = "dataset_version.created"
    DATASET_DELETED = "dataset.deleted"
    DATASET_SAMPLES_VIEWED = "dataset.samples_viewed"

    PRICE_BOOK_CREATED = "price_book.created"
    PRICE_BOOK_UPDATED = "price_book.updated"
    PRICE_ENTRY_CREATED = "price_entry.created"

    RETENTION_POLICY_CHANGED = "retention_policy.changed"
    EXPORT_REQUESTED = "export.requested"
    EXPORT_DOWNLOADED = "export.downloaded"

    PERMISSION_DENIED = "authz.denied"
    TENANT_MISMATCH = "authz.tenant_mismatch"
    RATE_LIMITED = "authz.rate_limited"

    DEAD_LETTER_REPLAYED = "operations.dead_letter_replayed"
    CONFIG_CHANGED = "operations.config_changed"


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """An audit event ready to write."""

    action: str
    resource_type: str
    resource_id: str | None = None
    project_id: str | None = None
    outcome: str = "success"
    metadata: dict[str, Any] | None = None


class AuditService:
    """Writes and reads audit events."""

    def __init__(
        self, *, database: Database, clock: Clock, redactor: Redactor | None = None
    ) -> None:
        self._database = database
        self._clock = clock
        self._redactor = redactor or Redactor()

    def build(
        self,
        *,
        principal: Principal | None,
        record: AuditRecord,
        organization_id: str | None = None,
    ) -> AuditEvent:
        """Construct the ORM object without persisting it.

        Callers add it to their own session so the audit record shares the
        transaction of the change it describes.
        """
        context = get_context()
        metadata = self._redactor.redact_attributes(record.metadata or {}).value
        return AuditEvent(
            id=generate_id(IdPrefix.AUDIT_EVENT),
            organization_id=organization_id
            or (principal.organization_id if principal else "")
            or "",
            occurred_at=self._clock.now(),
            action=record.action,
            actor_id=principal.id if principal else None,
            actor_type=principal.type.value if principal else "anonymous",
            actor_label=principal.label if principal else None,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            project_id=record.project_id,
            outcome=record.outcome,
            request_id=context.request_id if context else None,
            client_ip=context.client_ip if context else None,
            user_agent=(context.user_agent or "")[:512] if context else None,
            metadata_json=dict(metadata),
        )

    async def record(
        self,
        *,
        principal: Principal | None,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        project_id: str | None = None,
        outcome: str = "success",
        metadata: dict[str, Any] | None = None,
        organization_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        """Write an audit event, joining ``session`` when one is supplied."""
        event = self.build(
            principal=principal,
            organization_id=organization_id,
            record=AuditRecord(
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                project_id=project_id,
                outcome=outcome,
                metadata=metadata,
            ),
        )
        if not event.organization_id:
            # A failed login has no organisation yet. It is still worth
            # recording, so it goes to the structured log rather than being
            # dropped -- audit_events is tenant-scoped by design.
            log.info(
                "audit.untenanted",
                action=action,
                outcome=outcome,
                resource_type=resource_type,
            )
            return
        if session is not None:
            session.add(event)
            return
        async with self._database.session_scope() as own_session:
            own_session.add(event)

    async def search(
        self,
        *,
        organization_id: str,
        filters: Sequence[FilterCondition] = (),
        page: PageRequest | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Page[AuditEvent]:
        """List audit events for one organisation, newest first."""
        request = page or PageRequest()
        async with self._database.session_scope() as session:
            statement = select(AuditEvent).where(AuditEvent.organization_id == organization_id)
            if start is not None:
                statement = statement.where(AuditEvent.occurred_at >= start)
            if end is not None:
                statement = statement.where(AuditEvent.occurred_at < end)
            for condition in filters:
                statement = _apply_filter(statement, condition)
            if request.cursor:
                cursor_time = request.cursor.get("occurred_at")
                cursor_id = request.cursor.get("id")
                if cursor_time is not None and cursor_id is not None:
                    statement = statement.where(
                        (AuditEvent.occurred_at < cursor_time)
                        | ((AuditEvent.occurred_at == cursor_time) & (AuditEvent.id < cursor_id))
                    )
            statement = statement.order_by(
                AuditEvent.occurred_at.desc(), AuditEvent.id.desc()
            ).limit(request.limit + 1)
            rows = list((await session.execute(statement)).scalars().all())

        has_more = len(rows) > request.limit
        items = rows[: request.limit]
        return Page(items=items, has_more=has_more)


_AUDIT_COLUMNS = {
    "action": AuditEvent.action,
    "actor_id": AuditEvent.actor_id,
    "resource_type": AuditEvent.resource_type,
    "resource_id": AuditEvent.resource_id,
    "project_id": AuditEvent.project_id,
    "outcome": AuditEvent.outcome,
}


def _apply_filter(statement: Any, condition: FilterCondition) -> Any:
    column = _AUDIT_COLUMNS.get(condition.field.name)
    if column is None:
        return statement
    if condition.operator is FilterOperator.EQ:
        return statement.where(column == condition.value)
    if condition.operator is FilterOperator.NE:
        return statement.where(column != condition.value)
    if condition.operator is FilterOperator.IN:
        return statement.where(column.in_(condition.value))
    if condition.operator is FilterOperator.STARTS_WITH:
        return statement.where(column.like(f"{condition.value}%"))
    if condition.operator is FilterOperator.CONTAINS:
        return statement.where(column.like(f"%{condition.value}%"))
    return statement
