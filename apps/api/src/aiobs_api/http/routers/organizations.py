"""Organisation, project, environment, membership and API-key endpoints."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Response, status
from sqlalchemy import select

from ...core.errors import NotFoundError, ValidationFailedError
from ...domain.rbac import Permission, Role
from ...services.audit import AuditAction
from ...storage.postgres.models import ApiKey
from ..deps import PrincipalDep, ServicesDep
from ..schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    EnvironmentOut,
    MembershipOut,
    OrganizationOut,
    ProjectCreate,
    ProjectOut,
    UserOut,
)

__all__ = ["router"]

router = APIRouter(tags=["organizations"])


def _project_out(summary) -> ProjectOut:  # type: ignore[no-untyped-def]
    return ProjectOut(
        id=summary.project.id,
        slug=summary.project.slug,
        name=summary.project.name,
        description=summary.project.description,
        default_sampling_rate=summary.project.default_sampling_rate,
        environments=[
            EnvironmentOut(
                id=environment.id,
                name=environment.name,
                is_production=environment.is_production,
                settings=dict(environment.settings or {}),
            )
            for environment in summary.environments
        ],
        created_at=summary.project.created_at,
    )


# ---------------------------------------------------------------------------
# organisation
# ---------------------------------------------------------------------------


@router.get(
    "/organizations/current",
    response_model=OrganizationOut,
    summary="Describe the organization the current credential is scoped to",
)
async def current_organization(principal: PrincipalDep, services: ServicesDep) -> OrganizationOut:
    principal.require(Permission.ORG_READ)
    from ...storage.postgres.models import Organization

    async with services.container.database.session_scope() as session:
        organization = (
            await session.execute(
                select(Organization).where(Organization.id == principal.organization_id)
            )
        ).scalar_one_or_none()
        if organization is None:
            raise NotFoundError("organization", principal.organization_id)
        return OrganizationOut(
            id=organization.id,
            slug=organization.slug,
            name=organization.name,
            role=principal.role.value if principal.role else None,
            max_spans_per_day=organization.max_spans_per_day,
            max_projects=organization.max_projects,
            created_at=organization.created_at,
        )


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------


@router.get("/projects", response_model=list[ProjectOut], summary="List projects")
async def list_projects(principal: PrincipalDep, services: ServicesDep) -> list[ProjectOut]:
    summaries = await services.organizations.list_projects(principal)
    return [_project_out(summary) for summary in summaries]


@router.post(
    "/projects",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project with its default environments",
)
async def create_project(
    payload: ProjectCreate, principal: PrincipalDep, services: ServicesDep
) -> ProjectOut:
    summary = await services.organizations.create_project(
        principal=principal,
        name=payload.name,
        slug=payload.slug,
        description=payload.description,
    )
    await services.audit.record(
        principal=principal,
        action=AuditAction.PROJECT_CREATED,
        resource_type="project",
        resource_id=summary.project.id,
        project_id=summary.project.id,
        metadata={"name": payload.name},
    )
    return _project_out(summary)


@router.get("/projects/{project_id}", response_model=ProjectOut, summary="Get a project")
async def get_project(
    project_id: str, principal: PrincipalDep, services: ServicesDep
) -> ProjectOut:
    return _project_out(await services.organizations.get_project(principal, project_id))


@router.delete(
    "/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a project",
)
async def delete_project(
    project_id: str, principal: PrincipalDep, services: ServicesDep
) -> Response:
    await services.organizations.delete_project(principal=principal, project_id=project_id)
    await services.audit.record(
        principal=principal,
        action=AuditAction.PROJECT_DELETED,
        resource_type="project",
        resource_id=project_id,
        project_id=project_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/projects/{project_id}/environments",
    response_model=list[EnvironmentOut],
    summary="List a project's environments",
)
async def list_environments(
    project_id: str, principal: PrincipalDep, services: ServicesDep
) -> list[EnvironmentOut]:
    summary = await services.organizations.get_project(principal, project_id)
    return [
        EnvironmentOut(
            id=environment.id,
            name=environment.name,
            is_production=environment.is_production,
            settings=dict(environment.settings or {}),
        )
        for environment in summary.environments
    ]


# ---------------------------------------------------------------------------
# membership
# ---------------------------------------------------------------------------


@router.get("/members", response_model=list[MembershipOut], summary="List members")
async def list_members(principal: PrincipalDep, services: ServicesDep) -> list[MembershipOut]:
    rows = await services.organizations.list_members(principal)
    return [
        MembershipOut(
            user=UserOut(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                is_active=user.is_active,
                last_login_at=user.last_login_at,
            ),
            role=membership.role,
            project_scope=list(membership.project_scope or []),
            created_at=membership.created_at,
        )
        for user, membership in rows
    ]


@router.patch(
    "/members/{user_id}/role",
    response_model=MembershipOut,
    summary="Change a member's role",
)
async def change_role(
    user_id: str, role: str, principal: PrincipalDep, services: ServicesDep
) -> MembershipOut:
    try:
        resolved = Role(role)
    except ValueError as exc:
        raise ValidationFailedError(
            f"unknown role {role!r}; expected one of {[item.value for item in Role]}"
        ) from exc
    membership = await services.organizations.change_role(
        principal=principal, user_id=user_id, role=resolved
    )
    # A role change must invalidate outstanding tokens, otherwise a demoted user
    # keeps their old permissions until their access token expires.
    await services.auth.bump_token_epoch(user_id)
    await services.audit.record(
        principal=principal,
        action=AuditAction.MEMBER_ROLE_CHANGED,
        resource_type="membership",
        resource_id=user_id,
        metadata={"new_role": resolved.value},
    )
    rows = await services.organizations.list_members(principal)
    user = next(user for user, item in rows if item.id == membership.id)
    return MembershipOut(
        user=UserOut(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            is_active=user.is_active,
            last_login_at=user.last_login_at,
        ),
        role=membership.role,
        project_scope=list(membership.project_scope or []),
        created_at=membership.created_at,
    )


@router.delete(
    "/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member",
)
async def remove_member(user_id: str, principal: PrincipalDep, services: ServicesDep) -> Response:
    await services.organizations.remove_member(principal=principal, user_id=user_id)
    await services.auth.bump_token_epoch(user_id)
    await services.audit.record(
        principal=principal,
        action=AuditAction.MEMBER_REMOVED,
        resource_type="membership",
        resource_id=user_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# api keys
# ---------------------------------------------------------------------------


@router.get("/api-keys", response_model=list[ApiKeyOut], tags=["api-keys"], summary="List API keys")
async def list_api_keys(
    principal: PrincipalDep, services: ServicesDep, project_id: str | None = None
) -> list[ApiKeyOut]:
    principal.require(Permission.API_KEY_READ)
    async with services.container.database.session_scope() as session:
        statement = select(ApiKey).where(ApiKey.organization_id == principal.organization_id)
        if project_id:
            principal.require_project(project_id)
            statement = statement.where(ApiKey.project_id == project_id)
        elif principal.project_scope:
            statement = statement.where(ApiKey.project_id.in_(sorted(principal.project_scope)))
        keys = list(
            (await session.execute(statement.order_by(ApiKey.created_at.desc()))).scalars().all()
        )
    return [
        ApiKeyOut(
            id=key.id,
            name=key.name,
            prefix=key.prefix,
            project_id=key.project_id,
            environment_id=key.environment_id,
            scopes=list(key.scopes or []),
            created_at=key.created_at,
            expires_at=key.expires_at,
            revoked_at=key.revoked_at,
            last_used_at=key.last_used_at,
        )
        for key in keys
    ]


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["api-keys"],
    summary="Create an API key",
    description=(
        "The plaintext secret is returned **once**. It is stored only as a keyed "
        "hash and cannot be retrieved afterwards; issue a new key if it is lost."
    ),
)
async def create_api_key(
    payload: ApiKeyCreate, principal: PrincipalDep, services: ServicesDep
) -> ApiKeyCreated:
    principal.require(Permission.API_KEY_CREATE)
    principal.require_project(payload.project_id)
    expires_at = (
        services.container.clock.now() + timedelta(days=payload.expires_in_days)
        if payload.expires_in_days
        else None
    )
    issued = await services.auth.create_api_key(
        organization_id=principal.organization_id,
        project_id=payload.project_id,
        environment_id=payload.environment_id,
        name=payload.name,
        scopes=payload.scopes,
        created_by=principal.id,
        expires_at=expires_at,
    )
    await services.audit.record(
        principal=principal,
        action=AuditAction.API_KEY_CREATED,
        resource_type="api_key",
        resource_id=issued.id,
        project_id=payload.project_id,
        # The prefix is safe to record; the secret never appears anywhere.
        metadata={"prefix": issued.prefix, "scopes": list(issued.scopes)},
    )
    return ApiKeyCreated(
        id=issued.id,
        name=issued.name,
        prefix=issued.prefix,
        secret=issued.secret,
        project_id=issued.project_id,
        environment_id=issued.environment_id,
        scopes=list(issued.scopes),
        created_at=services.container.clock.now(),
        expires_at=issued.expires_at,
    )


@router.delete(
    "/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["api-keys"],
    summary="Revoke an API key",
)
async def revoke_api_key(key_id: str, principal: PrincipalDep, services: ServicesDep) -> Response:
    principal.require(Permission.API_KEY_REVOKE)
    await services.auth.revoke_api_key(
        organization_id=principal.organization_id, key_id=key_id, revoked_by=principal.id
    )
    await services.audit.record(
        principal=principal,
        action=AuditAction.API_KEY_REVOKED,
        resource_type="api_key",
        resource_id=key_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
