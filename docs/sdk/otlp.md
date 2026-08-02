# OTLP without an SDK

The platform accepts standard OTLP. If you already run the OpenTelemetry
Collector or an OTel SDK, point it here and you are done.

## Endpoint

```
POST /v1/otlp/v1/traces
```

Accepts `application/x-protobuf` and `application/json`. Gzip is supported.

Authenticate with `X-API-Key` or `Authorization: Bearer`.

## Collector configuration

```yaml
exporters:
  otlphttp/aiobs:
    endpoint: https://observability.example.com/v1/otlp
    headers:
      x-api-key: ${AIOBS_API_KEY}
    compression: gzip

service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [otlphttp/aiobs]
```

## What you get without the SDK

Everything structural: trace trees, durations, service names, span kinds, status,
events, links, the critical path, and W3C context propagation. Standard OTel
attributes are understood as-is.

## What you need attributes for

The AI-specific parts. Use the OTel GenAI conventions where they exist:

```
gen_ai.system                openai
gen_ai.request.model         gpt-4o
gen_ai.response.model        gpt-4o-2026-05-13
gen_ai.operation.name        chat
gen_ai.usage.input_tokens    1200
gen_ai.usage.output_tokens   340
```

And `aiobs.*` for what they do not cover:

```
aiobs.span.category              chat_completion | retrieval | tool_call | …
aiobs.usage.source               provider | estimated | reconciled | missing
aiobs.latency.time_to_first_token_ms
aiobs.prompt.version_id
aiobs.model.config_id
aiobs.retrieval.query
aiobs.retrieval.documents        JSON array
aiobs.agent.step_number
aiobs.agent.tool.name
```

The full registry is
`packages/shared-schemas/python/aiobs_schemas/semconv.py`, and
[concepts/semantic-conventions.md](../concepts/semantic-conventions.md)
explains the naming rules.

`aiobs.span.category` is the highest-value single attribute: it is what tells
the UI that a span is a generation rather than a retrieval, without
pattern-matching on names.

## The native endpoint

```
POST /v1/ingest/spans
```

A JSON batch with a first-class shape for usage, retrieval and agent payloads
rather than encoding them into attributes. The SDKs use it. Use it directly if
you are writing an SDK for another language:

```json
{
  "resource": {
    "service_name": "checkout-assistant",
    "service_version": "1.4.2",
    "environment": "production",
    "sdk_name": "my-sdk",
    "sdk_version": "0.1.0",
    "sdk_language": "go"
  },
  "spans": [
    {
      "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
      "span_id": "00f067aa0ba902b7",
      "parent_span_id": null,
      "name": "POST /chat",
      "kind": "server",
      "category": "workflow_step",
      "start_time_unix_nano": 1785579230000000000,
      "end_time_unix_nano": 1785579231500000000,
      "status": "ok",
      "attributes": { "http.request.method": "POST" },
      "usage": {
        "input_tokens": 1200,
        "output_tokens": 340,
        "source": "provider"
      }
    }
  ]
}
```

Response is `202` with per-span results — `accepted`, `duplicate` or `rejected`
with a reason. One malformed span does not fail the batch.

## Limits

```console
$ curl $AIOBS_ENDPOINT/v1/ingest/limits
```

Returns the current batch and body limits, so a client can size its batches
correctly rather than discovering them through 413s. Over the limit is a `429`
with `Retry-After`, which is a correct response and not an error.

## See also

- [Semantic conventions](../concepts/semantic-conventions.md)
- [Ingestion pipeline](../architecture/ingestion-pipeline.md)
