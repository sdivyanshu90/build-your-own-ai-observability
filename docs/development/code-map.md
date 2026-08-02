# Code map

Where to look, and what you will find.

## `apps/api/src/aiobs_api/`

| Path                                    | What lives there                                                                                                              |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `core/config.py`                        | Every setting, its default, and `validate_for_runtime()` — the guard that refuses development-shaped production configuration |
| `core/query.py`                         | The filter and sort grammar, keyset cursors, `CursorCodec`                                                                    |
| `core/errors.py`                        | The error taxonomy. Every failure a user can cause has a code here.                                                           |
| `core/logging.py`                       | Structured logging and request-context binding                                                                                |
| `domain/cost.py`                        | The cost engine: price rules, snapshots, exact decimal computation                                                            |
| `domain/rbac.py`                        | Roles, permissions, API key scopes, and the matrix between them                                                               |
| `domain/principal.py`                   | Who is making a request, and what tenant they belong to                                                                       |
| `http/app.py`                           | Application assembly, middleware order (with the reasoning)                                                                   |
| `http/middleware.py`                    | Request context, security headers, body limits, access log, the unhandled-error trap                                          |
| `http/errors.py`                        | The error envelope, and the handler for every exception type                                                                  |
| `http/routers/`                         | HTTP surface. Thin: parse, authorise, delegate, serialise.                                                                    |
| `ingest/normalizer.py`                  | Wire span → storage rows, including the raw/redacted split                                                                    |
| `ingest/rollup.py`                      | Trace roll-up computation                                                                                                     |
| `services/`                             | Business logic. This is where to look for how something actually works.                                                       |
| `storage/postgres/`                     | SQLAlchemy models and the `UtcDateTime` / `DecimalText` type decorators                                                       |
| `storage/analytics/sqlbase.py`          | Dialect-agnostic SQL, written once for both drivers                                                                           |
| `storage/analytics/clickhouse_store.py` | Production driver                                                                                                             |
| `storage/analytics/sqlite_store.py`     | Development driver, held to the same conformance suite                                                                        |
| `storage/analytics/schemas.py`          | The only place a user-facing field name maps to a column                                                                      |
| `storage/bus/`                          | At-least-once bus: leases, backoff, DLQ, replay                                                                               |
| `demo_data.py`                          | Deterministic demo generator; writes through the real ingest path                                                             |
| `cli.py`                                | `aiobs-admin`: bootstrap, seed, migrate-analytics, check-config, check-dependencies                                           |

## `apps/worker/src/aiobs_worker/`

| Path           | What                                                             |
| -------------- | ---------------------------------------------------------------- |
| `main.py`      | Supervisor, graceful shutdown, heartbeat and `--health-check`    |
| `consumers.py` | Bus consumers with lease renewal and backoff                     |
| `jobs.py`      | Retention sweep, orphan collection, reconciliation, outbox drain |

## `apps/web/`

| Path                           | What                                                                      |
| ------------------------------ | ------------------------------------------------------------------------- |
| `lib/api.ts`                   | The only place that talks to the API. Auth, errors, the query grammar.    |
| `lib/format.ts`                | Presentation rules: money never becomes a float, unknown is never zero    |
| `app/providers.tsx`            | React Query configuration and the workspace (project, environment, range) |
| `components/ui.tsx`            | Primitives. Every state — loading, empty, partial, error — is designed.   |
| `components/Waterfall.tsx`     | The span tree, critical path, keyboard navigation                         |
| `components/RetrievalView.tsx` | Pipeline stages, ranked documents, rank movement                          |
| `components/AgentGraph.tsx`    | Deterministic DAG layout                                                  |
| `components/Charts.tsx`        | Hand-rolled SVG: partial buckets, accessible tables, decimal-string money |

## `packages/`

| Path                                               | What                                                            |
| -------------------------------------------------- | --------------------------------------------------------------- |
| `shared-schemas/python/aiobs_schemas/semconv.py`   | The attribute registry. Drives redaction.                       |
| `shared-schemas/python/aiobs_schemas/canonical.py` | RFC 8785 canonical JSON, including ECMAScript number formatting |
| `shared-schemas/typescript/src/`                   | The TypeScript mirror, generated from the above                 |
| `python-sdk/src/aiobs/`                            | Tracer, exporter, context propagation, redaction, integrations  |
| `typescript-sdk/src/`                              | The same, for Node                                              |
| `provider-adapters/`                               | Normalising provider responses into token usage                 |

## `tests/`

| Path           | Runs                                                          |
| -------------- | ------------------------------------------------------------- |
| `unit/`        | Pure logic, property-based where it earns it                  |
| `contract/`    | The API surface and cross-language parity                     |
| `integration/` | Real dependencies, including the two-driver conformance suite |
| `security/`    | Cross-tenant access, injection, privilege escalation          |
| `chaos/`       | Dependency failures and degradation behaviour                 |
| `migration/`   | Upgrade, downgrade, upgrade; drift detection                  |
| `performance/` | Load harness (Python and k6). Measures; asserts nothing.      |

## Where to start reading

To understand **ingestion**: `http/routers/ingest.py` → `services/ingestion.py`
→ `ingest/normalizer.py` → `services/processing.py`.

To understand **query**: `core/query.py` → `storage/analytics/schemas.py` →
`storage/analytics/sqlbase.py`.

To understand **cost**: `domain/cost.py`, then
[concepts/cost-attribution.md](../concepts/cost-attribution.md).
