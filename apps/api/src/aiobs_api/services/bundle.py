"""Service bundle: constructs the service layer from a :class:`Container`.

Kept separate from the container so that storage wiring and business-logic
wiring are independently testable: a test can build a bundle over an in-memory
container without an HTTP app, and the worker can build the two services it
needs without instantiating the whole read-side.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..container import Container
from ..domain.redaction import RedactionMode, RedactionPolicy, Redactor
from ..ingest.normalizer import SpanNormalizer
from .audit import AuditService
from .auth import AuthService
from .exports import ExportService
from .ingestion import IngestionService
from .metrics import MetricsService
from .organizations import OrganizationService
from .pricing import PricingService
from .processing import SpanProcessor, TraceRollupProcessor
from .registry import DatasetRegistry, ModelRegistry, PromptRegistry
from .traces import TraceService

__all__ = ["ServiceBundle", "build_services"]


@dataclass(slots=True)
class ServiceBundle:
    """Every service the HTTP layer and the worker depend on."""

    container: Container
    auth: AuthService
    audit: AuditService
    organizations: OrganizationService
    ingestion: IngestionService
    traces: TraceService
    metrics: MetricsService
    pricing: PricingService
    prompts: PromptRegistry
    models: ModelRegistry
    datasets: DatasetRegistry
    exports: ExportService
    span_processor: SpanProcessor
    rollup_processor: TraceRollupProcessor
    redactor: Redactor


def build_services(container: Container) -> ServiceBundle:
    """Wire the service layer over an already-constructed container."""
    settings = container.settings
    clock = container.clock

    policy = RedactionPolicy(
        mode=(RedactionMode.STANDARD if settings.ingest.redact_by_default else RedactionMode.OFF),
        blocklist=frozenset(settings.ingest.always_redact_keys),
    )
    redactor = Redactor(policy)

    normalizer = SpanNormalizer(
        clock=clock,
        redactor=redactor,
        max_clock_skew_future_seconds=settings.ingest.max_clock_skew_future_seconds,
        max_backfill_age_seconds=settings.ingest.max_backfill_age_seconds,
    )
    pricing = PricingService(database=container.database, clock=clock)

    return ServiceBundle(
        container=container,
        auth=AuthService(database=container.database, settings=settings, clock=clock),
        audit=AuditService(database=container.database, clock=clock, redactor=redactor),
        organizations=OrganizationService(database=container.database, clock=clock),
        ingestion=IngestionService(
            settings=settings,
            database=container.database,
            bus=container.bus,
            kv=container.kv,
            normalizer=normalizer,
            clock=clock,
        ),
        traces=TraceService(analytics=container.analytics, clock=clock),
        metrics=MetricsService(analytics=container.analytics, clock=clock),
        pricing=pricing,
        prompts=PromptRegistry(database=container.database, clock=clock),
        models=ModelRegistry(database=container.database, clock=clock),
        datasets=DatasetRegistry(database=container.database, clock=clock),
        exports=ExportService(
            database=container.database,
            analytics=container.analytics,
            cursor_codec=container.cursor_codec,
            objects=container.objects,
            clock=clock,
            redactor=redactor,
        ),
        span_processor=SpanProcessor(
            settings=settings,
            analytics=container.analytics,
            pricing=pricing,
            clock=clock,
            bus=container.bus,
        ),
        rollup_processor=TraceRollupProcessor(analytics=container.analytics, clock=clock),
        redactor=redactor,
    )
