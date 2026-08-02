# TypeScript SDK

```console
$ npm install @aiobs/sdk
```

```bash
export AIOBS_ENDPOINT=https://observability.example.com
export AIOBS_API_KEY=aiobs_live_...
export AIOBS_SERVICE_NAME=checkout-assistant
```

Node 20.19+. The SDK uses `AsyncLocalStorage` for context propagation, so it
works across `await` without any explicit threading.

## The shape of it

```ts
import { Client } from '@aiobs/sdk';

const client = new Client();

await client.trace('answer-question', { sessionId }, async (trace) => {
  await client.span('retrieve', { category: 'retrieval' }, async (span) => {
    const documents = await retriever.search(question);
    span.recordRetrieval({ query: question, retrieverName: 'pgvector', documents });
  });

  await client.span('generate', { category: 'chat_completion' }, async (span) => {
    span.recordModel({ provider: 'openai', model: 'gpt-4o' });
    const response = await openai.chat.completions.create({ ... });
    span.recordUsage({ inputTokens: ..., outputTokens: ..., source: 'provider' });
  });
});
```

The callback form ends the span on return, records an exception if one is
thrown, and **re-throws it**. Instrumentation never swallows your errors.

For code that does not fit a callback:

```ts
const span = client.startSpan("work");
try {
  await doWork();
} finally {
  span.end();
}
```

## It cannot break your application

Every public method is guarded: a failure inside the SDK is logged and
swallowed. A circular reference, an unreachable endpoint, a value the schema
rejects — none of them propagate.

## Context propagation

Automatic within a process. Across services:

```ts
const response = await fetch(url, { headers: span.headers() });
```

Receiving:

```ts
import { extract, withContext } from '@aiobs/sdk';

const parent = extract(request.headers);
await withContext(parent, async () => {
  await client.trace('downstream-work', {}, async () => { ... });
});
```

The Express integration does this for you:

```ts
import { instrumentExpress } from "@aiobs/sdk/express";
app.use(instrumentExpress(client));
```

## Cross-language identity

Prompt and model versions hashed by this SDK produce **exactly the same ids** as
the Python SDK. Both implement RFC 8785 canonical JSON, and a 339-case fixture
pins ECMAScript's number formatting so the two cannot drift.

Without that, registering the same prompt from a TypeScript service and a Python
service would create two versions of identical content.

## Testing

```ts
import { createTestClient } from "@aiobs/sdk";

it("records the retrieval", async () => {
  const { client, captured } = createTestClient();

  await answerQuestion(client, "how do refunds work?");
  await client.flush();

  captured.assertWellFormed();
  expect(captured.ofCategory("retrieval")).toHaveLength(1);
});
```

## Configuration

The same `AIOBS_*` variables as the Python SDK, or constructor options:

```ts
const client = new Client({
  endpoint: "https://observability.example.com",
  apiKey: process.env.AIOBS_API_KEY,
  serviceName: "checkout-assistant",
  sampleRate: 0.1,
  capturePayloads: false,
  redactKeys: ["internal.customerRef"],
});
```

## Shutdown

```ts
await client.shutdown();
```

In a serverless handler, `await client.flush()` before returning — the process
may be frozen the moment it does.

## See also

- [Python SDK](python.md)
- [OTLP without an SDK](otlp.md)
