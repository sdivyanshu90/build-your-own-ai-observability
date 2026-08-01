"""Dashboard, latency, usage and cost endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from ...core.errors import ValidationFailedError
from ...core.query import FilterCondition, SortTerm
from ...domain.rbac import Permission
from ...services.audit import AuditAction
from ...services.pricing import PriceBookInput, PriceEntryInput
from ...storage.analytics.protocol import Aggregation, TimeInterval
from ...storage.analytics.schemas import SPAN_SCHEMA
from ..deps import PrincipalDep, ServicesDep, TimeRangeDep, query_parser
from ..schemas import PriceBookCreate, PriceBookOut, PriceEntryOut

__all__ = ["router"]

router = APIRouter()

SpanQuery = Annotated[
    tuple[tuple[FilterCondition, ...], tuple[SortTerm, ...]],
    Depends(query_parser(SPAN_SCHEMA)),
]


@router.get(
    "/metrics/overview",
    tags=["metrics"],
    summary="Headline dashboard numbers",
    description=(
        "Request volume, error rate, token totals, cost and latency "
        "percentiles. Pass `compare_previous=true` for the immediately "
        "preceding window of equal length."
    ),
)
async def overview(
    principal: PrincipalDep,
    services: ServicesDep,
    time_range: TimeRangeDep,
    parsed: SpanQuery,
    project_id: Annotated[str, Query()],
    environment: Annotated[str | None, Query()] = None,
    compare_previous: Annotated[bool, Query()] = False,
) -> dict[str, object]:
    filters, _ = parsed
    summary = await services.metrics.overview(
        principal=principal,
        project_id=project_id,
        environment=environment,
        start=time_range.start,
        end=time_range.end,
        filters=filters,
        compare_previous=compare_previous,
    )
    return summary.as_dict()


@router.get(
    "/metrics/timeseries",
    tags=["metrics"],
    summary="Bucketed time series",
    description=(
        "The bucket width is chosen automatically to yield roughly sixty "
        "points unless `interval` is given. Buckets that are still filling are "
        "listed in `partial_buckets`."
    ),
)
async def timeseries(
    principal: PrincipalDep,
    services: ServicesDep,
    time_range: TimeRangeDep,
    parsed: SpanQuery,
    project_id: Annotated[str, Query()],
    metric: Annotated[str | None, Query(description="Column to aggregate; omit for count")] = None,
    aggregation: Annotated[str, Query()] = "count",
    interval: Annotated[str | None, Query(description="1m, 5m, 15m, 1h, 6h, 1d, 7d")] = None,
    group_by: Annotated[list[str] | None, Query()] = None,
    environment: Annotated[str | None, Query()] = None,
    source: Annotated[str, Query()] = "spans",
) -> dict[str, object]:
    filters, _ = parsed
    try:
        resolved_aggregation = Aggregation(aggregation)
    except ValueError as exc:
        raise ValidationFailedError(
            f"unknown aggregation {aggregation!r}; "
            f"expected one of {[item.value for item in Aggregation]}"
        ) from exc
    resolved_interval = None
    if interval:
        try:
            resolved_interval = TimeInterval(interval)
        except ValueError as exc:
            raise ValidationFailedError(
                f"unknown interval {interval!r}; "
                f"expected one of {[item.value for item in TimeInterval]}"
            ) from exc
    series = await services.metrics.timeseries(
        principal=principal,
        project_id=project_id,
        environment=environment,
        start=time_range.start,
        end=time_range.end,
        metric=metric,
        aggregation=resolved_aggregation,
        interval=resolved_interval,
        group_by=group_by or [],
        filters=filters,
        source=source,
    )
    return series.as_dict()


@router.get(
    "/metrics/latency",
    tags=["metrics"],
    summary="Latency percentiles",
    description=(
        "P50/P75/P90/P95/P99 computed in the analytics store, never in "
        "application memory. Values are milliseconds."
    ),
)
async def latency(
    principal: PrincipalDep,
    services: ServicesDep,
    time_range: TimeRangeDep,
    parsed: SpanQuery,
    project_id: Annotated[str, Query()],
    group_by: Annotated[list[str] | None, Query()] = None,
    environment: Annotated[str | None, Query()] = None,
    source: Annotated[str, Query()] = "spans",
    column: Annotated[str, Query()] = "duration_ns",
) -> dict[str, object]:
    filters, _ = parsed
    results = await services.metrics.latency_percentiles(
        principal=principal,
        project_id=project_id,
        environment=environment,
        start=time_range.start,
        end=time_range.end,
        group_by=group_by or [],
        filters=filters,
        source=source,
        column=column,
    )
    return {
        "unit": "ms",
        "column": column,
        "groups": [result.as_dict() for result in results],
    }


@router.get(
    "/metrics/values",
    tags=["metrics"],
    summary="Distinct values for a dimension",
    description="Powers filter autocomplete in the trace explorer.",
)
async def distinct_values(
    principal: PrincipalDep,
    services: ServicesDep,
    time_range: TimeRangeDep,
    project_id: Annotated[str, Query()],
    column: Annotated[str, Query()],
    environment: Annotated[str | None, Query()] = None,
    prefix: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    values = await services.metrics.distinct_values(
        principal=principal,
        project_id=project_id,
        environment=environment,
        column=column,
        start=time_range.start,
        end=time_range.end,
        prefix=prefix,
    )
    return {
        "column": column,
        "values": [{"value": value, "count": count} for value, count in values],
    }


@router.get(
    "/costs",
    tags=["costs"],
    summary="Cost breakdown",
    description=(
        "Grouped spend from the cost records, which carry the price-book "
        "version and formula used. Amounts are decimal strings."
    ),
)
async def costs(
    principal: PrincipalDep,
    services: ServicesDep,
    time_range: TimeRangeDep,
    parsed: SpanQuery,
    project_id: Annotated[str, Query()],
    group_by: Annotated[list[str] | None, Query()] = None,
    environment: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    filters, _ = parsed
    groups = await services.metrics.cost_breakdown(
        principal=principal,
        project_id=project_id,
        environment=environment,
        start=time_range.start,
        end=time_range.end,
        group_by=group_by or ["model"],
        filters=filters,
    )
    return {
        "group_by": group_by or ["model"],
        "groups": [
            {
                "keys": list(group.keys),
                "total": None if group.total is None else str(group.total),
                "count": group.count,
            }
            for group in groups
        ],
    }


# ---------------------------------------------------------------------------
# price books
# ---------------------------------------------------------------------------


@router.get(
    "/price-books", response_model=list[PriceBookOut], tags=["costs"], summary="List price books"
)
async def list_price_books(principal: PrincipalDep, services: ServicesDep) -> list[PriceBookOut]:
    principal.require(Permission.PRICE_BOOK_READ)
    books = await services.pricing.list_books(principal.organization_id)
    result: list[PriceBookOut] = []
    for book in books:
        entries = await services.pricing.list_entries(book.id)
        result.append(
            PriceBookOut(
                id=book.id,
                organization_id=book.organization_id,
                version=book.version,
                name=book.name,
                description=book.description,
                currency=book.currency,
                is_active=book.is_active,
                published_at=book.published_at,
                frozen_at=book.frozen_at,
                entry_count=len(entries),
            )
        )
    return result


@router.post(
    "/price-books",
    response_model=PriceBookOut,
    status_code=status.HTTP_201_CREATED,
    tags=["costs"],
    summary="Publish a price book",
    description=(
        "Price books are versioned, never edited: re-running a historical cost "
        "report must reproduce the original numbers. Overlapping validity "
        "windows for the same model/category are rejected."
    ),
)
async def create_price_book(
    payload: PriceBookCreate, principal: PrincipalDep, services: ServicesDep
) -> PriceBookOut:
    principal.require(Permission.PRICE_BOOK_WRITE)
    if payload.scope not in {"organization", "public"}:
        raise ValidationFailedError("scope must be 'organization' or 'public'")
    book_id = await services.pricing.create_price_book(
        PriceBookInput(
            version=payload.version,
            name=payload.name,
            description=payload.description,
            source=payload.source,
            currency=payload.currency,
            organization_id=(
                principal.organization_id if payload.scope == "organization" else None
            ),
            entries=tuple(
                PriceEntryInput(
                    provider=entry.provider,
                    model_identifier=entry.model_identifier,
                    usage_category=entry.usage_category,
                    unit_price=entry.unit_price,
                    effective_from=entry.effective_from,
                    unit_quantity=entry.unit_quantity,
                    currency=entry.currency,
                    effective_to=entry.effective_to,
                    tier_min_units=entry.tier_min_units,
                    tier_max_units=entry.tier_max_units,
                    discount_rate=entry.discount_rate,
                    source_url=entry.source_url,
                    notes=entry.notes,
                )
                for entry in payload.entries
            ),
        ),
        created_by=principal.id,
    )
    await services.audit.record(
        principal=principal,
        action=AuditAction.PRICE_BOOK_CREATED,
        resource_type="price_book",
        resource_id=book_id,
        metadata={"version": payload.version, "entries": len(payload.entries)},
    )
    books = await services.pricing.list_books(principal.organization_id)
    book = next(item for item in books if item.id == book_id)
    return PriceBookOut(
        id=book.id,
        organization_id=book.organization_id,
        version=book.version,
        name=book.name,
        description=book.description,
        currency=book.currency,
        is_active=book.is_active,
        published_at=book.published_at,
        frozen_at=book.frozen_at,
        entry_count=len(payload.entries),
    )


@router.get(
    "/price-books/{price_book_id}/entries",
    response_model=list[PriceEntryOut],
    tags=["costs"],
    summary="List a price book's entries",
)
async def list_price_entries(
    price_book_id: str, principal: PrincipalDep, services: ServicesDep
) -> list[PriceEntryOut]:
    principal.require(Permission.PRICE_BOOK_READ)
    entries = await services.pricing.list_entries(price_book_id)
    return [
        PriceEntryOut(
            id=entry.id,
            provider=entry.provider,
            model_identifier=entry.model_identifier,
            usage_category=entry.usage_category,
            unit_quantity=entry.unit_quantity,
            unit_price=str(entry.unit_price),
            currency=entry.currency,
            effective_from=entry.effective_from,
            effective_to=entry.effective_to,
            tier_min_units=entry.tier_min_units,
            tier_max_units=entry.tier_max_units,
            discount_rate=None if entry.discount_rate is None else str(entry.discount_rate),
            source_url=entry.source_url,
        )
        for entry in entries
    ]
