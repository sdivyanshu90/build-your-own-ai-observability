# Pagination

## Keyset, not offset

```
GET /v1/traces?project_id=prj_...&start=...&end=...&limit=50
```

```json
{
  "items": [ ... ],
  "next_cursor": "eyJ0cyI6MTc4NTU3OTIzMH0...",
  "has_more": true
}
```

Pass `next_cursor` back to get the next page:

```
GET /v1/traces?...&limit=50&cursor=eyJ0cyI6MTc4NTU3OTIzMH0...
```

## Why not offset

Offset pagination over an append-only, time-ordered store silently duplicates
and skips rows as new data lands mid-session — which is precisely when someone
is paging through it. `LIMIT 50 OFFSET 100` on a table receiving a thousand rows
a second returns a page that overlaps the previous one by an arbitrary amount.

Keyset pagination asks "give me the next 50 after _this row_", which is stable
regardless of what arrives.

## The cursor

The sort key of the last row returned, **HMAC-signed**.

The signature is not confidentiality — the payload is only sort-key values. It
is integrity: an unsigned cursor is user-controlled input spliced into a `WHERE`
clause. A tampered cursor is rejected with `validation_failed`.

Decimal values are tagged and carried as exact strings. Encoding a cost as a
JSON number would round it, and a rounded boundary can skip or repeat a row.

Cursors are opaque and not durable. They are invalidated by a change to
`CURSOR_SECRET` and by a change to the sort. Do not store one.

## Ordering is always total

The resource's tiebreaker is appended to every sort. Without a strict total
order, two rows with equal sort values can appear on both sides of a page
boundary, or on neither.

## Limits

|         | Default | Maximum |
| ------- | ------- | ------- |
| `limit` | 50      | 500     |

A `limit` over the maximum is clamped, not rejected — the request is well-formed
and the intent is clear.

## Paging backwards

There is no `previous_cursor`. Clients keep the stack of cursors they have
visited and pop it, which is what the web application does. A bidirectional
cursor doubles the complexity of the predicate for a case that is only ever
"the page I just came from".

## See also

- [Filtering and sorting](filtering.md)
- [ADR-0009: keyset pagination with signed cursors](../adr/0009-keyset-pagination.md)
