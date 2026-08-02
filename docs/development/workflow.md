# Development workflow

## The loop

```console
$ make dev-local          # leave running
$ make test-unit          # fast, no services
$ make lint typecheck     # before you push
```

`make test-unit` is under ten seconds. Run it constantly. `make test` adds the
contract and integration suites and takes about two minutes.

## Adding an API endpoint

1. **Router** in `apps/api/src/aiobs_api/http/routers/`. Declare the required
   permission; do not check the role inline.
2. **Service** in `services/`. Routers do HTTP; services do work. A router that
   contains business logic cannot be tested without a client.
3. **Contract test** in `tests/contract/`. Assert the response _shape_ and the
   error envelope, not just the status code.
4. **Authorization test** in `tests/security/`. Add the endpoint to the
   cross-tenant matrix — the suite is parameterised, so an endpoint missing from
   it is a gap, not a pass.
5. `make openapi` to refresh the exported schema.

## Adding a queryable field

1. Add the column to `storage/analytics/columns.py`.
2. Add the `FieldSpec` to the relevant `ResourceSchema` in
   `storage/analytics/schemas.py`, with `sortable` set deliberately.
3. Write a ClickHouse migration.
4. Add a conformance test. It runs against both drivers automatically.

The `FieldSpec` is what makes the field filterable, sortable and aggregatable —
there is no second place to register it, and no way to filter on a field that is
not there.

## Adding a migration

```console
$ make migration MSG="add span reasoning tokens"
$ $EDITOR database/postgres/versions/<new file>
$ make migrate
$ make migrate-check     # fails if models and migrations disagree
```

**Expand only.** Add a column, backfill, switch reads, drop in a later release.
A migration that requires the new code to be deployed first cannot be run before
the rollout, and running it after means the new code runs against the old
schema.

Every migration must roll back. `tests/migration/` runs upgrade → downgrade →
upgrade and fails if it does not.

## Adding an attribute

See [contributing](../../CONTRIBUTING.md#adding-an-attribute). The short version:
one registry, `make generate`, and never a literal in application code.

## Regenerating

```console
$ make generate    # TypeScript semconv mirror, number fixture, SDK constants
$ make openapi     # openapi.json
$ make docs-check  # link check
```

`make generate` output is committed. CI does not regenerate it, so a stale
mirror is caught by the cross-language parity test rather than by a diff.

## Debugging

**Every response has a request id.** It is in the `X-Request-Id` header, in the
error envelope, and in every log line for that request.

```console
$ curl -i localhost:58000/v1/traces?project_id=... | grep x-request-id
$ grep req_8f3a2b1c logs/api.log
```

**Console logs locally.** `AIOBS_LOG_FORMAT=console` gives human-readable
output; JSON is the production default.

**The platform can trace itself**, but not to itself — pointing it at its own
endpoint creates a feedback loop, and startup validation refuses that
configuration.

## Before you push

```console
$ make lint typecheck test
```

CI runs lint, types, six Python suites, the ClickHouse conformance suite,
PostgreSQL migrations, the TypeScript suites, the browser journeys, the smoke
test, container builds and the documentation link check. Everything except the
container builds can be run locally.

## See also

- [Code map](code-map.md)
- [Testing strategy](../testing/strategy.md)
- [CONTRIBUTING.md](../../CONTRIBUTING.md)
