"""Liveness, readiness and health endpoints.

Three endpoints because orchestrators ask three different questions, and
answering them with one probe causes outages:

``/live``
    "Is the process wedged?" Answers without touching any dependency. A
    liveness probe that checks the database restarts every API pod when the
    database has a blip -- turning a degradation into an outage.

``/ready``
    "Should this instance receive traffic?" Checks every dependency. A failing
    readiness probe removes the instance from the load balancer and leaves it
    running, which is recoverable.

``/health``
    Human- and dashboard-facing detail. Unauthenticated but deliberately
    contentless about internals: driver names and version, never DSNs or hosts.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from ...core.logging import get_logger
from ..schemas import HealthOut

__all__ = ["router"]

log = get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get(
    "/live",
    summary="Liveness probe",
    response_class=Response,
    status_code=status.HTTP_204_NO_CONTENT,
)
async def live() -> Response:
    """Return 204 while the event loop is responsive.

    Deliberately performs no I/O.
    """
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/ready", summary="Readiness probe", response_model=HealthOut)
async def ready(request: Request, response: Response) -> HealthOut:
    """Check every dependency and report per-dependency status."""
    container = request.app.state.container
    settings = request.app.state.settings
    report = await container.health()
    if not report.healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        log.warning("health.not_ready", checks=report.checks)
    return HealthOut(
        status="ready" if report.healthy else "degraded",
        version=settings.version,
        git_commit=settings.git_commit,
        environment=settings.environment.value,
        checks=report.checks,
    )


@router.get("/health", summary="Detailed health", response_model=HealthOut)
async def health(request: Request, response: Response) -> HealthOut:
    """Health plus a redacted configuration summary."""
    container = request.app.state.container
    settings = request.app.state.settings
    report = await container.health()
    if not report.healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthOut(
        status="ok" if report.healthy else "degraded",
        version=settings.version,
        git_commit=settings.git_commit,
        environment=settings.environment.value,
        checks=report.checks,
        configuration=settings.describe(),
    )


@router.get(
    "/internal/metrics",
    summary="Prometheus metrics",
    include_in_schema=False,
    response_class=Response,
)
async def prometheus_metrics(request: Request) -> Response:
    """Expose the process's own metrics.

    Excluded from the OpenAPI schema and served on an internal path: it is for
    the scraper, not for API consumers, and should be network-restricted.
    """
    settings = request.app.state.settings
    if not settings.telemetry.enable_metrics:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    from ...telemetry.metrics import refresh_runtime_metrics

    await refresh_runtime_metrics(request.app.state.container)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
