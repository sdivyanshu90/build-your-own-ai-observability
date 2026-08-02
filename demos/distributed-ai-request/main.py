#!/usr/bin/env python
"""Demo 4: one logical AI request across three services.

    client -> api-gateway  (HTTP, traceparent)
           -> queue        (message headers carry the context)
           -> inference-worker
           -> model call

Demonstrates the two propagation mechanisms that matter and are usually got
wrong:

**HTTP** -- ``traceparent`` on the request, extracted by the receiving service's
middleware. Without it each service starts its own trace and the request looks
like three unrelated things.

**Queue** -- the same header carried as a message attribute. This is the one
people forget, because the queue client does not do it automatically. The
consumer span is a ``CONSUMER`` kind whose parent is the producer, and it also
carries a *link* back to the producer, which is how fan-in is expressed when one
consumer batch spans many producers.

Everything runs in one process on two local ports, so the demo needs no
infrastructure while still exercising real HTTP and real header propagation.

Run::

    python demos/distributed-ai-request/main.py
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402

import aiobs  # noqa: E402
from aiobs.context import SpanContext, extract, inject, use_context  # noqa: E402
from aiobs.integrations.fastapi import instrument_app  # noqa: E402
from _shared.mock_provider import MockProvider  # noqa: E402

GATEWAY_PORT = 58101
WORKER_PORT = 58102

#: Stands in for Kafka/SQS/Redpanda. What matters is that the *headers* travel
#: with the message, which is the part a real queue client will not do for you.
WORK_QUEUE: "queue.Queue[dict[str, Any]]" = queue.Queue()


def build_gateway(client: aiobs.Client) -> FastAPI:
    """The public-facing service: receives HTTP, enqueues work."""
    app = FastAPI(title="api-gateway")
    instrument_app(app, client=client)

    @app.post("/api/assist")
    async def assist(request: Request) -> dict[str, Any]:
        payload = await request.json()
        question = str(payload.get("question", ""))

        with client.span("validate-request", category="workflow_step") as span:
            span.set_attribute("aiobs.request.question_length", len(question))
            if not question:
                span.set_status("error", "question is required")
                return {"error": "question is required"}

        with client.span(
            "publish assist.requests", kind="producer", category="queue_operation"
        ) as span:
            span.set_attributes(
                {
                    "messaging.system": "demo-queue",
                    "messaging.destination.name": "assist.requests",
                    "messaging.operation.name": "send",
                }
            )
            # This is the whole trick: the current context is serialised into
            # the message headers so the consumer can continue the trace.
            headers = inject(span.context)
            WORK_QUEUE.put({"question": question, "headers": headers})
            span.set_attribute("messaging.message.body.size", len(question))

        deadline = time.time() + 10
        while time.time() < deadline:
            if RESULTS.get(question) is not None:
                break
            time.sleep(0.02)
        return {"answer": RESULTS.get(question, "timed out"), "trace_id": span.trace_id}

    return app


RESULTS: dict[str, str] = {}


def build_worker(client: aiobs.Client, provider: MockProvider) -> FastAPI:
    """The inference service: consumes from the queue, calls the model."""
    app = FastAPI(title="inference-worker")
    instrument_app(app, client=client)

    @app.get("/internal/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def consume_forever(client: aiobs.Client, provider: MockProvider, stop: threading.Event) -> None:
    """Queue consumer loop, continuing the producer's trace."""
    while not stop.is_set():
        try:
            message = WORK_QUEUE.get(timeout=0.1)
        except queue.Empty:
            continue

        parent = extract(message["headers"])
        question = message["question"]

        # kind=consumer with the producer as parent is what makes the two
        # halves of the queue hop line up in the waterfall.
        span = client.span(
            "consume assist.requests",
            kind="consumer",
            category="queue_operation",
            parent=parent,
        )
        span.set_attributes(
            {
                "messaging.system": "demo-queue",
                "messaging.destination.name": "assist.requests",
                "messaging.operation.name": "receive",
            }
        )
        if parent is not None:
            # A link in addition to the parent: when a consumer processes a
            # batch it has one parent but many producers, and the links are the
            # only way to express that.
            span.add_link(parent, relationship="queue_producer")

        with use_context(span.context):
            try:
                with client.span("generate", kind="client", category="chat_completion") as call:
                    call.record_model(provider=provider.provider, model=provider.model)
                    messages = [{"role": "user", "content": question}]
                    call.set_input(messages)
                    response = provider.complete(messages)
                    call.record_first_token()
                    call.set_output(response.content)
                    call.record_usage(
                        input_tokens=response.usage["prompt_tokens"],
                        output_tokens=response.usage["completion_tokens"],
                        raw=response.usage,
                    )
                RESULTS[question] = response.content
                span.set_status("ok")
            except Exception as exc:  # noqa: BLE001 - recorded, loop continues
                span.record_exception(exc)
                RESULTS[question] = f"error: {exc}"
            finally:
                span.end()


@contextmanager
def serve(app: FastAPI, port: int) -> Iterator[None]:
    """Run a uvicorn server in a thread for the life of the block."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError(f"server on port {port} did not start")
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Distributed trace propagation demo")
    parser.add_argument("--requests", type=int, default=3)
    arguments = parser.parse_args(argv)

    gateway_client = aiobs.Client(service_name="api-gateway", service_version="1.2.0")
    worker_client = aiobs.Client(service_name="inference-worker", service_version="1.2.0")
    provider = MockProvider()

    print(
        f"endpoint={gateway_client.config.endpoint} "
        f"authenticated={bool(gateway_client.config.api_key)}"
    )

    stop = threading.Event()
    consumer = threading.Thread(
        target=consume_forever, args=(worker_client, provider, stop), daemon=True
    )
    consumer.start()

    trace_ids: list[str] = []
    with serve(build_gateway(gateway_client), GATEWAY_PORT):
        with serve(build_worker(worker_client, provider), WORKER_PORT):
            with httpx.Client(base_url=f"http://127.0.0.1:{GATEWAY_PORT}", timeout=20) as http:
                for index in range(arguments.requests):
                    question = f"How do refunds work? (request {index})"
                    # A client-side root span, so the trace starts before the
                    # first HTTP hop -- which is where real user latency starts.
                    with gateway_client.trace(
                        "distributed-ai-request", tags=["demo", "distributed"]
                    ) as trace:
                        trace_ids.append(trace.trace_id)
                        with trace.span(
                            "POST /api/assist", kind="client", category="http_request"
                        ) as call:
                            call.set_attributes(
                                {
                                    "http.request.method": "POST",
                                    "url.full": f"http://127.0.0.1:{GATEWAY_PORT}/api/assist",
                                }
                            )
                            response = http.post(
                                "/api/assist",
                                json={"question": question},
                                # Propagating the caller's context is what ties
                                # the gateway's server span to this client span.
                                headers=call.headers(),
                            )
                            call.set_attribute(
                                "http.response.status_code", response.status_code
                            )
                        body = response.json()
                    print(f"  request {index}: {str(body.get('answer'))[:56]}...")

    stop.set()
    consumer.join(timeout=3)
    gateway_client.shutdown()
    worker_client.shutdown()

    print(f"\ntrace ids: {[value[:16] for value in trace_ids]}")
    print(f"gateway exporter: {gateway_client.stats()}")
    print(f"worker exporter:  {worker_client.stats()}")
    print(
        "\nEach trace id above should show spans from BOTH services "
        "(api-gateway and inference-worker) in one waterfall."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
