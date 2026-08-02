# ADR-0007: Two analytics drivers held to one conformance suite

## Status

Accepted.

## Context

ClickHouse is the right production store: columnar, compressed, fast at
aggregating a billion rows over a time range.

It is the wrong _development_ store. Requiring a ClickHouse container to run the
test suite means slower tests, a heavier contribution setup, and CI that needs a
service for every job.

## Decision

Two implementations of one `AnalyticsStore` interface: ClickHouse for
production, SQLite for development and testing. **One conformance suite runs
against both.**

The dialect-agnostic SQL is written once. Each driver supplies a handful of
primitives — parameter syntax, array containment, map access, percentile
computation — and its own execution and bulk-insert path.

## Consequences

**Good.** `make dev-local` starts in two seconds with no containers. Most CI
jobs need no services. "It works locally but not in production" becomes a test
failure rather than an incident, because the suite asserts identical behaviour.

The conformance suite has earned this repeatedly. Every driver-specific hook in
the codebase — the LIKE escape clause, the decimal sum, the money ordering
projection — exists because the suite caught a divergence.

**Costs.** Every analytics feature must be implemented twice, or at least
verified twice. The SQLite driver will not scale, so
`validate_for_runtime()` refuses to start a production process configured to use
it. The dialect abstraction is one more layer to read through.

## Alternatives considered

**ClickHouse only, containers everywhere.** Honest, and it makes the test suite
slow enough that people stop running it locally. That trade is not worth the
purity.

**Mock the analytics store in tests.** A mock asserts what you told it to
assert. Every serious bug found here — a filter that produced wrong SQL, a
cursor that skipped rows, a sum that lost precision — was in the SQL layer,
which is exactly what a mock does not exercise.

**An ORM abstracting both.** ORMs abstract row stores. `ReplacingMergeTree`,
`FINAL`, materialised views and `quantilesExactLow` are the reasons to use
ClickHouse, and an ORM hides all of them.
