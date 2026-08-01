"""Authentication endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from aiobs_schemas.errors import ErrorCode

from ...core.errors import AuthenticationError, ValidationFailedError
from ...core.logging import get_logger
from ...domain.principal import Principal
from ...services.audit import AuditAction
from ..deps import PrincipalDep, ServicesDep, get_principal
from ..schemas import (
    LoginRequest,
    MembershipOut,
    OrganizationOut,
    RefreshRequest,
    TokenResponse,
    UserOut,
)

__all__ = ["router"]

log = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Sign in with email and password",
    responses={401: {"description": "Invalid credentials or locked account"}},
)
async def login(payload: LoginRequest, services: ServicesDep, request: Request) -> TokenResponse:
    """Exchange credentials for an organisation-scoped token pair.

    A user belonging to several organisations must name one: a token is always
    scoped to a single tenant, so there is no correct default.
    """
    try:
        user = await services.auth.authenticate_user(payload.email, payload.password)
    except AuthenticationError:
        await services.audit.record(
            principal=None,
            action=AuditAction.LOGIN_FAILED,
            resource_type="user",
            resource_id=payload.email,
            outcome="denied",
        )
        raise

    memberships = await services.organizations.list_for_user(user.id)
    if not memberships:
        raise AuthenticationError(
            "this account is not a member of any organization",
            code=ErrorCode.PERMISSION_DENIED,
        )

    if payload.organization_id:
        match = next((item for item in memberships if item[0].id == payload.organization_id), None)
        if match is None:
            raise AuthenticationError(
                "not a member of the requested organization",
                code=ErrorCode.PERMISSION_DENIED,
            )
        organization, role = match
    elif len(memberships) == 1:
        organization, role = memberships[0]
    else:
        raise ValidationFailedError(
            "this account belongs to multiple organizations; "
            f"specify organization_id (one of {[item[0].id for item in memberships]})"
        )

    tokens = services.auth.issue_tokens(user=user, organization_id=organization.id)
    await services.audit.record(
        principal=Principal.for_user(
            user_id=user.id,
            email=user.email,
            organization_id=organization.id,
            role=role,
        ),
        action=AuditAction.LOGIN_SUCCEEDED,
        resource_type="user",
        resource_id=user.id,
        organization_id=organization.id,
    )
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        organization_id=organization.id,
        role=role,
    )


@router.post("/refresh", response_model=TokenResponse, summary="Refresh an access token")
async def refresh(payload: RefreshRequest, services: ServicesDep) -> TokenResponse:
    """Exchange a refresh token for a new pair.

    The refresh token's epoch is re-validated against the user record, so a
    password change or forced sign-out invalidates it even though it has not
    expired.
    """
    claims = services.auth.decode_token(payload.refresh_token, expected_type="refresh")
    user = await _load_user(services, str(claims["sub"]))
    organization_id = str(claims.get("org") or "")
    if int(claims.get("epoch", -1)) != user.token_epoch or not user.is_active:
        raise AuthenticationError(
            "refresh token has been invalidated; sign in again",
            code=ErrorCode.TOKEN_EXPIRED,
        )
    memberships = await services.organizations.list_for_user(user.id)
    match = next((item for item in memberships if item[0].id == organization_id), None)
    if match is None:
        raise AuthenticationError(
            "membership in the token's organization has been revoked",
            code=ErrorCode.PERMISSION_DENIED,
        )
    tokens = services.auth.issue_tokens(user=user, organization_id=organization_id)
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        organization_id=organization_id,
        role=match[1],
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Invalidate every token for the current user",
)
async def logout(principal: PrincipalDep, services: ServicesDep) -> Response:
    """Bump the user's token epoch, invalidating all outstanding tokens."""
    await services.auth.bump_token_epoch(principal.id)
    await services.audit.record(
        principal=principal,
        action=AuditAction.LOGOUT,
        resource_type="user",
        resource_id=principal.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=MembershipOut, summary="Describe the current principal")
async def me(principal: PrincipalDep, services: ServicesDep) -> MembershipOut:
    user = await _load_user(services, principal.id)
    return MembershipOut(
        user=UserOut(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            is_active=user.is_active,
            last_login_at=user.last_login_at,
        ),
        role=principal.role.value if principal.role else "viewer",
        project_scope=sorted(principal.project_scope),
        created_at=user.created_at,
    )


@router.get(
    "/organizations",
    response_model=list[OrganizationOut],
    summary="Organizations the current user belongs to",
)
async def my_organizations(
    principal: Annotated[Principal, Depends(get_principal)], services: ServicesDep
) -> list[OrganizationOut]:
    memberships = await services.organizations.list_for_user(principal.id)
    return [
        OrganizationOut(
            id=organization.id,
            slug=organization.slug,
            name=organization.name,
            role=role,
            max_spans_per_day=organization.max_spans_per_day,
            max_projects=organization.max_projects,
            created_at=organization.created_at,
        )
        for organization, role in memberships
    ]


async def _load_user(services: ServicesDep, user_id: str):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from ...core.errors import NotFoundError
    from ...storage.postgres.models import User

    async with services.container.database.session_scope() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is None:
            raise NotFoundError("user", user_id)
        session.expunge(user)
        return user
