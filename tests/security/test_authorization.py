"""Authorisation and tenant isolation, exercised through the HTTP API.

Unit tests prove the RBAC matrix is correct; these prove the API actually
consults it. The distinction matters: a permission model nobody calls is
decoration.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aiobs_api.domain.principal import Principal
from aiobs_api.domain.rbac import Role
from aiobs_api.services.bundle import ServiceBundle


def window() -> dict[str, str]:
    now = datetime.now(timezone.utc)
    return {
        "start": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "end": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    }


async def token_for(services: ServiceBundle, tenant: dict[str, str], role: Role) -> str:
    """Issue a token for a user holding ``role`` in the test organisation."""
    from sqlalchemy import select

    from aiobs_api.storage.postgres.models import Membership, User
    from aiobs_schemas.ids import IdPrefix, generate_id

    user_id = generate_id(IdPrefix.USER)
    async with services.container.database.session_scope() as session:
        session.add(
            User(
                id=user_id,
                email=f"{role.value}@test.invalid",
                display_name=role.value,
                password_hash=services.auth.hash_password("correct-horse-battery"),
            )
        )
        await session.flush()
        session.add(
            Membership(
                id=generate_id(IdPrefix.MEMBERSHIP),
                organization_id=tenant["organization_id"],
                user_id=user_id,
                role=role.value,
            )
        )

    async with services.container.database.session_scope() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        session.expunge(user)
    return services.auth.issue_tokens(
        user=user, organization_id=tenant["organization_id"]
    ).access_token


class TestUnauthenticated:
    @pytest.mark.parametrize(
        "path",
        [
            "/v1/projects",
            "/v1/traces",
            "/v1/prompts",
            "/v1/models",
            "/v1/price-books",
            "/v1/audit-events",
            "/v1/api-keys",
        ],
    )
    async def test_every_data_endpoint_requires_authentication(self, client, path: str) -> None:
        response = await client.get(path)
        assert response.status_code == 401
        assert response.json()["code"] in {"unauthenticated", "invalid_credentials"}

    async def test_health_endpoints_are_public(self, client) -> None:
        assert (await client.get("/live")).status_code == 204
        assert (await client.get("/health")).status_code in {200, 503}

    async def test_a_forged_token_is_rejected(self, client) -> None:
        import jwt

        forged = jwt.encode(
            {"sub": "usr_x", "org": "org_x", "typ": "access", "exp": 9_999_999_999},
            "not-the-real-secret-but-long-enough-for-hmac-0001",
            algorithm="HS256",
        )
        response = await client.get("/v1/projects", headers={"Authorization": f"Bearer {forged}"})
        assert response.status_code == 401

    async def test_an_expired_token_is_rejected(
        self, client, services: ServiceBundle, tenant: dict[str, str]
    ) -> None:
        import jwt

        settings = services.container.settings.auth
        expired = jwt.encode(
            {
                "sub": tenant["user_id"],
                "org": tenant["organization_id"],
                "typ": "access",
                "epoch": 0,
                "iss": settings.jwt_issuer,
                "aud": settings.jwt_audience,
                "iat": 1_700_000_000,
                "exp": 1_700_000_001,
            },
            settings.jwt_secret.get_secret_value(),
            algorithm="HS256",
        )
        response = await client.get("/v1/projects", headers={"Authorization": f"Bearer {expired}"})
        assert response.status_code == 401
        assert response.json()["code"] == "token_expired"


class TestRoleEnforcement:
    async def test_viewer_cannot_create_a_project(
        self, client, services: ServiceBundle, tenant: dict[str, str]
    ) -> None:
        token = await token_for(services, tenant, Role.VIEWER)
        response = await client.post(
            "/v1/projects",
            json={"name": "sneaky"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "permission_denied"

    async def test_viewer_cannot_create_an_api_key(
        self, client, services: ServiceBundle, tenant: dict[str, str]
    ) -> None:
        token = await token_for(services, tenant, Role.VIEWER)
        response = await client.post(
            "/v1/api-keys",
            json={
                "name": "sneaky",
                "project_id": tenant["project_id"],
                "environment_id": tenant["environment_id"],
                "scopes": ["ingest"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    async def test_developer_cannot_write_a_price_book(
        self, client, services: ServiceBundle, tenant: dict[str, str]
    ) -> None:
        token = await token_for(services, tenant, Role.DEVELOPER)
        response = await client.post(
            "/v1/price-books",
            json={"version": "v9", "name": "sneaky", "entries": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    async def test_developer_cannot_read_the_audit_log(
        self, client, services: ServiceBundle, tenant: dict[str, str]
    ) -> None:
        token = await token_for(services, tenant, Role.DEVELOPER)
        response = await client.get(
            "/v1/audit-events", params=window(), headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 403

    async def test_analyst_can_read_costs(
        self, client, services: ServiceBundle, tenant: dict[str, str]
    ) -> None:
        token = await token_for(services, tenant, Role.ANALYST)
        response = await client.get(
            "/v1/costs",
            params={"project_id": tenant["project_id"], **window()},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200

    async def test_viewer_can_read_traces(
        self, client, services: ServiceBundle, tenant: dict[str, str]
    ) -> None:
        token = await token_for(services, tenant, Role.VIEWER)
        response = await client.get(
            "/v1/traces",
            params={"project_id": tenant["project_id"], **window()},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200

    async def test_a_role_change_invalidates_outstanding_tokens(
        self, authenticated_client, services: ServiceBundle, tenant: dict[str, str]
    ) -> None:
        """A demoted user must not keep their old permissions until expiry."""
        token = await token_for(services, tenant, Role.ADMINISTRATOR)
        headers = {"Authorization": f"Bearer {token}"}
        assert (await authenticated_client.get("/v1/members", headers=headers)).status_code == 200

        # The owner demotes them.
        target = await _user_id_for(services, "administrator@test.invalid")
        response = await authenticated_client.patch(
            f"/v1/members/{target}/role", params={"role": "viewer"}
        )
        assert response.status_code == 200

        # The old token is dead immediately.
        after = await authenticated_client.get("/v1/members", headers=headers)
        assert after.status_code == 401


async def _user_id_for(services: ServiceBundle, email: str) -> str:
    from sqlalchemy import select

    from aiobs_api.storage.postgres.models import User

    async with services.container.database.session_scope() as session:
        return (await session.execute(select(User.id).where(User.email == email))).scalar_one()


class TestTenantIsolation:
    async def test_a_token_cannot_reach_another_organizations_project(
        self, client, services: ServiceBundle, tenant: dict[str, str]
    ) -> None:
        """Create a second tenant and confirm its data is invisible."""
        from aiobs_api.storage.postgres.models import User
        from aiobs_schemas.ids import IdPrefix, generate_id

        other_user = generate_id(IdPrefix.USER)
        async with services.container.database.session_scope() as session:
            session.add(
                User(
                    id=other_user,
                    email="other@evil.invalid",
                    display_name="Other",
                    password_hash=services.auth.hash_password("correct-horse-battery"),
                )
            )
        other_org = await services.organizations.create_organization(
            name="Other Org", slug="other-org", owner_user_id=other_user
        )
        other_principal = Principal.for_user(
            user_id=other_user,
            email="other@evil.invalid",
            organization_id=other_org.id,
            role=Role.OWNER,
        )
        await services.organizations.create_project(principal=other_principal, name="other-project")

        from sqlalchemy import select

        from aiobs_api.storage.postgres.models import User as UserModel

        async with services.container.database.session_scope() as session:
            user = (
                await session.execute(select(UserModel).where(UserModel.id == other_user))
            ).scalar_one()
            session.expunge(user)
        token = services.auth.issue_tokens(user=user, organization_id=other_org.id).access_token
        headers = {"Authorization": f"Bearer {token}"}

        # The other tenant sees only its own project.
        projects = (await client.get("/v1/projects", headers=headers)).json()
        assert [item["slug"] for item in projects] == ["other-project"]

        # And cannot read the first tenant's project by id.
        response = await client.get(f"/v1/projects/{tenant['project_id']}", headers=headers)
        assert response.status_code == 404
        # 404 rather than 403: telling an attacker the id exists is an oracle.
        assert response.json()["code"] == "not_found"

    async def test_a_token_for_one_organization_is_not_valid_for_another(
        self, services: ServiceBundle, tenant: dict[str, str]
    ) -> None:
        from sqlalchemy import select

        from aiobs_api.storage.postgres.models import User

        async with services.container.database.session_scope() as session:
            user = (
                await session.execute(select(User).where(User.id == tenant["user_id"]))
            ).scalar_one()
            session.expunge(user)
        token = services.auth.issue_tokens(
            user=user, organization_id="org_NOT_A_MEMBER"
        ).access_token

        from aiobs_api.core.errors import AuthenticationError

        with pytest.raises(AuthenticationError):
            await services.auth.principal_from_token(token)


class TestApiKeyLifecycle:
    async def test_a_revoked_key_stops_working(
        self, services: ServiceBundle, tenant: dict[str, str]
    ) -> None:
        from aiobs_api.core.errors import AuthenticationError

        issued = await services.auth.create_api_key(
            organization_id=tenant["organization_id"],
            project_id=tenant["project_id"],
            environment_id=tenant["environment_id"],
            name="temp",
            scopes=["ingest"],
            created_by=tenant["user_id"],
        )
        assert await services.auth.principal_from_api_key(issued.secret)

        await services.auth.revoke_api_key(
            organization_id=tenant["organization_id"],
            key_id=issued.id,
            revoked_by=tenant["user_id"],
        )
        with pytest.raises(AuthenticationError) as error:
            await services.auth.principal_from_api_key(issued.secret)
        assert error.value.code.value == "api_key_revoked"

    async def test_an_expired_key_stops_working(
        self, services: ServiceBundle, tenant: dict[str, str], clock
    ) -> None:
        from aiobs_api.core.errors import AuthenticationError

        issued = await services.auth.create_api_key(
            organization_id=tenant["organization_id"],
            project_id=tenant["project_id"],
            environment_id=tenant["environment_id"],
            name="short-lived",
            scopes=["ingest"],
            created_by=tenant["user_id"],
            expires_at=clock.now() + timedelta(hours=1),
        )
        assert await services.auth.principal_from_api_key(issued.secret)

        clock.advance(hours=2)
        with pytest.raises(AuthenticationError) as error:
            await services.auth.principal_from_api_key(issued.secret)
        assert error.value.code.value == "api_key_expired"

    async def test_a_tampered_key_is_rejected(
        self, services: ServiceBundle, tenant: dict[str, str]
    ) -> None:
        from aiobs_api.core.errors import AuthenticationError

        issued = await services.auth.create_api_key(
            organization_id=tenant["organization_id"],
            project_id=tenant["project_id"],
            environment_id=tenant["environment_id"],
            name="k",
            scopes=["ingest"],
            created_by=tenant["user_id"],
        )
        # Keep the (public, indexed) prefix, change the secret body.
        tampered = issued.secret[: len(issued.prefix) + 1] + "X" * 40
        with pytest.raises(AuthenticationError):
            await services.auth.principal_from_api_key(tampered)

    async def test_the_plaintext_is_never_stored(
        self, services: ServiceBundle, tenant: dict[str, str]
    ) -> None:
        from sqlalchemy import select

        from aiobs_api.storage.postgres.models import ApiKey

        issued = await services.auth.create_api_key(
            organization_id=tenant["organization_id"],
            project_id=tenant["project_id"],
            environment_id=tenant["environment_id"],
            name="k",
            scopes=["ingest"],
            created_by=tenant["user_id"],
        )
        async with services.container.database.session_scope() as session:
            row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.id))).scalar_one()
            assert issued.secret not in row.secret_hash
            assert row.secret_hash != issued.secret


class TestBruteForce:
    async def test_repeated_failures_lock_the_account(
        self, services: ServiceBundle, tenant: dict[str, str]
    ) -> None:
        from aiobs_api.core.errors import AuthenticationError

        limit = services.container.settings.auth.max_failed_logins
        for _ in range(limit):
            with pytest.raises(AuthenticationError):
                await services.auth.authenticate_user("owner@test.invalid", "wrong-password")

        # Even the correct password is refused while locked out.
        with pytest.raises(AuthenticationError) as error:
            await services.auth.authenticate_user("owner@test.invalid", "correct-horse-battery")
        assert "locked" in error.value.message

    async def test_an_unknown_account_is_indistinguishable_from_a_wrong_password(
        self, services: ServiceBundle, tenant: dict[str, str]
    ) -> None:
        from aiobs_api.core.errors import AuthenticationError

        with pytest.raises(AuthenticationError) as unknown:
            await services.auth.authenticate_user("nobody@test.invalid", "whatever")
        with pytest.raises(AuthenticationError) as wrong:
            await services.auth.authenticate_user("owner@test.invalid", "whatever")
        assert unknown.value.message == wrong.value.message
        assert unknown.value.code == wrong.value.code
