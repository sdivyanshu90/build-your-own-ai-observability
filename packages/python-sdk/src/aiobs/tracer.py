"""The tracing API: :class:`Client`, :class:`Trace` and :class:`Span`.

Design goals, in priority order:

1. **Never break the host application.** Every public method is wrapped so that
   an SDK bug produces a warning, not an exception in the caller's request path.
   Instrumentation that can crash production is instrumentation nobody enables.
2. **Read like the code it describes.** ``with trace.span("retrieve", kind="retrieval")``
   should be obvious to someone who has never seen the SDK.
3. **Work the same sync and async.** The same object is both a context manager
   and an async context manager, so adding ``await`` to a function does not
   require rewriting its instrumentation.
4. **Nest correctly by default.** Parent/child is derived from the ambient
   context, including an existing OpenTelemetry span, so nesting is automatic
   and manual parent wiring is the exception.
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import random
import socket
import time
import traceback
from collections.abc import Callable, Iterable, Mapping
from types import TracebackType
from typing import Any, TypeVar

from . import semconv
from .config import Config, from_env
from .context import SpanContext, inject, resolve_parent, use_context
from .exporter import SDK_NAME, SDK_VERSION, BatchExporter, Transport
from .redaction import Redactor

__all__ = ["Client", "Span", "Trace", "get_client", "init", "shutdown"]

log = logging.getLogger("aiobs")

F = TypeVar("F", bound=Callable[..., Any])

_NS = 1_000_000_000


def _now_ns() -> int:
    return time.time_ns()


def _safe(method: F) -> F:
    """Swallow SDK errors so instrumentation can never fail a request.

    The one thing worse than losing a span is taking down the request that
    produced it. Errors are logged once at WARNING with a stack trace under
    debug.
    """

    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            log.warning("aiobs: %s failed: %s", method.__name__, exc)
            if (
                getattr(getattr(self, "_client", None), "config", None)
                and self._client.config.debug
            ):
                log.warning(traceback.format_exc())
            return None

    return wrapper  # type: ignore[return-value]


class Span:
    """A unit of work inside a trace."""

    __slots__ = (
        "_agent_step",
        "_attributes",
        "_client",
        "_context",
        "_end_ns",
        "_ended",
        "_events",
        "_first_token_at",
        "_lineage",
        "_links",
        "_parent_id",
        "_retrieval",
        "_start_ns",
        "_status",
        "_status_message",
        "_token",
        "_trace_fields",
        "_usage",
        "category",
        "kind",
        "name",
    )

    def __init__(
        self,
        client: Client,
        name: str,
        *,
        context: SpanContext,
        parent_id: str | None,
        kind: str = "internal",
        category: str = "custom",
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._context = context
        self._parent_id = parent_id
        self.name = name[:512]
        self.kind = kind
        self.category = category
        self._start_ns = _now_ns()
        self._end_ns: int | None = None
        self._status = "unset"
        self._status_message: str | None = None
        self._attributes: dict[str, Any] = dict(attributes or {})
        self._events: list[dict[str, Any]] = []
        self._links: list[dict[str, Any]] = []
        self._usage: dict[str, Any] | None = None
        self._retrieval: dict[str, Any] | None = None
        self._agent_step: dict[str, Any] | None = None
        self._lineage: dict[str, Any] = {}
        self._trace_fields: dict[str, Any] = {}
        self._ended = False
        self._token: Any = None
        self._first_token_at: int | None = None

    # ------------------------------------------------------------------
    # identity
    # ------------------------------------------------------------------

    @property
    def trace_id(self) -> str:
        return self._context.trace_id

    @property
    def span_id(self) -> str:
        return self._context.span_id

    @property
    def context(self) -> SpanContext:
        return self._context

    def headers(self) -> dict[str, str]:
        """Propagation headers for an outbound HTTP request or queue message."""
        return inject(self._context)

    # ------------------------------------------------------------------
    # attributes and events
    # ------------------------------------------------------------------

    @_safe
    def set_attribute(self, key: str, value: Any) -> Span:
        self._attributes[key] = value
        return self

    @_safe
    def set_attributes(self, attributes: Mapping[str, Any]) -> Span:
        self._attributes.update(attributes)
        return self

    @_safe
    def set_tags(self, *tags: str) -> Span:
        existing = list(self._trace_fields.get("tags", []))
        self._trace_fields["tags"] = existing + [tag for tag in tags if tag not in existing]
        return self

    @_safe
    def add_event(self, name: str, **attributes: Any) -> Span:
        self._events.append(
            {"name": name, "time_unix_nano": _now_ns(), "attributes": dict(attributes)}
        )
        return self

    @_safe
    def add_link(self, context: SpanContext, **attributes: Any) -> Span:
        """Relate this span to another that is not its parent.

        Used for retries, fan-in and agent sub-graphs, none of which the
        single-parent tree can express.
        """
        self._links.append(
            {
                "trace_id": context.trace_id,
                "span_id": context.span_id,
                "attributes": dict(attributes),
            }
        )
        return self

    @_safe
    def record_exception(self, exception: BaseException, *, escaped: bool = True) -> Span:
        """Record an exception and mark the span failed."""
        self._events.append(
            {
                "name": "exception",
                "time_unix_nano": _now_ns(),
                "attributes": {
                    semconv.EXCEPTION_TYPE: type(exception).__name__,
                    semconv.EXCEPTION_MESSAGE: str(exception)[:4_000],
                    semconv.EXCEPTION_STACKTRACE: "".join(
                        traceback.format_exception(
                            type(exception), exception, exception.__traceback__
                        )
                    )[:16_000],
                    "exception.escaped": escaped,
                },
            }
        )
        self._attributes.setdefault(semconv.EXCEPTION_TYPE, type(exception).__name__)
        self._attributes.setdefault(semconv.EXCEPTION_MESSAGE, str(exception)[:4_000])
        self.set_status("error", str(exception)[:2_000])
        return self

    @_safe
    def set_status(self, status: str, message: str | None = None) -> Span:
        if status not in {"unset", "ok", "error"}:
            raise ValueError(f"status must be unset, ok or error; got {status!r}")
        self._status = status
        if message:
            self._status_message = message[:4_000]
        return self

    # ------------------------------------------------------------------
    # AI-specific recording
    # ------------------------------------------------------------------

    @_safe
    def set_input(self, value: Any) -> Span:
        """Record the span's input payload, redacted and truncated."""
        if not self._client.config.capture_payloads:
            return self
        text = value if isinstance(value, str) else _to_json(value)
        self._attributes[semconv.INPUT_VALUE] = self._client.redactor.payload(text)
        self._attributes[semconv.INPUT_BYTES] = len(text.encode("utf-8"))
        return self

    @_safe
    def set_output(self, value: Any) -> Span:
        if not self._client.config.capture_payloads:
            return self
        text = value if isinstance(value, str) else _to_json(value)
        self._attributes[semconv.OUTPUT_VALUE] = self._client.redactor.payload(text)
        self._attributes[semconv.OUTPUT_BYTES] = len(text.encode("utf-8"))
        return self

    @_safe
    def record_model(
        self,
        *,
        provider: str,
        model: str,
        operation: str = "chat",
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        seed: int | None = None,
        response_model: str | None = None,
        finish_reasons: Iterable[str] | None = None,
        system_fingerprint: str | None = None,
    ) -> Span:
        """Record which model was called and how it was configured."""
        self._attributes[semconv.GEN_AI_SYSTEM] = provider
        self._attributes[semconv.GEN_AI_REQUEST_MODEL] = model
        self._attributes[semconv.GEN_AI_OPERATION_NAME] = operation
        if response_model:
            self._attributes[semconv.GEN_AI_RESPONSE_MODEL] = response_model
        for key, value in (
            (semconv.GEN_AI_REQUEST_TEMPERATURE, temperature),
            (semconv.GEN_AI_REQUEST_TOP_P, top_p),
            (semconv.GEN_AI_REQUEST_MAX_TOKENS, max_tokens),
            (semconv.GEN_AI_REQUEST_SEED, seed),
            (semconv.MODEL_SYSTEM_FINGERPRINT, system_fingerprint),
        ):
            if value is not None:
                self._attributes[key] = value
        if finish_reasons:
            self._attributes[semconv.GEN_AI_RESPONSE_FINISH_REASONS] = list(finish_reasons)
        if self.category == "custom":
            self.category = "chat_completion" if operation == "chat" else "llm_generation"
        return self

    @_safe
    def record_usage(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        source: str = "provider",
        raw: Mapping[str, Any] | None = None,
    ) -> Span:
        """Record token usage.

        Pass ``source="estimated"`` when the numbers came from a local
        tokeniser rather than the provider. The platform renders the two
        differently and excludes estimates from billing-grade totals.
        """
        usage: dict[str, Any] = {"source": source}
        for key, value in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("total_tokens", total_tokens),
            ("cached_input_tokens", cached_input_tokens),
            ("cache_write_tokens", cache_write_tokens),
            ("reasoning_tokens", reasoning_tokens),
        ):
            if value is not None:
                usage[key] = int(value)
        if raw is not None:
            usage["raw"] = dict(raw)
        self._usage = usage
        return self

    @_safe
    def record_first_token(self) -> Span:
        """Mark the arrival of the first streamed token.

        Time to first token is what a user perceives as latency for a streaming
        response, and it is invisible in the span's total duration.
        """
        if self._first_token_at is not None:
            return self
        self._first_token_at = _now_ns()
        elapsed_ms = (self._first_token_at - self._start_ns) / 1_000_000
        self._attributes[semconv.LATENCY_TIME_TO_FIRST_TOKEN_MS] = round(elapsed_ms, 3)
        self._events.append(
            {
                "name": semconv.EVENT_FIRST_TOKEN,
                "time_unix_nano": self._first_token_at,
                "attributes": {},
            }
        )
        return self

    @_safe
    def record_retrieval(
        self,
        *,
        query: str | None = None,
        documents: Iterable[Mapping[str, Any]] = (),
        rewritten_query: str | None = None,
        retriever_name: str | None = None,
        retriever_version: str | None = None,
        knowledge_base_version: str | None = None,
        search_type: str | None = None,
        top_k: int | None = None,
        filters: Mapping[str, Any] | None = None,
        embedding_model: str | None = None,
        embedding_dimensions: int | None = None,
        embedding_latency_ms: float | None = None,
        reranker_model: str | None = None,
        reranker_latency_ms: float | None = None,
        retrieval_latency_ms: float | None = None,
        context_tokens: int | None = None,
        context_truncated: bool = False,
    ) -> Span:
        """Record a retrieval step and its ranked results."""
        redactor = self._client.redactor
        prepared: list[dict[str, Any]] = []
        for index, document in enumerate(documents):
            item = dict(document)
            item.setdefault("rank", index)
            item.setdefault("document_id", str(item.get("id", f"doc-{index}")))
            item.pop("id", None)
            if self._client.config.capture_payloads and isinstance(item.get("content"), str):
                item["content"] = redactor.payload(item["content"])
            else:
                item.pop("content", None)
            prepared.append(item)

        payload: dict[str, Any] = {"documents": prepared, "context_truncated": context_truncated}
        for key, value in (
            ("query", redactor.payload(query) if query else None),
            ("rewritten_query", redactor.payload(rewritten_query) if rewritten_query else None),
            ("retriever_name", retriever_name),
            ("retriever_version", retriever_version),
            ("knowledge_base_version", knowledge_base_version),
            ("search_type", search_type),
            ("top_k", top_k),
            ("embedding_model", embedding_model),
            ("embedding_dimensions", embedding_dimensions),
            ("embedding_latency_ms", embedding_latency_ms),
            ("reranker_model", reranker_model),
            ("reranker_latency_ms", reranker_latency_ms),
            ("retrieval_latency_ms", retrieval_latency_ms),
            ("context_tokens", context_tokens),
        ):
            if value is not None:
                payload[key] = value
        if filters:
            payload["filters"] = dict(filters)
        self._retrieval = payload
        if self.category == "custom":
            self.category = "retrieval"
        return self

    @_safe
    def record_agent_step(
        self,
        *,
        agent_id: str,
        step_number: int,
        step_type: str = "observation",
        agent_version: str | None = None,
        goal: str | None = None,
        parent_step: int | None = None,
        decision_summary: str | None = None,
        tool_name: str | None = None,
        tool_arguments: Mapping[str, Any] | None = None,
        tool_result_ref: str | None = None,
        tool_status: str | None = None,
        handoff_target: str | None = None,
        memory_read_keys: Iterable[str] = (),
        memory_write_keys: Iterable[str] = (),
        retry_of: int | None = None,
        branch_id: str | None = None,
        loop_iteration: int | None = None,
        approval_required: bool = False,
        approval_status: str | None = None,
        termination_reason: str | None = None,
        max_steps: int | None = None,
    ) -> Span:
        """Record one step of an agent trajectory.

        ``decision_summary`` is a short, deliberately-published rationale. The
        SDK has no field for private chain-of-thought and does not collect it;
        see ``docs/concepts/agent-trajectory-observability.md``.
        """
        redactor = self._client.redactor
        step: dict[str, Any] = {
            "agent_id": agent_id,
            "step_number": int(step_number),
            "step_type": step_type,
            "approval_required": approval_required,
        }
        for key, value in (
            ("agent_version", agent_version),
            ("goal", redactor.payload(goal) if goal else None),
            ("parent_step", parent_step),
            ("decision_summary", redactor.payload(decision_summary) if decision_summary else None),
            ("tool_name", tool_name),
            ("tool_result_ref", tool_result_ref),
            ("tool_status", tool_status),
            ("handoff_target", handoff_target),
            ("retry_of", retry_of),
            ("branch_id", branch_id),
            ("loop_iteration", loop_iteration),
            ("approval_status", approval_status),
            ("termination_reason", termination_reason),
            ("max_steps", max_steps),
        ):
            if value is not None:
                step[key] = value
        if tool_arguments is not None:
            step["tool_arguments"] = redactor.attributes(tool_arguments)
        if memory_read_keys:
            step["memory_read_keys"] = list(memory_read_keys)
        if memory_write_keys:
            step["memory_write_keys"] = list(memory_write_keys)
        self._agent_step = step
        if self.category == "custom":
            self.category = "tool_call" if step_type == "tool_call" else "agent_decision"
        return self

    @_safe
    def set_lineage(
        self,
        *,
        prompt_name: str | None = None,
        prompt_version_id: str | None = None,
        prompt_version_label: str | None = None,
        prompt_variables: Mapping[str, Any] | None = None,
        model_config_id: str | None = None,
        dataset_name: str | None = None,
        dataset_version_id: str | None = None,
        dataset_record_id: str | None = None,
        knowledge_base_version: str | None = None,
        experiment_id: str | None = None,
        experiment_run_id: str | None = None,
    ) -> Span:
        """Attach the immutable versions that produced this span."""
        for key, value in (
            ("prompt_name", prompt_name),
            ("prompt_version_id", prompt_version_id),
            ("prompt_version_label", prompt_version_label),
            ("model_config_id", model_config_id),
            ("dataset_name", dataset_name),
            ("dataset_version_id", dataset_version_id),
            ("dataset_record_id", dataset_record_id),
            ("knowledge_base_version", knowledge_base_version),
            ("experiment_id", experiment_id),
            ("experiment_run_id", experiment_run_id),
        ):
            if value is not None:
                self._lineage[key] = value
        if prompt_variables is not None:
            self._lineage["prompt_variables"] = self._client.redactor.attributes(prompt_variables)
        return self

    # ------------------------------------------------------------------
    # nesting
    # ------------------------------------------------------------------

    def span(
        self,
        name: str,
        *,
        kind: str = "internal",
        category: str = "custom",
        **attributes: Any,
    ) -> Span:
        """Start a child span of this one."""
        return self._client._start_span(
            name,
            parent=self._context,
            kind=kind,
            category=category,
            attributes=attributes,
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> Span:
        self._token = use_context(self._context)
        self._token.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc is not None:
            self.record_exception(exc)
        self.end()
        if self._token is not None:
            self._token.__exit__(exc_type, exc, tb)
        # Never suppress the caller's exception.
        return False

    async def __aenter__(self) -> Span:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return self.__exit__(exc_type, exc, tb)

    @_safe
    def end(self) -> None:
        """Finish the span and queue it for export. Idempotent."""
        if self._ended:
            return
        self._ended = True
        self._end_ns = _now_ns()
        if self._status == "unset":
            self._status = "ok"
        self._client._submit(self)

    def to_wire(self) -> dict[str, Any]:
        """Serialise into the platform's native ingest format."""
        redactor = self._client.redactor
        payload: dict[str, Any] = {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self._parent_id,
            "name": self.name,
            "kind": self.kind,
            "category": self.category,
            "start_time_unix_nano": self._start_ns,
            "end_time_unix_nano": self._end_ns,
            "status": self._status,
            "attributes": redactor.attributes(self._attributes),
        }
        if self._status_message:
            payload["status_message"] = self._status_message
        if self._events:
            payload["events"] = self._events
        if self._links:
            payload["links"] = self._links
        if self._usage:
            payload["usage"] = self._usage
        if self._retrieval:
            payload["retrieval"] = self._retrieval
        if self._agent_step:
            payload["agent_step"] = self._agent_step
        if self._lineage:
            payload["lineage"] = self._lineage
        payload.update(self._trace_fields)
        return payload


class Trace(Span):
    """The root span of a logical AI request.

    A ``Trace`` is a ``Span`` with trace-level metadata attached, rather than a
    separate concept: the platform's data model has no "trace" object of its
    own, only a span with no parent.
    """

    @_safe
    def set_session(self, session_id: str) -> Trace:
        self._trace_fields["session_id"] = session_id
        return self

    @_safe
    def set_subject(self, subject_id: str) -> Trace:
        """Attach a *pseudonymous* end-user identifier.

        Pass an opaque id, never an email or name: it is stored unredacted so
        that per-user cost attribution works.
        """
        self._trace_fields["subject_id"] = subject_id
        return self

    @_safe
    def set_name(self, name: str) -> Trace:
        self.name = name[:512]
        self._trace_fields["trace_name"] = self.name
        return self


class Client:
    """Entry point: owns configuration, the exporter and the redactor."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        transport: Transport | None = None,
        **overrides: Any,
    ) -> None:
        self.config = config or from_env(**overrides)
        self.redactor = Redactor(
            redact_keys=self.config.redact_keys,
            allowed_keys=self.config.allowed_keys,
            max_chars=self.config.max_payload_chars,
        )
        self._resource = self._build_resource()
        self.exporter = BatchExporter(self.config, transport, resource=self._resource)
        if self.config.enabled:
            self.exporter.start()

    def _build_resource(self) -> dict[str, Any]:
        return {
            "service_name": self.config.service_name,
            "service_version": self.config.service_version,
            "service_instance_id": self.config.service_instance_id
            or f"{socket.gethostname()}:{os.getpid()}",
            "environment": self.config.environment,
            "sdk_name": SDK_NAME,
            "sdk_version": SDK_VERSION,
            "sdk_language": "python",
            "attributes": dict(self.config.resource_attributes),
        }

    # ------------------------------------------------------------------
    # span creation
    # ------------------------------------------------------------------

    def trace(
        self,
        name: str,
        *,
        session_id: str | None = None,
        subject_id: str | None = None,
        tags: Iterable[str] = (),
        release: str | None = None,
        parent: SpanContext | None = None,
        **attributes: Any,
    ) -> Trace:
        """Start a new logical AI request.

        When an inbound context exists (an HTTP header, an OpenTelemetry span,
        an enclosing trace) this becomes a child of it, so a "trace" started
        inside a distributed request correctly joins that request rather than
        starting a new one.
        """
        inherited = parent or resolve_parent()
        context = inherited.child() if inherited else self._sampled_root()
        root = Trace(
            self,
            name,
            context=context,
            parent_id=inherited.span_id if inherited else None,
            kind="server",
            category="workflow_step",
            attributes=attributes,
        )
        root._trace_fields["trace_name"] = root.name
        if session_id:
            root.set_session(session_id)
        if subject_id:
            root.set_subject(subject_id)
        if tags:
            root.set_tags(*tags)
        resolved_release = release or self.config.release
        if resolved_release:
            root.set_lineage()
            root._lineage["release"] = resolved_release
        if self.config.git_commit:
            root._lineage["git_commit"] = self.config.git_commit
        return root

    def span(
        self,
        name: str,
        *,
        kind: str = "internal",
        category: str = "custom",
        parent: SpanContext | None = None,
        **attributes: Any,
    ) -> Span:
        """Start a span under the ambient context."""
        return self._start_span(
            name,
            parent=parent or resolve_parent(),
            kind=kind,
            category=category,
            attributes=attributes,
        )

    def _start_span(
        self,
        name: str,
        *,
        parent: SpanContext | None,
        kind: str,
        category: str,
        attributes: Mapping[str, Any],
    ) -> Span:
        context = parent.child() if parent else self._sampled_root()
        return Span(
            self,
            name,
            context=context,
            parent_id=parent.span_id if parent else None,
            kind=kind,
            category=category,
            attributes=attributes,
        )

    def _sampled_root(self) -> SpanContext:
        """Create a root context, applying head sampling once per trace.

        Sampling is decided here and inherited by every child, so a sampled
        trace is complete. Sampling per span would produce traces with holes,
        which are worse than no trace at all.
        """
        sampled = self.config.sample_rate >= 1.0 or random.random() < self.config.sample_rate
        return SpanContext.new_root(sampled=sampled)

    def _submit(self, span: Span) -> None:
        if not self.config.enabled or not span.context.sampled:
            return
        self.exporter.submit(span.to_wire())

    # ------------------------------------------------------------------
    # decorators
    # ------------------------------------------------------------------

    def observe(
        self,
        name: str | None = None,
        *,
        kind: str = "internal",
        category: str = "custom",
        capture_arguments: bool = False,
        capture_result: bool = False,
    ) -> Callable[[F], F]:
        """Decorate a function so each call becomes a span.

        Works on sync and async functions, and on generators, because a
        codebase rarely has only one of the three.

        ``capture_arguments`` is off by default: function arguments are the
        most likely place for a raw secret or a customer's data to appear, and
        an opt-out default would leak it before anyone noticed.
        """

        def decorator(function: F) -> F:
            span_name = name or f"{function.__module__}.{function.__qualname__}"

            if inspect.iscoroutinefunction(function):

                @functools.wraps(function)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    with self.span(span_name, kind=kind, category=category) as span:
                        if capture_arguments:
                            span.set_input({"args": _brief(args), "kwargs": _brief(kwargs)})
                        result = await function(*args, **kwargs)
                        if capture_result:
                            span.set_output(_brief(result))
                        return result

                return async_wrapper  # type: ignore[return-value]

            @functools.wraps(function)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                with self.span(span_name, kind=kind, category=category) as span:
                    if capture_arguments:
                        span.set_input({"args": _brief(args), "kwargs": _brief(kwargs)})
                    result = function(*args, **kwargs)
                    if capture_result:
                        span.set_output(_brief(result))
                    return result

            return sync_wrapper  # type: ignore[return-value]

        return decorator

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def flush(self, timeout: float | None = None) -> bool:
        """Block until buffered spans are exported. Returns whether it completed."""
        return self.exporter.flush(timeout)

    def shutdown(self) -> None:
        """Flush and stop. Safe to call more than once."""
        self.exporter.shutdown()

    def stats(self) -> dict[str, Any]:
        return self.exporter.stats()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()


_default_client: Client | None = None


def init(**kwargs: Any) -> Client:
    """Create and install the process-wide client.

    Calling it twice shuts the previous client down first, so a re-init in a
    notebook or a test does not leak an exporter thread.
    """
    global _default_client
    if _default_client is not None:
        _default_client.shutdown()
    _default_client = Client(**kwargs)
    return _default_client


def get_client() -> Client:
    """Return the process-wide client, creating a default one if needed."""
    global _default_client
    if _default_client is None:
        _default_client = Client()
    return _default_client


def shutdown() -> None:
    global _default_client
    if _default_client is not None:
        _default_client.shutdown()
        _default_client = None


def _to_json(value: Any) -> str:
    import json

    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return repr(value)


def _brief(value: Any, limit: int = 2_000) -> Any:
    """Shorten a value for capture without raising on exotic objects."""
    try:
        text = _to_json(value)
    except Exception:
        text = repr(value)
    return text[:limit]
