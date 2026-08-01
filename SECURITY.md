# Security

## Reporting a vulnerability

Email **security@example.com** with a description, the affected version, and a
reproduction if you have one. Please do not open a public issue.

We aim to acknowledge within two working days and to ship a fix or a mitigation
within 30 days for anything that allows cross-tenant access, authentication
bypass, or the disclosure of stored prompts and completions.

## What this platform holds

Assume the worst case, because it is the common one: **prompts and completions
contain personal data, credentials pasted by users, and proprietary business
logic**. A trace store is a high-value target that looks like a low-value one.

The design consequences are documented in
[docs/security/data-handling.md](docs/security/data-handling.md); the summary:

- **Two layers of redaction.** The SDK redacts before anything leaves your
  process, so a platform operator never sees what you removed. The platform
  redacts again on ingestion, so an application that forgot to configure the
  SDK is still covered. Neither layer trusts the other.
- **Payload storage is optional and per environment.** Production environments
  default to storing no payloads at all — only the shape of the request.
- **Retention is three independent horizons.** Payloads expire first, raw spans
  next, aggregates last. Deleting the text you must not keep does not cost you
  the dashboards.
- **Deletion is real.** A subject-deletion request removes payloads and subject
  identifiers immediately, not at the next retention sweep.

## Security properties

| Property         | How                                                                                                                               |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Passwords        | Argon2id, per-user salt, parameters in `docs/security/authentication.md`                                                          |
| API keys         | Keyed SHA-256 (HMAC with a server-side pepper). Shown once at creation, never retrievable.                                        |
| Tokens           | Short-lived JWTs with a per-user epoch, so revocation is immediate rather than eventual                                           |
| Tenant isolation | Every analytics query carries an organisation predicate constructed by the store, not by the caller. Tested in `tests/security/`. |
| Query injection  | A closed filter grammar. No user string ever becomes SQL text; column names come from a registry and values are always bound.     |
| Cursors          | HMAC-signed. A tampered cursor is rejected, not executed.                                                                         |
| Transport        | HSTS, strict CSP, `X-Content-Type-Options`, `X-Frame-Options: DENY`, no `Referer` leakage                                         |
| Rendering        | User content is rendered as text. `dangerouslySetInnerHTML` appears nowhere in the codebase.                                      |

## Hidden reasoning

The platform does not require, request, or provide storage for private
chain-of-thought. Agent steps record what the application chose to record: a
short decision summary and the observable action. There is no field for raw
model reasoning and no UI that would display it.

## What startup validation refuses

A process configured for `production` will not start with an in-memory rate
limiter, the SQLite analytics driver, a filesystem object store, anonymous
ingest enabled, a wildcard CORS origin, a plaintext credentialed CORS origin, or
insecure cookies. These are the misconfigurations that are easy to carry from a
laptop into production without noticing.

Run `aiobs-admin check-config` to see the full report for any environment.

## Supported versions

Pre-1.0. Security fixes land on `main`; there is no backport branch yet.
