# Testing strategy

## The rule

**Assert on values, not on status codes.** `assert response.status_code == 200`
passes against an endpoint that returns an empty list forever. Assert the
number, the ordering, the exact decimal, the specific error code.

Most of the defects found while building this were found by tests that asserted
a value: token counts zeroed by a redaction rule, retrieval documents that were
always empty, a cost total with float artefacts, a sort field the API rejected.
Every one of those returned 200.

## The suites

| Suite                | Runs against               | Answers                                                      |
| -------------------- | -------------------------- | ------------------------------------------------------------ |
| `tests/unit/`        | nothing external           | Does the logic hold, including at the edges?                 |
| `tests/contract/`    | an in-process app          | Does the API keep its shape, and do the two languages agree? |
| `tests/integration/` | real dependencies          | Do the pieces work together, on both storage drivers?        |
| `tests/security/`    | an in-process app          | Can a tenant reach another tenant? Can a role escalate?      |
| `tests/chaos/`       | broken dependencies        | Does it degrade the way the docs say?                        |
| `tests/migration/`   | a real database            | Does every migration roll back? Have the models drifted?     |
| `apps/web/test/`     | jsdom                      | Do the components render every state correctly?              |
| `apps/web/e2e/`      | a real browser, a real API | Do the journeys work end to end?                             |
| `tests/performance/` | a deployment               | Measures. Asserts nothing.                                   |

## The conformance suite

`tests/integration/test_analytics_conformance.py` runs **every test against both
storage drivers**. SQLite and ClickHouse are held to identical behaviour by one
suite.

That is what makes a second implementation an asset rather than a divergence
risk: "it works locally but not in production" becomes a test failure. The
ClickHouse parameterisation skips unless a server is reachable, so the suite is
useful on a laptop and complete in CI.

```console
$ AIOBS_TEST_CLICKHOUSE_URL=http://localhost:58123 pytest tests/integration
```

## Property-based tests

Used where the input space is large and the invariant is simple:

- **Canonical JSON**: round-trips, is deterministic, produces parseable output.
  This is how the NFC key bug was found — hypothesis generated `{'שׁ': None}`,
  a key that changes under normalisation, and the canonicaliser raised
  `KeyError` on it.
- **The filter parser**: hostile strings never produce SQL, only a validation
  error.
- **Cursors**: encode/decode round-trips exactly, including decimals.

## Cross-language parity

`tests/contract/test_cross_language_parity.py` asserts that Python and
TypeScript produce byte-identical canonical JSON and identical SHA-256 digests,
against a 339-case number fixture pinning ECMAScript's `String(Number)`.

Without it, a prompt registered by the Python SDK and the same prompt registered
by the TypeScript SDK would produce different version ids, and the registries
would quietly fill with duplicates.

## Cross-tenant tests

`tests/security/test_authorization.py` is written from the attacker's side: a
principal in organisation A attempts every read and write against a resource in
organisation B. It is parameterised over the endpoints, so adding an endpoint
without adding it to the matrix fails the suite.

## Browser journeys

Playwright, against a **real API with real data** — not mocks. The failures
worth catching here only happen when both halves are present: a filter the
server rejects, a cursor that does not round-trip, a sort grammar the client got
wrong, a 401 loop.

Three real defects were found this way and are now regression-tested: the sort
parameter format, the API key scope names, and a workspace that stayed empty
after sign-in because its provider had mounted before the token existed.

## Chaos tests

Assert the degradation the operations docs promise:

- ClickHouse down → ingest still accepts, dashboards return an error rather than
  an empty chart
- Redis down → rate limiting degrades to permissive, and says so in the health
  check
- Object storage down → spans stored, payloads not, and the span records that
- Bus down → 503, because there is nowhere to buffer

Documented behaviour that is not tested is a wish.

## Performance

`tests/performance/run_load_test.py` measures and prints. It reports **achieved**
rate rather than requested, separates client-side backpressure from server
latency, and asserts no thresholds — publishing a number measured on other
hardware is worse than publishing none.

A k6 profile (`ingest.k6.js`) exists for when the Python client is the
bottleneck.

## Running

```console
$ make test          # unit, contract, integration
$ make test-all      # plus security, chaos, migration
$ make test-frontend
$ make test-e2e      # needs a running stack
$ make coverage
```

## What is not tested

**Provider APIs.** The adapters are tested against recorded fixtures. Calling
OpenAI in CI would be slow, flaky and expensive, and it would test their uptime
rather than our code.

**ClickHouse itself.** The conformance suite tests our use of it.

**Visual appearance.** Snapshot tests of a rendered chart fail on every font
change and catch approximately nothing. The components assert structure and
accessibility instead: the tree levels, the ARIA roles, the accessible table
beside every chart.
