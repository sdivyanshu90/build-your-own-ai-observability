# ADR-0009: Keyset pagination with HMAC-signed cursors

## Status

Accepted.

## Context

Trace lists are paged. The underlying store is append-only and time-ordered, and
rows arrive continuously — often thousands per second, and most heavily during
exactly the incident someone is paging through.

## Decision

Keyset pagination. The cursor is the sort key of the last row returned,
HMAC-signed. The resource's tiebreaker is appended to every sort so the ordering
is a strict total order.

## Consequences

**Good.** Pages are stable regardless of what arrives mid-session. Performance
does not degrade with depth — page 400 costs the same as page 1, because the
predicate uses the index rather than counting rows. The signature makes a
tampered cursor a rejected request rather than an executed one.

**Costs.** No "jump to page 7", because there is no page 7 to jump to. No total
count, because counting means scanning. Paging backwards requires the client to
keep the cursors it has visited — which the web application does, as a stack.

The decimal handling is subtle: a cost in a sort key must be carried as an exact
string, because encoding it as a JSON number rounds it and a rounded boundary can
skip or repeat a row.

## Alternatives considered

**Offset pagination.** `LIMIT 50 OFFSET 100` on a table receiving a thousand
rows a second returns a page that overlaps the previous one by an arbitrary
amount, and misses rows entirely. It also gets slower with depth, because the
database counts the rows it skips.

**Unsigned cursors.** The payload is only sort-key values, so confidentiality is
not the issue. Integrity is: an unsigned cursor is user-controlled input spliced
into a `WHERE` clause.

**Opaque server-side cursors in Redis.** Stateful, expiring, and a dependency
on a store whose loss is otherwise survivable.

**Encrypted cursors.** Confidentiality the payload does not need, and key
rotation becomes an operational concern for no benefit.
