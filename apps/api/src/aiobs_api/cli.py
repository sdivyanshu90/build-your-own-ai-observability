"""Administrative CLI: ``aiobs-admin``.

Covers the operations an operator needs before the UI exists: bootstrapping the
first tenant, seeding a price book, generating demo telemetry, and validating a
candidate configuration.

Every command is idempotent. Running ``bootstrap`` twice does not create a
second organisation; it reports the existing one. That matters because these
commands run from container entrypoints and Kubernetes init containers, where a
retry is normal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from aiobs_schemas.ids import IdPrefix, generate_id

from .container import build_container
from .core.config import Settings, get_settings
from .core.logging import configure_logging, get_logger
from .domain.principal import Principal
from .domain.rbac import Role
from .services.bundle import build_services
from .services.pricing import PriceBookInput, PriceEntryInput
from .storage.postgres.models import User

__all__ = ["main"]

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# sample price book
# ---------------------------------------------------------------------------

#: Illustrative list prices for the seeded price book.
#:
#: **These are sample values for local development, not a maintained price
#: feed.** Provider pricing changes without notice and varies by region, tier
#: and contract. Before relying on cost figures, replace this book with one
#: built from your own invoices or the provider's current pricing page -- every
#: entry carries a ``source_url`` pointing at where to check.
#:
#: Prices are per one million tokens.
_SAMPLE_PRICES: tuple[tuple[str, str, str, str, str], ...] = (
    # provider, model, usage category, price per 1M, source
    ("openai", "gpt-4o", "input_tokens", "2.50", "https://openai.com/api/pricing/"),
    ("openai", "gpt-4o", "output_tokens", "10.00", "https://openai.com/api/pricing/"),
    ("openai", "gpt-4o", "cached_input_tokens", "1.25", "https://openai.com/api/pricing/"),
    ("openai", "gpt-4o-mini", "input_tokens", "0.15", "https://openai.com/api/pricing/"),
    ("openai", "gpt-4o-mini", "output_tokens", "0.60", "https://openai.com/api/pricing/"),
    ("openai", "text-embedding-3-small", "input_tokens", "0.02", "https://openai.com/api/pricing/"),
    ("openai", "text-embedding-3-large", "input_tokens", "0.13", "https://openai.com/api/pricing/"),
    ("anthropic", "claude-sonnet-4", "input_tokens", "3.00", "https://www.anthropic.com/pricing"),
    ("anthropic", "claude-sonnet-4", "output_tokens", "15.00", "https://www.anthropic.com/pricing"),
    (
        "anthropic",
        "claude-sonnet-4",
        "cached_input_tokens",
        "0.30",
        "https://www.anthropic.com/pricing",
    ),
    (
        "anthropic",
        "claude-sonnet-4",
        "cache_write_tokens",
        "3.75",
        "https://www.anthropic.com/pricing",
    ),
    ("anthropic", "claude-haiku-4-5", "input_tokens", "1.00", "https://www.anthropic.com/pricing"),
    ("anthropic", "claude-haiku-4-5", "output_tokens", "5.00", "https://www.anthropic.com/pricing"),
    # The deterministic provider used by tests and the offline demos. Round
    # numbers so an expected cost can be worked out by hand in a test assertion.
    ("mock", "mock-model-v1", "input_tokens", "1.00", "deterministic test fixture"),
    ("mock", "mock-model-v1", "output_tokens", "2.00", "deterministic test fixture"),
    ("mock", "mock-embedding-v1", "input_tokens", "0.10", "deterministic test fixture"),
    ("mock", "mock-reranker-v1", "input_tokens", "0.50", "deterministic test fixture"),
)


def _sample_price_entries(effective_from: datetime) -> tuple[PriceEntryInput, ...]:
    return tuple(
        PriceEntryInput(
            provider=provider,
            model_identifier=model,
            usage_category=category,
            unit_price=Decimal(price),
            unit_quantity=1_000_000,
            currency="USD",
            effective_from=effective_from,
            source_url=source,
            notes="Sample price for local development. Verify against the provider.",
        )
        for provider, model, category, price, source in _SAMPLE_PRICES
    )


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


async def _bootstrap(settings: Settings, arguments: argparse.Namespace) -> int:
    """Create the first organisation, user, project and API key."""
    container = build_container(settings)
    await container.start()
    services = build_services(container)
    try:
        async with container.database.session_scope() as session:
            existing = (
                await session.execute(select(User).where(User.email == arguments.email))
            ).scalar_one_or_none()
            user_id = existing.id if existing else None
            if existing is None:
                user = User(
                    id=generate_id(IdPrefix.USER),
                    email=arguments.email.lower().strip(),
                    display_name=arguments.name,
                    password_hash=services.auth.hash_password(arguments.password),
                )
                session.add(user)
                await session.flush()
                user_id = user.id

        assert user_id is not None
        # Three cases, all of which must work on a re-run: the user already
        # belongs to an organisation; the organisation exists but this user is
        # new to it (a second admin being added); neither exists yet.
        organizations = await services.organizations.list_for_user(user_id)
        if organizations:
            organization = organizations[0][0]
            print(f"organization already exists: {organization.slug} ({organization.id})")
        else:
            existing_organization = await services.organizations.find_by_slug(arguments.slug)
            if existing_organization is not None:
                organization = existing_organization
                await services.organizations.ensure_owner(
                    organization_id=organization.id, user_id=user_id
                )
                print(
                    f"joined existing organization {organization.slug} ({organization.id}) as owner"
                )
            else:
                organization = await services.organizations.create_organization(
                    name=arguments.organization, slug=arguments.slug, owner_user_id=user_id
                )
                print(f"created organization {organization.slug} ({organization.id})")

        principal = Principal.for_user(
            user_id=user_id,
            email=arguments.email,
            organization_id=organization.id,
            role=Role.OWNER,
        )

        projects = await services.organizations.list_projects(principal)
        summary = next((item for item in projects if item.project.slug == arguments.project), None)
        if summary is None:
            summary = await services.organizations.create_project(
                principal=principal, name=arguments.project, slug=arguments.project
            )
            print(f"created project {summary.project.slug} ({summary.project.id})")
        else:
            print(f"project already exists: {summary.project.slug} ({summary.project.id})")

        environment = next(
            (item for item in summary.environments if item.name == arguments.environment),
            summary.environments[0],
        )

        issued = await services.auth.create_api_key(
            organization_id=organization.id,
            project_id=summary.project.id,
            environment_id=environment.id,
            name=arguments.key_name,
            scopes=["ingest", "read"],
            created_by=user_id,
        )

        books = await services.pricing.list_books(organization.id)
        if not any(book.version == "sample-v1" for book in books):
            await services.pricing.create_price_book(
                PriceBookInput(
                    version="sample-v1",
                    name="Sample public price book",
                    description=(
                        "Illustrative prices for local development. Replace with "
                        "prices verified against your provider invoices."
                    ),
                    source="aiobs-admin bootstrap",
                    currency="USD",
                    organization_id=None,
                    entries=_sample_price_entries(datetime.now(timezone.utc) - timedelta(days=365)),
                ),
                created_by=user_id,
            )
            print("seeded sample price book 'sample-v1' (verify prices before use)")

        print()
        print("=" * 68)
        print("Bootstrap complete. Store the API key now -- it is not recoverable.")
        print("=" * 68)
        print(f"  organization : {organization.id}")
        print(f"  project      : {summary.project.id}")
        print(f"  environment  : {environment.name} ({environment.id})")
        print(f"  api key      : {issued.secret}")
        print()
        print("  export AIOBS_API_KEY=" + issued.secret)
        print(f"  export AIOBS_ENDPOINT={settings.public_url}")
        print(f"  export AIOBS_PROJECT_ID={summary.project.id}")
        print()
        return 0
    finally:
        await container.stop()


async def _seed_demo(settings: Settings, arguments: argparse.Namespace) -> int:
    """Generate deterministic demo telemetry directly into the analytics store."""
    from .demo_data import generate_demo_data

    container = build_container(settings)
    await container.start()
    services = build_services(container)
    try:
        written = await generate_demo_data(
            services=services,
            project_id=arguments.project_id,
            environment=arguments.environment,
            traces=arguments.traces,
            seed=arguments.seed,
        )
        print(f"seeded {written['traces']} traces / {written['spans']} spans")
        return 0
    finally:
        await container.stop()


async def _check_config(settings: Settings, _: argparse.Namespace) -> int:
    """Validate the configuration for the target environment."""
    problems = settings.validate_for_runtime()
    print(json.dumps(settings.describe(), indent=2, sort_keys=True))
    if problems:
        print("\nconfiguration problems:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("\nconfiguration is valid for this environment")
    return 0


async def _check_dependencies(settings: Settings, _: argparse.Namespace) -> int:
    """Connect to every dependency and report status."""
    container = build_container(settings)
    try:
        await container.start()
        report = await container.health()
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        return 0 if report.healthy else 1
    except Exception as exc:
        print(f"failed to start dependencies: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            await container.stop()
        except Exception:
            pass


async def _migrate_analytics(settings: Settings, _: argparse.Namespace) -> int:
    """Create or update the analytics schema."""
    container = build_container(settings)
    await container.start()
    try:
        await container.analytics.migrate()
        print(f"analytics schema up to date ({settings.analytics.driver.value})")
        return 0
    finally:
        await container.stop()


async def _list_price_books(settings: Settings, _: argparse.Namespace) -> int:
    container = build_container(settings)
    await container.start()
    services = build_services(container)
    try:
        books = await services.pricing.list_books(None)
        for book in books:
            entries = await services.pricing.list_entries(book.id)
            scope = book.organization_id or "public"
            print(f"{book.version:20} {scope:20} {len(entries):4} entries  {book.name}")
        return 0
    finally:
        await container.stop()


_COMMANDS = {
    "bootstrap": _bootstrap,
    "seed-demo": _seed_demo,
    "check-config": _check_config,
    "check-dependencies": _check_dependencies,
    "migrate-analytics": _migrate_analytics,
    "price-books": _list_price_books,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aiobs-admin", description="AI Observability Platform administration"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser(
        "bootstrap", help="Create the first organization, user, project and API key"
    )
    bootstrap.add_argument("--email", default="admin@example.com")
    bootstrap.add_argument("--password", default="change-me-immediately-please")
    bootstrap.add_argument("--name", default="Platform Administrator")
    bootstrap.add_argument("--organization", default="Demo Organization")
    bootstrap.add_argument("--slug", default="demo")
    bootstrap.add_argument("--project", default="demo-project")
    bootstrap.add_argument("--environment", default="development")
    bootstrap.add_argument("--key-name", default="local-development")

    seed = subparsers.add_parser("seed-demo", help="Generate deterministic demo telemetry")
    seed.add_argument("--project-id", required=True)
    seed.add_argument("--environment", default="development")
    seed.add_argument("--traces", type=int, default=120)
    seed.add_argument("--seed", type=int, default=1234)

    subparsers.add_parser("check-config", help="Validate configuration for this environment")
    subparsers.add_parser("check-dependencies", help="Connect to every dependency")
    subparsers.add_parser("migrate-analytics", help="Create or update the analytics schema")
    subparsers.add_parser("price-books", help="List price books")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings)
    handler = _COMMANDS[arguments.command]
    return asyncio.run(handler(settings, arguments))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
