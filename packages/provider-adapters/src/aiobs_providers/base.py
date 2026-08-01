"""Provider adapter contract.

Model providers change their response shapes without warning and disagree about
almost everything: what a "cached token" is, whether reasoning tokens are
included in the output count, what a model identifier looks like, and whether
usage is reported at all on a streamed response.

An adapter's job is to answer three questions in one place per provider, so
that nothing downstream has to care:

1. What is the canonical ``(provider, model)`` for this call?
2. What did it consume, in the platform's normalised usage schema?
3. Which of the request's parameters define its *configuration version*?

Everything a provider reports that does not fit the canonical schema is kept in
``provider_extras``. That is deliberate: dropping it would lose information the
next person debugging a weird response needs, and promoting it into the canonical
model would make the cross-provider schema meaningless.

Adding a provider means implementing this one class -- see
``docs/development/adding-provider-adapters.md``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aiobs_schemas.canonical import content_hash

__all__ = ["ModelCallRecord", "NormalizedUsageDict", "ProviderAdapter", "registry"]

#: Usage in the platform's canonical shape. A plain dict rather than an import
#: from the backend, so this package stays dependency-light and usable from an
#: SDK that never talks to the API.
NormalizedUsageDict = dict[str, Any]


@dataclass(slots=True)
class ModelCallRecord:
    """Everything an adapter extracted from one model call."""

    provider: str
    model: str
    #: 'chat', 'completion', 'embedding', 'rerank', ...
    operation: str = "chat"
    model_family: str | None = None
    usage: NormalizedUsageDict = field(default_factory=dict)
    #: Canonical configuration inputs, hashed into the model version id.
    config: dict[str, Any] = field(default_factory=dict)
    #: Provider-specific fields preserved verbatim.
    provider_extras: dict[str, Any] = field(default_factory=dict)
    response_id: str | None = None
    system_fingerprint: str | None = None
    finish_reasons: list[str] = field(default_factory=list)
    #: Set when the provider signalled an error rather than a completion.
    error_type: str | None = None
    error_message: str | None = None

    def config_hash(self) -> str:
        """Deterministic hash of the canonical configuration.

        Only ``config`` is hashed. ``provider_extras``, ``response_id`` and
        ``system_fingerprint`` are observations of a particular call, not inputs
        to it, so including them would make every call its own "version".
        """
        return content_hash(
            {
                "provider": self.provider,
                "model": self.model,
                "operation": self.operation,
                "config": self.config,
            }
        )


class ProviderAdapter(ABC):
    """Maps one provider's request/response shapes onto the canonical schema."""

    #: Canonical provider identifier, used as the ``gen_ai.system`` value.
    name: str = ""
    #: Substrings that identify a model as belonging to this provider, used by
    #: :func:`registry.detect` when the caller did not name one.
    model_hints: tuple[str, ...] = ()

    @abstractmethod
    def normalize_usage(self, raw: Mapping[str, Any] | None) -> NormalizedUsageDict:
        """Convert a provider usage object into the canonical shape.

        Must return ``{}`` (not zeros) when the provider reported nothing --
        "unknown" and "zero" are different facts and the cost engine treats
        them differently.
        """

    @abstractmethod
    def extract_config(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Return the canonical configuration inputs from a request payload."""

    @abstractmethod
    def extract_call(
        self,
        request: Mapping[str, Any],
        response: Mapping[str, Any] | None = None,
        *,
        error: BaseException | None = None,
    ) -> ModelCallRecord:
        """Build a :class:`ModelCallRecord` from a request/response pair."""

    def model_family(self, model: str) -> str | None:
        """Best-effort family classification, e.g. ``gpt``, ``claude``."""
        lowered = model.lower()
        for hint in self.model_hints:
            if hint in lowered:
                return hint
        return None

    def canonical_model(self, model: str) -> str:
        """Normalise a model identifier (default: unchanged)."""
        return model


class _Registry:
    """Lookup of adapters by name, with model-based detection as a fallback."""

    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter) -> ProviderAdapter:
        if not adapter.name:
            raise ValueError("provider adapter must declare a name")
        self._adapters[adapter.name] = adapter
        return adapter

    def get(self, name: str) -> ProviderAdapter | None:
        return self._adapters.get(name.lower())

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def detect(self, model: str) -> ProviderAdapter | None:
        """Guess the provider from a model identifier.

        Only used when the caller did not specify one. An explicit provider
        always wins, because guessing wrong silently attributes cost to the
        wrong vendor.
        """
        lowered = model.lower()
        for adapter in self._adapters.values():
            if any(hint in lowered for hint in adapter.model_hints):
                return adapter
        return None

    def resolve(self, *, provider: str | None, model: str) -> ProviderAdapter | None:
        if provider:
            adapter = self.get(provider)
            if adapter is not None:
                return adapter
        return self.detect(model)


registry = _Registry()
