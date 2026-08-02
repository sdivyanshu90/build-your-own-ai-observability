# Storage

Three stores, because the data has three shapes.

| Store              | Holds                                                                                                           | Why not one of the others                                                                                  |
| ------------------ | --------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| **PostgreSQL**     | Organisations, users, memberships, API keys, projects, environments, registries, price books, audit log, outbox | Relational, transactional, small. Needs foreign keys and real transactions.                                |
| **ClickHouse**     | Spans, traces, events, retrieval documents, agent steps, cost records                                           | Billions of rows, append-mostly, queried by time range and aggregated. A row store cannot do this at cost. |
| **Object storage** | Prompt and completion payloads                                                                                  | Large, immutable, rarely read, and the thing you most want to expire independently                         |

Plus **Redis** for rate limit counters and idempotency records — state that is
allowed to be lost.

## Why not put spans in PostgreSQL

It works to a few million spans and then it does not. The queries are "p95
latency by model over the last 7 days, grouped by prompt version" — a scan and
aggregate over one or two columns of a very wide table. A row store reads every
column of every row to answer it.

## Why not put metadata in ClickHouse

ClickHouse has no foreign keys, no transactions worth the name, and `UPDATE` is
a mutation that rewrites parts. "Create an organisation, its first project, its
environments and the owner membership, atomically" is a transaction. Emulating
that on ClickHouse produces half-created tenants.

## The analytics abstraction

Both drivers implement one interface, `AnalyticsStore`, and the dialect-agnostic
SQL is written once in `sqlbase.py`. Each driver supplies a handful of
primitives — parameter syntax, array containment, map access, percentile
computation — and its own execution and bulk-insert path.

**The conformance suite runs against both.** That is what makes a second
implementation an asset rather than a divergence risk: "it works locally but not
in production" becomes a test failure rather than an incident.

Where the drivers genuinely differ, the difference is a named hook with a
comment explaining it:

- `_like_escape()` — SQLite needs `ESCAPE '\'`; ClickHouse rejects the clause.
- `_decimal_sum()` — ClickHouse has a decimal type; SQLite needs a
  user-defined aggregate to avoid summing money through a float.
- `_null_safe()` — SQLite orders money text lexicographically, so ordering is
  projected through `REAL` while the returned values stay exact.

## ClickHouse schema

```sql
CREATE TABLE spans (...)
ENGINE = ReplacingMergeTree(ingest_version)
PARTITION BY toDate(start_unix_nano / 1000000000)
ORDER BY (organization_id, project_id, environment, start_unix_nano, trace_id, span_id)
```

- **`ReplacingMergeTree(ingest_version)`** gives idempotent ingest: a
  re-delivered span replaces rather than duplicates.
- **Daily partitions** make retention a `DROP PARTITION` instead of a delete.
- **Sort key starts with the tenant**, so every query prunes to one
  organisation's data before reading anything.
- **`LowCardinality`** on model, provider, environment, status, category.
- **Skip indexes**: bloom filters on trace and span ids, set indexes on the
  low-cardinality columns.
- **`AggregatingMergeTree` materialised views** pre-roll the counters the
  dashboards read, so a 30-day chart does not scan 30 days of spans.

`FINAL` is used only where correctness demands it — counting distinct spans —
because it forces a merge at query time.

## PostgreSQL schema

Standard normalised relational design. Two details worth knowing:

**`UtcDateTime` everywhere.** A `TypeDecorator` that rejects naive datetimes on
write and attaches UTC on read. SQLite returns naive datetimes and PostgreSQL
does not; without this, comparisons silently do the wrong thing on one of them.

**`DecimalText` for money.** Exact on both dialects.

Migrations are Alembic, expand-only, and `make migrate-check` fails if the
models have drifted.

## Object storage

Payloads are **content-addressed**: the key is the hash of the content. Two
identical prompts stored twice occupy one object. Writes are get-or-create,
because a plain create violates the uniqueness constraint the moment two
requests send the same prompt.

Objects are written only when the environment allows payload storage. Production
environments default to off, so the shape of a request is recorded and its text
is not.

## See also

- [Query model](query-model.md)
- [ADR-0007: two analytics drivers, one conformance suite](../adr/0007-two-analytics-drivers.md)
- [ADR-0008: ReplacingMergeTree for idempotent ingest](../adr/0008-replacing-merge-tree.md)
