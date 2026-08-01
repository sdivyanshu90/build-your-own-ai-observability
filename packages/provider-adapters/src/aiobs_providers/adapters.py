"""Concrete provider adapters.

Four are shipped:

``OpenAICompatibleAdapter``
    The generic case, covering OpenAI itself and the large family of
    OpenAI-compatible endpoints (Azure OpenAI, Together, Groq, vLLM, Ollama's
    compat layer, LiteLLM proxies).

``AnthropicAdapter``
    Different usage shape and, critically, a different *cache convention*: its
    ``input_tokens`` excludes cached reads, where OpenAI's includes them.

``BedrockAdapter``
    Amazon Bedrock's Converse API, which nests usage differently again and
    identifies models by ARN-like ids.

``MockAdapter``
    Fully deterministic, used by the test suite and the offline demos so that
    the whole platform can be exercised end-to-end with no network access, no
    API keys and no cost.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import ModelCallRecord, NormalizedUsageDict, ProviderAdapter, registry

__all__ = [
    "AnthropicAdapter",
    "BedrockAdapter",
    "MockAdapter",
    "OpenAICompatibleAdapter",
]


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _config_from(request: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Extract declared configuration keys, dropping absent ones.

    Absent keys are omitted rather than stored as ``None`` so that a request
    which simply does not set ``top_p`` hashes identically to one that never
    could -- otherwise every SDK version bump would fork the config version.
    """
    config: dict[str, Any] = {}
    for key in keys:
        if key in request and request[key] is not None:
            config[key] = request[key]
    return config


_COMMON_CONFIG_KEYS = (
    "model",
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "stop",
    "stop_sequences",
    "seed",
    "response_format",
    "tools",
    "tool_choice",
    "frequency_penalty",
    "presence_penalty",
)


class OpenAICompatibleAdapter(ProviderAdapter):
    """OpenAI and any endpoint speaking its schema."""

    name = "openai"
    model_hints = ("gpt", "o1", "o3", "o4", "text-embedding", "davinci", "babbage")

    def normalize_usage(self, raw: Mapping[str, Any] | None) -> NormalizedUsageDict:
        if not raw:
            return {}
        prompt_details = raw.get("prompt_tokens_details") or {}
        completion_details = raw.get("completion_tokens_details") or {}
        usage: NormalizedUsageDict = {
            "input_tokens": _as_int(raw.get("prompt_tokens") or raw.get("input_tokens")),
            "output_tokens": _as_int(raw.get("completion_tokens") or raw.get("output_tokens")),
            "total_tokens": _as_int(raw.get("total_tokens")),
            "cached_input_tokens": _as_int(prompt_details.get("cached_tokens")),
            "reasoning_tokens": _as_int(completion_details.get("reasoning_tokens")),
            # OpenAI counts cached tokens *inside* prompt_tokens.
            "cache_convention": "inclusive",
            "source": "provider",
        }
        if audio := _as_int(prompt_details.get("audio_tokens")):
            usage["audio_input_tokens"] = audio
        return {key: value for key, value in usage.items() if value is not None}

    def extract_config(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return _config_from(request, _COMMON_CONFIG_KEYS)

    def extract_call(
        self,
        request: Mapping[str, Any],
        response: Mapping[str, Any] | None = None,
        *,
        error: BaseException | None = None,
    ) -> ModelCallRecord:
        model = str((response or {}).get("model") or request.get("model") or "unknown")
        record = ModelCallRecord(
            provider=self.name,
            model=model,
            operation="embedding" if "embedding" in model else "chat",
            model_family=self.model_family(model),
            config=self.extract_config(request),
        )
        if error is not None:
            record.error_type = type(error).__name__
            record.error_message = str(error)[:2_000]
            return record
        if response:
            record.usage = self.normalize_usage(response.get("usage"))
            record.response_id = response.get("id")
            record.system_fingerprint = response.get("system_fingerprint")
            record.finish_reasons = [
                str(choice.get("finish_reason"))
                for choice in response.get("choices", [])
                if choice.get("finish_reason")
            ]
            record.provider_extras = {
                key: value
                for key, value in response.items()
                if key in {"service_tier", "object", "created"}
            }
        return record


class AnthropicAdapter(ProviderAdapter):
    """Anthropic's Messages API."""

    name = "anthropic"
    model_hints = ("claude", "opus", "sonnet", "haiku")

    def normalize_usage(self, raw: Mapping[str, Any] | None) -> NormalizedUsageDict:
        if not raw:
            return {}
        input_tokens = _as_int(raw.get("input_tokens"))
        output_tokens = _as_int(raw.get("output_tokens"))
        cache_read = _as_int(raw.get("cache_read_input_tokens"))
        cache_write = _as_int(raw.get("cache_creation_input_tokens"))
        usage: NormalizedUsageDict = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cache_read,
            "cache_write_tokens": cache_write,
            # Anthropic reports uncached input separately from cache reads, so
            # the two must be added rather than subtracted. Getting this
            # backwards under-bills every cached request.
            "cache_convention": "exclusive",
            "source": "provider",
        }
        if input_tokens is not None and output_tokens is not None:
            usage["total_tokens"] = (
                input_tokens + output_tokens + (cache_read or 0) + (cache_write or 0)
            )
        return {key: value for key, value in usage.items() if value is not None}

    def extract_config(self, request: Mapping[str, Any]) -> dict[str, Any]:
        config = _config_from(request, _COMMON_CONFIG_KEYS)
        if request.get("system"):
            # The system prompt is part of the prompt version, not the model
            # config; only its presence is recorded here.
            config["has_system_prompt"] = True
        for key in ("thinking", "metadata"):
            if request.get(key):
                config[key] = request[key]
        return config

    def extract_call(
        self,
        request: Mapping[str, Any],
        response: Mapping[str, Any] | None = None,
        *,
        error: BaseException | None = None,
    ) -> ModelCallRecord:
        model = str((response or {}).get("model") or request.get("model") or "unknown")
        record = ModelCallRecord(
            provider=self.name,
            model=model,
            operation="chat",
            model_family=self.model_family(model),
            config=self.extract_config(request),
        )
        if error is not None:
            record.error_type = type(error).__name__
            record.error_message = str(error)[:2_000]
            return record
        if response:
            record.usage = self.normalize_usage(response.get("usage"))
            record.response_id = response.get("id")
            stop_reason = response.get("stop_reason")
            record.finish_reasons = [str(stop_reason)] if stop_reason else []
            record.provider_extras = {
                key: value
                for key, value in response.items()
                if key in {"type", "role", "stop_sequence"}
            }
        return record


class BedrockAdapter(ProviderAdapter):
    """Amazon Bedrock Converse API."""

    name = "bedrock"
    model_hints = ("bedrock", "amazon.", "anthropic.claude-", "meta.llama", "mistral.")

    def normalize_usage(self, raw: Mapping[str, Any] | None) -> NormalizedUsageDict:
        if not raw:
            return {}
        usage: NormalizedUsageDict = {
            "input_tokens": _as_int(raw.get("inputTokens")),
            "output_tokens": _as_int(raw.get("outputTokens")),
            "total_tokens": _as_int(raw.get("totalTokens")),
            "cached_input_tokens": _as_int(raw.get("cacheReadInputTokens")),
            "cache_write_tokens": _as_int(raw.get("cacheWriteInputTokens")),
            "cache_convention": "exclusive",
            "source": "provider",
        }
        return {key: value for key, value in usage.items() if value is not None}

    def extract_config(self, request: Mapping[str, Any]) -> dict[str, Any]:
        inference = request.get("inferenceConfig") or {}
        config = _config_from(
            {**request, **inference},
            (*_COMMON_CONFIG_KEYS, "maxTokens", "topP", "stopSequences", "modelId"),
        )
        if "guardrailConfig" in request:
            config["guardrailConfig"] = request["guardrailConfig"]
        return config

    def canonical_model(self, model: str) -> str:
        """Strip the region prefix from a cross-region inference profile id.

        ``us.anthropic.claude-sonnet-4-v1:0`` and
        ``eu.anthropic.claude-sonnet-4-v1:0`` are the same model deployed in two
        regions. Pricing and dashboards should treat them as one; the region is
        recorded separately on the model version.
        """
        for prefix in ("us.", "eu.", "apac.", "us-gov."):
            if model.startswith(prefix):
                return model[len(prefix) :]
        return model

    def extract_call(
        self,
        request: Mapping[str, Any],
        response: Mapping[str, Any] | None = None,
        *,
        error: BaseException | None = None,
    ) -> ModelCallRecord:
        raw_model = str(request.get("modelId") or (response or {}).get("modelId") or "unknown")
        model = self.canonical_model(raw_model)
        record = ModelCallRecord(
            provider=self.name,
            model=model,
            operation="chat",
            model_family=self.model_family(model),
            config=self.extract_config(request),
            provider_extras={"raw_model_id": raw_model} if raw_model != model else {},
        )
        if error is not None:
            record.error_type = type(error).__name__
            record.error_message = str(error)[:2_000]
            return record
        if response:
            record.usage = self.normalize_usage(response.get("usage"))
            stop = response.get("stopReason")
            record.finish_reasons = [str(stop)] if stop else []
            metrics = response.get("metrics") or {}
            if metrics:
                record.provider_extras["latency_ms"] = metrics.get("latencyMs")
        return record


class MockAdapter(ProviderAdapter):
    """Deterministic provider used by tests and offline demos.

    Every number it produces is a pure function of its inputs, so a test can
    assert an exact token count and an exact cost, and a demo produces identical
    telemetry on every run. That determinism is what makes the end-to-end suite
    assert on real values instead of "greater than zero".
    """

    name = "mock"
    model_hints = ("mock", "fixture", "deterministic")

    def normalize_usage(self, raw: Mapping[str, Any] | None) -> NormalizedUsageDict:
        if not raw:
            return {}
        return {
            "input_tokens": _as_int(raw.get("input_tokens")) or 0,
            "output_tokens": _as_int(raw.get("output_tokens")) or 0,
            "total_tokens": _as_int(raw.get("total_tokens"))
            or ((_as_int(raw.get("input_tokens")) or 0) + (_as_int(raw.get("output_tokens")) or 0)),
            "cached_input_tokens": _as_int(raw.get("cached_input_tokens")),
            "cache_convention": "inclusive",
            "source": "provider",
        }

    def extract_config(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return _config_from(request, _COMMON_CONFIG_KEYS)

    def extract_call(
        self,
        request: Mapping[str, Any],
        response: Mapping[str, Any] | None = None,
        *,
        error: BaseException | None = None,
    ) -> ModelCallRecord:
        model = str(request.get("model") or "mock-model-v1")
        record = ModelCallRecord(
            provider=self.name,
            model=model,
            operation=str(request.get("operation") or "chat"),
            model_family="mock",
            config=self.extract_config(request),
        )
        if error is not None:
            record.error_type = type(error).__name__
            record.error_message = str(error)[:2_000]
            return record
        if response:
            record.usage = self.normalize_usage(response.get("usage"))
            record.response_id = response.get("id", "mock-response")
            record.system_fingerprint = "mock-fingerprint-v1"
            record.finish_reasons = ["stop"]
        return record


registry.register(OpenAICompatibleAdapter())
registry.register(AnthropicAdapter())
registry.register(BedrockAdapter())
registry.register(MockAdapter())
