# Configuration reference

Every setting is an environment variable prefixed `AIOBS_`. Nested settings use
a double underscore: `AIOBS_DATABASE__URL`.

Settings are validated at startup. A process with an invalid configuration
fails immediately with a message naming the problem, rather than failing later
in a way that looks like a bug.

## Core

| Variable            | Default                  | Notes                                                         |
| ------------------- | ------------------------ | ------------------------------------------------------------- |
| `AIOBS_ENVIRONMENT` | `development`            | `production` enables the strict validation below              |
| `AIOBS_PUBLIC_URL`  | `http://localhost:58000` | The URL clients use. Appears in OpenAPI and bootstrap output. |
| `AIOBS_WEB_URL`     | `http://localhost:53000` | Used for links in exports and notifications                   |
| `AIOBS_LOG_LEVEL`   | `info`                   |                                                               |
| `AIOBS_LOG_FORMAT`  | `json`                   | `console` for human-readable local output                     |

## Database

| Variable                               | Default                                    |
| -------------------------------------- | ------------------------------------------ |
| `AIOBS_DATABASE__URL`                  | `sqlite+aiosqlite:///./.aiobs/metadata.db` |
| `AIOBS_DATABASE__POOL_SIZE`            | `10`                                       |
| `AIOBS_DATABASE__MAX_OVERFLOW`         | `20`                                       |
| `AIOBS_DATABASE__STATEMENT_TIMEOUT_MS` | `10000`                                    |

## Analytics

| Variable                                   | Default                 | Notes                                                 |
| ------------------------------------------ | ----------------------- | ----------------------------------------------------- |
| `AIOBS_ANALYTICS__DRIVER`                  | `sqlite`                | `clickhouse` in production; `sqlite` is refused there |
| `AIOBS_ANALYTICS__URL`                     | —                       | e.g. `http://clickhouse:8123`                         |
| `AIOBS_ANALYTICS__DATABASE`                | `aiobs`                 |                                                       |
| `AIOBS_ANALYTICS__USERNAME` / `__PASSWORD` | —                       |                                                       |
| `AIOBS_ANALYTICS__SQLITE_PATH`             | `./.aiobs/analytics.db` | development only                                      |
| `AIOBS_ANALYTICS__QUERY_TIMEOUT_MS`        | `30000`                 |                                                       |

## Key-value store

| Variable           | Default  | Notes                                                                                 |
| ------------------ | -------- | ------------------------------------------------------------------------------------- |
| `AIOBS_KV__DRIVER` | `memory` | `redis` in production; `memory` cannot enforce a limit across replicas and is refused |
| `AIOBS_KV__URL`    | —        | `redis://host:6379/0`                                                                 |

## Bus

| Variable                           | Default        | Notes                          |
| ---------------------------------- | -------------- | ------------------------------ |
| `AIOBS_BUS__DRIVER`                | `database`     | `kafka` in production          |
| `AIOBS_BUS__BROKERS`               | —              | comma-separated                |
| `AIOBS_BUS__TOPIC`                 | `aiobs.spans`  |                                |
| `AIOBS_BUS__CONSUMER_GROUP`        | `aiobs-worker` |                                |
| `AIOBS_BUS__MAX_DELIVERY_ATTEMPTS` | `5`            | then the dead-letter queue     |
| `AIOBS_BUS__LEASE_SECONDS`         | `120`          | longer than your slowest batch |

## Object storage

| Variable                                               | Default      | Notes                                                    |
| ------------------------------------------------------ | ------------ | -------------------------------------------------------- |
| `AIOBS_OBJECTS__DRIVER`                                | `filesystem` | `s3` in production; filesystem is node-local and refused |
| `AIOBS_OBJECTS__BUCKET`                                | —            |                                                          |
| `AIOBS_OBJECTS__REGION`                                | —            |                                                          |
| `AIOBS_OBJECTS__ENDPOINT_URL`                          | —            | only for non-AWS S3-compatible storage                   |
| `AIOBS_OBJECTS__ACCESS_KEY_ID` / `__SECRET_ACCESS_KEY` | —            | prefer IRSA or an instance role                          |

## Authentication

| Variable                                | Default   | Notes                                                               |
| --------------------------------------- | --------- | ------------------------------------------------------------------- |
| `AIOBS_AUTH__JWT_SECRET`                | —         | **required outside development.** Rotating invalidates every token. |
| `AIOBS_AUTH__API_KEY_PEPPER`            | —         | **required.** Rotating invalidates every API key.                   |
| `AIOBS_AUTH__ACCESS_TOKEN_TTL_SECONDS`  | `3600`    |                                                                     |
| `AIOBS_AUTH__REFRESH_TOKEN_TTL_SECONDS` | `2592000` |                                                                     |
| `AIOBS_AUTH__MAX_FAILED_LOGINS`         | `10`      | then a lockout window                                               |
| `AIOBS_AUTH__OIDC_ISSUER`               | —         | enables OIDC; local passwords can then be disabled                  |

## Security

| Variable                                 | Default                                               | Notes                                                                                                                                                               |
| ---------------------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AIOBS_SECURITY__CORS_ALLOW_ORIGINS`     | `["http://localhost:53000","http://127.0.0.1:53000"]` | A browser treats the two spellings of loopback as different origins. In production, https only — no loopback exemption.                                             |
| `AIOBS_SECURITY__CORS_ALLOW_CREDENTIALS` | `true`                                                |                                                                                                                                                                     |
| `AIOBS_SECURITY__COOKIE_SECURE`          | `true`                                                | must stay true outside development                                                                                                                                  |
| `AIOBS_SECURITY__MAX_REQUEST_BYTES`      | `8388608`                                             | checked before the body is read                                                                                                                                     |
| `AIOBS_SECURITY__TRUSTED_PROXY_HOPS`     | `0`                                                   | how many proxies sit in front. Too high lets a client spoof its address and evade rate limiting; too low makes every request appear to come from the load balancer. |
| `AIOBS_SECURITY__CURSOR_SECRET`          | —                                                     | HMAC key for pagination cursors                                                                                                                                     |
| `AIOBS_SECURITY__HSTS_MAX_AGE_SECONDS`   | `31536000`                                            |                                                                                                                                                                     |

## Ingest

| Variable                               | Default | Notes                                          |
| -------------------------------------- | ------- | ---------------------------------------------- |
| `AIOBS_INGEST__MAX_BATCH_SPANS`        | `2000`  | advertised at `/v1/ingest/limits`              |
| `AIOBS_INGEST__ALLOW_ANONYMOUS_INGEST` | `false` | development convenience; refused in production |
| `AIOBS_INGEST__DEFAULT_SAMPLING_RATE`  | `1.0`   |                                                |
| `AIOBS_INGEST__MAX_ATTRIBUTE_BYTES`    | `32768` | per attribute value                            |

## Retention

Organisation-wide defaults; per-project policies override them.

| Variable                            | Default |
| ----------------------------------- | ------- |
| `AIOBS_RETENTION__RAW_SPAN_DAYS`    | `30`    |
| `AIOBS_RETENTION__AGGREGATE_DAYS`   | `400`   |
| `AIOBS_RETENTION__PAYLOAD_DAYS`     | `14`    |
| `AIOBS_RETENTION__SWEEP_BATCH_SIZE` | `10000` |

## Telemetry

| Variable                          | Default                   | Notes                                                                                                |
| --------------------------------- | ------------------------- | ---------------------------------------------------------------------------------------------------- |
| `AIOBS_TELEMETRY__ENABLE_TRACING` | `false`                   |                                                                                                      |
| `AIOBS_TELEMETRY__OTLP_ENDPOINT`  | —                         | **not this platform.** Pointing it at itself creates a feedback loop; startup validation refuses it. |
| `AIOBS_TELEMETRY__EXCLUDED_PATHS` | `/health,/ready,/metrics` |                                                                                                      |

## What production refuses

`aiobs-admin check-config` reports all of these:

- the in-memory key-value driver (cannot enforce limits across replicas)
- the SQLite analytics driver (not a production analytics engine)
- the filesystem object store (node-local; payloads lost on rescheduling)
- anonymous ingest
- a wildcard CORS origin
- a plaintext (`http://`) credentialed CORS origin, loopback included
- `COOKIE_SECURE=false`
- an OTLP endpoint pointing at this platform's own public URL

## Price books

Price books are data, not configuration. Manage them through the API or the
settings UI:

```console
$ aiobs-admin price-books                  # list
$ curl -X POST "$AIOBS_ENDPOINT/v1/price-books" -d @book.json
```

A book published for an organisation overrides the built-in default. See
[concepts/cost-attribution.md](../concepts/cost-attribution.md).
