# AI Observability Platform
#
# Two ways to run the platform:
#
#   make dev        -- everything in Docker (PostgreSQL, ClickHouse, Redis,
#                      Redpanda, MinIO, API, worker, web). One command, closest
#                      to production.
#   make dev-local  -- API and worker on the host against SQLite files. No
#                      Docker, no containers, starts in seconds. Used by CI and
#                      by anyone who just wants to read a trace.
#
# Every target is safe to re-run.

SHELL := /bin/bash
.DEFAULT_GOAL := help
.ONESHELL:

VENV       ?= .venv
PY         := $(VENV)/bin/python
PIP        := $(VENV)/bin/pip
ALEMBIC    := $(PY) -m alembic -c database/postgres/alembic.ini
COMPOSE    := docker compose
API_PORT   ?= 58000
WEB_PORT   ?= 53000

# Local (non-Docker) data lives here and is safe to delete.
export AIOBS_DATABASE__URL          ?= sqlite+aiosqlite:///./.aiobs/metadata.db
export AIOBS_ANALYTICS__SQLITE_PATH ?= ./.aiobs/analytics.db
export AIOBS_OBJECTS__ROOT_PATH     ?= ./.aiobs/objects
export AIOBS_LOG_FORMAT             ?= console

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------

.PHONY: install
install: $(VENV)/.installed ## Create the virtualenv and install every package

$(VENV)/.installed: requirements/dev.txt $(wildcard */*/pyproject.toml)
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip setuptools wheel --quiet
	$(PIP) install -r requirements/dev.txt --quiet
	$(PIP) install -e packages/shared-schemas --no-deps --quiet
	$(PIP) install -e packages/provider-adapters --no-deps --quiet
	$(PIP) install -e packages/python-sdk --no-deps --quiet
	$(PIP) install -e apps/api --no-deps --quiet
	$(PIP) install -e apps/worker --no-deps --quiet
	@touch $@
	@echo "Python environment ready."

.PHONY: install-web
install-web: ## Install frontend and TypeScript SDK dependencies
	npm install --workspaces --include-workspace-root

.PHONY: setup
setup: install install-web ## Install everything
	@test -f .env || cp .env.example .env
	@echo "Setup complete. Next: make dev-local"

.PHONY: check-setup
check-setup: ## Verify the toolchain and report anything missing
	@$(PY) scripts/check-setup.py

# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

.PHONY: migrate
migrate: install ## Apply relational migrations
	@mkdir -p .aiobs
	$(ALEMBIC) upgrade head

.PHONY: migrate-down
migrate-down: ## Roll back one migration
	$(ALEMBIC) downgrade -1

.PHONY: migrate-sql
migrate-sql: ## Print the migration SQL without applying it (for review)
	$(ALEMBIC) upgrade head --sql

.PHONY: migration
migration: ## Autogenerate a migration: make migration MSG="add x"
	@test -n "$(MSG)" || { echo "usage: make migration MSG=\"description\""; exit 1; }
	$(ALEMBIC) revision --autogenerate -m "$(MSG)"

.PHONY: migrate-check
migrate-check: ## Fail if the models have drifted from the migrations
	$(ALEMBIC) check

.PHONY: migrate-analytics
migrate-analytics: ## Create or update the analytics schema
	$(VENV)/bin/aiobs-admin migrate-analytics

.PHONY: bootstrap
bootstrap: migrate ## Create the first organization, project and API key
	$(VENV)/bin/aiobs-admin bootstrap

.PHONY: seed
seed: ## Generate demo telemetry: make seed PROJECT=prj_...
	@test -n "$(PROJECT)" || { echo "usage: make seed PROJECT=prj_..."; exit 1; }
	$(VENV)/bin/aiobs-admin seed-demo --project-id $(PROJECT) --traces $${TRACES:-200}

.PHONY: reset
reset: ## Delete all local data (destructive)
	@echo "Deleting .aiobs/ -- all local traces, metadata and objects."
	rm -rf .aiobs
	@mkdir -p .aiobs

# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------

.PHONY: dev
dev: ## Start the full stack in Docker (one command)
	$(COMPOSE) up -d --build
	@echo
	@echo "Waiting for the API to become ready..."
	@for i in $$(seq 1 90); do \
		curl -sf http://localhost:$(API_PORT)/ready >/dev/null 2>&1 && break; \
		sleep 2; \
	done
	@$(COMPOSE) exec -T api aiobs-admin bootstrap || true
	@echo
	@echo "  API   http://localhost:$(API_PORT)/docs"
	@echo "  Web   http://localhost:$(WEB_PORT)"
	@echo
	@echo "Logs: make logs   Stop: make down"

.PHONY: dev-local
dev-local: migrate ## Start the API and worker on the host (no Docker)
	@mkdir -p .aiobs
	@$(VENV)/bin/aiobs-admin bootstrap
	@echo "Starting API on :$(API_PORT) and worker. Ctrl-C to stop."
	@trap 'kill 0' INT TERM EXIT; \
	$(VENV)/bin/aiobs-api --port $(API_PORT) & \
	$(VENV)/bin/aiobs-worker & \
	wait

.PHONY: api
api: ## Run only the API
	$(VENV)/bin/aiobs-api --port $(API_PORT) --reload

.PHONY: worker
worker: ## Run only the worker
	$(VENV)/bin/aiobs-worker

.PHONY: web
web: ## Run the frontend development server
	npm run dev --workspace @aiobs/web

.PHONY: logs
logs: ## Tail the Docker stack logs
	$(COMPOSE) logs -f --tail=100

.PHONY: down
down: ## Stop the Docker stack
	$(COMPOSE) down

.PHONY: down-volumes
down-volumes: ## Stop the Docker stack and delete its data (destructive)
	$(COMPOSE) down -v

# ---------------------------------------------------------------------------
# demos
# ---------------------------------------------------------------------------

.PHONY: demo
demo: demo-simple demo-rag demo-agent demo-distributed ## Run every demo

.PHONY: demo-simple
demo-simple: ## Demo: a single instrumented model call
	$(PY) demos/simple-llm-app/main.py

.PHONY: demo-rag
demo-rag: ## Demo: an instrumented RAG pipeline
	$(PY) demos/rag-application/main.py

.PHONY: demo-agent
demo-agent: ## Demo: a multi-step agent with tools, retries and approval
	$(PY) demos/multi-step-agent/main.py

.PHONY: demo-distributed
demo-distributed: ## Demo: one request across three services
	$(PY) demos/distributed-ai-request/main.py

# ---------------------------------------------------------------------------
# quality
# ---------------------------------------------------------------------------

.PHONY: format
format: ## Format Python and TypeScript
	$(VENV)/bin/ruff format apps packages demos tests scripts
	$(VENV)/bin/ruff check --fix apps packages demos tests scripts
	-npm run format --workspaces --if-present

.PHONY: lint
lint: ## Lint everything
	$(VENV)/bin/ruff check apps packages demos tests scripts
	$(VENV)/bin/ruff format --check apps packages demos tests scripts
	-npm run lint --workspaces --if-present

.PHONY: typecheck
typecheck: ## Static type checking
	$(VENV)/bin/mypy apps/api/src apps/worker/src packages/python-sdk/src packages/shared-schemas/python packages/provider-adapters/src
	-npm run typecheck --workspaces --if-present

.PHONY: test
test: test-unit test-contract test-integration ## Run the default test suite

.PHONY: test-unit
test-unit: ## Unit and property-based tests
	$(VENV)/bin/pytest tests/unit -q

.PHONY: test-contract
test-contract: ## API contract and cross-language parity tests
	$(VENV)/bin/pytest tests/contract -q

.PHONY: test-integration
test-integration: ## Integration tests against real disposable dependencies
	$(VENV)/bin/pytest tests/integration -q

.PHONY: test-sdk
test-sdk: ## SDK tests
	$(VENV)/bin/pytest tests/sdk -q
	-npm run test --workspace @aiobs/sdk --if-present

.PHONY: test-security
test-security: ## Authorization, isolation and injection tests
	$(VENV)/bin/pytest tests/security -q

.PHONY: test-chaos
test-chaos: ## Dependency-failure and resilience tests
	$(VENV)/bin/pytest tests/chaos -q

.PHONY: test-migration
test-migration: ## Migration install, upgrade and rollback tests
	$(VENV)/bin/pytest tests/migration -q

.PHONY: test-frontend
test-frontend: ## Frontend component and state tests
	npm run test --workspace @aiobs/web

.PHONY: test-e2e
test-e2e: ## End-to-end browser journeys
	npm run test:e2e --workspace @aiobs/web

.PHONY: test-all
test-all: test test-sdk test-security test-chaos test-migration ## Every Python suite
	$(VENV)/bin/pytest tests -q --ignore=tests/performance

.PHONY: coverage
coverage: ## Test suite with a coverage report
	$(VENV)/bin/pytest tests --ignore=tests/performance --ignore=tests/end-to-end \
		--cov=aiobs_api --cov=aiobs --cov=aiobs_schemas --cov=aiobs_providers \
		--cov-report=term-missing --cov-report=html

.PHONY: smoke
smoke: ## End-to-end smoke test against a running API
	$(PY) scripts/smoke-test.py --api http://localhost:$(API_PORT)

# ---------------------------------------------------------------------------
# security
# ---------------------------------------------------------------------------

.PHONY: security
security: ## Dependency, secret and static security scans
	@echo "== dependency audit =="
	-$(PIP) install pip-audit --quiet && $(VENV)/bin/pip-audit --strict || true
	@echo "== python static analysis =="
	$(VENV)/bin/ruff check --select S apps packages || true
	@echo "== secret scan =="
	@$(PY) scripts/scan-secrets.py

.PHONY: sbom
sbom: ## Generate a software bill of materials
	-$(PIP) install cyclonedx-bom --quiet
	-$(VENV)/bin/cyclonedx-py environment $(VENV) --output-format json --outfile sbom-python.json
	-npm sbom --sbom-format cyclonedx > sbom-node.json

# ---------------------------------------------------------------------------
# performance
# ---------------------------------------------------------------------------

.PHONY: load-test
load-test: ## Ingestion load test: make load-test RATE=1000 DURATION=60
	$(PY) tests/performance/run_load_test.py \
		--rate $${RATE:-100} --duration $${DURATION:-30} \
		--endpoint http://localhost:$(API_PORT)

.PHONY: load-test-k6
load-test-k6: ## Ingestion load test with k6 (requires k6)
	k6 run tests/performance/ingest.k6.js

# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

.PHONY: build
build: ## Build container images
	$(COMPOSE) build

.PHONY: build-web
build-web: ## Build the production frontend bundle
	npm run build --workspace @aiobs/web

.PHONY: openapi
openapi: ## Export the OpenAPI schema to openapi.json
	$(PY) scripts/export-openapi.py > openapi.json
	@echo "wrote openapi.json"

.PHONY: generate
generate: ## Regenerate all generated code and fixtures
	$(PY) scripts/gen-sdk-semconv.py
	node scripts/gen-number-fixture.mjs
	$(MAKE) openapi

.PHONY: docs-check
docs-check: ## Verify documentation links and referenced files
	$(PY) scripts/check-docs.py

.PHONY: clean
clean: ## Remove build artefacts and caches
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	rm -rf apps/web/.next apps/web/node_modules/.cache
	@echo "Cleaned. Local data in .aiobs/ was kept; use 'make reset' to remove it."
