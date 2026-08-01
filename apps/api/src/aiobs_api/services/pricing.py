"""Price-book management and snapshot loading.

The worker costs thousands of spans per second, so it cannot query prices per
span. Instead it loads a :class:`PriceBookSnapshot` -- the whole applicable rule
set, indexed in memory -- and refreshes it periodically. The snapshot is
immutable, so a refresh swaps a reference rather than mutating shared state.

Tenant-specific books shadow the platform's public book for the models they
cover, and fall through to it for the models they do not. That is how a customer
with negotiated rates on two models gets public pricing on everything else
without having to restate the entire catalogue.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from sqlalchemy import or_, select

from aiobs_schemas.ids import IdPrefix, generate_id

from ..core.errors import ConflictError, NotFoundError, ValidationFailedError
from ..core.logging import get_logger
from ..core.timeutil import Clock
from ..domain.cost import CostCalculator, PriceBookSnapshot, PriceRule, TierMode
from ..domain.usage import UsageCategory
from ..storage.postgres.models import PriceBook, PriceEntry
from ..storage.postgres.session import Database

__all__ = ["PriceBookInput", "PriceEntryInput", "PricingService"]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PriceEntryInput:
    provider: str
    model_identifier: str
    usage_category: str
    unit_price: Decimal
    effective_from: datetime
    unit_quantity: int = 1_000_000
    currency: str = "USD"
    effective_to: datetime | None = None
    tier_min_units: int = 0
    tier_max_units: int | None = None
    discount_rate: Decimal | None = None
    source_url: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class PriceBookInput:
    version: str
    name: str
    description: str | None = None
    source: str | None = None
    currency: str = "USD"
    organization_id: str | None = None
    entries: tuple[PriceEntryInput, ...] = ()


class PricingService:
    """Creates price books and materialises them into calculator snapshots."""

    def __init__(self, *, database: Database, clock: Clock, cache_seconds: int = 300) -> None:
        self._database = database
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[datetime, PriceBookSnapshot]] = {}

    # ------------------------------------------------------------------
    # snapshots
    # ------------------------------------------------------------------

    async def snapshot_for(self, organization_id: str | None) -> PriceBookSnapshot:
        """Return the price rules in force for a tenant, cached briefly.

        The cache TTL is short because a price-book edit must take effect
        promptly, and long enough that costing a burst of spans does not
        re-read the table thousands of times.
        """
        key = organization_id or "__public__"
        cached = self._cache.get(key)
        now = self._clock.now()
        if cached is not None and (now - cached[0]).total_seconds() < self._cache_seconds:
            return cached[1]

        rules: list[PriceRule] = []
        book_id = ""
        version = ""
        currency = "USD"

        async with self._database.session_scope() as session:
            books = list(
                (
                    await session.execute(
                        select(PriceBook)
                        .where(
                            PriceBook.is_active.is_(True),
                            # SQL `IN (NULL)` never matches a NULL row, so the
                            # platform-wide (organization_id IS NULL) book has to
                            # be an explicit disjunct. Getting this wrong makes
                            # every cost silently unpriced.
                            or_(
                                PriceBook.organization_id == organization_id,
                                PriceBook.organization_id.is_(None),
                            )
                            if organization_id
                            else PriceBook.organization_id.is_(None),
                        )
                        .order_by(PriceBook.published_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            if not books:
                log.warning("pricing.no_active_price_book", organization_id=organization_id)
                return PriceBookSnapshot([], version="none")

            # Tenant book first so its rules are indexed ahead of the public
            # ones; PriceBookSnapshot keeps the first match per key.
            books.sort(
                key=lambda book: (book.organization_id is None, -book.published_at.timestamp())
            )
            preferred = books[0]
            book_id, version, currency = preferred.id, preferred.version, preferred.currency

            seen_keys: set[tuple[str, str, str, int]] = set()
            for book in books:
                entries = list(
                    (
                        await session.execute(
                            select(PriceEntry).where(PriceEntry.price_book_id == book.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                for entry in entries:
                    identity = (
                        entry.provider.lower(),
                        entry.model_identifier.lower(),
                        entry.usage_category,
                        entry.tier_min_units,
                    )
                    if identity in seen_keys:
                        # A tenant rule already covers this key; the public
                        # fallback must not override it.
                        continue
                    seen_keys.add(identity)
                    try:
                        category = UsageCategory(entry.usage_category)
                    except ValueError:
                        log.warning(
                            "pricing.unknown_usage_category",
                            category=entry.usage_category,
                            entry_id=entry.id,
                        )
                        continue
                    rules.append(
                        PriceRule(
                            provider=entry.provider,
                            model_identifier=entry.model_identifier,
                            category=category,
                            unit_quantity=entry.unit_quantity,
                            unit_price=entry.unit_price,
                            currency=entry.currency,
                            effective_from=entry.effective_from,
                            effective_to=entry.effective_to,
                            tier_min_units=entry.tier_min_units,
                            tier_max_units=entry.tier_max_units,
                            discount_rate=entry.discount_rate,
                            entry_id=entry.id,
                            price_book_id=book.id,
                            price_book_version=book.version,
                            source_url=entry.source_url,
                        )
                    )

        snapshot = PriceBookSnapshot(
            rules,
            price_book_id=book_id,
            version=version,
            currency=currency,
            tier_mode=TierMode.VOLUME,
        )
        self._cache[key] = (now, snapshot)
        log.info(
            "pricing.snapshot_loaded",
            organization_id=organization_id,
            version=version,
            rules=len(snapshot),
        )
        return snapshot

    async def calculator_for(self, organization_id: str | None) -> CostCalculator:
        return CostCalculator(await self.snapshot_for(organization_id))

    def invalidate(self, organization_id: str | None = None) -> None:
        """Drop cached snapshots after a price change."""
        if organization_id is None:
            self._cache.clear()
        else:
            self._cache.pop(organization_id, None)
            self._cache.pop("__public__", None)

    # ------------------------------------------------------------------
    # administration
    # ------------------------------------------------------------------

    async def create_price_book(self, spec: PriceBookInput, *, created_by: str | None) -> str:
        """Create a price book and its entries, rejecting overlapping windows."""
        self._validate_entries(spec.entries)
        now = self._clock.now()
        async with self._database.session_scope() as session:
            existing = (
                await session.execute(
                    select(PriceBook).where(
                        PriceBook.organization_id == spec.organization_id,
                        PriceBook.version == spec.version,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ConflictError(
                    f"price book version {spec.version!r} already exists; "
                    "publish a new version rather than editing history"
                )
            book = PriceBook(
                id=generate_id(IdPrefix.PRICE_BOOK),
                organization_id=spec.organization_id,
                version=spec.version,
                name=spec.name,
                description=spec.description,
                source=spec.source,
                currency=spec.currency,
                is_active=True,
                published_at=now,
                published_by=created_by,
            )
            session.add(book)
            await session.flush()
            for entry in spec.entries:
                session.add(
                    PriceEntry(
                        id=generate_id(IdPrefix.PRICE_ENTRY),
                        price_book_id=book.id,
                        provider=entry.provider.lower(),
                        model_identifier=entry.model_identifier,
                        usage_category=entry.usage_category,
                        unit_quantity=entry.unit_quantity,
                        unit_price=entry.unit_price,
                        currency=entry.currency,
                        effective_from=entry.effective_from,
                        effective_to=entry.effective_to,
                        tier_min_units=entry.tier_min_units,
                        tier_max_units=entry.tier_max_units,
                        discount_rate=entry.discount_rate,
                        source_url=entry.source_url,
                        notes=entry.notes,
                        created_at=now,
                    )
                )
            self.invalidate(spec.organization_id)
            return book.id

    async def add_entries(
        self, *, price_book_id: str, organization_id: str | None, entries: Sequence[PriceEntryInput]
    ) -> int:
        """Append entries to an existing, unfrozen book."""
        self._validate_entries(entries)
        now = self._clock.now()
        async with self._database.session_scope() as session:
            book = (
                await session.execute(select(PriceBook).where(PriceBook.id == price_book_id))
            ).scalar_one_or_none()
            if book is None or (
                book.organization_id is not None and book.organization_id != organization_id
            ):
                raise NotFoundError("price book", price_book_id)
            if book.frozen_at is not None:
                raise ConflictError(
                    "price book is frozen because costs have been calculated against it; "
                    "publish a new version instead"
                )
            for entry in entries:
                session.add(
                    PriceEntry(
                        id=generate_id(IdPrefix.PRICE_ENTRY),
                        price_book_id=book.id,
                        provider=entry.provider.lower(),
                        model_identifier=entry.model_identifier,
                        usage_category=entry.usage_category,
                        unit_quantity=entry.unit_quantity,
                        unit_price=entry.unit_price,
                        currency=entry.currency,
                        effective_from=entry.effective_from,
                        effective_to=entry.effective_to,
                        tier_min_units=entry.tier_min_units,
                        tier_max_units=entry.tier_max_units,
                        discount_rate=entry.discount_rate,
                        source_url=entry.source_url,
                        notes=entry.notes,
                        created_at=now,
                    )
                )
        self.invalidate(organization_id)
        return len(entries)

    def _validate_entries(self, entries: Sequence[PriceEntryInput]) -> None:
        """Reject overlapping validity windows for the same key.

        Overlaps make price lookup ambiguous, and an ambiguous price makes a
        cost figure unreproducible -- the exact property the whole design exists
        to guarantee. Catching it at write time is the only place it is cheap.
        """
        by_key: dict[tuple[str, str, str, int], list[PriceEntryInput]] = {}
        for entry in entries:
            if entry.unit_price < 0:
                raise ValidationFailedError("unit_price must not be negative")
            if entry.unit_quantity <= 0:
                raise ValidationFailedError("unit_quantity must be positive")
            if entry.discount_rate is not None and not (
                Decimal(0) <= entry.discount_rate < Decimal(1)
            ):
                raise ValidationFailedError("discount_rate must be in [0, 1)")
            try:
                UsageCategory(entry.usage_category)
            except ValueError as exc:
                raise ValidationFailedError(
                    f"unknown usage category {entry.usage_category!r}; "
                    f"expected one of {[category.value for category in UsageCategory]}"
                ) from exc
            key = (
                entry.provider.lower(),
                entry.model_identifier.lower(),
                entry.usage_category,
                entry.tier_min_units,
            )
            by_key.setdefault(key, []).append(entry)

        for key, group in by_key.items():
            ordered = sorted(group, key=lambda item: item.effective_from)
            for previous, current in pairwise(ordered):
                if previous.effective_to is None or previous.effective_to > current.effective_from:
                    raise ValidationFailedError(
                        f"overlapping price windows for {key[0]}/{key[1]}/{key[2]}: "
                        f"the entry effective from {previous.effective_from.isoformat()} "
                        f"must end before {current.effective_from.isoformat()}"
                    )

    async def freeze_book(self, price_book_id: str) -> None:
        """Mark a book immutable once a cost has referenced it."""
        async with self._database.session_scope() as session:
            book = (
                await session.execute(select(PriceBook).where(PriceBook.id == price_book_id))
            ).scalar_one_or_none()
            if book is not None and book.frozen_at is None:
                book.frozen_at = self._clock.now()

    async def list_books(self, organization_id: str | None) -> list[PriceBook]:
        async with self._database.session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(PriceBook)
                        .where(
                            or_(
                                PriceBook.organization_id == organization_id,
                                PriceBook.organization_id.is_(None),
                            )
                            if organization_id
                            else PriceBook.organization_id.is_(None)
                        )
                        .order_by(PriceBook.published_at.desc())
                    )
                )
                .scalars()
                .all()
            )

    async def list_entries(self, price_book_id: str) -> list[PriceEntry]:
        async with self._database.session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(PriceEntry)
                        .where(PriceEntry.price_book_id == price_book_id)
                        .order_by(
                            PriceEntry.provider,
                            PriceEntry.model_identifier,
                            PriceEntry.usage_category,
                            PriceEntry.effective_from,
                        )
                    )
                )
                .scalars()
                .all()
            )


def default_effective_window(clock: Clock) -> tuple[datetime, datetime | None]:
    """A window starting at the beginning of the current month, open-ended."""
    now = clock.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, None


def one_year_window(clock: Clock) -> tuple[datetime, datetime]:
    now = clock.now()
    return now, now + timedelta(days=365)
