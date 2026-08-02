# Sampling and retention

## Sampling is per trace, decided once

The head sampling decision is made when a trace starts and inherited by every
child span. Sampling per span would produce traces with holes, and a trace with
holes is worse than no trace: the waterfall is wrong, the critical path is
wrong, and the totals are wrong in a way nothing indicates.

```python
client = Client(sample_rate=0.1)   # 10% of traces, whole
```

Two exceptions are worth configuring:

- **Errors are always kept.** A 10% sample of failures is a 10% chance of having
  the trace you need.
- **An explicit decision wins.** `client.trace(..., sampled=True)` forces
  retention for a request you already know you care about.

## Retention is three independent horizons

The data has three different risk profiles, so it has three different lifetimes:

| Horizon          | What it covers                                 | Default | Why separate                                                                                                                    |
| ---------------- | ---------------------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `payload_days`   | Prompt and completion bodies in object storage | 14      | Highest exposure. Expiring these first limits how long sensitive text exists while keeping the shape of the request observable. |
| `raw_span_days`  | Per-span rows behind the waterfall             | 30      | Once these expire a trace cannot be opened, but its aggregate contribution remains.                                             |
| `aggregate_days` | Pre-rolled counters and percentile state       | 400     | Almost no exposure, and this is what dashboards read. Governs how far back charts go.                                           |

Collapsing them into one number forces a choice between "delete the text we must
not keep" and "keep the dashboards", and there is no reason to make that trade.

## What deletion actually does

The sweep runs in the worker, in bounded batches, and deletes in dependency
order: **rows first, then the objects they reference.** A partial sweep
therefore leaves a trace pointing at an object that still exists — recoverable —
rather than at one that does not — a broken link that looks like corruption.

Orphaned objects are collected separately by a reconciliation job that lists the
bucket and removes anything with no referencing row, so an interrupted sweep
does not leak storage forever.

## Subject deletion

A subject-deletion request is not the retention sweep. It removes payloads and
subject identifiers for one subject **immediately**, independent of every
horizon, and records the request in the audit log.

What survives is the aggregate contribution: the request happened, it cost
money, it took 3 seconds. Removing that would corrupt every historical total,
and it contains nothing about the subject once the identifier is gone.

## Rate limiting and back-pressure

Ingest is rate limited per API key, and the limit is advertised:
`/v1/ingest/limits` returns the current batch and body limits so an SDK can size
its batches correctly rather than discovering them through 413s.

Under sustained overload the platform returns `429` with `Retry-After`. The SDKs
honour it with jittered exponential backoff. A 429 is a correct response, not an
error — the load-test harness and the k6 profile both count it separately for
that reason.

## Configuring

Per project, via the API or the settings UI:

```console
$ curl -X PUT "$AIOBS_ENDPOINT/v1/projects/$PROJECT/retention" \
    -H "authorization: Bearer $TOKEN" \
    -d '{"raw_span_days": 60, "aggregate_days": 400, "payload_days": 7}'
```

Organisation-wide defaults are in [operations/configuration.md](../operations/configuration.md).

## See also

- [Security: data handling](../security/data-handling.md)
- [Operations: capacity](../operations/capacity.md)
