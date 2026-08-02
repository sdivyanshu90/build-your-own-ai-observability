# ADR-0001: Separate the ingest and query paths through a durable bus

## Status

Accepted.

## Context

Ingest and query have opposite characteristics. Ingest is high-volume,
write-only, latency-sensitive for the _caller_ and tolerant of a few seconds of
delay in becoming visible. Query is low-volume, read-only, and latency-sensitive
for a human staring at a loading spinner.

Handling both in one synchronous path means the slowest component sets the pace
for everything.

## Decision

The API validates, authorises, deduplicates and publishes to a durable bus, then
returns `202`. A separate worker consumes, normalises, prices, stores and rolls
up.

## Consequences

**Good.** A slow analytics store cannot add latency to an instrumented
application. The bus absorbs the backlog and the API keeps returning 202. Ingest
and query scale independently — the API on request rate, the worker on lag.

**Costs.** Traces are visible a few seconds after the request, not instantly.
There is a bus to operate, and its retention window becomes a capacity decision.
Debugging spans a process boundary. A durable bus with no graceful degradation
becomes the one dependency that must not fail.

The eventual-consistency window is the part users notice, and it is the right
thing to trade: nobody has ever needed a trace within 200 milliseconds, and
everybody notices when their application gets slower.

## Alternatives considered

**Write to the analytics store synchronously.** Simplest, and works until the
store hiccups — at which point the observability platform is adding latency to
every instrumented application. That is a failure mode that erodes trust
permanently.

**Buffer in the API process.** No bus to operate, but the buffer dies with the
pod. Every deploy loses spans, and a crash loses more.

**Write to PostgreSQL first, move to ClickHouse later.** Turns PostgreSQL into
the ingest bottleneck and adds a second copy of the data. The bus does the same
job better and is designed for it.
