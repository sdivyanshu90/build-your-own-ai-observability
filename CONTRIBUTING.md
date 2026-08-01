# Contributing

## Getting set up

```console
$ make setup        # virtualenv, Python and Node dependencies, .env
$ make check-setup  # reports anything missing from your toolchain
$ make dev-local    # API and worker on the host, no Docker
```

`make dev-local` uses SQLite files under `.aiobs/`. Everything in that directory
is reproducible from `make migrate && make bootstrap && make seed`; delete it
whenever you want a clean slate (`make reset`).

## Before you open a pull request

```console
$ make lint        # ruff, prettier
$ make typecheck   # mypy, tsc
$ make test        # unit, contract, integration
```

`make test-all` additionally runs the security, chaos and migration suites, and
`make test-e2e` runs the browser journeys against a live stack. CI runs all of
them; running them locally is faster than finding out from a red build.

## What the review looks for

**Correctness of the boring things.** Money is `Decimal`, never `float`.
Timestamps are timezone-aware. A missing value renders as unknown, not as zero.
These are the defects that survive review because they look fine.

**Tests that assert on values.** `assert response.status_code == 200` passes
against an endpoint that returns an empty list forever. Assert the number, the
ordering, the exact decimal.

**Comments that explain why.** The code already says what it does. A comment
earns its place by explaining a constraint that is not visible locally: why the
lock is held across the write, why the dedup claim is released on failure, why
the sort is on UTF-16 code units. If a comment restates the line below it,
delete it.

**Error paths that are designed.** Every failure the user can cause has a code
in `ErrorCode` and a documented meaning. A 500 is a bug, not a category.

## Conventions

- **Python**: `ruff` for lint and format, `mypy --strict` where practical.
  Public functions have docstrings; private helpers have them when the reason
  they exist is not obvious.
- **TypeScript**: `tsc --strict` with `noUncheckedIndexedAccess`. Prettier for
  format.
- **Commits**: imperative subject, and a body that explains the reasoning if the
  change is not self-evident. "Fix cost rounding" is a subject; the body says
  which rounding, found how, and why the fix is the right one.
- **Migrations**: expand-only. Add a column, backfill, switch reads, drop later
  — three releases, not one. `make migrate-check` fails if models and
  migrations have drifted.

## Adding an attribute

Attributes live in one registry, `packages/shared-schemas/python/aiobs_schemas/semconv.py`.
Add it there, mark whether it is sensitive, then run `make generate` to
regenerate the TypeScript mirror. Never add an attribute name as a literal in
application code: the registry is what drives redaction, and an attribute the
registry does not know about is redacted by heuristic rather than by policy.

Prefer an existing OpenTelemetry convention over a new one. `gen_ai.*` names
come from the OTel GenAI semantic conventions; `aiobs.*` is for what those do
not cover.

## Adding an architecture decision record

Anything that would be expensive to reverse gets an ADR in `docs/adr/`. Copy the
most recent one for the format. Record the alternatives you rejected and why —
the value of an ADR is almost entirely in that section, because the decision
itself is visible in the code.

## Reporting a bug

Include the request id from the error response. Every response carries one, and
it correlates to the exact log line and, if tracing is on, to the trace of the
request that failed.
