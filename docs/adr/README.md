# Architecture decision records

Decisions that would be expensive to reverse, with the alternatives that were
rejected and why. The rejected-alternatives section is where the value is: the
decision itself is visible in the code.

| ADR                                         | Decision                                                      |
| ------------------------------------------- | ------------------------------------------------------------- |
| [0001](0001-separate-ingest-and-query.md)   | Separate the ingest and query paths through a durable bus     |
| [0002](0002-otel-native.md)                 | Be OpenTelemetry-native, not OpenTelemetry-inspired           |
| [0003](0003-content-addressed-versions.md)  | Content-addressed immutable versions with movable aliases     |
| [0004](0004-canonical-json.md)              | RFC 8785 canonical JSON for cross-language hashing            |
| [0005](0005-decimal-money.md)               | Exact decimal money, end to end                               |
| [0006](0006-effective-dated-price-books.md) | Effective-dated price books instead of price constants        |
| [0007](0007-two-analytics-drivers.md)       | Two analytics drivers held to one conformance suite           |
| [0008](0008-replacing-merge-tree.md)        | `ReplacingMergeTree` for idempotent ingest                    |
| [0009](0009-keyset-pagination.md)           | Keyset pagination with HMAC-signed cursors                    |
| [0010](0010-two-layer-redaction.md)         | Two-layer redaction driven by a declared sensitivity registry |
| [0011](0011-api-key-hashing.md)             | Keyed SHA-256 for API keys, not Argon2id                      |
| [0012](0012-no-hidden-reasoning.md)         | No storage for hidden chain-of-thought                        |

## Format

```markdown
# ADR-NNNN: Title

## Status

Accepted | Superseded by ADR-NNNN

## Context

What forced a decision.

## Decision

What was decided.

## Consequences

What this costs, including the parts that are annoying.

## Alternatives considered

What was rejected and why. This is the important section.
```

Add one for anything expensive to reverse. Copy the most recent for the format.
