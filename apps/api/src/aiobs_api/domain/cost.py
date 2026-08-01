"""Cost calculation.

Every number this module produces must satisfy four properties, and each one
shaped the design:

**Exact.** All arithmetic is :class:`decimal.Decimal` under an explicit context.
Binary floats cannot represent 0.1, and a platform whose entire purpose includes
"how much did this cost" cannot afford to be approximately right about money.

**Effective-dated.** Prices are looked up by the span's *event time*, not by
"now". Re-running last quarter's report after a provider price change must
reproduce last quarter's numbers, or the report is not a report.

**Reproducible.** Each cost record stores the price-book version, the usage
inputs, the per-component breakdown and a human-readable formula. Given the
record alone you can recompute the total by hand and get the same answer.

**Honest about ignorance.** An unknown model produces ``UNPRICED``, not zero.
Zero is a claim; ``UNPRICED`` is the truth, and the dashboards render the two
differently so nobody budgets against a model whose price we never knew.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import Enum
from typing import Any

from aiobs_schemas.enums import CostEstimationStatus, UsageSource

from .usage import NormalizedUsage, UsageCategory

__all__ = [
    "ZERO",
    "CostBreakdown",
    "CostCalculator",
    "CostComponent",
    "PriceBookSnapshot",
    "PriceRule",
    "TierMode",
]

#: Working precision. 34 significant digits is IEEE-754 decimal128; far more
#: than any price needs, and enough that intermediate products never round.
_PRECISION = 34
#: Fractional digits retained on a stored total. Per-token prices reach 1e-9,
#: and a single request can be a fraction of a microcent, so 12 decimal places
#: is the floor at which per-request costs stay meaningful.
_STORAGE_SCALE = Decimal("0.000000000001")

ZERO = Decimal("0")


class TierMode(str, Enum):
    """How a multi-tier price applies to a quantity."""

    #: The whole quantity is charged at the rate of the tier it falls into.
    #: This is how "long context" surcharges work: a 250k-token prompt is
    #: charged entirely at the >200k rate.
    VOLUME = "volume"
    #: The quantity is split across tiers, each portion at its own rate. This
    #: is how committed-use discounts usually work.
    GRADUATED = "graduated"


@dataclass(frozen=True, slots=True)
class PriceRule:
    """One effective-dated price for one usage category of one model."""

    provider: str
    model_identifier: str
    category: UsageCategory
    #: Quantity the price covers, e.g. 1_000_000 for "per million tokens".
    unit_quantity: int
    unit_price: Decimal
    currency: str
    effective_from: datetime
    effective_to: datetime | None = None
    tier_min_units: int = 0
    tier_max_units: int | None = None
    #: Multiplicative discount, e.g. Decimal("0.15") for 15% off.
    discount_rate: Decimal | None = None
    entry_id: str = ""
    price_book_id: str = ""
    price_book_version: str = ""
    source_url: str | None = None

    def covers(self, moment: datetime) -> bool:
        """Whether this rule is in effect at ``moment`` (half-open interval)."""
        if moment < self.effective_from:
            return False
        return self.effective_to is None or moment < self.effective_to

    def covers_quantity(self, quantity: float) -> bool:
        if quantity < self.tier_min_units:
            return False
        return self.tier_max_units is None or quantity < self.tier_max_units

    @property
    def effective_unit_price(self) -> Decimal:
        """Unit price after any discount."""
        if self.discount_rate is None:
            return self.unit_price
        return self.unit_price * (Decimal(1) - self.discount_rate)


@dataclass(frozen=True, slots=True)
class CostComponent:
    """The contribution of one usage category to a total."""

    category: UsageCategory
    quantity: Decimal
    unit_quantity: int
    unit_price: Decimal
    amount: Decimal
    currency: str
    entry_id: str = ""
    discount_rate: Decimal | None = None
    tier_min_units: int = 0
    tier_max_units: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "quantity": str(self.quantity),
            "unit_quantity": self.unit_quantity,
            "unit_price": str(self.unit_price),
            "amount": str(self.amount),
            "currency": self.currency,
            "entry_id": self.entry_id,
            "discount_rate": None if self.discount_rate is None else str(self.discount_rate),
            "tier_min_units": self.tier_min_units,
            "tier_max_units": self.tier_max_units,
        }

    @property
    def formula(self) -> str:
        """Human-checkable derivation of ``amount``."""
        base = f"{self.quantity}/{self.unit_quantity}*{self.unit_price}"
        if self.discount_rate is not None:
            return f"({base})*(1-{self.discount_rate})"
        return base


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """A complete, auditable cost calculation."""

    total: Decimal
    currency: str
    components: tuple[CostComponent, ...] = ()
    estimation_status: CostEstimationStatus = CostEstimationStatus.FINAL
    price_book_id: str = ""
    price_book_version: str = ""
    usage_source: UsageSource = UsageSource.PROVIDER
    #: Categories present in the usage for which no price rule matched.
    unpriced_categories: tuple[str, ...] = ()

    @property
    def formula(self) -> str:
        if not self.components:
            return "0"
        return " + ".join(component.formula for component in self.components)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": str(self.total),
            "currency": self.currency,
            "components": [component.as_dict() for component in self.components],
            "estimation_status": self.estimation_status.value,
            "price_book_id": self.price_book_id,
            "price_book_version": self.price_book_version,
            "usage_source": self.usage_source.value,
            "unpriced_categories": list(self.unpriced_categories),
            "formula": self.formula,
        }

    @classmethod
    def unpriced(
        cls,
        *,
        currency: str = "USD",
        usage_source: UsageSource = UsageSource.MISSING,
        categories: Sequence[str] = (),
        price_book_version: str = "",
    ) -> CostBreakdown:
        return cls(
            total=ZERO,
            currency=currency,
            estimation_status=CostEstimationStatus.UNPRICED,
            usage_source=usage_source,
            unpriced_categories=tuple(categories),
            price_book_version=price_book_version,
        )


class PriceBookSnapshot:
    """An immutable, indexed view of the price rules in force.

    Loaded once per worker cycle and reused across thousands of spans. The index
    is ``(provider, model, category) -> rules sorted by effective_from``, so a
    lookup is a dictionary hit plus a binary search rather than a scan, which
    matters when costing ten thousand spans a second.

    Model matching falls back from the exact identifier to a normalised form
    (``gpt-4o-2026-01-15`` -> ``gpt-4o``) and finally to a provider wildcard, so
    a newly-released dated snapshot of a known model is priced rather than
    silently unpriced.
    """

    __slots__ = ("_currency", "_id", "_index", "_rule_count", "_tier_mode", "_version")

    def __init__(
        self,
        rules: Iterable[PriceRule],
        *,
        price_book_id: str = "",
        version: str = "",
        currency: str = "USD",
        tier_mode: TierMode = TierMode.VOLUME,
    ) -> None:
        index: dict[tuple[str, str, UsageCategory], list[PriceRule]] = {}
        count = 0
        for rule in rules:
            key = (rule.provider.lower(), rule.model_identifier.lower(), rule.category)
            index.setdefault(key, []).append(rule)
            count += 1
        for bucket in index.values():
            bucket.sort(key=lambda item: (item.effective_from, item.tier_min_units))
        self._index = index
        self._tier_mode = tier_mode
        self._id = price_book_id
        self._version = version
        self._currency = currency
        self._rule_count = count

    @property
    def price_book_id(self) -> str:
        return self._id

    @property
    def version(self) -> str:
        return self._version

    @property
    def currency(self) -> str:
        return self._currency

    @property
    def tier_mode(self) -> TierMode:
        return self._tier_mode

    def __len__(self) -> int:
        return self._rule_count

    def candidate_keys(self, provider: str, model: str) -> tuple[tuple[str, str], ...]:
        """Model identifiers to try, most specific first."""
        provider_key = provider.lower()
        model_key = model.lower()
        candidates = [(provider_key, model_key)]
        normalised = _normalise_model(model_key)
        if normalised != model_key:
            candidates.append((provider_key, normalised))
        candidates.append((provider_key, "*"))
        return tuple(candidates)

    def rules_for(
        self, provider: str, model: str, category: UsageCategory, moment: datetime
    ) -> list[PriceRule]:
        """All tiers in effect for a model/category at ``moment``."""
        for provider_key, model_key in self.candidate_keys(provider, model):
            bucket = self._index.get((provider_key, model_key, category))
            if not bucket:
                continue
            # Binary search to the first rule that could be in effect, then
            # filter: buckets are sorted by effective_from.
            starts = [rule.effective_from for rule in bucket]
            upper = bisect_right(starts, moment)
            active = [rule for rule in bucket[:upper] if rule.covers(moment)]
            if active:
                return sorted(active, key=lambda item: item.tier_min_units)
        return []


def _normalise_model(model: str) -> str:
    """Strip a trailing date or version suffix from a model identifier.

    ``gpt-4o-2026-01-15`` -> ``gpt-4o``; ``claude-sonnet-4-20260514`` ->
    ``claude-sonnet-4``. Purely a *fallback*: an exact price entry always wins,
    so an operator who prices dated snapshots individually is never overridden.
    """
    import re

    return re.sub(r"[-@](?:\d{4}-\d{2}-\d{2}|\d{6,8}|v\d+|latest)$", "", model)


class CostCalculator:
    """Computes reproducible costs from usage and a price-book snapshot."""

    __slots__ = ("_snapshot",)

    def __init__(self, snapshot: PriceBookSnapshot) -> None:
        self._snapshot = snapshot

    @property
    def snapshot(self) -> PriceBookSnapshot:
        return self._snapshot

    def compute(
        self,
        *,
        provider: str,
        model: str,
        usage: NormalizedUsage,
        at: datetime,
    ) -> CostBreakdown:
        """Cost the given usage at the prices in force at ``at``."""
        if usage.is_missing:
            return CostBreakdown.unpriced(
                currency=self._snapshot.currency,
                usage_source=UsageSource.MISSING,
                price_book_version=self._snapshot.version,
            )
        if not provider or not model:
            return CostBreakdown.unpriced(
                currency=self._snapshot.currency,
                usage_source=usage.source,
                categories=[category.value for category in usage.quantities()],
                price_book_version=self._snapshot.version,
            )

        quantities = usage.quantities()
        components: list[CostComponent] = []
        unpriced: list[str] = []
        currency = self._snapshot.currency

        with localcontext() as context:
            context.prec = _PRECISION
            context.rounding = ROUND_HALF_EVEN

            for category, quantity in quantities.items():
                if quantity <= 0:
                    continue
                rules = self._snapshot.rules_for(provider, model, category, at)
                if not rules:
                    unpriced.append(category.value)
                    continue
                currency = rules[0].currency
                components.extend(
                    self._price_quantity(category, Decimal(str(quantity)), rules, currency)
                )

            total = sum((component.amount for component in components), start=ZERO)
            total = total.quantize(_STORAGE_SCALE, rounding=ROUND_HALF_EVEN)

        if not components:
            return CostBreakdown.unpriced(
                currency=currency,
                usage_source=usage.source,
                categories=unpriced,
                price_book_version=self._snapshot.version,
            )

        # A cost is only "final" when every dimension was priced AND the usage
        # came from the provider. Estimated tokens or a partially-priced call
        # produce an explicitly estimated figure.
        if unpriced or usage.source in {UsageSource.ESTIMATED, UsageSource.MISSING}:
            status = CostEstimationStatus.ESTIMATED
        else:
            status = CostEstimationStatus.FINAL

        return CostBreakdown(
            total=total,
            currency=currency,
            components=tuple(components),
            estimation_status=status,
            price_book_id=self._snapshot.price_book_id,
            price_book_version=self._snapshot.version,
            usage_source=usage.source,
            unpriced_categories=tuple(unpriced),
        )

    def _price_quantity(
        self,
        category: UsageCategory,
        quantity: Decimal,
        rules: Sequence[PriceRule],
        currency: str,
    ) -> list[CostComponent]:
        """Apply tiering to a single category's quantity."""
        if self._snapshot.tier_mode is TierMode.VOLUME or len(rules) == 1:
            rule = _select_volume_tier(rules, float(quantity))
            if rule is None:
                return []
            return [_component(category, quantity, rule, currency)]

        # Graduated: split the quantity across successive tiers.
        components: list[CostComponent] = []
        remaining = quantity
        for rule in rules:
            if remaining <= 0:
                break
            lower = Decimal(rule.tier_min_units)
            upper = Decimal(rule.tier_max_units) if rule.tier_max_units is not None else None
            if quantity <= lower:
                continue
            band_top = quantity if upper is None else min(quantity, upper)
            portion = band_top - lower
            if portion <= 0:
                continue
            components.append(_component(category, portion, rule, currency))
            remaining -= portion
        return components


def _select_volume_tier(rules: Sequence[PriceRule], quantity: float) -> PriceRule | None:
    """Pick the single tier whose band contains ``quantity``."""
    for rule in rules:
        if rule.covers_quantity(quantity):
            return rule
    # Quantity above every declared band: charge at the highest tier rather than
    # leaving it unpriced, which would understate spend precisely when it is
    # largest.
    return rules[-1] if rules else None


def _component(
    category: UsageCategory, quantity: Decimal, rule: PriceRule, currency: str
) -> CostComponent:
    amount = (quantity / Decimal(rule.unit_quantity)) * rule.effective_unit_price
    return CostComponent(
        category=category,
        quantity=quantity,
        unit_quantity=rule.unit_quantity,
        unit_price=rule.unit_price,
        amount=amount,
        currency=currency,
        entry_id=rule.entry_id,
        discount_rate=rule.discount_rate,
        tier_min_units=rule.tier_min_units,
        tier_max_units=rule.tier_max_units,
    )


@dataclass(slots=True)
class MultiCurrencyTotal:
    """Aggregated spend, kept separated by currency.

    The platform refuses to sum across currencies without an explicit,
    effective-dated FX rate, because a single "total cost" that silently mixed
    USD and EUR at an unstated rate would be wrong in a way nobody could detect
    from the number itself. Reports show one row per currency; a caller who
    wants one figure must supply rates and accept responsibility for them.
    """

    totals: dict[str, Decimal] = field(default_factory=dict)

    def add(self, amount: Decimal, currency: str) -> None:
        self.totals[currency] = self.totals.get(currency, ZERO) + amount

    def convert_to(self, target: str, rates: Mapping[str, Decimal]) -> tuple[Decimal, list[str]]:
        """Convert every currency into ``target`` using ``rates``.

        Returns the converted total and the list of currencies that had no rate
        and were therefore *excluded* -- surfaced to the caller so a partial
        conversion is never mistaken for a complete one.
        """
        with localcontext() as context:
            context.prec = _PRECISION
            context.rounding = ROUND_HALF_EVEN
            total = ZERO
            missing: list[str] = []
            for currency, amount in self.totals.items():
                if currency == target:
                    total += amount
                    continue
                rate = rates.get(currency)
                if rate is None:
                    missing.append(currency)
                    continue
                total += amount * rate
            return total.quantize(_STORAGE_SCALE, rounding=ROUND_HALF_EVEN), sorted(missing)

    def as_dict(self) -> dict[str, str]:
        return {currency: str(amount) for currency, amount in sorted(self.totals.items())}


def format_cost(amount: Decimal, currency: str = "USD", *, places: int = 6) -> str:
    """Render a cost for display, keeping sub-cent precision visible.

    Two decimal places would render almost every individual AI request as
    ``$0.00``, which is useless. Six places keeps a per-request figure legible
    while remaining exact enough to sum.
    """
    quantum = Decimal(1).scaleb(-places)
    return f"{amount.quantize(quantum, rounding=ROUND_HALF_EVEN)} {currency}"
