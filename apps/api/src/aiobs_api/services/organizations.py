"""Organisation, project, environment and membership management."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select

from aiobs_schemas.ids import IdPrefix, generate_id

from ..core.errors import ConflictError, NotFoundError, ValidationFailedError
from ..core.logging import get_logger
from ..core.timeutil import Clock
from ..domain.principal import Principal
from ..domain.rbac import Permission, Role
from ..storage.postgres.models import (
    Environment,
    Membership,
    Organization,
    Project,
    RetentionPolicy,
    User,
)
from ..storage.postgres.session import Database

__all__ = ["OrganizationService", "ProjectSummary"]

log = get_logger(__name__)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
#: Created with every project. Production is separate because it is an
#: authorisation boundary, not a label.
_DEFAULT_ENVIRONMENTS = (("development", False), ("staging", False), ("production", True))


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:64] or "project"


@dataclass(slots=True)
class ProjectSummary:
    project: Project
    environments: list[Environment]


class OrganizationService:
    """Tenant lifecycle and membership."""

    def __init__(self, *, database: Database, clock: Clock) -> None:
        self._database = database
        self._clock = clock

    # ------------------------------------------------------------------
    # organisations
    # ------------------------------------------------------------------

    async def create_organization(
        self, *, name: str, slug: str, owner_user_id: str
    ) -> Organization:
        """Create a tenant and make ``owner_user_id`` its owner.

        The owner membership is created in the same transaction: an
        organisation with no owner would be unadministrable and undeletable.
        """
        if not _SLUG_RE.match(slug):
            raise ValidationFailedError(
                "slug must be 3-64 lowercase alphanumeric characters or hyphens, "
                "not starting or ending with a hyphen"
            )
        async with self._database.session_scope() as session:
            existing = (
                await session.execute(select(Organization).where(Organization.slug == slug))
            ).scalar_one_or_none()
            if existing is not None:
                raise ConflictError(f"organization slug {slug!r} is already taken")
            organization = Organization(id=generate_id(IdPrefix.ORGANIZATION), slug=slug, name=name)
            session.add(organization)
            await session.flush()
            session.add(
                Membership(
                    id=generate_id(IdPrefix.MEMBERSHIP),
                    organization_id=organization.id,
                    user_id=owner_user_id,
                    role=Role.OWNER.value,
                )
            )
            await session.flush()
            session.expunge(organization)
            return organization

    async def find_by_slug(self, slug: str) -> Organization | None:
        """Look up an organisation by slug, ignoring soft-deleted ones.

        Used by bootstrap so re-running it against an existing installation
        joins the existing tenant instead of failing on the slug conflict.
        """
        async with self._database.session_scope() as session:
            organization = (
                await session.execute(
                    select(Organization).where(
                        Organization.slug == slug, Organization.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
            if organization is not None:
                session.expunge(organization)
            return organization

    async def ensure_owner(self, *, organization_id: str, user_id: str) -> None:
        """Make ``user_id`` an owner of ``organization_id`` if they are not a member.

        Deliberately not exposed over the API -- granting yourself ownership is
        exactly the privilege escalation :meth:`add_member` exists to prevent.
        This is an administrative bootstrap path, reachable only from the CLI by
        someone who already has database access.
        """
        async with self._database.session_scope() as session:
            existing = (
                await session.execute(
                    select(Membership).where(
                        Membership.organization_id == organization_id,
                        Membership.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return
            session.add(
                Membership(
                    id=generate_id(IdPrefix.MEMBERSHIP),
                    organization_id=organization_id,
                    user_id=user_id,
                    role=Role.OWNER.value,
                )
            )
            await session.flush()

    async def list_for_user(self, user_id: str) -> list[tuple[Organization, str]]:
        """Organisations a user belongs to, with their role in each."""
        async with self._database.session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(Organization, Membership.role)
                        .join(Membership, Membership.organization_id == Organization.id)
                        .where(
                            Membership.user_id == user_id,
                            Organization.deleted_at.is_(None),
                        )
                        .order_by(Organization.name)
                    )
                )
                .tuples()
                .all()
            )
            for organization, _ in rows:
                session.expunge(organization)
            return [(organization, role) for organization, role in rows]

    # ------------------------------------------------------------------
    # projects
    # ------------------------------------------------------------------

    async def create_project(
        self,
        *,
        principal: Principal,
        name: str,
        slug: str | None = None,
        description: str | None = None,
    ) -> ProjectSummary:
        principal.require(Permission.PROJECT_CREATE)
        resolved_slug = slug or slugify(name)
        if not _SLUG_RE.match(resolved_slug):
            raise ValidationFailedError("project slug is not a valid identifier")

        async with self._database.session_scope() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(Project)
                    .where(
                        Project.organization_id == principal.organization_id,
                        Project.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            organization = (
                await session.execute(
                    select(Organization).where(Organization.id == principal.organization_id)
                )
            ).scalar_one_or_none()
            if organization is None:
                raise NotFoundError("organization", principal.organization_id)
            if count >= organization.max_projects:
                raise ConflictError(
                    f"project limit of {organization.max_projects} reached for this organization"
                )

            existing = (
                await session.execute(
                    select(Project).where(
                        Project.organization_id == principal.organization_id,
                        Project.slug == resolved_slug,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ConflictError(f"project slug {resolved_slug!r} already exists")

            project = Project(
                id=generate_id(IdPrefix.PROJECT),
                organization_id=principal.organization_id,
                slug=resolved_slug,
                name=name,
                description=description,
            )
            session.add(project)
            await session.flush()

            environments: list[Environment] = []
            for environment_name, is_production in _DEFAULT_ENVIRONMENTS:
                environment = Environment(
                    id=generate_id(IdPrefix.ENVIRONMENT),
                    organization_id=principal.organization_id,
                    project_id=project.id,
                    name=environment_name,
                    is_production=is_production,
                    # Production defaults are privacy-conscious: payload capture
                    # off, strict redaction on. Opting in is a deliberate act.
                    settings=(
                        {"store_payloads": False, "redaction_mode": "strict"}
                        if is_production
                        else {"store_payloads": True, "redaction_mode": "standard"}
                    ),
                )
                session.add(environment)
                environments.append(environment)

            session.add(
                RetentionPolicy(
                    id=generate_id(IdPrefix.RETENTION_POLICY),
                    organization_id=principal.organization_id,
                    project_id=project.id,
                    environment_id=None,
                )
            )
            await session.flush()
            session.expunge(project)
            for environment in environments:
                session.expunge(environment)
            return ProjectSummary(project=project, environments=environments)

    async def list_projects(self, principal: Principal) -> list[ProjectSummary]:
        principal.require(Permission.PROJECT_READ)
        async with self._database.session_scope() as session:
            projects = list(
                (
                    await session.execute(
                        select(Project)
                        .where(
                            Project.organization_id == principal.organization_id,
                            Project.deleted_at.is_(None),
                        )
                        .order_by(Project.name)
                    )
                )
                .scalars()
                .all()
            )
            # Project-scoped principals (API keys, scoped memberships) see only
            # their own projects, filtered here rather than in the caller.
            if principal.project_scope:
                projects = [
                    project for project in projects if project.id in principal.project_scope
                ]
            environments = list(
                (
                    await session.execute(
                        select(Environment).where(
                            Environment.organization_id == principal.organization_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_project: dict[str, list[Environment]] = {}
            for environment in environments:
                by_project.setdefault(environment.project_id, []).append(environment)
                session.expunge(environment)
            for project in projects:
                session.expunge(project)
            return [
                ProjectSummary(project=project, environments=by_project.get(project.id, []))
                for project in projects
            ]

    async def get_project(self, principal: Principal, project_id: str) -> ProjectSummary:
        principal.require(Permission.PROJECT_READ)
        principal.require_project(project_id)
        async with self._database.session_scope() as session:
            project = (
                await session.execute(
                    select(Project).where(
                        Project.id == project_id,
                        Project.organization_id == principal.organization_id,
                        Project.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if project is None:
                raise NotFoundError("project", project_id)
            environments = list(
                (
                    await session.execute(
                        select(Environment)
                        .where(Environment.project_id == project_id)
                        .order_by(Environment.name)
                    )
                )
                .scalars()
                .all()
            )
            session.expunge(project)
            for environment in environments:
                session.expunge(environment)
            return ProjectSummary(project=project, environments=environments)

    async def resolve_environment(
        self, *, organization_id: str, project_id: str, name: str
    ) -> Environment:
        async with self._database.session_scope() as session:
            environment = (
                await session.execute(
                    select(Environment).where(
                        Environment.organization_id == organization_id,
                        Environment.project_id == project_id,
                        Environment.name == name,
                    )
                )
            ).scalar_one_or_none()
            if environment is None:
                raise NotFoundError("environment", name)
            session.expunge(environment)
            return environment

    async def delete_project(self, *, principal: Principal, project_id: str) -> None:
        """Soft-delete a project. Telemetry is purged asynchronously."""
        principal.require(Permission.PROJECT_DELETE)
        principal.require_project(project_id)
        async with self._database.session_scope() as session:
            project = (
                await session.execute(
                    select(Project).where(
                        Project.id == project_id,
                        Project.organization_id == principal.organization_id,
                    )
                )
            ).scalar_one_or_none()
            if project is None:
                raise NotFoundError("project", project_id)
            project.deleted_at = self._clock.now()

    # ------------------------------------------------------------------
    # membership
    # ------------------------------------------------------------------

    async def list_members(self, principal: Principal) -> list[tuple[User, Membership]]:
        principal.require(Permission.MEMBER_READ)
        async with self._database.session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(User, Membership)
                        .join(Membership, Membership.user_id == User.id)
                        .where(Membership.organization_id == principal.organization_id)
                        .order_by(User.email)
                    )
                )
                .tuples()
                .all()
            )
            for user, membership in rows:
                session.expunge(user)
                session.expunge(membership)
            return [(user, membership) for user, membership in rows]

    async def add_member(
        self,
        *,
        principal: Principal,
        user_id: str,
        role: Role,
        project_scope: Sequence[str] = (),
    ) -> Membership:
        principal.require(Permission.MEMBER_INVITE)
        self._guard_privilege_escalation(principal, role)
        async with self._database.session_scope() as session:
            existing = (
                await session.execute(
                    select(Membership).where(
                        Membership.organization_id == principal.organization_id,
                        Membership.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ConflictError("user is already a member of this organization")
            membership = Membership(
                id=generate_id(IdPrefix.MEMBERSHIP),
                organization_id=principal.organization_id,
                user_id=user_id,
                role=role.value,
                project_scope=list(project_scope),
                invited_by=principal.id,
            )
            session.add(membership)
            await session.flush()
            session.expunge(membership)
            return membership

    async def change_role(self, *, principal: Principal, user_id: str, role: Role) -> Membership:
        principal.require(Permission.MEMBER_UPDATE_ROLE)
        self._guard_privilege_escalation(principal, role)
        async with self._database.session_scope() as session:
            membership = (
                await session.execute(
                    select(Membership).where(
                        Membership.organization_id == principal.organization_id,
                        Membership.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if membership is None:
                raise NotFoundError("membership", user_id)
            if membership.role == Role.OWNER.value and role is not Role.OWNER:
                await self._require_another_owner(session, principal.organization_id, user_id)
            membership.role = role.value
            await session.flush()
            session.expunge(membership)
            return membership

    async def remove_member(self, *, principal: Principal, user_id: str) -> None:
        principal.require(Permission.MEMBER_REMOVE)
        async with self._database.session_scope() as session:
            membership = (
                await session.execute(
                    select(Membership).where(
                        Membership.organization_id == principal.organization_id,
                        Membership.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if membership is None:
                raise NotFoundError("membership", user_id)
            if membership.role == Role.OWNER.value:
                await self._require_another_owner(session, principal.organization_id, user_id)
            await session.delete(membership)

    def _guard_privilege_escalation(self, principal: Principal, role: Role) -> None:
        """Refuse to grant a role above the granter's own.

        An administrator inviting an owner would be a privilege escalation with
        extra steps; requiring an owner to create an owner keeps the ceiling
        meaningful.
        """
        if role is Role.OWNER and principal.role is not Role.OWNER:
            raise ValidationFailedError("only an owner may grant the owner role")

    async def _require_another_owner(
        self, session: object, organization_id: str, excluding_user_id: str
    ) -> None:
        """Refuse to remove or demote the last owner."""
        remaining = (
            await session.execute(  # type: ignore[attr-defined]
                select(func.count())
                .select_from(Membership)
                .where(
                    Membership.organization_id == organization_id,
                    Membership.role == Role.OWNER.value,
                    Membership.user_id != excluding_user_id,
                )
            )
        ).scalar_one()
        if int(remaining) == 0:
            raise ConflictError(
                "an organization must retain at least one owner; "
                "promote another member before removing this one"
            )
