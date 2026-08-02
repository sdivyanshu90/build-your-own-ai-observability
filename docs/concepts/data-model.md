# Data model

## The short version

A **trace** is one request. A **span** is one operation inside it. Spans form a
tree by parent id, and a span may **link** to spans in other traces. Spans carry
**events** (timestamped points) and **attributes** (typed key-value pairs).

That is OpenTelemetry, deliberately. Nothing here replaces an OTel concept with
a proprietary one — a trace id is a W3C trace id, a span kind is an OTel span
kind, and an OTLP exporter you already run will work against this platform
without modification. What this platform adds is a vocabulary for the parts of
an AI request that OTel does not yet describe, and derived rows that make those
parts queryable.

## Trace

A trace is a roll-up, not a stored object the SDK sends. The worker computes it
from the spans:

| Field                                          | Meaning                                                                  |
| ---------------------------------------------- | ------------------------------------------------------------------------ |
| `trace_id`                                     | 32 hex characters, W3C                                                   |
| `name`                                         | The root span's name, or the first span seen if the root has not arrived |
| `status`                                       | `ok`, `error`, `incomplete`, `unset`                                     |
| `duration_ms`                                  | Root span duration, or the observed span if no root                      |
| `complete`                                     | Whether a root span with an end time has been seen                       |
| `total_tokens`, `cost`, `cost_status`          | Summed from the spans, with provenance                                   |
| `models`, `providers`, `prompt_version_ids`, … | Distinct values across the trace                                         |

`complete = false` matters. A trace can be incomplete because it is still
running, because a span was sampled out, or because a service crashed before
flushing. The UI says so rather than showing a total that is quietly a lower
bound.

## Span

Every span has the OTel fields — id, parent id, name, kind, start, end, status,
attributes, events, links — plus columns this platform promotes out of
attributes because they are queried on every screen:

```
category           llm_generation, retrieval, tool_call, agent_decision, …
provider, model    who generated, with what
input_tokens, output_tokens, cached_input_tokens, reasoning_tokens
usage_source       provider | estimated | reconciled | missing
cost, cost_status  final | estimated | unpriced
prompt_version_id, model_config_id, dataset_version_id
time_to_first_token_ms
on_critical_path, self_time_ms
```

Promoting a column is a storage decision, not a modelling one: the attribute is
still in the attribute map, and the promoted column exists so a filter on
`model` does not scan a JSON blob. See
[architecture/storage.md](../architecture/storage.md).

### Category

`category` is the one field with no OTel equivalent, and it earns its place: it
is what lets the UI know that this span is a generation and that one is a
retrieval, without pattern-matching on names. The closed set is in
`aiobs_schemas.enums.SpanCategory`.

### Critical path

`on_critical_path` is computed by the worker, not reported by the SDK. It marks
the chain of spans that determined the total duration — the longest path through
the tree once concurrency is accounted for. Optimising anything not on that
chain cannot make the request faster, which is exactly the question someone
staring at a waterfall is trying to answer.

## Events and links

**Events** are timestamped points inside a span. The platform uses them for the
first streamed token (`aiobs.first_token`), for exceptions, and for anything an
application wants to mark.

**Links** relate a span to one that is not its parent. The cases that matter
here are a retry pointing at the attempt it replaces, a fan-in step pointing at
the branches it merged, and a sub-agent trace pointing at the trace that spawned
it. A link is not a parent: the linked span belongs to a different tree, and
drawing it as a child would produce a waterfall that lies about duration.

## Derived rows

Three tables are populated by the worker from span attributes, because querying
them from inside a JSON map at scale is not viable:

| Table                 | One row per        | Used by                                   |
| --------------------- | ------------------ | ----------------------------------------- |
| `retrieval_documents` | retrieved document | Retrieval view, rank-movement diagnostics |
| `agent_steps`         | agent step         | Trajectory graph                          |
| `cost_records`        | priced span        | Cost dashboards, invoice reconciliation   |

These are derived, not authoritative. Deleting and recomputing them from the
spans is always safe, which is what makes the reconciliation job in the worker
possible.

## Identifiers

| Kind                       | Format                                     | Why                                                                                  |
| -------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------ |
| Trace id                   | 32 hex                                     | W3C                                                                                  |
| Span id                    | 16 hex                                     | W3C                                                                                  |
| Everything else            | `prefix_ULID` — `prj_01J...`, `pmv_01J...` | Sortable by creation time, and the prefix makes a misrouted id obvious in a log line |
| Content-addressed versions | `prefix_<hash prefix>`                     | Identical content converges on the same id                                           |

## What is deliberately not modelled

**Hidden reasoning.** There is no field for raw chain-of-thought and no UI that
would display it. Agent steps record a short `decision_summary` the application
chooses to write, and the observable action. See
[agent-trajectories.md](agent-trajectories.md).

**Evaluation scores.** Recording a quality score is easy; recording it in a way
that survives the eval harness changing is not. Datasets are versioned so an
eval run can reference exactly what it ran against, but the scores themselves
belong in your eval tooling.

**Per-token logprobs.** Enormous, rarely queried, and almost never the reason a
request went wrong. Put them in an attribute if you need them for a specific
investigation; do not make them part of the steady-state ingest volume.
