# ADR-0005: Exact decimal money, end to end

## Status

Accepted.

## Context

Per-token prices are around `0.0000025`. A typical request costs a fraction of a
cent. A month of a busy application is a few thousand dollars made of hundreds
of millions of those fractions.

IEEE-754 doubles cannot represent most of them exactly, and the error
accumulates. A float sum of a few hundred spans produces totals like
`2.1456037299999977` — a number that will eventually appear next to an invoice
someone is trying to reconcile.

## Decision

Money is never a binary float anywhere in the system.

- `Decimal` in Python, `Decimal(38, 18)` in ClickHouse, exact text in SQLite.
- Summed by the database's decimal type, or by a user-defined aggregate where
  the engine has none.
- Serialised as **JSON strings**, never JSON numbers.
- Rendered from the string in the browser; the formatter takes a string and
  returns a string.

## Consequences

**Good.** Totals reconcile. The exact string a user sees is the exact value
stored. A per-call cost of `$0.0000004` displays as itself rather than as
`$0.00`.

**Costs.** Every layer needs a decimal-aware path, and each one is a place to
get it wrong:

- SQLite has no decimal type, so `SUM()` over the text column coerces to float.
  A user-defined aggregate accumulates in `Decimal` instead.
- SQLite orders text lexicographically, so `"9" > "10"`. Ordering is projected
  through `REAL` while the returned values stay exact, and the schema tiebreaker
  keeps the total order strict.
- A `Decimal` cannot be bound as a SQLite parameter at all, so keyset cursors
  over a cost sort need the same projection.
- JSON numbers are doubles by definition, so the API contract must be "money is
  a string" and every client must respect it.

None of that is visible in a demo. All of it is visible in an invoice.

## Alternatives considered

**Store cents as integers.** Works for currency-scale amounts, fails at
per-token scale: a price of `0.0000025` is not an integer number of cents, and
scaling to micro-cents just moves the problem.

**Use floats and round at display time.** The error is already in the sum by
then. Rounding a wrong number produces a rounded wrong number.

**Use floats and accept the imprecision.** Defensible for a dashboard, indefensible
for a bill. The platform is used for both, and the same number appears in both.
