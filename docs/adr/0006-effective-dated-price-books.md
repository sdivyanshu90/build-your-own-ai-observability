# ADR-0006: Effective-dated price books instead of price constants

## Status

Accepted.

## Context

Model prices change. Providers cut them, introduce tiers, add cached-input rates
and change which tokens count as input.

A trace from March must be priced with March's rates. If prices are constants in
code, re-pricing that trace after a change produces a different number, and the
historical cost data silently rewrites itself.

## Decision

Prices live in versioned, effective-dated **price books**. Every entry has
`effective_from` and an optional `effective_to`. Pricing a span looks up the
entry effective at the span's start time.

A built-in default book ships with the platform. An organisation may publish its
own to override it.

## Consequences

**Good.** Historical costs are stable and reproducible. Negotiated rates,
internal chargeback multipliers and providers the defaults do not cover are all
just another book. Every cost is traceable to a specific rule with a source URL,
and the `cost_records` row stores the formula.

**Costs.** More machinery than a dictionary: a table, an effective-date lookup,
a snapshot resolved once per batch rather than per span. Somebody has to keep
the books current — a new model with no entry is `unpriced` until one is added.
Which is the right failure: visible, named, and not silently zero.

## Alternatives considered

**Hard-code prices as constants.** One dictionary, no tables. Changing a price
rewrites history, and every deployment carries whatever prices were current when
its image was built.

**Fetch prices from the provider's API.** Some providers do not offer one, the
formats differ, and it puts a network call on the costing path. It also cannot
express a negotiated rate.

**Store the price on the span at ingest time.** Correct historically, but
re-pricing after a mistake becomes impossible, and a pricing bug becomes
permanent in the data.
