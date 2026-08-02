# Query model

## A closed grammar

Every list endpoint accepts filters in one form:

```
field:operator:value
```

`field` must be declared in the resource's `ResourceSchema`. `operator` comes
from a closed enum. `value` is always bound as a parameter. **No user string
ever becomes SQL text.**

```
?filter=status:eq:error&filter=model:contains:gpt-4&filter=cost:gt:0.01
```

Repeated parameters are ANDed. An unknown field is rejected with the list of
valid ones — a 422 that tells you what to type is worth more than a 500.

### Operators

| Operator                  | Applies to              | Notes                                                                            |
| ------------------------- | ----------------------- | -------------------------------------------------------------------------------- |
| `eq`, `ne`                | everything              |                                                                                  |
| `gt`, `gte`, `lt`, `lte`  | numbers, timestamps     |                                                                                  |
| `contains`, `starts_with` | strings                 | LIKE wildcards in the value are escaped, so a literal `%` matches a percent sign |
| `in`, `not_in`            | everything              | comma-separated                                                                  |
| `exists`                  | nullable and map fields |                                                                                  |
| `has`                     | string arrays           | `model:has:gpt-4o` on a trace's model list                                       |

Fields may declare an `allowed_values` set. `status:eq:banana` is rejected
against the field definition rather than returning zero rows, because zero rows
and "that value does not exist" are different answers.

## Sorting

```
?sort=-start_time,duration_ms
```

Leading `-` means descending. Comma-separated for multiple keys. Only fields
marked `sortable` are accepted — sorting on an unindexed column of a
billion-row table is a request to scan it.

The schema's **tiebreaker** is always appended. Without a strict total order,
keyset pagination returns rows on both sides of a page boundary, or neither.

## Pagination is keyset, not offset

```
?limit=50&cursor=eyJ0cyI6...
```

Offset pagination over an append-only, time-ordered store silently duplicates
and skips rows as new data lands mid-session — which is exactly when someone is
paging through it.

The cursor is the sort key of the last row, **HMAC-signed**. The signature is
not about confidentiality; the payload is only sort-key values. It is about
integrity: an unsigned cursor is user-controlled input spliced into a `WHERE`
clause.

Decimal sort keys are tagged and carried as exact strings. Encoding a cost as a
JSON number would round it and could place the boundary on the wrong side of a
row.

Full details: [api/pagination.md](../api/pagination.md).

## Aggregation

```
GET /v1/metrics/timeseries?metric=duration_ms&aggregation=p95&group_by=model&interval=1h
```

- **Metric names are logical**, the same vocabulary as filters and sort. Ask for
  `duration_ms` and get milliseconds, even though the column stores nanoseconds.
- **Aggregatable columns are a closed set**, so a metric query cannot be used to
  scan a column it has no business touching.
- **Percentiles are computed by the store** over raw rows. Averaging
  percentiles across groups is arithmetically meaningless, and any design that
  makes it possible will eventually have it done.
- **Bucket width** is chosen automatically from the range, or given explicitly.
- **Group cardinality is bounded**: the top N groups by the aggregate, so a
  high-cardinality dimension returns a chart rather than three thousand series.

### Partial buckets are labelled

The most recent bucket is still filling. The API returns `partial_buckets`
explicitly — the server knows its own bucket boundaries and ingestion lag, the
browser does not — and the UI draws them dashed. Without this, every dashboard
appears to end in a cliff, and people page each other about it.

## Full-text search

`?q=` searches a small set of indexed text columns: span name, trace name,
session id, subject id. Not attribute values and not payloads — a substring
search over a JSON map on a billion rows is not a feature, it is an outage.

## Tenancy

Every analytics query carries an organisation predicate, added by the store
rather than by the caller. There is no code path that constructs an analytics
query without one, and `tests/security/test_authorization.py` asserts it.

## See also

- [api/filtering.md](../api/filtering.md)
- [api/pagination.md](../api/pagination.md)
- [Multi-tenancy](multi-tenancy.md)
- [ADR-0009: keyset pagination with signed cursors](../adr/0009-keyset-pagination.md)
