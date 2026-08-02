# Development setup

## Requirements

- **Python 3.10+** (3.11 or 3.12 recommended; CI runs 3.11)
- **Node 20.19+** (CI runs 22)
- **Docker** — optional, only for `make dev`

`make check-setup` reports what is missing and what version it found.

## Fastest path

```console
$ make setup      # virtualenv, Python and Node dependencies, .env from .env.example
$ make dev-local  # API on :58000 and a worker, on the host
```

`make dev-local` uses SQLite files under `.aiobs/` and needs no containers. It
starts in about two seconds.

In another terminal:

```console
$ make bootstrap                          # first organisation, project and API key
$ make seed PROJECT=prj_...               # demo traces, prompts, models, datasets
$ npm run dev --workspace @aiobs/web      # http://localhost:53000
```

The bootstrap output includes the API key. Copy it — it is shown once.

## Full stack in Docker

```console
$ make dev
```

Brings up PostgreSQL, ClickHouse, Redis, Redpanda, MinIO, the API, the worker
and the web application, and runs bootstrap. Ports are deliberately unusual
(55432, 58123, 56379, 59092, 59001, 58000, 53000) so the stack never collides
with a database you already run.

```console
$ make logs    # tail
$ make down    # stop
$ make down-volumes  # stop and delete the data
```

## Which one to use

|                       | `make dev-local` | `make dev` |
| --------------------- | ---------------- | ---------- |
| Start time            | ~2 s             | ~60 s      |
| Needs Docker          | no               | yes        |
| Analytics             | SQLite           | ClickHouse |
| Bus                   | database-backed  | Redpanda   |
| Closest to production | no               | yes        |

Use `dev-local` for everything except changes to the ClickHouse driver, the
Kafka consumer or the S3 object store — for those, use `dev`, because that is
where their behaviour actually differs.

## Environment

`.env` is created from `.env.example` by `make setup`. The Makefile exports the
local-development overrides itself, so a bare `.env` works.

Every setting is documented in
[operations/configuration.md](../operations/configuration.md). The ones you are
most likely to change locally:

```bash
AIOBS_LOG_LEVEL=debug
AIOBS_LOG_FORMAT=console        # human-readable instead of JSON
AIOBS_INGEST__ALLOW_ANONYMOUS_INGEST=true   # skip API keys while experimenting
```

## Resetting

```console
$ make reset      # delete .aiobs/ — all local traces, metadata and objects
$ make migrate && make bootstrap
```

Everything under `.aiobs/` is reproducible. Nothing there is precious.

## Running the tests

```console
$ make test          # unit, contract, integration
$ make test-all      # plus security, chaos, migration
$ make test-frontend # vitest component tests
$ make test-e2e      # Playwright, needs a running stack
```

The ClickHouse conformance suite is skipped unless a server is reachable:

```console
$ docker run -d --name ch -p 58123:8123 clickhouse/clickhouse-server:25.3-alpine
$ AIOBS_TEST_CLICKHOUSE_URL=http://localhost:58123 .venv/bin/pytest tests/integration
```

## Troubleshooting

**`ModuleNotFoundError: aiobs_api`** — the editable installs did not run.
`make install` again.

**Login fails in the browser with a network error** — a CORS mismatch. The API
allows `http://localhost:53000` and `http://127.0.0.1:53000` by default; if you
serve the web app from another port, add it to
`AIOBS_SECURITY__CORS_ALLOW_ORIGINS`.

**The UI shows no traces after seeding** — `seed-demo` writes to the
`development` environment by default and the UI defaults to `production`. Switch
the environment picker, or seed with `--environment production`.

**Port already in use** — everything is configurable:
`make dev-local API_PORT=58001`.

## See also

- [Workflow](workflow.md)
- [Code map](code-map.md)
- [Testing strategy](../testing/strategy.md)
