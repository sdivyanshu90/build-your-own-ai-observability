"""FastAPI application factory.

Startup opens every dependency and refuses to serve if any is unusable; a
process that is up but broken is worse than one that failed to start, because
orchestrators restart the latter and route traffic to the former.

Shutdown drains in the opposite order with a grace period, so in-flight
ingestion completes before the bus closes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from ..container import Container, build_container
from ..core.config import Settings, get_settings
from ..core.logging import configure_logging, get_logger
from ..core.timeutil import Clock
from ..services.bundle import ServiceBundle, build_services
from .errors import install_exception_handlers
from .middleware import (
    AccessLogMiddleware,
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    UnhandledErrorMiddleware,
)
from .routers import (
    auth,
    health,
    ingest,
    metrics,
    operations,
    organizations,
    registries,
    stream,
    traces,
)

__all__ = ["create_app"]

log = get_logger(__name__)

API_PREFIX = "/v1"

_DESCRIPTION = """
Open-source AI observability and tracing platform.

**Authentication** — send either an `Authorization: Bearer <token>` header (for
user and service-account credentials) or an `X-API-Key: aiobs_...` header (for
SDK keys). API keys are bound to a single project and environment.

**Pagination** — list endpoints are keyset paginated. Pass the `next_cursor`
from a response as the `cursor` parameter of the next request. There is no
total count; see the pagination guide for why.

**Filtering** — repeatable `filter=<field>:<operator>:<value>` parameters.
Unknown fields and operators are rejected rather than ignored.

**Idempotency** — mutating endpoints accept an `Idempotency-Key` header.
Replaying a key returns the original result; replaying it with a different body
returns `409 idempotency_key_reused`.

**Errors** — every non-2xx response uses one envelope with a stable machine-
readable `code`. Never parse the human-readable `message`.
"""


def create_app(
    settings: Settings | None = None,
    *,
    container: Container | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Build the ASGI application.

    ``container`` may be supplied by tests to inject fakes or a scratch
    database; production passes nothing and the container is built from
    settings.
    """
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)
    resolved_container = container or build_container(resolved_settings, clock=clock)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        await resolved_container.start()
        application.state.container = resolved_container
        application.state.services = build_services(resolved_container)
        log.info(
            "api.started",
            version=resolved_settings.version,
            environment=resolved_settings.environment.value,
        )
        try:
            yield
        finally:
            # Give in-flight requests a moment to finish before dependencies go
            # away; without it, a rolling deploy turns healthy requests into 500s.
            await asyncio.sleep(min(resolved_settings.shutdown_grace_seconds, 1.0))
            await resolved_container.stop()

    application = FastAPI(
        title="AI Observability Platform API",
        version=resolved_settings.version,
        description=_DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        # Errors are produced by our own handlers with a consistent envelope;
        # FastAPI's default 422 shape would be an exception to that rule.
        responses={},
    )
    application.state.settings = resolved_settings

    _install_middleware(application, resolved_settings)
    install_exception_handlers(application)
    _install_routers(application)
    _customise_openapi(application, resolved_settings)
    return application


def _install_middleware(application: FastAPI, settings: Settings) -> None:
    # Starlette applies middleware in reverse registration order, so the last
    # registered is outermost. Register inner-to-outer.
    #
    # The unhandled-error trap is innermost so that a 500 still travels back out
    # through CORS; Starlette's own ServerErrorMiddleware sits outside every
    # application middleware and its responses reach a browser without CORS
    # headers, which presents as an unexplained network failure.
    application.add_middleware(UnhandledErrorMiddleware)
    application.add_middleware(
        AccessLogMiddleware, excluded_paths=tuple(settings.telemetry.excluded_paths)
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.security.cors_allow_origins,
        allow_credentials=settings.security.cors_allow_credentials,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-API-Key",
            "X-Request-Id",
            "Idempotency-Key",
            "traceparent",
            "tracestate",
            "baggage",
        ],
        expose_headers=[
            "X-Request-Id",
            "RateLimit-Limit",
            "RateLimit-Remaining",
            "RateLimit-Reset",
            "Retry-After",
        ],
        max_age=600,
    )
    application.add_middleware(
        BodySizeLimitMiddleware, max_bytes=settings.security.max_request_bytes
    )
    application.add_middleware(SecurityHeadersMiddleware, settings=settings)
    application.add_middleware(
        RequestContextMiddleware, trusted_proxy_hops=settings.security.trusted_proxy_hops
    )


def _install_routers(application: FastAPI) -> None:
    # Health endpoints are unversioned: an orchestrator's probe configuration
    # should not have to change when the API version does.
    application.include_router(health.router)
    application.include_router(auth.router, prefix=API_PREFIX)
    application.include_router(organizations.router, prefix=API_PREFIX)
    application.include_router(traces.router, prefix=API_PREFIX)
    application.include_router(registries.router, prefix=API_PREFIX)
    application.include_router(metrics.router, prefix=API_PREFIX)
    application.include_router(operations.router, prefix=API_PREFIX)
    application.include_router(stream.router, prefix=API_PREFIX)
    # Ingestion is mounted at the root as well, because the OTLP specification
    # fixes the path at /v1/traces and exporters do not allow a prefix.
    application.include_router(ingest.router)


def _customise_openapi(application: FastAPI, settings: Settings) -> None:
    """Attach security schemes and servers to the generated OpenAPI document."""

    def openapi() -> dict[str, object]:
        if application.openapi_schema:
            return application.openapi_schema
        schema = get_openapi(
            title=application.title,
            version=application.version,
            description=application.description,
            routes=application.routes,
        )
        schema["servers"] = [{"url": settings.public_url, "description": "This deployment"}]
        components = schema.setdefault("components", {})
        components["securitySchemes"] = {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "User or service-account token.",
            },
            "apiKeyAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "description": "SDK key bound to one project and environment.",
            },
        }
        schema["security"] = [{"bearerAuth": []}, {"apiKeyAuth": []}]
        schema["tags"] = [
            {"name": "health", "description": "Liveness, readiness and health."},
            {"name": "auth", "description": "Sign-in and token lifecycle."},
            {"name": "organizations", "description": "Tenants, projects, environments, members."},
            {"name": "api-keys", "description": "SDK credential management."},
            {"name": "ingest", "description": "OTLP and native telemetry ingestion."},
            {"name": "traces", "description": "Trace search, detail, retrieval and trajectories."},
            {"name": "prompts", "description": "Prompt registry and versioning."},
            {"name": "models", "description": "Model configuration registry."},
            {"name": "datasets", "description": "Dataset registry and manifests."},
            {"name": "metrics", "description": "Dashboards, latency and usage."},
            {"name": "costs", "description": "Cost attribution and price books."},
            {"name": "operations", "description": "Exports, audit log, retention."},
            {"name": "stream", "description": "Live trace updates over SSE."},
        ]
        application.openapi_schema = schema
        return schema

    application.openapi = openapi  # type: ignore[method-assign]


def get_container(application: FastAPI) -> Container:
    container: Container = application.state.container
    return container


def get_service_bundle(application: FastAPI) -> ServiceBundle:
    services: ServiceBundle = application.state.services
    return services
