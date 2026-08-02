# Multi-tenancy

## The hierarchy

```
Organization          the tenant boundary; billing, members, price books
  └── Project         an application; retention policy, API keys
        └── Environment   development | staging | production
```

Every stored row carries `organization_id`, `project_id` and `environment`.
Every query filters on at least the first.

## Isolation is enforced, not intended

Three layers, and each assumes the others may fail:

**1. The principal carries the tenant.** A token or API key resolves to a
`Principal` with an organisation id. It is not a parameter the request supplies.

**2. The store adds the predicate.** `AnalyticsScope` is required by every
analytics method, and the SQL builder emits the tenancy predicate first, always.
There is no code path that builds an analytics query without one — not "we
remember to add it", but "the function will not compile without it".

**3. Resource lookups verify ownership.** Fetching a prompt by id checks it
belongs to the caller's organisation, and returns **404, not 403**, when it does
not. A 403 confirms the resource exists, which turns an id into an oracle.

## Cross-tenant tests

`tests/security/test_authorization.py` is written from the attacker's side: a
principal in organisation A attempts every read and write against a resource in
organisation B, and asserts 404 or 403 for each. It is parameterised over the
endpoints, so adding an endpoint without adding it to the matrix fails the
suite.

## Roles

| Role            | Can                                                                        |
| --------------- | -------------------------------------------------------------------------- |
| `owner`         | Everything, including deleting the organisation and transferring ownership |
| `administrator` | Manage members, keys, retention, price books, projects                     |
| `developer`     | Read all telemetry, write registries, manage their own API keys            |
| `analyst`       | Read telemetry and registries, create exports                              |
| `viewer`        | Read telemetry and registries                                              |

Enforced as a permission matrix — role → set of permissions, endpoint →
required permission — not as `if role == "admin"` scattered through the
routers. Adding a permission means adding a row, and the matrix is the thing the
tests assert against.

**Privilege escalation is blocked explicitly**: a member cannot grant a role
higher than their own, and the last owner cannot be removed or demoted. An
organisation with no owner is unadministrable and undeletable.

## API keys

Scoped to a project and an environment, with coarse permissions:

| Scope    | Grants                                                                  |
| -------- | ----------------------------------------------------------------------- |
| `ingest` | Send telemetry; read prompts and models so the SDK can resolve versions |
| `read`   | Read traces, spans, metrics, costs and registries for the project       |

Deliberately coarse. A key that can administer the tenant is not an SDK
credential, it is an account, and it should go through a role.

Keys are stored as **keyed hashes** — HMAC-SHA-256 with a server-side pepper.
The full secret is shown once at creation and is never retrievable. See
[security/authentication.md](../security/authentication.md) for why the key hash
is not Argon2id.

## Revocation is immediate

Access tokens carry a **user epoch**. Bumping the epoch — on password change,
role change or explicit revocation — invalidates every token that user holds,
without a token blacklist and without waiting for expiry.

The epoch is checked on every request, from PostgreSQL, cached briefly. That is
a cost on the hot path, paid deliberately: "revoked, but their token works for
another 55 minutes" is not revocation.

## Noisy neighbours

Rate limits are per API key and per organisation. Query cost is bounded by the
maximum time range (400 days), the maximum page size, and the group-cardinality
cap — one tenant cannot ask a question expensive enough to affect another.

## See also

- [Security: authentication](../security/authentication.md)
- [Security: threat model](../security/threat-model.md)
