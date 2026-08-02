"""OpenAI-compatible client instrumentation.

Wraps a client object rather than monkey-patching the library. Patching a
third-party module at import time is how an SDK ends up fighting another SDK
that patched the same symbol, and how it breaks silently on a minor version
bump. A wrapper is explicit, inspectable and trivially removable.

Works with any client exposing the OpenAI shape: OpenAI itself, Azure OpenAI,
Together, Groq, vLLM, Ollama's compatibility layer, LiteLLM proxies.

Streaming is handled by wrapping the iterator, so time-to-first-token is
recorded at the moment the first chunk arrives -- the number that actually
matters for a streamed response, and one that total duration hides completely.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from ..tracer import Client, Span, get_client

__all__ = ["instrument_openai", "trace_completion"]


def _usage_from(response: Any) -> dict[str, Any]:
    """Extract usage from a response object or dictionary."""
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return {}
    if not isinstance(usage, dict):
        usage = getattr(usage, "model_dump", lambda: vars(usage))()
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    if not isinstance(prompt_details, dict):
        prompt_details = getattr(prompt_details, "model_dump", lambda: vars(prompt_details))()
    if not isinstance(completion_details, dict):
        completion_details = getattr(
            completion_details, "model_dump", lambda: vars(completion_details)
        )()
    return {
        "input_tokens": usage.get("prompt_tokens") or usage.get("input_tokens"),
        "output_tokens": usage.get("completion_tokens") or usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cached_input_tokens": prompt_details.get("cached_tokens"),
        "reasoning_tokens": completion_details.get("reasoning_tokens"),
        # OpenAI counts cached tokens inside prompt_tokens; the platform needs
        # to know that to avoid charging them twice.
        "raw": {**usage, "cache_convention": "inclusive"},
    }


def _record_request(span: Span, kwargs: dict[str, Any], *, provider: str) -> None:
    span.record_model(
        provider=provider,
        model=str(kwargs.get("model", "unknown")),
        operation="chat",
        temperature=kwargs.get("temperature"),
        top_p=kwargs.get("top_p"),
        max_tokens=kwargs.get("max_tokens") or kwargs.get("max_completion_tokens"),
        seed=kwargs.get("seed"),
    )
    if kwargs.get("tools"):
        span.set_attribute("gen_ai.request.tool_count", len(kwargs["tools"]))
    messages = kwargs.get("messages")
    if messages:
        span.set_input(messages)


def _record_response(span: Span, response: Any) -> None:
    usage = {key: value for key, value in _usage_from(response).items() if value is not None}
    if usage:
        span.record_usage(**usage)
    response_id = getattr(response, "id", None)
    if response_id:
        span.set_attribute("gen_ai.response.id", str(response_id))
    fingerprint = getattr(response, "system_fingerprint", None)
    if fingerprint:
        span.set_attribute("aiobs.model.system_fingerprint", str(fingerprint))
    choices = getattr(response, "choices", None) or []
    reasons = [
        str(getattr(choice, "finish_reason", "") or "")
        for choice in choices
        if getattr(choice, "finish_reason", None)
    ]
    if reasons:
        span.set_attribute("gen_ai.response.finish_reasons", reasons)
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None) if message else None
        if content:
            span.set_output(content)


class _StreamProxy:
    """Wraps a streaming response so the span closes when the stream does.

    A streamed call's span must not end when the function returns -- it returns
    immediately, before any tokens exist. Ending it here, when the iterator is
    exhausted or abandoned, is what makes streaming durations truthful.
    """

    def __init__(self, stream: Iterable[Any], span: Span) -> None:
        self._stream = stream
        self._span = span
        self._chunks = 0
        self._content: list[str] = []
        self._usage: dict[str, Any] = {}

    def __iter__(self) -> Iterator[Any]:
        try:
            for chunk in self._stream:
                if self._chunks == 0:
                    self._span.record_first_token()
                self._chunks += 1
                self._absorb(chunk)
                yield chunk
        except Exception as exc:
            self._span.record_exception(exc)
            raise
        finally:
            self._finish()

    async def __aiter__(self) -> Any:
        try:
            async for chunk in self._stream:  # type: ignore[union-attr]
                if self._chunks == 0:
                    self._span.record_first_token()
                self._chunks += 1
                self._absorb(chunk)
                yield chunk
        except Exception as exc:
            self._span.record_exception(exc)
            raise
        finally:
            self._finish()

    def _absorb(self, chunk: Any) -> None:
        usage = _usage_from(chunk)
        if any(value is not None for key, value in usage.items() if key != "raw"):
            self._usage = usage
        choices = getattr(chunk, "choices", None) or []
        if choices:
            delta = getattr(choices[0], "delta", None)
            content = getattr(delta, "content", None) if delta else None
            if content:
                self._content.append(str(content))

    def _finish(self) -> None:
        self._span.set_attribute("aiobs.stream.chunks", self._chunks)
        if self._content:
            self._span.set_output("".join(self._content))
        cleaned = {key: value for key, value in self._usage.items() if value is not None}
        if cleaned:
            self._span.record_usage(**cleaned)
        else:
            # Many providers omit usage on streamed responses unless asked. Say
            # so explicitly rather than reporting zero tokens.
            self._span.set_attribute("aiobs.usage.source", "missing")
            self._span.set_attribute(
                "aiobs.stream.usage_missing_hint",
                "pass stream_options={'include_usage': True} to receive token counts",
            )
        self._span.end()


def instrument_openai(
    openai_client: Any, *, client: Client | None = None, provider: str = "openai"
) -> Any:
    """Return ``openai_client`` with chat completions traced.

    The original object is mutated in place (its bound method is replaced) and
    also returned, so both usage styles work::

        client = instrument_openai(OpenAI())
        instrument_openai(existing_client)
    """
    tracer = client or get_client()
    completions = openai_client.chat.completions
    if getattr(completions, "_aiobs_instrumented", False):
        return openai_client

    original = completions.create

    def traced_create(*args: Any, **kwargs: Any) -> Any:
        span = tracer.span(f"{provider}.chat", kind="client", category="chat_completion")
        span.__enter__()
        try:
            _record_request(span, kwargs, provider=provider)
            response = original(*args, **kwargs)
        except Exception as exc:
            span.record_exception(exc)
            span.end()
            raise
        if kwargs.get("stream"):
            # The span stays open until the stream is consumed.
            return _StreamProxy(response, span)
        _record_response(span, response)
        span.end()
        return response

    traced_create._aiobs_original = original  # type: ignore[attr-defined]
    completions.create = traced_create  # type: ignore[method-assign]
    completions._aiobs_instrumented = True  # type: ignore[attr-defined]
    return openai_client


def uninstrument_openai(openai_client: Any) -> Any:
    """Undo :func:`instrument_openai`. Useful between tests."""
    completions = openai_client.chat.completions
    original = getattr(completions.create, "_aiobs_original", None)
    if original is not None:
        completions.create = original  # type: ignore[method-assign]
        completions._aiobs_instrumented = False  # type: ignore[attr-defined]
    return openai_client


def trace_completion(
    *,
    provider: str,
    model: str,
    client: Client | None = None,
    name: str | None = None,
    **model_kwargs: Any,
) -> Any:
    """Context manager for a hand-instrumented model call.

    For providers with no client wrapper, or for a call assembled by hand::

        with trace_completion(provider="anthropic", model="claude-sonnet-4") as span:
            span.set_input(messages)
            response = anthropic.messages.create(...)
            span.set_output(response.content[0].text)
            span.record_usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
    """
    tracer = client or get_client()
    span = tracer.span(name or f"{provider}.chat", kind="client", category="chat_completion")
    span.record_model(provider=provider, model=model, **model_kwargs)
    return span
