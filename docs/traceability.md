# Requirement traceability matrix

Maps each required capability to the code that implements it and the tests that
prove it. Test counts are from `pytest --collect-only` and `vitest --run` at the
time of writing.

**Legend**

- **Verified** — implemented, and exercised by a test that was executed
  successfully in this environment.
- **Implemented, not executable here** — the code exists, but the tooling needed
  to run it (Helm, kubectl, Terraform, k6) is not installed in this environment.

---

## 1. End-to-end request tracing

| Requirement                          | Implementation                                                     | Tests                                                                                 | Status   |
| ------------------------------------ | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------- | -------- |
| Trace and span model                 | `packages/shared-schemas/.../wire.py`, `storage/analytics/rows.py` | `tests/unit/test_analysis.py`, conformance suite                                      | Verified |
| OTLP HTTP ingest (protobuf and JSON) | `http/routers/otlp.py`, `ingest/otlp.py`                           | `tests/contract/test_api_contract.py`, `tests/integration/test_ingestion_pipeline.py` | Verified |
| Native batch ingest                  | `http/routers/ingest.py`, `services/ingestion.py`                  | `tests/integration/test_ingestion_pipeline.py`, `scripts/smoke-test.py`               | Verified |
| W3C context propagation              | `packages/*/context.*`                                             | `tests/unit/`, `packages/typescript-sdk/test/tracer.test.ts`                          | Verified |
| Span links                           | `ingest/normalizer.py`, `components/Waterfall.tsx`                 | conformance suite, `apps/web/test/waterfall.test.tsx`                                 | Verified |
| Cross-service traces                 | demo `demos/distributed-ai-request/`                               | executed manually; one trace across two services                                      | Verified |
| Critical path                        | `ingest/rollup.py`                                                 | `tests/unit/test_analysis.py`                                                         | Verified |
| Retry grouping                       | `ingest/rollup.py`, `components/Waterfall.tsx`                     | `tests/unit/test_analysis.py`                                                         | Verified |
| Waterfall UI                         | `apps/web/components/Waterfall.tsx`                                | `apps/web/test/waterfall.test.tsx` (11), `e2e/tracing.spec.ts`                        | Verified |
| Orphan and late span handling        | `ingest/normalizer.py`, `services/processing.py`                   | conformance suite, `tests/integration/`                                               | Verified |
| Idempotent ingest                    | `services/ingestion.py`, `ReplacingMergeTree` / `ON CONFLICT`      | conformance suite (both drivers)                                                      | Verified |

## 2. Prompt, model and dataset versioning

| Requirement                              | Implementation                                       | Tests                                                                                                            | Status   |
| ---------------------------------------- | ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- | -------- |
| Content-addressed immutable versions     | `services/registry.py`, `aiobs_schemas/canonical.py` | `tests/unit/test_canonical.py` (43)                                                                              | Verified |
| Movable aliases with rollback target     | `services/registry.py::promote_alias`                | `tests/integration/`, `e2e/platform.spec.ts`                                                                     | Verified |
| Prompt diff (message and variable level) | `services/registry.py::diff_prompt_versions`         | `tests/integration/`                                                                                             | Verified |
| Model configuration versions             | `services/registry.py::ModelRegistry`                | `tests/integration/`, `e2e/platform.spec.ts`                                                                     | Verified |
| Dataset versions by file manifest        | `services/registry.py::DatasetRegistry`              | `tests/integration/`                                                                                             | Verified |
| Cross-language hash parity               | both `canonical` implementations                     | `tests/contract/test_cross_language_parity.py`, `packages/shared-schemas/typescript/test/canonical.test.ts` (16) | Verified |
| Lineage on traces and spans              | `ingest/normalizer.py`, trace detail UI              | `e2e/tracing.spec.ts`                                                                                            | Verified |
| Registry UI                              | `apps/web/app/{prompts,models,datasets}/`            | `e2e/platform.spec.ts`                                                                                           | Verified |

## 3. Retrieval visualisation

| Requirement                                                                | Implementation                                       | Tests                                                             | Status   |
| -------------------------------------------------------------------------- | ---------------------------------------------------- | ----------------------------------------------------------------- | -------- |
| Pipeline stage reconstruction                                              | `services/traces.py`, `components/RetrievalView.tsx` | `apps/web/test/retrieval.test.tsx` (10)                           | Verified |
| Per-document rank, score and selection                                     | `storage/analytics/rows.py::RetrievalDocumentRow`    | conformance suite                                                 | Verified |
| Rank movement through reranking                                            | `services/traces.py`, `RetrievalView.tsx`            | `apps/web/test/retrieval.test.tsx`                                | Verified |
| Diagnostics (unused ratio, margin, duplicates, truncation, missing source) | `services/traces.py`                                 | `apps/web/test/retrieval.test.tsx`, `tests/unit/test_analysis.py` | Verified |
| Derived rows built before redaction                                        | `ingest/normalizer.py`                               | `tests/integration/test_ingestion_pipeline.py`                    | Verified |
| Retrieval UI                                                               | `apps/web/components/RetrievalView.tsx`              | `e2e/tracing.spec.ts`                                             | Verified |

## 4. Agent trajectory visualisation

| Requirement                                                          | Implementation                                             | Tests                                     | Status   |
| -------------------------------------------------------------------- | ---------------------------------------------------------- | ----------------------------------------- | -------- |
| Step model (type, agent, branch, retry, loop, approval, termination) | `storage/analytics/rows.py::AgentStepRow`                  | conformance suite                         | Verified |
| Graph construction                                                   | `services/traces.py::AgentGraph`                           | `tests/unit/test_analysis.py`             | Verified |
| Deterministic layout                                                 | `apps/web/components/AgentGraph.tsx::computeLayout`        | `apps/web/test/agent-graph.test.tsx` (16) | Verified |
| Branch, retry, loop and handoff edges                                | `AgentGraph.tsx`                                           | `apps/web/test/agent-graph.test.tsx`      | Verified |
| No hidden chain-of-thought stored or displayed                       | registry has no such attribute; `AgentGraph.tsx` states it | `apps/web/test/agent-graph.test.tsx`      | Verified |
| Trajectory UI                                                        | `apps/web/components/AgentGraph.tsx`                       | `e2e/tracing.spec.ts`                     | Verified |

## 5. Latency, token and cost monitoring

| Requirement                           | Implementation                                                    | Tests                                                                                              | Status   |
| ------------------------------------- | ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- | -------- |
| Exact decimal money, end to end       | `domain/cost.py`, `storage/analytics/*`, `apps/web/lib/format.ts` | `tests/unit/test_cost.py`, conformance `TestMoneyAggregation`, `apps/web/test/format.test.ts` (17) | Verified |
| Effective-dated price books           | `services/pricing.py`, `domain/cost.py`                           | `tests/unit/test_cost.py`                                                                          | Verified |
| Volume and graduated tiers            | `domain/cost.py`                                                  | `tests/unit/test_cost.py`                                                                          | Verified |
| Cache-inclusive/exclusive conventions | `domain/cost.py`                                                  | `tests/unit/test_cost.py`                                                                          | Verified |
| Auditable cost formula                | `domain/cost.py::CostBreakdown`, span detail UI                   | `tests/unit/test_cost.py`, `apps/web/test/waterfall.test.tsx`                                      | Verified |
| `UNPRICED` never treated as zero      | `domain/cost.py`, `services/metrics.py`                           | `tests/unit/test_cost.py`, conformance suite                                                       | Verified |
| Token provenance                      | `ingest/normalizer.py`, `provider-adapters`                       | `tests/unit/`, `tests/integration/`                                                                | Verified |
| Percentiles computed over raw rows    | `storage/analytics/*::percentiles`                                | conformance suite (both drivers)                                                                   | Verified |
| Partial buckets labelled              | `services/metrics.py`, `components/Charts.tsx`                    | `apps/web/test/charts.test.tsx` (11)                                                               | Verified |
| Multi-currency refusal to sum         | `domain/cost.py::MultiCurrencyTotal`                              | `tests/unit/test_cost.py`                                                                          | Verified |
| Cost and latency dashboards           | `apps/web/app/{costs,latency}/`                                   | `e2e/platform.spec.ts`                                                                             | Verified |

## 6. Platform requirements

| Requirement                           | Implementation                                          | Tests                                                                    | Status   |
| ------------------------------------- | ------------------------------------------------------- | ------------------------------------------------------------------------ | -------- |
| Multi-tenancy and isolation           | `domain/principal.py`, `storage/analytics/sqlbase.py`   | `tests/security/test_authorization.py` (25)                              | Verified |
| RBAC matrix                           | `domain/rbac.py`                                        | `tests/security/`, `tests/unit/test_rbac_and_redaction.py`               | Verified |
| API keys stored as hashes, shown once | `services/auth.py`                                      | `tests/security/`, `e2e/platform.spec.ts`                                | Verified |
| Immediate revocation via token epoch  | `services/auth.py`                                      | `tests/security/`                                                        | Verified |
| Two-layer redaction                   | `packages/*/redaction.*`, `ingest/normalizer.py`        | `tests/unit/test_rbac_and_redaction.py`, `packages/typescript-sdk/test/` | Verified |
| Closed filter and sort grammar        | `core/query.py`, `storage/analytics/schemas.py`         | `tests/unit/`, `tests/security/`, `e2e/tracing.spec.ts`                  | Verified |
| Keyset pagination with signed cursors | `core/query.py::CursorCodec`                            | conformance suite, `tests/contract/`                                     | Verified |
| Structured error envelope             | `http/errors.py`, `aiobs_schemas/errors.py`             | `tests/contract/test_api_contract.py` (30)                               | Verified |
| Errors readable cross-origin          | `http/middleware.py::UnhandledErrorMiddleware`          | `tests/contract/test_api_contract.py::TestCrossOriginBehaviour`          | Verified |
| Audit log                             | `services/audit.py`                                     | `tests/security/`, `e2e/platform.spec.ts`                                | Verified |
| Retention with three horizons         | `apps/worker/jobs.py`                                   | `tests/integration/`                                                     | Verified |
| Startup configuration validation      | `core/config.py::validate_for_runtime`                  | `tests/unit/test_configuration_guards.py` (7)                            | Verified |
| Idempotent bootstrap                  | `cli.py::_bootstrap`, `services/organizations.py`       | `tests/integration/test_bootstrap.py` (5)                                | Verified |
| Graceful shutdown                     | `http/app.py`, `apps/worker/main.py`                    | `tests/chaos/`                                                           | Verified |
| Health, readiness and liveness        | `http/routers/health.py`, `aiobs-worker --health-check` | `tests/contract/`, executed manually                                     | Verified |
| Dependency-failure degradation        | `container.py`, services                                | `tests/chaos/test_dependency_failures.py` (10)                           | Verified |
| At-least-once bus with DLQ and replay | `storage/bus/`                                          | `tests/integration/`                                                     | Verified |
| Migrations, reversible, drift-checked | `database/postgres/`                                    | `tests/migration/test_migrations.py` (7)                                 | Verified |
| OpenAPI schema                        | `http/app.py`                                           | `tests/contract/` — 53 paths                                             | Verified |

## 7. Frontend

| Requirement                                           | Implementation                                 | Tests                                                     | Status   |
| ----------------------------------------------------- | ---------------------------------------------- | --------------------------------------------------------- | -------- |
| Login                                                 | `app/login/page.tsx`                           | `e2e/auth.spec.ts`                                        | Verified |
| Overview dashboard                                    | `app/page.tsx`                                 | `e2e/tracing.spec.ts`                                     | Verified |
| Trace explorer with URL-addressable filters           | `app/traces/page.tsx`                          | `e2e/tracing.spec.ts`                                     | Verified |
| Trace detail, waterfall, span detail                  | `app/traces/[traceId]/page.tsx`                | `apps/web/test/waterfall.test.tsx`, `e2e`                 | Verified |
| Retrieval view                                        | `components/RetrievalView.tsx`                 | `apps/web/test/retrieval.test.tsx`, `e2e`                 | Verified |
| Agent trajectory view                                 | `components/AgentGraph.tsx`                    | `apps/web/test/agent-graph.test.tsx`, `e2e`               | Verified |
| Trace comparison                                      | `app/traces/compare/page.tsx`                  | `e2e/tracing.spec.ts`                                     | Verified |
| Latency dashboard                                     | `app/latency/page.tsx`                         | `e2e/platform.spec.ts`                                    | Verified |
| Cost dashboard                                        | `app/costs/page.tsx`                           | `e2e/platform.spec.ts`                                    | Verified |
| Prompt, model and dataset registries                  | `app/{prompts,models,datasets}/`               | `e2e/platform.spec.ts`                                    | Verified |
| API key management                                    | `app/settings/api-keys/page.tsx`               | `e2e/platform.spec.ts`                                    | Verified |
| Members and roles                                     | `app/settings/members/page.tsx`                | `e2e` (navigation)                                        | Verified |
| Retention settings                                    | `app/settings/retention/page.tsx`              | `e2e/platform.spec.ts`                                    | Verified |
| Price book viewer                                     | `app/settings/price-books/page.tsx`            | `e2e/platform.spec.ts`                                    | Verified |
| Exports                                               | `app/settings/exports/page.tsx`                | `e2e` (navigation)                                        | Verified |
| Audit log viewer                                      | `app/settings/audit/page.tsx`                  | `e2e/platform.spec.ts`                                    | Verified |
| Every state designed (loading, empty, partial, error) | `components/ui.tsx`                            | `apps/web/test/ui.test.tsx` (14)                          | Verified |
| Status never by colour alone                          | `components/ui.tsx::StatusBadge`, `Charts.tsx` | `apps/web/test/ui.test.tsx`                               | Verified |
| Accessible charts (table equivalent)                  | `components/Charts.tsx`                        | `apps/web/test/charts.test.tsx`, `e2e`                    | Verified |
| Keyboard navigation                                   | `components/Waterfall.tsx`                     | `apps/web/test/waterfall.test.tsx`, `e2e/tracing.spec.ts` | Verified |
| User content rendered as text                         | `components/ui.tsx::SafeText`                  | `apps/web/test/ui.test.tsx`                               | Verified |

## 8. Infrastructure and delivery

| Requirement                        | Implementation                            | Verification                                                | Status                           |
| ---------------------------------- | ----------------------------------------- | ----------------------------------------------------------- | -------------------------------- |
| Dockerfiles, non-root, multi-stage | `infrastructure/docker/`                  | API image built and run as uid 10001                        | Verified                         |
| Local stack in one command         | `docker-compose.yml`, `Makefile`          | `make dev-local` executed                                   | Verified                         |
| CI pipeline                        | `.github/workflows/ci.yml`                | YAML valid; not executed (no GitHub runner here)            | Implemented, not executable here |
| Security pipeline                  | `.github/workflows/security.yml`          | `scan-secrets.py` executed, clean                           | Partially verified               |
| Helm chart                         | `infrastructure/helm/aiobs/`              | Templates written; `helm lint` not run (helm not installed) | Implemented, not executable here |
| Kubernetes manifests               | `infrastructure/kubernetes/base/`         | YAML parsed, 14 documents; `kubectl` not installed          | Implemented, not executable here |
| Terraform module                   | `infrastructure/terraform/modules/aiobs/` | `terraform validate` not run (not installed)                | Implemented, not executable here |
| Load-test harness                  | `tests/performance/run_load_test.py`      | Executed against the local stack                            | Verified                         |
| k6 profile                         | `tests/performance/ingest.k6.js`          | k6 not installed                                            | Implemented, not executable here |
| SBOM generation                    | `Makefile::sbom`, security workflow       | Not executed                                                | Implemented, not executable here |

## 9. Documentation

| Requirement                | Location                                                                 | Status          |
| -------------------------- | ------------------------------------------------------------------------ | --------------- |
| Concepts                   | `docs/concepts/` (7 pages)                                               | Complete        |
| Architecture with diagrams | `docs/architecture/` (5 pages, Mermaid)                                  | Complete        |
| Development guides         | `docs/development/` (3 pages)                                            | Complete        |
| Operations                 | `docs/operations/` (6 pages)                                             | Complete        |
| Security                   | `docs/security/` (3 pages) + `SECURITY.md`                               | Complete        |
| API reference              | `docs/api/` (3 pages) + OpenAPI                                          | Complete        |
| SDK guides                 | `docs/sdk/` (3 pages)                                                    | Complete        |
| Tutorials                  | `docs/tutorials/` (3 pages)                                              | Complete        |
| ADRs                       | `docs/adr/` (12 records)                                                 | Complete        |
| Root documents             | `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`, `LICENSE` | Complete        |
| Link check                 | `scripts/check-docs.py`                                                  | Executed, clean |

---

## Test totals as executed

| Suite                                               | Count              | Result   |
| --------------------------------------------------- | ------------------ | -------- |
| Python — unit                                       | 195                | pass     |
| Python — contract                                   | 30                 | pass     |
| Python — integration (incl. conformance ×2 drivers) | 78 + 30 ClickHouse | pass     |
| Python — security                                   | 25                 | pass     |
| Python — chaos                                      | 10                 | pass     |
| Python — migration                                  | 7                  | pass     |
| **Python total**                                    | **377**            | **pass** |
| TypeScript — shared schemas                         | 16                 | pass     |
| TypeScript — SDK                                    | 14                 | pass     |
| Frontend — component                                | 91                 | pass     |
| Browser — Playwright                                | 27                 | pass     |
| **Grand total**                                     | **525**            | **pass** |

Executed on Python 3.10.12, Node 21.5.0, ClickHouse 25.3 (container),
Chromium 151 headless.

## Static analysis as executed

| Check                                    | Result                               |
| ---------------------------------------- | ------------------------------------ |
| `ruff check apps packages tests scripts` | clean                                |
| `ruff format --check`                    | 148 files, all formatted             |
| `prettier --check`                       | clean                                |
| `tsc --noEmit` (3 workspaces)            | clean                                |
| `python scripts/scan-secrets.py`         | 561 files, clean                     |
| `python scripts/check-docs.py`           | 60 documents, 191 links, clean       |
| `mypy --strict apps packages`            | **22 errors across 14 of 110 files** |

The mypy result is not clean and is reported as it is. The remaining errors are:

| Category                                                              | Count | What they are                                                                                            |
| --------------------------------------------------------------------- | ----- | -------------------------------------------------------------------------------------------------------- |
| `no-any-return`                                                       | 7     | Returning a value from an untyped third-party call (`clickhouse-connect`, `argon2`, SQLAlchemy `Result`) |
| `arg-type`                                                            | 5     | Loosely-typed helpers in the demo generator and the registry                                             |
| `attr-defined`                                                        | 3     | SQLAlchemy `Result` attributes mypy cannot see                                                           |
| `assignment`                                                          | 3     | Same, in the demo generator                                                                              |
| `untyped-decorator`, `no-untyped-call`, `return-value`, `exit-return` | 4     | SDK integration shims wrapping untyped third-party APIs                                                  |

None is a runtime defect, and the suites above cover the paths they occur on.
They are honest debt: `--strict` is enabled deliberately so they stay visible
rather than being suppressed.
