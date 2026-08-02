# Authentication and authorization

## Two credential types

**Bearer tokens** for humans. Short-lived JWTs issued at sign-in, refreshed with
a longer-lived refresh token.

**API keys** for machines. Long-lived, scoped to a project and environment,
presented as `X-API-Key` or `Authorization: Bearer`.

They are validated differently, and the difference is deliberate.

## Passwords: Argon2id

```
memory   64 MiB
time     3 iterations
lanes    4
salt     16 bytes, per user
```

Verification is constant-time, and a login against a non-existent account still
performs the hash — otherwise response time distinguishes "no such user" from
"wrong password", and the login form becomes an account-enumeration oracle. The
error message is identical for both, and an end-to-end test asserts that the two
messages are byte-identical.

Ten failed attempts triggers a lockout window. The counter is committed even
when the attempt fails: an earlier version incremented it inside the transaction
that then raised, rolling the increment back and making the lockout unreachable.

## API keys: keyed SHA-256, not Argon2id

A deliberate, documented departure.

Argon2id is designed to be slow — that is its value against offline cracking of
a stolen password database. An API key is checked on **every ingest request**, at
thousands per second. A 100 ms verification would make the platform's throughput
a function of its password hash parameters.

So: HMAC-SHA-256 with a server-side pepper. The security argument is different
and it holds:

- The key is **256 bits of cryptographic randomness**, not a human-chosen
  password. There is no dictionary to attack.
- The pepper is **not in the database**. An attacker with a database dump cannot
  even begin an offline attack without also compromising the application
  configuration.
- Brute-forcing 256 bits of entropy is not a threat model, it is a physics
  problem.

Format: `aiobs_<env>_<prefix>_<secret>`. The prefix is stored in clear so a key
can be identified in a list; the secret is never stored at all.

**Shown once, at creation.** There is no endpoint that returns it, because there
is nothing to return.

## Tokens and immediate revocation

Access tokens carry a **user epoch**. Bumping it — on password change, role
change, or explicit revocation — invalidates every token that user holds,
without a blacklist and without waiting for expiry.

The epoch is read on every request from PostgreSQL, cached briefly. That cost on
the hot path is paid deliberately: "revoked, but their token works for another
55 minutes" is not revocation.

Expiry is checked against the injected clock, not the wall clock. This sounds
pedantic until a test issues a token with a frozen clock and the library
validates it against the real one, at which point every auth test is flaky.

## OIDC

Set `AIOBS_AUTH__OIDC_ISSUER` and local passwords can be disabled entirely.
Group claims map to roles through a configurable mapping. The provider is the
source of truth for identity; the platform still owns authorization.

## Authorization

A matrix, not scattered conditionals:

```
role → set of permissions
endpoint → required permission
```

Adding a permission means adding a row. The matrix is what the tests assert
against, so a router that forgets to declare a permission fails the suite rather
than shipping open.

| Role            | Permissions                                                        |
| --------------- | ------------------------------------------------------------------ |
| `owner`         | everything, including organisation deletion and ownership transfer |
| `administrator` | members, keys, retention, price books, projects                    |
| `developer`     | read telemetry, write registries, manage own keys                  |
| `analyst`       | read telemetry and registries, create exports                      |
| `viewer`        | read telemetry and registries                                      |

**Escalation is blocked**: no member may grant a role above their own, and the
last owner cannot be removed or demoted.

## API key scopes

| Scope    | Grants                                                                  |
| -------- | ----------------------------------------------------------------------- |
| `ingest` | Send telemetry; read prompts and models so the SDK can resolve versions |
| `read`   | Read traces, spans, metrics, costs and registries for the project       |

Deliberately coarse. Anything richer is an account with a role, not a key.

## The audit log

Append-only. Every privileged action: authentication, key issuance and
revocation, role changes, exports, retention changes, price book edits.

Each entry carries the actor, the action, the resource, the outcome and the
**request id** — so an audit entry correlates to the exact API log line that
produced it.

## See also

- [Multi-tenancy](../architecture/multi-tenancy.md)
- [Threat model](threat-model.md)
- [ADR-0011: keyed hashes for API keys](../adr/0011-api-key-hashing.md)
