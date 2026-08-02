#!/usr/bin/env python
"""Demo 1: a single instrumented model call.

Shows the smallest useful instrumentation: prompt rendering, a model call, a
streamed response, token accounting, and the two failure paths that matter
(provider error and streaming cancellation).

Run::

    export AIOBS_ENDPOINT=http://localhost:58000
    export AIOBS_API_KEY=aiobs_...
    python demos/simple-llm-app/main.py

With no API key it still runs: the SDK builds spans and drops them, which makes
the demo safe to execute anywhere.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiobs  # noqa: E402
from _shared.mock_provider import MockProvider, ProviderError  # noqa: E402

PROMPT_NAME = "support-reply"
PROMPT_VERSION_ID = "pmv_DEMO_SUPPORT_V1"
PROMPT_TEMPLATE = [
    {
        "role": "system",
        "content": "You are a concise customer support agent for an online retailer. "
        "Answer in at most three sentences.",
    },
    {"role": "user", "content": "{question}"},
]

QUESTIONS = (
    "How do refunds work for damaged items?",
    "How long does express shipping take?",
    "What does the warranty cover?",
)


def render_prompt(client: aiobs.Client, question: str) -> list[dict[str, str]]:
    """Render the prompt template, recording the version that was used.

    The render is its own span because prompt assembly is a real, sometimes
    slow step -- and because it is where the *version* lineage attaches, which
    is what makes a trace answer "which prompt produced this?" six weeks later.
    """
    with client.span("render-prompt", category="prompt_render") as span:
        span.set_lineage(
            prompt_name=PROMPT_NAME,
            prompt_version_id=PROMPT_VERSION_ID,
            prompt_version_label="v1",
            prompt_variables={"question": question},
        )
        rendered = [
            {"role": message["role"], "content": message["content"].format(question=question)}
            for message in PROMPT_TEMPLATE
        ]
        span.set_attribute("aiobs.prompt.message_count", len(rendered))
        return rendered


def answer(client: aiobs.Client, provider: MockProvider, question: str) -> str:
    """Answer one question inside a trace."""
    with client.trace(
        "customer-support-request",
        subject_id=f"user-{abs(hash(question)) % 1000}",
        tags=["demo", "simple-llm"],
    ) as trace:
        trace.set_input(question)
        messages = render_prompt(client, question)

        with trace.span("generate", kind="client", category="chat_completion") as span:
            span.record_model(
                provider=provider.provider,
                model=provider.model,
                temperature=0.2,
                max_tokens=512,
            )
            span.set_input(messages)
            span.set_lineage(prompt_name=PROMPT_NAME, prompt_version_id=PROMPT_VERSION_ID)
            try:
                response = provider.complete(messages, temperature=0.2)
            except ProviderError as exc:
                # Recording and re-raising: the span carries the failure, and
                # the application still sees the exception.
                span.record_exception(exc)
                span.set_attribute("aiobs.provider.status_code", exc.status_code)
                raise

            span.set_output(response.content)
            span.record_usage(
                input_tokens=response.usage["prompt_tokens"],
                output_tokens=response.usage["completion_tokens"],
                total_tokens=response.usage["total_tokens"],
                cached_input_tokens=response.usage.get("cached_tokens") or None,
                raw=response.usage,
            )
            span.set_attribute("gen_ai.response.id", response.id)
            span.set_attribute("gen_ai.response.finish_reasons", [response.finish_reason])

        trace.set_output(response.content)
        return response.content


def answer_streaming(
    client: aiobs.Client, provider: MockProvider, question: str, *, cancel_after: int | None = None
) -> str:
    """Answer with a streamed response, recording time to first token.

    Total duration says nothing about how a streamed response *felt*; the
    first-token timestamp is the number a user experiences. Cancellation is
    exercised here too, because an abandoned stream is the easiest way to leave
    a span unclosed.
    """
    with client.trace(
        "customer-support-request-streamed", tags=["demo", "streaming"]
    ) as trace:
        messages = render_prompt(client, question)
        with trace.span("generate-stream", kind="client", category="chat_completion") as span:
            span.record_model(provider=provider.provider, model=provider.model)
            span.set_input(messages)
            chunks: list[str] = []
            cancelled = False
            failed = False
            try:
                for index, chunk in enumerate(
                    provider.stream(messages, cancel_after=cancel_after)
                ):
                    if index == 0:
                        span.record_first_token()
                    chunks.append(chunk)
            except ProviderError as exc:
                # A stream that dies partway is still a span worth keeping: it
                # records how far it got before failing, which is exactly what
                # a partial-response investigation needs.
                failed = True
                span.record_exception(exc)
                span.set_attribute("aiobs.provider.status_code", exc.status_code)
            except GeneratorExit:  # pragma: no cover - defensive
                cancelled = True
                raise
            finally:
                text = "".join(chunks)
                span.set_output(text)
                span.set_attribute("aiobs.stream.chunks", len(chunks))
                if cancel_after is not None:
                    cancelled = True
                span.set_attribute("aiobs.stream.cancelled", cancelled)
                if cancelled and not failed:
                    # A cancelled stream is not an error, but it must not be
                    # reported as a clean completion either.
                    span.set_status("ok", "stream cancelled by the caller")
                    span.add_event("aiobs.stream_cancelled", chunks=len(chunks))
                # Streaming responses often omit usage; say so rather than
                # implying zero tokens were consumed.
                span.record_usage(
                    input_tokens=max(1, sum(len(m["content"]) for m in messages) // 4),
                    output_tokens=max(1, len(text) // 4),
                    source="estimated",
                )
        return "".join(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Simple instrumented LLM demo")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument(
        "--failure-rate",
        type=float,
        default=0.25,
        help="Fraction of calls that fail, to exercise the error path.",
    )
    arguments = parser.parse_args(argv)

    client = aiobs.init(service_name="simple-llm-demo", service_version="1.0.0")
    provider = MockProvider(failure_rate=arguments.failure_rate, cache_hit_rate=0.3)
    print(f"endpoint={client.config.endpoint} authenticated={bool(client.config.api_key)}")

    succeeded = failed = 0
    for iteration in range(arguments.iterations):
        for question in QUESTIONS:
            try:
                text = answer(client, provider, question)
                succeeded += 1
                print(f"  ok   {question[:44]:46} -> {text[:52]}...")
            except ProviderError as exc:
                failed += 1
                print(f"  FAIL {question[:44]:46} -> {exc}")

        # Streaming: a full response, and one the caller abandons early.
        answer_streaming(client, provider, QUESTIONS[0])
        answer_streaming(client, provider, QUESTIONS[1], cancel_after=3)

    client.shutdown()
    print(f"\n{succeeded} succeeded, {failed} failed (failures are deliberate)")
    print(f"exporter: {client.stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
