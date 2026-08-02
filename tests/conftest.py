"""Shared test fixtures.

Design rules for this suite:

* **Real dependencies over mocks.** Tests run against a real SQLite metadata
  store, a real analytics store, a real object store and a real event bus. They
  are the development drivers rather than the production ones, but they are
  genuine implementations held to the same contract -- so a test that passes
  here is evidence about behaviour, not about a mock's configuration.
* **Deterministic time.** A :class:`FrozenClock` is injected everywhere, so
  retention, expiry, rate-limit windows and roll-up versioning are tested by
  advancing time rather than by sleeping.
* **Isolation per test.** Every fixture is function-scoped over temporary
  directories, so tests cannot leak state into each other or depend on order.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiobs_api.container import Container
from aiobs_api.core.config import (
    AnalyticsSettings,
    AuthSettings,
    BusSettings,
    DatabaseSettings,
    Environment,
    IngestSettings,
    KeyValueSettings,
    ObjectStoreSettings,
    SecuritySettings,
    Settings,
)
from aiobs_api.core.query import CursorCodec
from aiobs_api.core.timeutil import FrozenClock
from aiobs_api.domain.principal import Principal
from aiobs_api.domain.rbac import Role
from aiobs_api.services.bundle import ServiceBundle, build_services
from aiobs_api.storage.analytics.rows import AnalyticsScope

#: The instant every test's clock starts at.
#:
#: Anchored to the real current time rather than a hard-coded date. The clock is
#: still frozen -- it only moves when a test advances it, so duration and expiry
#: logic stays deterministic -- but it agrees with the database's own ``now()``,
#: which server-side timestamp defaults use. A clock two months in the past
#: makes every freshly-issued token look expired and every ``revoked_at >=
#: created_at`` check fail, neither of which is a real defect.
FIXED_NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def clock() -> FrozenClock:
    """A clock the test controls."""
    return FrozenClock(FIXED_NOW)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointing entirely at a per-test temporary directory."""
    return Settings(
        environment=Environment.TEST,
        service_name="aiobs-test",
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}"),
        analytics=AnalyticsSettings(sqlite_path=tmp_path / "analytics.db"),
        objects=ObjectStoreSettings(root_path=tmp_path / "objects"),
        kv=KeyValueSettings(),
        bus=BusSettings(),
        auth=AuthSettings(
            jwt_secret="test-secret-that-is-long-enough-for-hmac-signing-0001",
            # Argon2 at production cost makes an auth test suite take minutes.
            # The parameters under test are the *policy*, not the cost.
            argon2_time_cost=1,
            argon2_memory_cost_kib=8_192,
            argon2_parallelism=1,
        ),
        security=SecuritySettings(cookie_secure=False),
        ingest=IngestSettings(),
    )


@pytest.fixture
async def container(settings: Settings, clock: FrozenClock) -> AsyncIterator[Container]:
    """A started container over per-test temporary storage."""
    from aiobs_api.container import build_container

    instance = build_container(settings, clock=clock)
    # create_all rather than Alembic: migrations are exercised by their own
    # suite, and running them per test would dominate the runtime.
    await instance.database.create_all()
    await instance.analytics.start()
    await instance.objects.start()
    await instance.kv.start()
    await instance.bus.start()
    try:
        yield instance
    finally:
        await instance.stop()


@pytest.fixture
async def services(container: Container) -> ServiceBundle:
    return build_services(container)


@pytest.fixture
def cursor_codec() -> CursorCodec:
    return CursorCodec("test-cursor-secret")


# ---------------------------------------------------------------------------
# tenancy fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def tenant(services: ServiceBundle) -> dict[str, str]:
    """Create an organisation, owner, project and environments.

    Returns the identifiers most tests need, so individual tests do not each
    reimplement bootstrapping.
    """
    from aiobs_api.storage.postgres.models import User
    from aiobs_schemas.ids import IdPrefix, generate_id

    user_id = generate_id(IdPrefix.USER)
    async with services.container.database.session_scope() as session:
        session.add(
            User(
                id=user_id,
                email="owner@test.invalid",
                display_name="Test Owner",
                password_hash=services.auth.hash_password("correct-horse-battery"),
            )
        )

    organization = await services.organizations.create_organization(
        name="Test Organization", slug="test-org", owner_user_id=user_id
    )
    principal = Principal.for_user(
        user_id=user_id,
        email="owner@test.invalid",
        organization_id=organization.id,
        role=Role.OWNER,
    )
    summary = await services.organizations.create_project(principal=principal, name="test-project")
    development = next(item for item in summary.environments if item.name == "development")
    production = next(item for item in summary.environments if item.name == "production")

    return {
        "user_id": user_id,
        "organization_id": organization.id,
        "project_id": summary.project.id,
        "environment_id": development.id,
        "environment_name": development.name,
        "production_environment_id": production.id,
    }


@pytest.fixture
def owner(tenant: dict[str, str]) -> Principal:
    return Principal.for_user(
        user_id=tenant["user_id"],
        email="owner@test.invalid",
        organization_id=tenant["organization_id"],
        role=Role.OWNER,
    )


@pytest.fixture
def viewer(tenant: dict[str, str]) -> Principal:
    """A read-only principal, for authorisation tests."""
    return Principal.for_user(
        user_id="usr_viewer",
        email="viewer@test.invalid",
        organization_id=tenant["organization_id"],
        role=Role.VIEWER,
    )


@pytest.fixture
def other_tenant_principal() -> Principal:
    """An owner of a *different* organisation, for isolation tests."""
    return Principal.for_user(
        user_id="usr_intruder",
        email="intruder@evil.invalid",
        organization_id="org_SOMEONEELSE",
        role=Role.OWNER,
    )


@pytest.fixture
async def ingest_principal(services: ServiceBundle, tenant: dict[str, str]) -> Principal:
    """An API-key principal scoped to the test project and environment."""
    issued = await services.auth.create_api_key(
        organization_id=tenant["organization_id"],
        project_id=tenant["project_id"],
        environment_id=tenant["environment_id"],
        name="test-key",
        scopes=["ingest", "read"],
        created_by=tenant["user_id"],
    )
    return Principal.for_api_key(
        key_id=issued.id,
        name=issued.name,
        organization_id=tenant["organization_id"],
        project_id=tenant["project_id"],
        environment_id=tenant["environment_id"],
        environment_name=tenant["environment_name"],
        scopes=["ingest", "read"],
    )


@pytest.fixture
def scope(tenant: dict[str, str]) -> AnalyticsScope:
    return AnalyticsScope(
        organization_id=tenant["organization_id"],
        project_id=tenant["project_id"],
        environment=tenant["environment_name"],
    )


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------


@pytest.fixture
async def price_book(services: ServiceBundle, clock: FrozenClock) -> str:
    """A price book with round numbers, so expected costs are computable by hand.

    ``mock-model-v1`` costs $1 per million input tokens and $2 per million
    output tokens: 1,000 input + 500 output is exactly $0.002.
    """
    from datetime import timedelta
    from decimal import Decimal

    from aiobs_api.services.pricing import PriceBookInput, PriceEntryInput

    effective = clock.now() - timedelta(days=30)
    entries = tuple(
        PriceEntryInput(
            provider=provider,
            model_identifier=model,
            usage_category=category,
            unit_price=Decimal(price),
            unit_quantity=1_000_000,
            effective_from=effective,
        )
        for provider, model, category, price in (
            ("mock", "mock-model-v1", "input_tokens", "1.00"),
            ("mock", "mock-model-v1", "output_tokens", "2.00"),
            ("mock", "mock-model-v1", "cached_input_tokens", "0.10"),
            ("mock", "mock-embedding-v1", "input_tokens", "0.10"),
        )
    )
    return await services.pricing.create_price_book(
        PriceBookInput(
            version="test-v1",
            name="Test price book",
            organization_id=None,
            entries=entries,
        ),
        created_by="usr_test",
    )


# ---------------------------------------------------------------------------
# HTTP application
# ---------------------------------------------------------------------------


@pytest.fixture
async def app(settings: Settings, container: Container):  # type: ignore[no-untyped-def]
    """A FastAPI app wired to the test container.

    The container is already started by its own fixture, so the app's lifespan
    is bypassed; tests that need the lifespan use ``asgi_client`` instead.
    """
    from aiobs_api.http.app import create_app

    application = create_app(settings, container=container)
    application.state.container = container
    application.state.services = build_services(container)
    return application


@pytest.fixture
async def client(app):  # type: ignore[no-untyped-def]
    """An httpx client bound to the app in-process (no network, no port)."""
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test.invalid") as instance:
        yield instance


@pytest.fixture
async def authenticated_client(client, services: ServiceBundle, tenant: dict[str, str]):  # type: ignore[no-untyped-def]
    """A client carrying a valid owner bearer token."""
    from sqlalchemy import select

    from aiobs_api.storage.postgres.models import User

    async with services.container.database.session_scope() as session:
        user = (
            await session.execute(select(User).where(User.id == tenant["user_id"]))
        ).scalar_one()
        session.expunge(user)
    tokens = services.auth.issue_tokens(user=user, organization_id=tenant["organization_id"])
    client.headers["Authorization"] = f"Bearer {tokens.access_token}"
    return client


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def span_factory(tenant: dict[str, str], clock: FrozenClock):  # type: ignore[no-untyped-def]
    """Build :class:`SpanRow` objects with sensible defaults."""
    from aiobs_api.storage.analytics.rows import SpanRow
    from aiobs_schemas.ids import generate_span_id, generate_trace_id

    def make(
        *,
        trace_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        name: str = "span",
        category: str = "chat_completion",
        offset_ns: int = 0,
        duration_ns: int = 5_000_000,
        status: str = "ok",
        **overrides: object,
    ) -> SpanRow:
        start = int(clock.now().timestamp() * 1e9) + offset_ns
        return SpanRow(
            organization_id=tenant["organization_id"],
            project_id=tenant["project_id"],
            environment=tenant["environment_name"],
            trace_id=trace_id or generate_trace_id(),
            span_id=span_id or generate_span_id(),
            parent_span_id=parent_span_id,
            name=name,
            kind="client",
            category=category,
            start_unix_nano=start,
            end_unix_nano=start + duration_ns,
            duration_ns=duration_ns,
            status=status,
            ingested_at=clock.now(),
            ingest_version=1,
            **overrides,  # type: ignore[arg-type]
        )

    return make
