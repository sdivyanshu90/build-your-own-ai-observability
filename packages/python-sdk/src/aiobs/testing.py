"""Test utilities.

Instrumented code should be testable without a running platform, and assertions
should be about *what was recorded*, not about whether an HTTP call happened.

    def test_answers_are_traced():
        with capture_spans() as spans:
            answer_question("how do refunds work?")

        generation = spans.of_category("chat_completion")[0]
        assert generation.attributes["gen_ai.request.model"] == "claude-sonnet-4"
        assert generation.usage["input_tokens"] > 0
        assert spans.trace_ids() == {generation.trace_id}
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .config import Config
from .exporter import MemoryTransport
from .tracer import Client

__all__ = ["CapturedSpan", "CapturedSpans", "TestClient", "capture_spans"]


@dataclass(frozen=True, slots=True)
class CapturedSpan:
    """One exported span, in a shape convenient for assertions."""

    raw: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.raw["name"])

    @property
    def trace_id(self) -> str:
        return str(self.raw["trace_id"])

    @property
    def span_id(self) -> str:
        return str(self.raw["span_id"])

    @property
    def parent_span_id(self) -> str | None:
        return self.raw.get("parent_span_id")

    @property
    def category(self) -> str:
        return str(self.raw.get("category", "custom"))

    @property
    def kind(self) -> str:
        return str(self.raw.get("kind", "internal"))

    @property
    def status(self) -> str:
        return str(self.raw.get("status", "unset"))

    @property
    def attributes(self) -> dict[str, Any]:
        return dict(self.raw.get("attributes", {}))

    @property
    def usage(self) -> dict[str, Any]:
        return dict(self.raw.get("usage") or {})

    @property
    def retrieval(self) -> dict[str, Any]:
        return dict(self.raw.get("retrieval") or {})

    @property
    def agent_step(self) -> dict[str, Any]:
        return dict(self.raw.get("agent_step") or {})

    @property
    def lineage(self) -> dict[str, Any]:
        return dict(self.raw.get("lineage") or {})

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self.raw.get("events") or [])

    @property
    def duration_ms(self) -> float | None:
        end = self.raw.get("end_time_unix_nano")
        if end is None:
            return None
        return (int(end) - int(self.raw["start_time_unix_nano"])) / 1_000_000

    def __repr__(self) -> str:
        return f"<CapturedSpan {self.name!r} category={self.category} status={self.status}>"


class CapturedSpans(Sequence[CapturedSpan]):
    """A queryable list of captured spans.

    Reading it flushes the exporter first. Without that, a test would have to
    remember to flush before every assertion and would be flaky when it forgot
    -- the single most annoying property a tracing SDK's test helper can have.
    """

    def __init__(self, transport: MemoryTransport, client: Any | None = None) -> None:
        self._transport = transport
        self._client = client

    def _flush(self) -> None:
        if self._client is not None:
            self._client.flush(timeout=2.0)

    def _spans(self) -> list[CapturedSpan]:
        self._flush()
        return [CapturedSpan(raw) for raw in self._transport.spans]

    def __len__(self) -> int:
        self._flush()
        return len(self._transport.spans)

    def __getitem__(self, index: int) -> CapturedSpan:  # type: ignore[override]
        return self._spans()[index]

    def __iter__(self) -> Iterator[CapturedSpan]:
        return iter(self._spans())

    def __repr__(self) -> str:
        return f"<CapturedSpans {len(self)} spans>"

    # --- queries -------------------------------------------------------

    def named(self, name: str) -> list[CapturedSpan]:
        return [span for span in self._spans() if span.name == name]

    def of_category(self, category: str) -> list[CapturedSpan]:
        return [span for span in self._spans() if span.category == category]

    def errors(self) -> list[CapturedSpan]:
        return [span for span in self._spans() if span.status == "error"]

    def roots(self) -> list[CapturedSpan]:
        return [span for span in self._spans() if not span.parent_span_id]

    def children_of(self, span_id: str) -> list[CapturedSpan]:
        return [span for span in self._spans() if span.parent_span_id == span_id]

    def trace_ids(self) -> set[str]:
        return {span.trace_id for span in self._spans()}

    def total_tokens(self) -> int:
        return sum(int(span.usage.get("total_tokens") or 0) for span in self._spans())

    def assert_well_formed(self) -> None:
        """Assert the structural invariants the platform will enforce on ingest.

        Running this in a unit test catches broken instrumentation before it
        produces a batch of rejected spans in production.
        """
        spans = self._spans()
        assert spans, "no spans were recorded"
        by_id = {span.span_id: span for span in spans}
        for span in spans:
            assert len(span.trace_id) == 32, f"{span.name}: malformed trace id"
            assert len(span.span_id) == 16, f"{span.name}: malformed span id"
            assert span.raw.get("end_time_unix_nano") is not None, (
                f"{span.name}: span was never ended"
            )
            assert span.duration_ms is not None and span.duration_ms >= 0, (
                f"{span.name}: negative duration"
            )
            parent = span.parent_span_id
            if parent and parent in by_id:
                assert by_id[parent].trace_id == span.trace_id, (
                    f"{span.name}: child and parent are in different traces"
                )


class TestClient(Client):
    """A client that exports into memory instead of over the network."""

    #: pytest collects any class named Test*; this one is a helper, not a suite.
    __test__ = False

    def __init__(self, **overrides: Any) -> None:
        transport = MemoryTransport()
        config = Config(
            endpoint="http://test.invalid",
            api_key="test-key",
            service_name=str(overrides.pop("service_name", "test-service")),
            # Flush inline so a test never has to sleep waiting for a thread.
            max_batch_size=1,
            flush_interval_seconds=0.01,
            enabled=True,
            **overrides,
        )
        super().__init__(config, transport=transport)
        self.transport = transport
        self.captured = CapturedSpans(transport, client=self)

    def collect(self) -> CapturedSpans:
        """Flush and return everything recorded so far."""
        self.flush(timeout=2.0)
        return self.captured


@contextmanager
def capture_spans(**overrides: Any) -> Iterator[CapturedSpans]:
    """Install a memory-backed client for the duration of a block.

    Restores whatever client was previously installed, so tests do not leak
    configuration into each other.
    """
    from . import tracer

    previous = tracer._default_client
    client = TestClient(**overrides)
    tracer._default_client = client
    try:
        yield client.captured
    finally:
        client.flush(timeout=2.0)
        client.shutdown()
        tracer._default_client = previous
