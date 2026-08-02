# ADR-0008: `ReplacingMergeTree` for idempotent ingest

## Status

Accepted.

## Context

Ingest is at-least-once. Networks retry, consumers redeliver after a lease
expiry, and applications resend on timeout. The same span will arrive more than
once, routinely.

ClickHouse has no primary key constraint and no `INSERT ... ON CONFLICT`.
Deduplicating at query time means a `GROUP BY` on every read.

## Decision

`ENGINE = ReplacingMergeTree(ingest_version)` with a sort key of
`(organization_id, project_id, environment, start_unix_nano, trace_id, span_id)`.

A re-delivered span with an equal or newer `ingest_version` replaces the row on
merge; it never adds one.

The SQLite driver reproduces the semantics with
`ON CONFLICT ... DO UPDATE` guarded on `ingest_version`, so both drivers behave
identically and the conformance suite can assert it.

## Consequences

**Good.** Retries are free. Replaying the bus after a ClickHouse restore
produces no duplicates, which makes replay a real recovery strategy rather than
a theoretical one. Correcting a span — a reconciled token count — is just an
insert with a higher version.

**Costs.** Deduplication happens **on merge**, not on insert. Between insert and
merge, both rows exist. Queries that must be exact — counting distinct spans —
use `FINAL`, which forces a merge at query time and is slower. Aggregates that
tolerate a transient double-count do not, deliberately.

Choosing per query where correctness demands `FINAL` is a judgement call, and
getting it wrong produces a number that is subtly high for a few minutes after
ingest.

## Alternatives considered

**Deduplicate only at the API, in Redis.** Fast, and wrong when Redis is flushed
or unavailable. It makes correctness depend on a store whose loss is otherwise
survivable.

**`GROUP BY` on every read.** Correct and slow, on every query, forever, to
handle a case that is rare.

**Accept duplicates.** Span counts, token totals and costs all become
approximate, and the platform's core numbers stop being trustworthy.

**Insert into a staging table and deduplicate in a job.** A second copy of the
data, a job that can fall behind, and a window where queries see neither table
correctly.
