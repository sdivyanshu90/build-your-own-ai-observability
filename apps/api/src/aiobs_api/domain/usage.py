"""Canonical token accounting.

Providers report usage in mutually incompatible shapes: OpenAI nests cached
tokens under ``prompt_tokens_details``, Anthropic reports
``cache_creation_input_tokens`` and ``cache_read_input_tokens`` as siblings of
``input_tokens``, some report only a total, and streaming responses often report
nothing at all until the final chunk.

Normalising them into one shape is what makes a cross-provider cost dashboard
possible. Three rules keep the normalisation honest:

1. **Never invent numbers.** A missing count is ``None``, not ``0``. Zero means
   "the provider told us zero"; ``None`` means "we do not know". Summing
   ``None`` as zero would silently understate spend.
2. **Always record provenance.** :class:`~aiobs_schemas.enums.UsageSource`
   distinguishes provider-reported, locally-estimated and reconciled numbers, and
   the UI shows the difference. An estimate presented as fact is worse than no
   estimate.
3. **Never discard the raw payload.** The provider's original object is stored
   verbatim so a mis-normalisation is diagnosable and re-derivable, rather than
   being a permanent loss of fidelity.

A deliberate subtlety: providers differ on whether cached input tokens are
*included in* or *additional to* the reported input token count. Getting this
wrong double-bills or under-bills every cached request. The normaliser records
which convention the adapter declared, and the cost engine bills accordingly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from aiobs_schemas.enums import UsageSource

__all__ = [
    "CacheConvention",
    "NormalizedUsage",
    "UsageCategory",
    "estimate_tokens",
    "merge_usage",
]


class UsageCategory(str, Enum):
    """Billable dimensions. These are the keys a price book is indexed by."""

    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    CACHED_INPUT_TOKENS = "cached_input_tokens"
    CACHE_WRITE_TOKENS = "cache_write_tokens"
    REASONING_TOKENS = "reasoning_tokens"
    AUDIO_INPUT_SECONDS = "audio_input_seconds"
    AUDIO_OUTPUT_SECONDS = "audio_output_seconds"
    IMAGE_INPUT_COUNT = "image_input_count"
    IMAGE_OUTPUT_COUNT = "image_output_count"
    #: Flat per-call charge, used by some rerank and moderation endpoints.
    REQUEST = "request"


class CacheConvention(str, Enum):
    """Whether cached input tokens are counted inside ``input_tokens``.

    ``INCLUSIVE``
        ``input_tokens`` already contains the cached tokens (OpenAI's
        convention). Billing must subtract them before applying the full input
        rate, then charge the cached rate separately.

    ``EXCLUSIVE``
        ``input_tokens`` counts only the uncached remainder (Anthropic's
        convention). Billing applies both rates directly.
    """

    INCLUSIVE = "inclusive"
    EXCLUSIVE = "exclusive"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class NormalizedUsage:
    """Provider-independent usage for one model call."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    audio_input_seconds: float | None = None
    audio_output_seconds: float | None = None
    image_input_count: int | None = None
    image_output_count: int | None = None
    source: UsageSource = UsageSource.MISSING
    cache_convention: CacheConvention = CacheConvention.UNKNOWN
    #: Verbatim provider payload, never interpreted after normalisation.
    raw: Mapping[str, Any] | None = None

    @property
    def is_missing(self) -> bool:
        """Whether no usage dimension at all was reported."""
        return all(
            getattr(self, name) is None
            for name in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cached_input_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "audio_input_seconds",
                "audio_output_seconds",
                "image_input_count",
                "image_output_count",
            )
        )

    @property
    def billable_input_tokens(self) -> int | None:
        """Input tokens charged at the full (uncached) rate.

        Under the inclusive convention the cached tokens must be removed first;
        under the exclusive convention they were never included. Getting this
        backwards is the single most common cost-reporting bug with prompt
        caching, which is why it lives in one place with a name.
        """
        if self.input_tokens is None:
            return None
        if self.cache_convention is CacheConvention.INCLUSIVE and self.cached_input_tokens:
            return max(self.input_tokens - self.cached_input_tokens, 0)
        return self.input_tokens

    @property
    def effective_total_tokens(self) -> int | None:
        """Total tokens, derived if the provider did not report one."""
        if self.total_tokens is not None:
            return self.total_tokens
        parts = [value for value in (self.input_tokens, self.output_tokens) if value is not None]
        return sum(parts) if parts else None

    def quantities(self) -> dict[UsageCategory, int | float]:
        """Billable quantity per category, omitting unknown dimensions.

        Token counts stay ``int``. Coercing them to ``float`` would render the
        stored cost formula as ``1200.0/1000000*3.00`` -- still correct, but the
        formula exists to be checked by a human, and a spurious ``.0`` on a
        token count is exactly the kind of noise that makes an audit trail
        harder to read than it needs to be.
        """
        result: dict[UsageCategory, int | float] = {}
        billable_input = self.billable_input_tokens
        if billable_input is not None:
            result[UsageCategory.INPUT_TOKENS] = billable_input
        if self.output_tokens is not None:
            result[UsageCategory.OUTPUT_TOKENS] = self.output_tokens
        if self.cached_input_tokens:
            result[UsageCategory.CACHED_INPUT_TOKENS] = self.cached_input_tokens
        if self.cache_write_tokens:
            result[UsageCategory.CACHE_WRITE_TOKENS] = self.cache_write_tokens
        if self.reasoning_tokens:
            result[UsageCategory.REASONING_TOKENS] = self.reasoning_tokens
        if self.audio_input_seconds:
            result[UsageCategory.AUDIO_INPUT_SECONDS] = float(self.audio_input_seconds)
        if self.audio_output_seconds:
            result[UsageCategory.AUDIO_OUTPUT_SECONDS] = float(self.audio_output_seconds)
        if self.image_input_count:
            result[UsageCategory.IMAGE_INPUT_COUNT] = self.image_input_count
        if self.image_output_count:
            result[UsageCategory.IMAGE_OUTPUT_COUNT] = self.image_output_count
        return result

    def with_source(self, source: UsageSource) -> NormalizedUsage:
        return replace(self, source=source)

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.effective_total_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "audio_input_seconds": self.audio_input_seconds,
            "audio_output_seconds": self.audio_output_seconds,
            "image_input_count": self.image_input_count,
            "image_output_count": self.image_output_count,
            "source": self.source.value,
            "cache_convention": self.cache_convention.value,
        }


def merge_usage(base: NormalizedUsage, update: NormalizedUsage) -> NormalizedUsage:
    """Combine two observations of the same call, preferring the better source.

    Streaming calls commonly produce a partial estimate mid-stream and an exact
    provider figure at the end. Reconciliation jobs later produce a third. This
    resolves them by provenance rank, never by arithmetic -- adding an estimate
    to a provider figure would be double counting.
    """
    ranking = {
        UsageSource.MISSING: 0,
        UsageSource.ESTIMATED: 1,
        UsageSource.PROVIDER: 2,
        UsageSource.RECONCILED: 3,
    }
    if ranking[update.source] < ranking[base.source]:
        return base
    if ranking[update.source] > ranking[base.source]:
        return update
    # Same provenance: fill gaps rather than overwrite known values.
    values: dict[str, Any] = {}
    for name in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "audio_input_seconds",
        "audio_output_seconds",
        "image_input_count",
        "image_output_count",
    ):
        current = getattr(base, name)
        candidate = getattr(update, name)
        values[name] = current if current is not None else candidate
    convention = (
        base.cache_convention
        if base.cache_convention is not CacheConvention.UNKNOWN
        else update.cache_convention
    )
    return NormalizedUsage(
        **values,
        source=base.source,
        cache_convention=convention,
        raw=base.raw or update.raw,
    )


#: Average characters per token across English prose and code, measured against
#: cl100k_base. Only used when a provider reports nothing at all, and the result
#: is always tagged ESTIMATED so no dashboard presents it as fact.
_CHARS_PER_TOKEN = 3.8


def estimate_tokens(text: str | None) -> int | None:
    """Rough token count for text, used only when the provider reports nothing.

    This is a character-ratio heuristic, not a tokeniser. Shipping a real
    tokeniser would mean vendoring per-model vocabularies, keeping them current,
    and still being wrong for models whose tokeniser is not published -- while
    implying a precision the number does not have. The platform instead marks
    the value ESTIMATED and shows it differently in the UI.

    Accuracy is roughly ±20% on English prose and worse on dense code or
    non-Latin scripts; see ``docs/concepts/token-accounting.md``.
    """
    if not text:
        return None
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


@dataclass(slots=True)
class UsageAccumulator:
    """Sums usage across many spans while preserving provenance semantics.

    Used for trace roll-ups. The resulting ``source`` is the *weakest* input
    source present: a trace containing one estimated span is an estimated
    trace, because reporting it as provider-exact would be a lie about the
    weakest link.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    observed_sources: set[UsageSource] = field(default_factory=set)
    contributing_spans: int = 0

    def add(self, usage: NormalizedUsage) -> None:
        if usage.is_missing:
            self.observed_sources.add(UsageSource.MISSING)
            return
        self.input_tokens += usage.input_tokens or 0
        self.output_tokens += usage.output_tokens or 0
        self.total_tokens += usage.effective_total_tokens or 0
        self.cached_input_tokens += usage.cached_input_tokens or 0
        self.reasoning_tokens += usage.reasoning_tokens or 0
        self.observed_sources.add(usage.source)
        self.contributing_spans += 1

    @property
    def source(self) -> UsageSource:
        if not self.observed_sources:
            return UsageSource.MISSING
        # Ordered weakest-first: MISSING < ESTIMATED < PROVIDER < RECONCILED.
        for candidate in (
            UsageSource.MISSING,
            UsageSource.ESTIMATED,
            UsageSource.PROVIDER,
            UsageSource.RECONCILED,
        ):
            if candidate in self.observed_sources:
                return candidate
        return UsageSource.MISSING
