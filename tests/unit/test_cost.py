"""Cost calculation.

The properties under test are the four the design promises: exactness,
effective dating, reproducibility, and honesty about ignorance.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from aiobs_api.domain.cost import (
    CostCalculator,
    MultiCurrencyTotal,
    PriceBookSnapshot,
    PriceRule,
    TierMode,
    format_cost,
)
from aiobs_api.domain.usage import CacheConvention, NormalizedUsage, UsageCategory
from aiobs_schemas.enums import CostEstimationStatus, UsageSource

JAN = datetime(2026, 1, 1, tzinfo=timezone.utc)
FEB = datetime(2026, 2, 1, tzinfo=timezone.utc)
MAR = datetime(2026, 3, 1, tzinfo=timezone.utc)


def rule(
    category: UsageCategory,
    price: str,
    *,
    effective_from: datetime = JAN,
    effective_to: datetime | None = None,
    model: str = "mock-model-v1",
    tier_min: int = 0,
    tier_max: int | None = None,
    discount: str | None = None,
) -> PriceRule:
    return PriceRule(
        provider="mock",
        model_identifier=model,
        category=category,
        unit_quantity=1_000_000,
        unit_price=Decimal(price),
        currency="USD",
        effective_from=effective_from,
        effective_to=effective_to,
        tier_min_units=tier_min,
        tier_max_units=tier_max,
        discount_rate=Decimal(discount) if discount else None,
    )


def snapshot(*rules: PriceRule, tier_mode: TierMode = TierMode.VOLUME) -> PriceBookSnapshot:
    return PriceBookSnapshot(rules, version="test-v1", tier_mode=tier_mode)


BASIC = snapshot(
    rule(UsageCategory.INPUT_TOKENS, "1.00"),
    rule(UsageCategory.OUTPUT_TOKENS, "2.00"),
    rule(UsageCategory.CACHED_INPUT_TOKENS, "0.10"),
)


class TestBasicCalculation:
    def test_computes_an_exact_total(self) -> None:
        calculator = CostCalculator(BASIC)
        usage = NormalizedUsage(input_tokens=1_000, output_tokens=500, source=UsageSource.PROVIDER)
        result = calculator.compute(provider="mock", model="mock-model-v1", usage=usage, at=FEB)

        # 1000/1e6 * 1.00 + 500/1e6 * 2.00 = 0.001 + 0.001 = 0.002 exactly.
        assert result.total == Decimal("0.002")
        assert result.currency == "USD"
        assert result.estimation_status is CostEstimationStatus.FINAL

    def test_unstated_provenance_downgrades_to_estimated(self) -> None:
        """Counts with no declared source are not billing-grade.

        A caller that supplies numbers without saying where they came from has
        told us nothing about their trustworthiness, and the platform must not
        upgrade that silence into a claim.
        """
        calculator = CostCalculator(BASIC)
        usage = NormalizedUsage(input_tokens=1_000, output_tokens=500)
        result = calculator.compute(provider="mock", model="mock-model-v1", usage=usage, at=FEB)
        assert result.total == Decimal("0.002")
        assert result.estimation_status is CostEstimationStatus.ESTIMATED

    def test_records_a_reproducible_formula(self) -> None:
        calculator = CostCalculator(BASIC)
        usage = NormalizedUsage(input_tokens=1_200, output_tokens=340, source=UsageSource.PROVIDER)
        result = calculator.compute(provider="mock", model="mock-model-v1", usage=usage, at=FEB)
        assert result.formula == "1200/1000000*1.00 + 340/1000000*2.00"
        # Every component carries what it needs to be re-derived by hand.
        for component in result.components:
            assert component.unit_quantity == 1_000_000
            assert component.unit_price > 0

    def test_uses_decimal_not_float(self) -> None:
        """0.1 + 0.2 must be 0.3, which binary floats cannot manage."""
        calculator = CostCalculator(
            snapshot(
                rule(UsageCategory.INPUT_TOKENS, "100000.00"),
                rule(UsageCategory.OUTPUT_TOKENS, "200000.00"),
            )
        )
        usage = NormalizedUsage(input_tokens=1, output_tokens=1)
        result = calculator.compute(provider="mock", model="mock-model-v1", usage=usage, at=FEB)
        assert result.total == Decimal("0.3")
        assert str(result.total) != "0.30000000000000004"

    def test_zero_usage_produces_no_component(self) -> None:
        calculator = CostCalculator(BASIC)
        usage = NormalizedUsage(input_tokens=0, output_tokens=0)
        result = calculator.compute(provider="mock", model="mock-model-v1", usage=usage, at=FEB)
        assert result.total == Decimal("0")


class TestEffectiveDating:
    def test_uses_the_price_in_force_at_the_event_time(self) -> None:
        calculator = CostCalculator(
            snapshot(
                rule(UsageCategory.INPUT_TOKENS, "1.00", effective_from=JAN, effective_to=FEB),
                rule(UsageCategory.INPUT_TOKENS, "3.00", effective_from=FEB),
            )
        )
        usage = NormalizedUsage(input_tokens=1_000_000)

        january = calculator.compute(
            provider="mock", model="mock-model-v1", usage=usage, at=JAN + timedelta(days=5)
        )
        march = calculator.compute(provider="mock", model="mock-model-v1", usage=usage, at=MAR)

        # The same usage costs different amounts depending on *when* it happened.
        assert january.total == Decimal("1.00")
        assert march.total == Decimal("3.00")

    def test_a_price_that_had_not_started_is_not_used(self) -> None:
        calculator = CostCalculator(
            snapshot(rule(UsageCategory.INPUT_TOKENS, "1.00", effective_from=FEB))
        )
        result = calculator.compute(
            provider="mock",
            model="mock-model-v1",
            usage=NormalizedUsage(input_tokens=1_000),
            at=JAN,
        )
        assert result.estimation_status is CostEstimationStatus.UNPRICED

    def test_validity_windows_are_half_open(self) -> None:
        """[from, to) -- so adjacent windows tile without overlapping."""
        calculator = CostCalculator(
            snapshot(
                rule(UsageCategory.INPUT_TOKENS, "1.00", effective_from=JAN, effective_to=FEB),
                rule(UsageCategory.INPUT_TOKENS, "3.00", effective_from=FEB),
            )
        )
        at_boundary = calculator.compute(
            provider="mock",
            model="mock-model-v1",
            usage=NormalizedUsage(input_tokens=1_000_000),
            at=FEB,
        )
        assert at_boundary.total == Decimal("3.00")


class TestUnpricedAndEstimated:
    def test_unknown_model_is_unpriced_not_zero(self) -> None:
        calculator = CostCalculator(BASIC)
        result = calculator.compute(
            provider="mock",
            model="a-model-nobody-priced",
            usage=NormalizedUsage(input_tokens=1_000),
            at=FEB,
        )
        assert result.estimation_status is CostEstimationStatus.UNPRICED
        assert "input_tokens" in result.unpriced_categories

    def test_estimated_usage_produces_an_estimated_cost(self) -> None:
        calculator = CostCalculator(BASIC)
        result = calculator.compute(
            provider="mock",
            model="mock-model-v1",
            usage=NormalizedUsage(
                input_tokens=1_000, output_tokens=500, source=UsageSource.ESTIMATED
            ),
            at=FEB,
        )
        assert result.estimation_status is CostEstimationStatus.ESTIMATED
        assert result.total == Decimal("0.002")

    def test_partially_priced_usage_is_estimated(self) -> None:
        """Some categories priced and some not means the total is a floor."""
        calculator = CostCalculator(snapshot(rule(UsageCategory.INPUT_TOKENS, "1.00")))
        result = calculator.compute(
            provider="mock",
            model="mock-model-v1",
            usage=NormalizedUsage(input_tokens=1_000, output_tokens=500),
            at=FEB,
        )
        assert result.estimation_status is CostEstimationStatus.ESTIMATED
        assert result.unpriced_categories == ("output_tokens",)

    def test_missing_usage_is_unpriced(self) -> None:
        calculator = CostCalculator(BASIC)
        result = calculator.compute(
            provider="mock", model="mock-model-v1", usage=NormalizedUsage(), at=FEB
        )
        assert result.estimation_status is CostEstimationStatus.UNPRICED
        assert result.total == Decimal("0")


class TestCaching:
    def test_inclusive_convention_subtracts_cached_tokens(self) -> None:
        """OpenAI counts cached tokens inside prompt_tokens; charging the full
        rate on all of them would over-bill every cached request."""
        calculator = CostCalculator(BASIC)
        usage = NormalizedUsage(
            input_tokens=1_000,
            cached_input_tokens=400,
            output_tokens=0,
            cache_convention=CacheConvention.INCLUSIVE,
        )
        result = calculator.compute(provider="mock", model="mock-model-v1", usage=usage, at=FEB)
        # 600 uncached at 1.00 + 400 cached at 0.10.
        assert result.total == Decimal("0.00064")

    def test_exclusive_convention_adds_cached_tokens(self) -> None:
        """Anthropic reports uncached input separately; subtracting would
        under-bill."""
        calculator = CostCalculator(BASIC)
        usage = NormalizedUsage(
            input_tokens=1_000,
            cached_input_tokens=400,
            output_tokens=0,
            cache_convention=CacheConvention.EXCLUSIVE,
        )
        result = calculator.compute(provider="mock", model="mock-model-v1", usage=usage, at=FEB)
        # 1000 at 1.00 + 400 cached at 0.10.
        assert result.total == Decimal("0.00104")


class TestTiersAndDiscounts:
    def test_volume_tier_prices_the_whole_quantity(self) -> None:
        calculator = CostCalculator(
            snapshot(
                rule(UsageCategory.INPUT_TOKENS, "1.00", tier_min=0, tier_max=200_000),
                rule(UsageCategory.INPUT_TOKENS, "2.00", tier_min=200_000),
                tier_mode=TierMode.VOLUME,
            )
        )
        result = calculator.compute(
            provider="mock",
            model="mock-model-v1",
            usage=NormalizedUsage(input_tokens=250_000),
            at=FEB,
        )
        # The whole 250k at the higher rate, as a long-context surcharge works.
        assert result.total == Decimal("0.5")

    def test_graduated_tier_splits_the_quantity(self) -> None:
        calculator = CostCalculator(
            snapshot(
                rule(UsageCategory.INPUT_TOKENS, "1.00", tier_min=0, tier_max=200_000),
                rule(UsageCategory.INPUT_TOKENS, "2.00", tier_min=200_000),
                tier_mode=TierMode.GRADUATED,
            )
        )
        result = calculator.compute(
            provider="mock",
            model="mock-model-v1",
            usage=NormalizedUsage(input_tokens=250_000),
            at=FEB,
        )
        # 200k at 1.00 + 50k at 2.00 = 0.2 + 0.1
        assert result.total == Decimal("0.3")
        assert len(result.components) == 2

    def test_discount_is_applied_multiplicatively(self) -> None:
        calculator = CostCalculator(
            snapshot(rule(UsageCategory.INPUT_TOKENS, "1.00", discount="0.20"))
        )
        result = calculator.compute(
            provider="mock",
            model="mock-model-v1",
            usage=NormalizedUsage(input_tokens=1_000_000),
            at=FEB,
        )
        assert result.total == Decimal("0.8")
        assert result.formula == "(1000000/1000000*1.00)*(1-0.20)"


class TestModelFallback:
    def test_dated_snapshot_falls_back_to_the_base_model(self) -> None:
        calculator = CostCalculator(BASIC)
        result = calculator.compute(
            provider="mock",
            model="mock-model-v1-2026-05-01",
            usage=NormalizedUsage(input_tokens=1_000_000),
            at=FEB,
        )
        assert result.total == Decimal("1.00")

    def test_exact_entry_beats_the_fallback(self) -> None:
        calculator = CostCalculator(
            snapshot(
                rule(UsageCategory.INPUT_TOKENS, "1.00"),
                rule(UsageCategory.INPUT_TOKENS, "9.00", model="mock-model-v1-2026-05-01"),
            )
        )
        result = calculator.compute(
            provider="mock",
            model="mock-model-v1-2026-05-01",
            usage=NormalizedUsage(input_tokens=1_000_000),
            at=FEB,
        )
        assert result.total == Decimal("9.00")


class TestMultiCurrency:
    def test_totals_stay_separated_by_currency(self) -> None:
        total = MultiCurrencyTotal()
        total.add(Decimal("10.00"), "USD")
        total.add(Decimal("8.00"), "EUR")
        assert total.as_dict() == {"EUR": "8.00", "USD": "10.00"}

    def test_conversion_reports_currencies_it_could_not_convert(self) -> None:
        """A partial conversion must never look like a complete one."""
        total = MultiCurrencyTotal()
        total.add(Decimal("10.00"), "USD")
        total.add(Decimal("8.00"), "EUR")
        total.add(Decimal("5.00"), "JPY")
        converted, missing = total.convert_to("USD", {"EUR": Decimal("1.10")})
        assert converted == Decimal("18.800000000000")
        assert missing == ["JPY"]


class TestFormatting:
    def test_keeps_sub_cent_precision(self) -> None:
        # Two decimal places would render almost every request as $0.00.
        assert format_cost(Decimal("0.0000123")) == "0.000012 USD"


class TestCostProperties:
    @given(
        input_tokens=st.integers(min_value=0, max_value=10_000_000),
        output_tokens=st.integers(min_value=0, max_value=10_000_000),
    )
    @hypothesis_settings(max_examples=200, deadline=None)
    def test_cost_is_never_negative(self, input_tokens: int, output_tokens: int) -> None:
        calculator = CostCalculator(BASIC)
        result = calculator.compute(
            provider="mock",
            model="mock-model-v1",
            usage=NormalizedUsage(input_tokens=input_tokens, output_tokens=output_tokens),
            at=FEB,
        )
        assert result.total >= 0

    @given(tokens=st.integers(min_value=1, max_value=1_000_000))
    @hypothesis_settings(max_examples=100, deadline=None)
    def test_cost_is_monotonic_in_usage(self, tokens: int) -> None:
        calculator = CostCalculator(BASIC)
        smaller = calculator.compute(
            provider="mock",
            model="mock-model-v1",
            usage=NormalizedUsage(input_tokens=tokens),
            at=FEB,
        )
        larger = calculator.compute(
            provider="mock",
            model="mock-model-v1",
            usage=NormalizedUsage(input_tokens=tokens + 1),
            at=FEB,
        )
        assert larger.total >= smaller.total

    @given(
        input_tokens=st.integers(min_value=0, max_value=100_000),
        output_tokens=st.integers(min_value=0, max_value=100_000),
    )
    @hypothesis_settings(max_examples=150, deadline=None)
    def test_recomputation_is_identical(self, input_tokens: int, output_tokens: int) -> None:
        """Costing the same span twice must give the same number -- this is what
        makes the at-least-once ingestion pipeline safe."""
        calculator = CostCalculator(BASIC)
        usage = NormalizedUsage(input_tokens=input_tokens, output_tokens=output_tokens)
        first = calculator.compute(provider="mock", model="mock-model-v1", usage=usage, at=FEB)
        second = calculator.compute(provider="mock", model="mock-model-v1", usage=usage, at=FEB)
        assert first.total == second.total
        assert first.formula == second.formula
