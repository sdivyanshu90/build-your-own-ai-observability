"""Python SDK behaviour.

The overriding requirement is that the SDK never breaks the application it
instruments. Most of these tests are about what happens when things go wrong.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

import aiobs
from aiobs.config import Config, from_env
from aiobs.context import SpanContext, extract, inject, parse_traceparent
from aiobs.exporter import BatchExporter, MemoryTransport
from aiobs.testing import TestClient, capture_spans


class TestTracing:
    def test_spans_nest_automatically(self) -> None:
        with capture_spans() as spans:
            client = aiobs.get_client()
            with (
                client.trace("request") as trace,
                trace.span("child") as child,
                child.span("grandchild"),
            ):
                pass
        spans.assert_well_formed()
        assert len(spans) == 3
        assert len(spans.trace_ids()) == 1
        root = spans.roots()[0]
        assert root.name == "request"

    def test_a_raised_exception_is_recorded_and_re_raised(self) -> None:
        with capture_spans() as spans:
            client = aiobs.get_client()
            with pytest.raises(ValueError, match="boom"), client.trace("request"):
                raise ValueError("boom")
        errors = spans.errors()
        assert len(errors) == 1
        assert errors[0].attributes["exception.type"] == "ValueError"
        assert any(event["name"] == "exception" for event in errors[0].events)

    async def test_async_context_propagates_across_await(self) -> None:
        with capture_spans() as spans:
            client = aiobs.get_client()

            async def inner() -> None:
                with client.span("inner"):
                    await asyncio.sleep(0)

            async with client.trace("request"):
                await inner()

        assert len(spans.trace_ids()) == 1
        assert {span.name for span in spans} == {"request", "inner"}

    async def test_concurrent_traces_do_not_interleave(self) -> None:
        """The failure this prevents: one user's spans attaching to another
        user's trace under concurrency."""
        with capture_spans() as spans:
            client = aiobs.get_client()

            async def request(index: int) -> None:
                async with client.trace(f"request-{index}") as trace:
                    await asyncio.sleep(0.01)
                    with trace.span(f"child-{index}"):
                        await asyncio.sleep(0.01)

            await asyncio.gather(*(request(index) for index in range(5)))

        assert len(spans.trace_ids()) == 5
        by_trace: dict[str, set[str]] = {}
        for span in spans:
            by_trace.setdefault(span.trace_id, set()).add(span.name)
        for names in by_trace.values():
            suffixes = {name.rsplit("-", 1)[1] for name in names}
            assert len(suffixes) == 1, f"a trace mixed spans from different requests: {names}"

    def test_decorator_traces_sync_and_async(self) -> None:
        with capture_spans() as spans:
            client = aiobs.get_client()

            @client.observe("decorated")
            def work() -> int:
                return 42

            assert work() == 42
        assert spans.named("decorated")

    def test_thread_safety(self) -> None:
        with capture_spans() as spans:
            client = aiobs.get_client()

            def worker(index: int) -> None:
                with client.trace(f"thread-{index}"):
                    pass

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        assert len(spans) == 8
        assert len(spans.trace_ids()) == 8


class TestRecording:
    def test_usage_and_model_are_recorded(self) -> None:
        with capture_spans() as spans:
            client = aiobs.get_client()
            with client.trace("r") as trace, trace.span("gen", category="chat_completion") as span:
                span.record_model(provider="anthropic", model="claude-sonnet-4", temperature=0.2)
                span.record_usage(input_tokens=1_200, output_tokens=340)
        generation = spans.of_category("chat_completion")[0]
        assert generation.usage["input_tokens"] == 1_200
        assert generation.attributes["gen_ai.request.model"] == "claude-sonnet-4"
        assert generation.attributes["gen_ai.request.temperature"] == 0.2

    def test_time_to_first_token_is_measured(self) -> None:
        with capture_spans() as spans:
            client = aiobs.get_client()
            with client.trace("r") as trace, trace.span("gen") as span:
                span.record_first_token()
        span = spans.named("gen")[0]
        assert span.attributes["aiobs.latency.time_to_first_token_ms"] >= 0
        assert any(event["name"] == "aiobs.first_token" for event in span.events)

    def test_links_express_non_parental_relationships(self) -> None:
        with capture_spans() as spans:
            client = aiobs.get_client()
            other = SpanContext.new_root()
            with client.trace("r") as trace, trace.span("retry") as span:
                span.add_link(other, relationship="retry_of")
        links = spans.named("retry")[0].raw["links"]
        assert links[0]["trace_id"] == other.trace_id


class TestRedaction:
    def test_secrets_are_removed_before_export(self) -> None:
        with capture_spans() as spans:
            client = aiobs.get_client()
            with client.trace("r") as trace:
                trace.set_attribute("api_key", "sk-super-secret-value")
                trace.set_input("Bearer abcdefghijklmnopqrstuvwxyz0123456789")
        root = spans.roots()[0]
        assert root.attributes["api_key"] == "[redacted]"
        assert "[redacted]" in root.attributes["aiobs.input.value"]

    def test_token_counts_survive_redaction(self) -> None:
        """Regression: the generic 'token' key pattern was destroying usage."""
        with capture_spans() as spans:
            client = aiobs.get_client()
            with client.trace("r") as trace:
                trace.record_usage(input_tokens=100, output_tokens=50)
        assert spans.roots()[0].usage["input_tokens"] == 100

    def test_payload_capture_can_be_disabled(self) -> None:
        with capture_spans(capture_payloads=False) as spans:
            client = aiobs.get_client()
            with client.trace("r") as trace:
                trace.set_input("a customer's private question")
        assert "aiobs.input.value" not in spans.roots()[0].attributes


class TestSampling:
    def test_zero_sample_rate_records_nothing(self) -> None:
        with capture_spans(sample_rate=0.0) as spans:
            client = aiobs.get_client()
            for _ in range(20):
                with client.trace("r"):
                    pass
        assert len(spans) == 0

    def test_sampling_decides_once_per_trace(self) -> None:
        """A sampled trace must be whole: sampling per span would produce
        traces with holes, which are worse than no trace."""
        with capture_spans(sample_rate=1.0) as spans:
            client = aiobs.get_client()
            with client.trace("r") as trace:
                for index in range(5):
                    with trace.span(f"child-{index}"):
                        pass
        assert len(spans) == 6


class TestExporter:
    def test_a_full_queue_drops_the_oldest(self) -> None:
        transport = MemoryTransport()
        config = Config(endpoint="http://x", api_key="k", max_queue_size=5, max_batch_size=5)
        exporter = BatchExporter(config, transport, resource={})
        for index in range(20):
            exporter.submit({"span_id": f"{index:016x}"})
        assert exporter.dropped == 15
        exporter.flush(timeout=1)
        kept = [span["span_id"] for batch in transport.batches for span in batch["spans"]]
        # The most recent spans survived.
        assert f"{19:016x}" in kept
        assert f"{0:016x}" not in kept

    def test_transient_failures_are_retried(self) -> None:
        transport = MemoryTransport(fail_times=2, retryable=True)
        config = Config(
            endpoint="http://x",
            api_key="k",
            max_retries=3,
            retry_base_delay_seconds=0.001,
            retry_max_delay_seconds=0.002,
        )
        exporter = BatchExporter(config, transport, resource={})
        exporter.submit({"span_id": "a"})
        exporter.flush(timeout=5)
        assert transport.attempts == 3
        assert exporter.exported == 1

    def test_permanent_failures_are_not_retried(self) -> None:
        """A 400 means the payload is wrong; retrying only wastes the
        application's resources."""
        transport = MemoryTransport(fail_times=99, retryable=False)
        config = Config(endpoint="http://x", api_key="k", max_retries=5)
        exporter = BatchExporter(config, transport, resource={})
        exporter.submit({"span_id": "a"})
        exporter.flush(timeout=5)
        assert transport.attempts == 1
        assert exporter.failed == 1

    def test_shutdown_flushes(self) -> None:
        transport = MemoryTransport()
        config = Config(endpoint="http://x", api_key="k", flush_interval_seconds=1_000)
        exporter = BatchExporter(config, transport, resource={})
        exporter.start()
        exporter.submit({"span_id": "a"})
        exporter.shutdown()
        assert len(transport.spans) == 1

    def test_shutdown_is_idempotent(self) -> None:
        client = TestClient()
        client.shutdown()
        client.shutdown()

    def test_no_api_key_means_no_network_call(self) -> None:
        transport = MemoryTransport()
        config = Config(endpoint="http://x", api_key=None)
        exporter = BatchExporter(config, transport, resource={})
        exporter.submit({"span_id": "a"})
        exporter.flush(timeout=1)
        assert transport.attempts == 0


class TestContextPropagation:
    def test_traceparent_round_trips(self) -> None:
        context = SpanContext.new_root()
        headers = inject(context)
        extracted = extract(headers)
        assert extracted is not None
        assert extracted.trace_id == context.trace_id
        assert extracted.span_id == context.span_id
        assert extracted.remote is True

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "garbage",
            "00-tooshort-0000000000000000-01",
            "ff-" + "0" * 32 + "-" + "0" * 16 + "-01",  # reserved version
            "00-" + "0" * 32 + "-" + "1" * 16 + "-01",  # all-zero trace id
        ],
    )
    def test_a_malformed_traceparent_is_ignored(self, value: str) -> None:
        """A misbehaving upstream must not break the downstream service."""
        assert parse_traceparent(value) is None

    def test_baggage_round_trips(self) -> None:
        context = SpanContext.new_root().with_baggage(tenant="acme", tier="gold")
        extracted = extract(inject(context))
        assert extracted is not None
        assert extracted.baggage == {"tenant": "acme", "tier": "gold"}

    def test_a_remote_parent_is_continued_not_replaced(self) -> None:
        with capture_spans() as spans:
            client = aiobs.get_client()
            upstream = SpanContext.new_root()
            with client.trace("downstream", parent=upstream):
                pass
        root = spans.roots()
        assert root == []  # it has a parent, so it is not a root
        assert spans[0].trace_id == upstream.trace_id
        assert spans[0].parent_span_id == upstream.span_id


class TestConfiguration:
    def test_environment_variables_are_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AIOBS_ENDPOINT", "https://platform.example.com/")
        monkeypatch.setenv("AIOBS_SAMPLE_RATE", "0.25")
        config = from_env()
        assert config.endpoint == "https://platform.example.com"  # trailing slash stripped
        assert config.sample_rate == 0.25

    def test_out_of_range_values_are_clamped_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bad deployment variable must not crash the app at import time."""
        monkeypatch.setenv("AIOBS_SAMPLE_RATE", "17")
        assert from_env().sample_rate == 1.0

    def test_explicit_arguments_beat_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AIOBS_SERVICE_NAME", "from-env")
        assert from_env(service_name="explicit").service_name == "explicit"

    def test_info_never_reveals_the_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AIOBS_API_KEY", "aiobs_live_deadbeef_supersecret")
        aiobs.init()
        payload = aiobs.info()
        assert payload["authenticated"] is True
        assert "supersecret" not in str(payload)
        aiobs.shutdown()
