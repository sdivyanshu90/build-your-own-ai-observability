# Your first trace

Fifteen minutes, from nothing to a trace you can click through.

## 1. Start the platform

```console
$ make setup
$ make dev-local
```

Leave it running. It listens on `http://localhost:58000`.

## 2. Create an organisation and an API key

```console
$ make bootstrap
```

```
created organization demo (org_01KY...)
created project demo-project (prj_01KY...)

====================================================================
Bootstrap complete. Store the API key now -- it is not recoverable.
====================================================================
  organization : org_01KY...
  project      : prj_01KY...
  environment  : development (env_01KY...)
  api key      : aiobs_test_9a01b9ab_qQmpAm0sja9Q...

  export AIOBS_API_KEY=aiobs_test_9a01b9ab_qQmpAm0sja9Q...
  export AIOBS_ENDPOINT=http://localhost:58000
```

Copy those two exports. The key is shown once.

## 3. Send a trace

```python
# first_trace.py
import os
import time
from aiobs import Client

client = Client(service_name="tutorial")

with client.trace("answer-question", session_id="session-1") as trace:
    with trace.span("retrieve", category="retrieval") as span:
        time.sleep(0.15)
        span.record_retrieval(
            query="how do refunds work?",
            retriever_name="tutorial-retriever",
            documents=[
                {"document_id": "doc-1", "rank": 0, "score": 0.91,
                 "title": "Refund policy", "selected": True},
                {"document_id": "doc-2", "rank": 1, "score": 0.44,
                 "title": "Shipping", "selected": False},
            ],
        )

    with trace.span("generate", category="chat_completion") as span:
        span.record_model(provider="openai", model="gpt-4o", temperature=0.2)
        time.sleep(0.4)
        span.record_first_token()
        span.record_usage(input_tokens=1200, output_tokens=340, source="provider")

client.shutdown()
print("sent")
```

```console
$ python first_trace.py
sent
```

## 4. Look at it

```console
$ npm run dev --workspace @aiobs/web
```

Open <http://localhost:53000> and sign in with the bootstrap credentials
(`admin@example.com` / `change-me-immediately-please` by default).

Switch the **environment** picker to `development` — that is where the SDK sent
it, and the UI defaults to production.

You should see one trace. Open it.

## 5. What to notice

**The waterfall.** Three spans. `generate` is on the critical path (full
opacity, left rule); `retrieve` is not, because it finished before the
generation started.

**The retrieval tab.** Two documents, one selected. The unused ratio is 50% —
half of what you fetched never reached the model.

**Cost.** `$0.0064`, or "unpriced" if the bootstrap price book does not cover
`gpt-4o`. Open the span detail and read the formula: the platform shows the
arithmetic, not just the answer.

**Time to first token.** `record_first_token()` produced it. It is what your
user perceives; total duration is what your bill perceives.

## 6. Break something on purpose

```python
with client.trace("failing-request") as trace:
    with trace.span("generate", category="chat_completion"):
        raise RuntimeError("provider returned 529 overloaded")
```

The exception propagates — instrumentation does not swallow it — and the trace
appears in the explorer marked as an error, with the message on the span.

Filter the explorer with the **Errors only** quick filter and notice the URL
changes. That URL is shareable: filters live in the URL, so a debugging session
can be pasted into an incident channel.

## Next

- [Instrumenting a RAG pipeline](instrumenting-rag.md)
- [Instrumenting an agent](instrumenting-agents.md)
- [Python SDK reference](../sdk/python.md)
- `make seed PROJECT=prj_...` for a few hundred traces with realistic shape
