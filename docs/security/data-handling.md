# Data handling

## Assume prompts are sensitive

Not "may contain sensitive data" — _are_. Users paste credentials into chat
boxes. Retrieved documents are your knowledge base. Completions echo both back.

Every design decision below follows from taking that seriously.

## Two layers of redaction

**The SDK redacts before anything leaves your process.** A platform operator
therefore never sees what you removed, and cannot: it was never sent.

**The platform redacts again on ingestion.** An application that forgot to
configure the SDK is still covered.

Neither layer trusts the other, which is the point. Removing either leaves a
gap that is invisible until it matters.

### What the SDK removes

- Attribute keys matching credential-shaped names: `password`, `secret`,
  `api_key`, `authorization`, `cookie`, `credential`, `ssn`, `credit_card`, …
- Values matching high-confidence patterns: private key headers, AWS access key
  ids, bearer tokens, JWTs
- Anything an application-supplied detector flags

Applications add their own:

```python
client = Client(
    redact_keys=["internal.customer_ref"],
    detectors=[("employee_id", lambda text: EMPLOYEE_ID.search(text) is not None)],
)
```

### The namespace rule

Attributes under `aiobs.` and `gen_ai.` are **safe unless declared sensitive**.
Attributes outside those namespaces fall through to the substring heuristics.

This inversion exists because the naive version was wrong in an expensive way: a
substring rule on "token" matched `aiobs.usage.input_tokens` and
`aiobs.latency.time_to_first_token_ms`, zeroing every token count, every cost
and every latency chart. Redaction is supposed to remove things, so nothing
looked broken.

The sensitive set is small, closed and reviewable. The safe set is open-ended.

## Payload storage is per environment

| Environment | Default         |
| ----------- | --------------- |
| development | payloads stored |
| staging     | payloads stored |
| production  | **no payloads** |

Production records the _shape_ of a request — durations, token counts, models,
document ids, scores, ranks, selection — and not the text. Everything the
platform is for still works; the exposure does not exist.

Turn it on per environment when you need it, and turn it off again.

## Retention

Three independent horizons, because payloads and aggregates have different risk
profiles. See
[concepts/sampling-and-retention.md](../concepts/sampling-and-retention.md).

Deletion order is rows first, then the objects they reference. A partial sweep
therefore leaves an unreferenced object — recoverable, collected later — rather
than a row pointing at nothing.

## Subject deletion

```console
$ curl -X POST "$AIOBS_ENDPOINT/v1/subjects/$SUBJECT_ID/delete" \
    -H "authorization: Bearer $TOKEN"
```

Removes payloads and subject identifiers for that subject immediately,
independent of every retention horizon, and records the request in the audit
log.

What survives is the aggregate contribution: the request happened, it cost
money, it took 3 seconds. Removing that would corrupt every historical total,
and once the identifier is gone it contains nothing about the subject.

## Pseudonymous subject ids

`aiobs.trace.subject_id` is **not redacted**, deliberately: a subject id you
cannot query is useless, and "show me every trace for this user" is the most
common support question there is.

The obligation that comes with it: applications must supply a pseudonymous id.
A hash, an internal id, anything that means nothing outside your system. Not an
email address.

The registry entry says so, and so does this page, because that is the whole
enforcement mechanism — the platform cannot tell the difference.

## Hidden reasoning

There is no field for raw chain-of-thought, no attribute in the registry, and no
UI that would render it. Agent steps record a short, application-authored
`decision_summary` and the observable action.

This is a decision, not an oversight. Hidden reasoning is the most sensitive
text an agent produces, providers increasingly forbid retaining it, and a trace
store that keeps it by default creates an obligation nobody asked for.

## Exports

Exports are audited: who, what resource, what window, how many rows, redacted or
not. Files expire. Redacted is the default, and an unredacted export is labelled
as such in the UI and in the audit entry.

## What the platform logs

Structured JSON with the request id, the route, the status, the duration and the
principal. **Not** request bodies, **not** attribute values, **not** payloads.

A log line that contains a prompt is a payload store with no retention policy.

## See also

- [Threat model](threat-model.md)
- [Sampling and retention](../concepts/sampling-and-retention.md)
- [ADR-0010: two-layer redaction with a declared sensitivity registry](../adr/0010-two-layer-redaction.md)
