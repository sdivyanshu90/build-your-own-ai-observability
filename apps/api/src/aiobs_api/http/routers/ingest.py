"""Telemetry ingestion endpoints: OTLP and the native batch format.

``POST /v1/traces`` is mounted at the path the OTLP/HTTP specification fixes.
OpenTelemetry exporters append ``/v1/traces`` to their configured endpoint and
offer no way to change it, so the path is not negotiable.

Both endpoints return ``202 Accepted`` with a per-span outcome. Accepting the
batch means "durably queued", not "queryable": visibility follows once the
worker has processed it, typically within a second. That distinction is
explicit in the response and in the documentation, because a client that
assumes read-after-write will write a flaky test.
"""

from __future__ import annotations

import gzip
import json
import zlib
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response, status

from aiobs_schemas.errors import ErrorCode
from aiobs_schemas.wire import IngestBatch, IngestResponse

from ...core.errors import AiobsError, ValidationFailedError
from ...core.logging import get_logger
from ...domain.rbac import Permission
from ...ingest.otlp import (
    OtlpDecodeError,
    decode_otlp_json,
    decode_otlp_protobuf,
)
from ..deps import PrincipalDep, ServicesDep

__all__ = ["router"]

log = get_logger(__name__)

router = APIRouter(tags=["ingest"])

_MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024


async def _read_body(request: Request, encoding: str | None) -> bytes:
    """Read and decompress the request body with a decompression-bomb guard.

    A 1 MB gzip payload can expand to gigabytes. Decompressing incrementally
    with a hard ceiling turns that from an out-of-memory kill into a 413.
    """
    raw = await request.body()
    if not encoding:
        return raw
    normalised = encoding.lower().strip()
    try:
        if normalised == "gzip":
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif normalised in {"deflate", "zlib"}:
            decompressor = zlib.decompressobj()
        else:
            raise ValidationFailedError(
                f"unsupported Content-Encoding {encoding!r}; use gzip or deflate"
            )
        decompressed = decompressor.decompress(raw, _MAX_DECOMPRESSED_BYTES)
        if decompressor.unconsumed_tail:
            raise AiobsError(
                "decompressed payload exceeds the maximum permitted size",
                code=ErrorCode.PAYLOAD_TOO_LARGE,
            )
        return decompressed
    except (zlib.error, gzip.BadGzipFile) as exc:
        raise ValidationFailedError(f"malformed compressed body: {exc}") from exc


def _apply_rate_limit_headers(response: Response, result) -> None:  # type: ignore[no-untyped-def]
    if result is not None:
        for key, value in result.as_headers().items():
            response.headers[key] = value


@router.post(
    "/v1/ingest/spans",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a batch of spans (native format)",
    description=(
        "Accepts the platform's native JSON batch format. Returns per-span "
        "rejections rather than failing the whole batch. Supply an "
        "`Idempotency-Key` header to make retries safe."
    ),
)
async def ingest_spans(
    request: Request,
    response: Response,
    principal: PrincipalDep,
    services: ServicesDep,
    content_encoding: Annotated[str | None, Header()] = None,
) -> IngestResponse:
    principal.require(Permission.INGEST_WRITE)
    if not principal.project_id:
        raise ValidationFailedError(
            "ingestion requires an API key bound to a project and environment"
        )

    # The body is read manually rather than declared as a Pydantic parameter so
    # that Content-Encoding is handled identically to the OTLP endpoint. SDKs
    # compress by default -- a batch of spans with payloads is several hundred
    # kilobytes -- and FastAPI does not decompress request bodies.
    raw = await _read_body(request, content_encoding)
    try:
        batch = IngestBatch.model_validate_json(raw)
    except UnicodeDecodeError as exc:
        raise ValidationFailedError(f"request body is not valid UTF-8: {exc}") from exc

    result = await services.ingestion.ingest(
        principal=principal,
        batch=batch,
        source="native_json",
        payload_bytes=len(raw),
    )
    _apply_rate_limit_headers(response, result.rate_limit)
    return result.response


@router.post(
    "/v1/traces",
    status_code=status.HTTP_202_ACCEPTED,
    summary="OTLP/HTTP trace ingestion",
    description=(
        "Standard OpenTelemetry OTLP/HTTP endpoint. Accepts "
        "`application/x-protobuf` and `application/json`, with optional gzip. "
        "Point any OpenTelemetry exporter at this deployment's base URL."
    ),
    responses={
        202: {"description": "Accepted, possibly with partial rejections"},
        415: {"description": "Unsupported content type"},
    },
)
async def ingest_otlp(
    request: Request,
    response: Response,
    principal: PrincipalDep,
    services: ServicesDep,
    content_type: Annotated[str | None, Header()] = None,
    content_encoding: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    principal.require(Permission.INGEST_WRITE)
    if not principal.project_id:
        raise ValidationFailedError(
            "ingestion requires an API key bound to a project and environment"
        )

    body = await _read_body(request, content_encoding)
    media_type = (content_type or "application/x-protobuf").split(";", 1)[0].strip().lower()

    try:
        if media_type in {
            "application/x-protobuf",
            "application/protobuf",
            "application/octet-stream",
        }:
            decoded = decode_otlp_protobuf(body)
            source = "otlp_http_proto"
        elif media_type == "application/json":
            decoded = decode_otlp_json(json.loads(body.decode("utf-8")))
            source = "otlp_http_json"
        else:
            raise AiobsError(
                f"unsupported content type {media_type!r}; "
                "use application/x-protobuf or application/json",
                code=ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            )
    except OtlpDecodeError as exc:
        raise ValidationFailedError(str(exc)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationFailedError(f"malformed OTLP JSON payload: {exc}") from exc

    result = await services.ingestion.ingest_otlp_groups(
        principal=principal,
        groups=decoded.groups,
        decode_rejections=decoded.rejected,
        source=source,
        payload_bytes=len(body),
    )
    _apply_rate_limit_headers(response, result.rate_limit)

    # OTLP's partial-success shape, which collectors understand and log.
    rejected = result.response.rejected
    return {
        "partialSuccess": (
            {
                "rejectedSpans": str(rejected),
                "errorMessage": "; ".join(
                    f"[{item.index}] {item.message}" for item in result.response.rejections[:10]
                ),
            }
            if rejected
            else {}
        ),
        "batchId": result.response.batch_id,
        "acceptedSpans": result.response.accepted,
        "duplicateSpans": result.response.duplicates,
    }


@router.get(
    "/v1/ingest/limits",
    summary="Report the ingestion limits this deployment enforces",
    description=(
        "SDKs read this at start-up to size their batches. Limits may be "
        "tightened per tenant but never loosened beyond the values here."
    ),
)
async def ingest_limits(principal: PrincipalDep, services: ServicesDep) -> dict[str, object]:
    from aiobs_schemas.wire import LIMITS

    settings = services.container.settings
    return {
        "max_spans_per_batch": LIMITS.MAX_SPANS_PER_BATCH,
        "max_attributes_per_span": LIMITS.MAX_ATTRIBUTES_PER_SPAN,
        "max_events_per_span": LIMITS.MAX_EVENTS_PER_SPAN,
        "max_links_per_span": LIMITS.MAX_LINKS_PER_SPAN,
        "max_attribute_value_length": LIMITS.MAX_ATTRIBUTE_VALUE_LENGTH,
        "max_body_bytes": settings.security.max_request_bytes,
        "max_clock_skew_future_seconds": settings.ingest.max_clock_skew_future_seconds,
        "max_backfill_age_seconds": settings.ingest.max_backfill_age_seconds,
        "rate_limit_per_minute": settings.security.ingest_rate_limit_per_minute,
        "burst": settings.security.ingest_burst,
        "otlp_endpoint": f"{settings.public_url}/v1/traces",
        "native_endpoint": f"{settings.public_url}/v1/ingest/spans",
    }
