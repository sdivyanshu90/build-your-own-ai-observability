# Python SDK

```console
$ pip install aiobs
```

```bash
export AIOBS_ENDPOINT=https://observability.example.com
export AIOBS_API_KEY=aiobs_live_...
export AIOBS_SERVICE_NAME=checkout-assistant
```

## The shape of it

```python
from aiobs import Client

client = Client()   # reads AIOBS_* from the environment

with client.trace("answer-question", session_id=session, subject_id=user_hash) as trace:
    with trace.span("retrieve", category="retrieval") as span:
        documents = retriever.search(question)
        span.record_retrieval(query=question, documents=documents)

    with trace.span("generate", category="chat_completion") as span:
        span.record_model(provider="openai", model="gpt-4o", temperature=0.2)
        response = openai.chat.completions.create(...)
        span.record_usage(input_tokens=..., output_tokens=..., source="provider")
```

A `with` block starts the span, ends it on exit, and records an exception if one
escaped — including re-raising it. **Instrumentation never swallows your
application's errors.**

## It cannot break your application

Every public method is wrapped so that a failure inside the SDK is logged and
swallowed. An unreachable platform, a serialisation error, a value the wire
schema rejects — none of them propagate.

```python
span.set_attribute("bad", circular_reference)   # logged, ignored, no exception
```

This is not defensive programming for its own sake. Instrumentation that can
take down the thing it observes is worse than no instrumentation, and the first
time it happens nobody trusts the tool again.

## Context propagation

Automatic within a process, via `contextvars` — so it works across `await`
points and in threads.

Across services, inject the W3C headers:

```python
headers = span.headers()          # traceparent, tracestate, baggage
requests.post(url, headers=headers)
```

On the receiving side, the FastAPI integration extracts them for you; otherwise:

```python
from aiobs import extract, use_context

with use_context(extract(request.headers)):
    with client.trace("downstream-work") as trace:
        ...
```

`client.trace()` inside an existing context becomes a **child** of it, not a new
root. A "trace" started inside a distributed request belongs to that request.

## Recording usage and cost

```python
span.record_usage(
    input_tokens=1200,
    output_tokens=340,
    cached_input_tokens=800,
    source="provider",     # provider | estimated | reconciled | missing
)
```

`source` matters. A cost computed from `estimated` counts is reported as
estimated all the way to the dashboard. See
[concepts/cost-attribution.md](../concepts/cost-attribution.md).

The provider adapters normalise a raw response for you:

```python
from aiobs.integrations.openai import record_openai_response
record_openai_response(span, response)
```

## Streaming

```python
with trace.span("generate", category="chat_completion") as span:
    for chunk in stream:
        span.record_first_token()      # idempotent; only the first call counts
        yield chunk
```

Time to first token is what a user perceives. Total duration is what your
infrastructure bill perceives. They are different numbers and both are recorded.

## Retrieval and agents

See [concepts/retrieval.md](../concepts/retrieval.md) and
[concepts/agent-trajectories.md](../concepts/agent-trajectories.md) for what the
fields mean. The calls are `span.record_retrieval(...)` and
`span.record_agent_step(...)`.

## Lineage

```python
span.set_lineage(
    prompt_name="support-reply",
    prompt_version_id=prompt.version_id,   # the resolved id, never the alias
    model_config_id=config.version_id,
)
```

## Redaction

On by default, before anything leaves your process:

```python
client = Client(
    redact_keys=["internal.customer_ref"],
    allowed_keys=[...],          # allowlist mode: everything else is dropped
    detectors=[("employee_id", lambda text: EMPLOYEE_ID.search(text) is not None)],
    capture_payloads=False,      # record shape, not text
)
```

See [security/data-handling.md](../security/data-handling.md).

## Sampling

```python
client = Client(sample_rate=0.1)     # 10% of traces, whole
```

Decided once per trace and inherited by every child. A sampled trace is
complete; sampling per span produces traces with holes.

## Integrations

```python
from aiobs.integrations.fastapi import instrument_fastapi
instrument_fastapi(app, client)      # extracts context, traces every request

from aiobs.integrations.openai import instrument_openai
instrument_openai(client)            # wraps the client, records model and usage

from aiobs.integrations.retrieval import retrieval_span
with retrieval_span(client, "vector-search") as span:
    ...
```

## Testing your instrumentation

```python
from aiobs import capture_spans

def test_records_the_retrieval():
    with capture_spans() as captured:
        answer_question("how do refunds work?")

    captured.assert_well_formed()      # ids, durations, parentage
    assert len(captured.of_category("retrieval")) == 1
    assert captured.named("generate")[0]["usage"]["input_tokens"] > 0
```

`assert_well_formed()` checks the structural invariants the platform enforces on
ingest, so broken instrumentation fails in your test suite rather than as a
batch of rejected spans in production.

## Shutdown

```python
client.shutdown()      # flush and stop; safe to call twice
```

Registered with `atexit` automatically. In a serverless environment, call
`client.flush()` before the handler returns — the process may be frozen the
instant it does.

## Configuration

Every setting is an `AIOBS_*` environment variable or a `Client(...)` keyword.
The ones worth knowing:

| Variable                       | Default                  |                                      |
| ------------------------------ | ------------------------ | ------------------------------------ |
| `AIOBS_ENDPOINT`               | `http://localhost:58000` |                                      |
| `AIOBS_API_KEY`                | —                        |                                      |
| `AIOBS_SERVICE_NAME`           | `unknown_service`        | set it                               |
| `AIOBS_ENABLED`                | `true`                   | `false` disables everything          |
| `AIOBS_SAMPLE_RATE`            | `1.0`                    |                                      |
| `AIOBS_CAPTURE_PAYLOADS`       | `true`                   | `false` records shape, not text      |
| `AIOBS_MAX_BATCH_SIZE`         | `200`                    |                                      |
| `AIOBS_FLUSH_INTERVAL_SECONDS` | `2.0`                    |                                      |
| `AIOBS_MAX_QUEUE_SIZE`         | `10000`                  | full queue drops oldest, and says so |
| `AIOBS_COMPRESS`               | `true`                   | gzip                                 |

## See also

- [Tutorial: your first trace](../tutorials/first-trace.md)
- [TypeScript SDK](typescript.md)
- [OTLP without an SDK](otlp.md)
