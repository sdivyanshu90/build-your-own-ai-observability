# Semantic conventions

## Use OpenTelemetry's names where they exist

The rule, in order:

1. If OpenTelemetry defines an attribute, use it. `http.request.method`,
   `db.system`, `server.address`.
2. If the OTel **GenAI** semantic conventions define it, use that.
   `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`.
3. Only then reach for `aiobs.*`.

Inventing a proprietary name for something OTel already names is how a platform
stops working with the collector, the exporters and the instrumentation
libraries its users already run.

## The registry

Every attribute the platform knows about is declared once, in
`packages/shared-schemas/python/aiobs_schemas/semconv.py`:

```python
SpecAttribute(
    name="aiobs.retrieval.documents",
    type=AttributeType.JSON,
    sensitive=True,
    description="Ranked documents returned by a retrieval step.",
)
```

The TypeScript mirror is generated from it (`make generate`), so the two
languages cannot drift.

### The registry is what drives redaction

This is the part that matters. Sensitivity is **declared**, not guessed.

An earlier version guessed, using substring rules — anything containing "token"
or "key" was sensitive. That rule zeroed `aiobs.usage.input_tokens` and
`aiobs.latency.time_to_first_token_ms` on every span, silently destroying every
token count, every cost and every latency chart in the product. The bug was
invisible because redaction is supposed to remove things.

So: attributes in the platform's own namespaces are judged by the registry.
Substring heuristics apply only to unknown, application-supplied keys, where a
false positive costs you one attribute rather than the product's core metric.

## The `aiobs.*` namespace

| Prefix                            | Covers                                                                  |
| --------------------------------- | ----------------------------------------------------------------------- |
| `aiobs.trace.*`                   | Trace-level identity: name, session, subject, tags, release             |
| `aiobs.usage.*`                   | Token counts and their provenance                                       |
| `aiobs.cost.*`                    | Computed cost and its status                                            |
| `aiobs.latency.*`                 | Time to first token, queue time, provider time                          |
| `aiobs.prompt.*`                  | Prompt name, version id, hash, rendered variables                       |
| `aiobs.model.*`                   | Model configuration id and hash, system fingerprint                     |
| `aiobs.retrieval.*`               | Query, rewritten query, retriever, documents, context composition       |
| `aiobs.agent.*`                   | Step number, type, agent id, tool, branch, retry, approval, termination |
| `aiobs.dataset.*`                 | Dataset name, version id, record id                                     |
| `aiobs.input.*`, `aiobs.output.*` | Payload values or references to stored payloads                         |

The full list, with types and sensitivity flags, is the registry itself — it is
short enough to read, and reading it is more reliable than reading a copy of it.

## Adding one

1. Add the `SpecAttribute` to the registry, with `sensitive` set deliberately.
2. `make generate` to regenerate the TypeScript mirror.
3. If it should be queryable as a column rather than from the attribute map, add
   it to `storage/analytics/columns.py` and the relevant `ResourceSchema` in
   `storage/analytics/schemas.py`, and write a migration.

Step 3 is a real cost — a new column is a schema migration on a table with
billions of rows — so promote an attribute only when a filter or aggregation on
it is a normal thing to want.

## Subject identifiers

`aiobs.trace.subject_id` is marked **not sensitive**, and that is a deliberate
decision with an obligation attached: applications must supply a _pseudonymous_
id, not an email address or a user id that means something outside your system.

The reason it is not redacted is that a subject id you cannot query is useless —
"show me every trace for the user reporting this bug" is the single most common
support question. The reason it must be pseudonymous is that this store holds
prompts, and joining prompts to a real identity is exactly what you do not want
to have done by default.

## See also

- [Data model](data-model.md)
- [Security: data handling](../security/data-handling.md)
- [ADR-0002: OpenTelemetry-native, not OpenTelemetry-inspired](../adr/0002-otel-native.md)
