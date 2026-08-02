# ADR-0011: Keyed SHA-256 for API keys, not Argon2id

## Status

Accepted.

## Context

User passwords are hashed with Argon2id, correctly: they are low-entropy,
human-chosen, and reused across sites, so a slow hash is the defence against
offline cracking of a stolen database.

API keys are verified on **every ingest request** — thousands per second. At
Argon2id's intended cost of ~100 ms, verification alone would cap the platform
at ten requests per second per core.

## Decision

API keys are hashed with **HMAC-SHA-256 using a server-side pepper**
(`AIOBS_AUTH__API_KEY_PEPPER`), not with Argon2id.

Passwords remain Argon2id.

## Consequences

**Good.** Verification is microseconds, so ingest throughput is a function of
the work being done rather than of a password-hashing parameter. Because the
pepper is not in the database, an attacker with a database dump cannot begin an
offline attack without also compromising the application configuration.

**Costs.** Rotating the pepper invalidates every issued API key, and the stored
hashes cannot be recomputed — the input is the secret nobody has. Rotation is
therefore a migration with an announcement, documented in
[operations/secrets.md](../operations/secrets.md).

This departs from "always use a slow hash", which is a rule people apply without
checking whether its premise holds. Writing down why is the point of this ADR.

## Why this is safe

The premise of a slow hash is that the input is guessable. It is not, here:

- The key is **256 bits of cryptographic randomness**. There is no dictionary,
  no password-reuse corpus, no rainbow table.
- Brute-forcing 256 bits is not a threat model, it is a physics problem.
- The pepper adds a second factor an attacker must obtain separately.

The slow hash is defending against a threat that does not exist for this input,
at a cost that would be paid on every request.

## Alternatives considered

**Argon2id with reduced parameters.** Slower than SHA-256 without being
meaningfully harder to attack, and it invites someone to "fix" the parameters
back to the recommended values and halt ingestion.

**Cache Argon2id verifications.** A cache of verified secrets in memory, which
is a plaintext credential store with a TTL.

**Store keys encrypted rather than hashed.** Then the platform can decrypt them,
which means an attacker with the key-encryption key gets every API key in clear.
Hashing means there is nothing to steal.

**Bearer tokens instead of API keys.** Refreshing a token from a batch job or a
serverless function is real operational burden, and the failure mode — telemetry
silently stops when a refresh fails — is bad.
