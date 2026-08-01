"""The authenticated caller.

One immutable object represents every kind of caller -- an interactive user, an
SDK API key, a CI service account -- so that authorisation code has exactly one
shape to reason about. The differences between them are data (which permissions,
which projects, which environment), not control flow.

The two methods that matter are :meth:`Principal.require` and
:meth:`Principal.require_project`. Together they enforce the platform's central
invariant: *no caller may touch data outside the organisation it authenticated
into, and within it, only what its role permits.*
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..core.errors import PermissionDeniedError, TenantMismatchError
from .rbac import Permission, Role, permissions_for, permissions_for_scopes

__all__ = ["Principal", "PrincipalType"]


class PrincipalType(str, Enum):
    USER = "user"
    API_KEY = "api_key"
    SERVICE_ACCOUNT = "service_account"


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller and everything authorisation needs to know."""

    id: str
    type: PrincipalType
    organization_id: str
    #: Display label for audit records: an email, a key name, an account name.
    label: str
    role: Role | None = None
    #: Effective permission set, precomputed at authentication time.
    permissions: frozenset[Permission] = field(default_factory=frozenset)
    #: Empty means "every project in the organisation". Non-empty restricts the
    #: principal to exactly these project ids.
    project_scope: frozenset[str] = field(default_factory=frozenset)
    #: API keys are bound to a single project and environment.
    project_id: str | None = None
    environment_id: str | None = None
    environment_name: str | None = None
    authenticated_at: datetime | None = None
    #: Set for API keys and service accounts so expiry can be surfaced.
    expires_at: datetime | None = None

    # ------------------------------------------------------------------
    # constructors
    # ------------------------------------------------------------------

    @classmethod
    def for_user(
        cls,
        *,
        user_id: str,
        email: str,
        organization_id: str,
        role: Role | str,
        project_scope: Iterable[str] = (),
        authenticated_at: datetime | None = None,
    ) -> Principal:
        resolved = role if isinstance(role, Role) else Role(role)
        return cls(
            id=user_id,
            type=PrincipalType.USER,
            organization_id=organization_id,
            label=email,
            role=resolved,
            permissions=permissions_for(resolved),
            project_scope=frozenset(project_scope),
            authenticated_at=authenticated_at,
        )

    @classmethod
    def for_api_key(
        cls,
        *,
        key_id: str,
        name: str,
        organization_id: str,
        project_id: str,
        environment_id: str,
        environment_name: str,
        scopes: Iterable[str],
        expires_at: datetime | None = None,
        authenticated_at: datetime | None = None,
    ) -> Principal:
        return cls(
            id=key_id,
            type=PrincipalType.API_KEY,
            organization_id=organization_id,
            label=name,
            role=None,
            permissions=permissions_for_scopes(scopes),
            # An API key is *always* pinned to one project. This is the reason a
            # leaked staging ingest key cannot write into production.
            project_scope=frozenset({project_id}),
            project_id=project_id,
            environment_id=environment_id,
            environment_name=environment_name,
            expires_at=expires_at,
            authenticated_at=authenticated_at,
        )

    @classmethod
    def for_service_account(
        cls,
        *,
        account_id: str,
        name: str,
        organization_id: str,
        role: Role | str,
        project_scope: Iterable[str] = (),
        expires_at: datetime | None = None,
        authenticated_at: datetime | None = None,
    ) -> Principal:
        resolved = role if isinstance(role, Role) else Role(role)
        return cls(
            id=account_id,
            type=PrincipalType.SERVICE_ACCOUNT,
            organization_id=organization_id,
            label=name,
            role=resolved,
            permissions=permissions_for(resolved),
            project_scope=frozenset(project_scope),
            expires_at=expires_at,
            authenticated_at=authenticated_at,
        )

    # ------------------------------------------------------------------
    # authorisation
    # ------------------------------------------------------------------

    def can(self, permission: Permission) -> bool:
        """Whether this principal holds ``permission``."""
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        """Raise :class:`PermissionDeniedError` unless ``permission`` is held."""
        if permission not in self.permissions:
            raise PermissionDeniedError(permission.value)

    def require_any(self, *permissions: Permission) -> None:
        """Raise unless at least one of ``permissions`` is held."""
        if not any(permission in self.permissions for permission in permissions):
            raise PermissionDeniedError(" or ".join(p.value for p in permissions))

    def require_organization(self, organization_id: str) -> None:
        """Raise :class:`TenantMismatchError` on a cross-tenant access attempt.

        Distinct from a permission failure on purpose: this is either a serious
        application bug or an attack, and it is logged and alerted differently.
        """
        if organization_id != self.organization_id:
            raise TenantMismatchError(
                "resource belongs to a different organization",
                context={
                    "principal_organization_id": self.organization_id,
                    "requested_organization_id": organization_id,
                },
            )

    def require_project(self, project_id: str) -> None:
        """Raise unless this principal may act within ``project_id``.

        An empty ``project_scope`` means organisation-wide access; a non-empty
        one is an allowlist. API keys always have a single-project scope.
        """
        if self.project_scope and project_id not in self.project_scope:
            raise PermissionDeniedError(
                "project:access",
                resource=project_id,
                context={"project_scope": sorted(self.project_scope)},
            )

    def require_environment(self, environment_id: str) -> None:
        """Raise unless this principal may act within ``environment_id``."""
        if self.environment_id is not None and environment_id != self.environment_id:
            raise PermissionDeniedError("environment:access", resource=environment_id)

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now

    # ------------------------------------------------------------------
    # observability
    # ------------------------------------------------------------------

    def as_log_fields(self) -> dict[str, str]:
        fields = {
            "principal_id": self.id,
            "principal_type": self.type.value,
            "organization_id": self.organization_id,
        }
        if self.role is not None:
            fields["role"] = self.role.value
        if self.project_id is not None:
            fields["project_id"] = self.project_id
        return fields

    def __repr__(self) -> str:
        # Deliberately omits the label, which may be an email address.
        return (
            f"Principal(type={self.type.value}, id={self.id!r}, "
            f"org={self.organization_id!r}, role={self.role.value if self.role else None!r})"
        )
