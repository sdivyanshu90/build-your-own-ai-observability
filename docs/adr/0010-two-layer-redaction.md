# ADR-0010: Two-layer redaction driven by a declared sensitivity registry

## Status

Accepted.

## Context

Prompts contain credentials users pasted, personal data, and proprietary
business logic. Redaction is not optional.

Where it happens matters. Redacting only on the server means the sensitive data
crossed the network and existed in the platform's memory. Redacting only in the
SDK means an application that forgot to configure it is unprotected.

## Decision

**Both.** The SDK redacts before anything leaves the process; the platform
redacts again on ingestion. Neither layer trusts the other.

Sensitivity is **declared** in the attribute registry, not guessed. Attributes
under `aiobs.` and `gen_ai.` are safe unless the registry says otherwise;
substring heuristics apply only to unknown, application-supplied keys.

## Consequences

**Good.** A platform operator cannot see what the SDK removed — it was never
sent. An application that forgot the SDK configuration is still covered.
Applications can add their own detectors without the platform knowing what they
detect.

Declaring sensitivity means adding an attribute is a decision about whether it is
sensitive, made once, in one place, reviewably.

**Costs.** Redaction runs twice, so it costs CPU twice. The registry must be
kept accurate — an attribute added without thought about sensitivity defaults to
the wrong answer in one direction or the other. And redaction is lossy: once a
value is removed at the SDK, no amount of platform configuration recovers it.

## The bug that produced this design

The first version guessed, with substring rules: anything containing "token",
"key" or "secret" was sensitive.

That rule matched `aiobs.usage.input_tokens` and
`aiobs.latency.time_to_first_token_ms`. Every token count, every cost and every
latency chart in the product was silently zeroed. It was invisible because
redaction is _supposed_ to remove things — nothing looked broken, the numbers
were just wrong.

Enumerating the safe names instead was tried and is unmaintainable: the safe set
is open-ended and grows with every feature. The sensitive set is small, closed
and reviewable.

## Alternatives considered

**Server-side only.** Simpler, and the sensitive data has already crossed the
network and been written to a log buffer somewhere.

**SDK only.** Nothing protects an application that forgot to configure it, and
"the SDK does it" is not an answer a security review accepts.

**Machine-learning PII classification.** Non-deterministic, expensive on the hot
path, and produces false negatives that nobody notices. The detector hook exists
for teams who want to plug one in behind their own judgement.
