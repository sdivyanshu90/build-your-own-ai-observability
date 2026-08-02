# ADR-0004: RFC 8785 canonical JSON for cross-language hashing

## Status

Accepted.

## Context

[ADR-0003](0003-content-addressed-versions.md) requires hashing a structure. A
hash is only useful if every producer computes the same one — and producers here
are Python and TypeScript SDKs, hashing the same prompt.

`json.dumps` and `JSON.stringify` disagree on key order, whitespace, escaping
and — worst — number formatting. `0.1 + 0.2` prints as `0.30000000000000004` in
both, but `1e21` prints as `1e+21` in Python and `1e21` in JavaScript.

## Decision

Implement [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785) in both languages,
including reimplementing ECMAScript's `Number::toString` in Python. Pin the
number formatting with a shared 339-case fixture that both test suites assert
against.

One documented departure from the RFC: keys are NFC-normalised, so two keys that
render identically hash identically.

## Consequences

**Good.** A prompt registered by the Python SDK and the same prompt registered
by the TypeScript SDK produce the same version id. Content addressing works
across languages, which is the only way it is useful in a polyglot organisation.

**Costs.** Reimplementing `Number::toString` in Python is genuinely subtle —
shortest round-trip representation, the exponential-notation thresholds at 1e21
and 1e-7, negative zero. The fixture is what keeps it honest. Canonicalisation
is not free, though it only runs on registration, not on the ingest path.

The NFC departure has a sharp edge, found by property testing: U+FB2C is one
code point that normalises to three. Emitting the normalised key while looking
the value up under it raised `KeyError` on every object with such a key. Both
implementations now carry `(normalised, original)` pairs.

## Alternatives considered

**Hash a fixed field order manually.** Works for a flat structure, breaks on
nested variable schemas, and every new field is a chance to get the order wrong
in one language.

**Use a binary format — protobuf, msgpack.** Deterministic, but then the stored
representation is opaque and a version cannot be read without the schema.

**Hash whatever the producer serialises and accept divergence.** Two SDKs, two
version ids, duplicate registry entries. This is the failure this ADR exists to
prevent.

**Ship a canonical JSON library.** The available implementations disagree on
the number formatting edge cases, which is precisely the part that matters.
