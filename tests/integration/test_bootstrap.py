"""Bootstrap and tenant provisioning.

``make dev-local`` runs ``aiobs-admin bootstrap`` on every start, and the Docker
target runs it after every ``make dev``. Anything that is not idempotent there
turns a routine restart into a failure, so re-running is the property under
test.
"""

from __future__ import annotations

import pytest

from aiobs_api.core.errors import ConflictError
from aiobs_api.domain.rbac import Role
from aiobs_api.services.bundle import ServiceBundle
from aiobs_schemas.ids import IdPrefix, generate_id


async def _make_user(services: ServiceBundle, email: str) -> str:
    """Create a user directly, the way bootstrap does."""
    from aiobs_api.storage.postgres.models import User

    user_id = generate_id(IdPrefix.USER)
    async with services.container.database.session_scope() as session:
        session.add(
            User(
                id=user_id,
                email=email,
                display_name=email.split("@")[0],
                password_hash=services.auth.hash_password("bootstrap-password"),
            )
        )
    return user_id


class TestOrganizationProvisioning:
    async def test_a_slug_can_only_be_claimed_once(self, services: ServiceBundle) -> None:
        first = await _make_user(services, "first@example.test")
        second = await _make_user(services, "second@example.test")

        await services.organizations.create_organization(
            name="Acme", slug="acme-co", owner_user_id=first
        )
        with pytest.raises(ConflictError):
            await services.organizations.create_organization(
                name="Acme Again", slug="acme-co", owner_user_id=second
            )

    async def test_an_existing_organization_can_be_found_by_slug(
        self, services: ServiceBundle
    ) -> None:
        owner = await _make_user(services, "owner@example.test")
        created = await services.organizations.create_organization(
            name="Acme", slug="acme-lookup", owner_user_id=owner
        )

        found = await services.organizations.find_by_slug("acme-lookup")
        assert found is not None and found.id == created.id
        assert await services.organizations.find_by_slug("no-such-slug") is None

    async def test_a_second_administrator_joins_the_existing_tenant(
        self, services: ServiceBundle
    ) -> None:
        """The exact shape of a re-run of bootstrap with a different email.

        Before this existed, the second run failed with "organization slug
        'demo' is already taken" and left the new user with no tenant at all.
        """
        first = await _make_user(services, "admin-one@example.test")
        organization = await services.organizations.create_organization(
            name="Demo", slug="demo-rerun", owner_user_id=first
        )

        second = await _make_user(services, "admin-two@example.test")
        assert await services.organizations.list_for_user(second) == []

        await services.organizations.ensure_owner(organization_id=organization.id, user_id=second)

        memberships = await services.organizations.list_for_user(second)
        assert [(item.id, role) for item, role in memberships] == [
            (organization.id, Role.OWNER.value)
        ]

    async def test_ensuring_ownership_twice_does_not_duplicate_the_membership(
        self, services: ServiceBundle
    ) -> None:
        owner = await _make_user(services, "idempotent@example.test")
        organization = await services.organizations.create_organization(
            name="Demo", slug="demo-idempotent", owner_user_id=owner
        )

        await services.organizations.ensure_owner(organization_id=organization.id, user_id=owner)
        await services.organizations.ensure_owner(organization_id=organization.id, user_id=owner)

        memberships = await services.organizations.list_for_user(owner)
        assert len(memberships) == 1

    async def test_an_existing_role_is_not_silently_escalated(
        self, services: ServiceBundle
    ) -> None:
        """`ensure_owner` adds a missing membership; it does not promote one.

        Turning an existing viewer into an owner because a CLI command was
        re-run would be a privilege escalation hidden inside a routine restart.
        """
        owner = await _make_user(services, "the-owner@example.test")
        organization = await services.organizations.create_organization(
            name="Demo", slug="demo-no-escalation", owner_user_id=owner
        )

        viewer = await _make_user(services, "the-viewer@example.test")
        from aiobs_api.domain.principal import Principal

        principal = Principal.for_user(
            user_id=owner,
            email="the-owner@example.test",
            organization_id=organization.id,
            role=Role.OWNER,
        )
        await services.organizations.add_member(
            principal=principal, user_id=viewer, role=Role.VIEWER
        )

        await services.organizations.ensure_owner(organization_id=organization.id, user_id=viewer)

        memberships = await services.organizations.list_for_user(viewer)
        assert [role for _, role in memberships] == [Role.VIEWER.value]
