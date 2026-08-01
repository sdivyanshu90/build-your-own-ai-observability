"""The ingestion service: the API half of the telemetry pipeline.

Responsibilities, in order:

1. **Enforce the budget.** Per-key token bucket, checked before any parsing, so
   a runaway producer costs one Redis round-trip rather than a full batch decode.
2. **Replay safely.** A batch carrying an ``Idempotency-Key`` that has already
   been processed returns the original response without re-applying it.
3. **Validate and normalise.** Bad spans are rejected individually with a
   machine-readable reason; the rest of the batch proceeds.
4. **De-duplicate.** A content-hash ``SET NX`` per span drops replays before
   they reach the analytics store.
5. **Hand off durably.** Spans are published to the bus. The API returns 202
   only after the broker has acknowledged them, so "accepted" means "will be
   processed", not "probably".

The API deliberately does *not* write to the analytics store. Keeping the write
path asynchronous is what lets ingestion absorb a burst that the analytics store
could not: the queue grows, latency to visibility rises, and nothing is lost.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiobs_schemas.ids import IdPrefix, generate_id
from aiobs_schemas.wire import (
    IngestBatch,
    IngestResponse,
    ResourceDescriptor,
    SpanRejection,
    WireSpan,
)

from ..core.config import Settings
from ..core.errors import QuotaExceededError, RateLimitedError
from ..core.logging import get_logger
from ..core.timeutil import Clock
from ..domain.principal import Principal
from ..ingest.normalizer import (
    IngestScope,
    NormalizedSpan,
    SpanNormalizer,
    normalize_batch,
)
from ..ingest.serialization import encode_span_message
from ..storage.bus.protocol import BusMessageEnvelope, EventBus, Topics
from ..storage.kv.protocol import KeyValueStore, RateLimitResult
from ..storage.postgres.models import IngestBatchRecord
from ..storage.postgres.session import Database

__all__ = ["IngestionResult", "IngestionService"]

log = get_logger(__name__)


@dataclass(slots=True)
class IngestionResult:
    """Outcome of one ingest request, plus the rate-limit headers to emit."""

    response: IngestResponse
    rate_limit: RateLimitResult | None = None


class IngestionService:
    """Accepts, validates and enqueues telemetry."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        bus: EventBus,
        kv: KeyValueStore,
        normalizer: SpanNormalizer,
        clock: Clock,
    ) -> None:
        self._settings = settings
        self._database = database
        self._bus = bus
        self._kv = kv
        self._normalizer = normalizer
        self._clock = clock

    # ------------------------------------------------------------------
    # budget
    # ------------------------------------------------------------------

    async def check_rate_limit(self, principal: Principal, *, cost: int = 1) -> RateLimitResult:
        """Consume ingest budget for ``principal``.

        When the key-value store is unreachable and ``fail_open`` is set, the
        request is allowed. That is a deliberate availability-over-enforcement
        trade: dropping a customer's telemetry because *our* cache is down is
        the worse outcome, and the analytics store's de-duplication still
        protects correctness.
        """
        limits = self._settings.security
        try:
            return await self._kv.check_rate_limit(
                f"ingest:{principal.id}",
                limit_per_minute=limits.ingest_rate_limit_per_minute,
                burst=limits.ingest_burst,
                cost=cost,
            )
        except Exception as exc:
            if not self._settings.kv.fail_open:
                raise
            log.warning("ingest.rate_limit_unavailable", error=str(exc))
            return RateLimitResult(
                allowed=True,
                limit=limits.ingest_rate_limit_per_minute,
                remaining=limits.ingest_burst,
                retry_after=0.0,
                reset_at=self._clock.now_unix_seconds() + 60,
            )

    async def check_quota(self, principal: Principal, span_count: int) -> None:
        """Enforce the tenant's daily span quota.

        Counted in the key-value store rather than by querying the analytics
        store: a per-request ``COUNT(*)`` over a day of spans would cost more
        than the ingestion it guards.
        """
        day = self._clock.now().strftime("%Y%m%d")
        key = f"quota:{principal.organization_id}:{day}"
        try:
            used = await self._kv.increment(key, span_count, ttl_seconds=2 * 86_400)
        except Exception as exc:
            if not self._settings.kv.fail_open:
                raise
            log.warning("ingest.quota_unavailable", error=str(exc))
            return
        limit = await self._organization_quota(principal.organization_id)
        if limit and used > limit:
            raise QuotaExceededError(
                f"daily span quota of {limit} exceeded",
                context={"used": used, "limit": limit, "window": "day"},
                retry_after_seconds=_seconds_until_midnight(self._clock.now()),
            )

    async def _organization_quota(self, organization_id: str) -> int:
        from sqlalchemy import select

        from ..storage.postgres.models import Organization

        cached = await self._kv.get(f"quota-limit:{organization_id}")
        if cached is not None:
            return int(cached)
        async with self._database.session_scope() as session:
            value = (
                await session.execute(
                    select(Organization.max_spans_per_day).where(Organization.id == organization_id)
                )
            ).scalar_one_or_none()
        limit = int(value or 0)
        await self._kv.set(f"quota-limit:{organization_id}", str(limit).encode(), ttl_seconds=300)
        return limit

    # ------------------------------------------------------------------
    # ingest
    # ------------------------------------------------------------------

    async def ingest(
        self,
        *,
        principal: Principal,
        batch: IngestBatch,
        source: str,
        payload_bytes: int,
    ) -> IngestionResult:
        """Validate, de-duplicate and enqueue a batch of spans."""
        rate_limit = await self.check_rate_limit(principal, cost=max(len(batch.spans) // 50, 1))
        if not rate_limit.allowed:
            raise RateLimitedError(
                rate_limit.retry_after,
                limit=rate_limit.limit,
                window="minute",
            )
        await self.check_quota(principal, len(batch.spans))

        scope = self._scope_for(principal, batch)
        batch_id = generate_id(IdPrefix.INGEST_BATCH)

        replayed = await self._replay_if_seen(principal, batch, batch_id)
        if replayed is not None:
            return IngestionResult(response=replayed, rate_limit=rate_limit)

        normalized, failures = normalize_batch(self._normalizer, batch.spans, batch.resource, scope)
        rejections = [
            SpanRejection(
                span_id=batch.spans[index].span_id if index < len(batch.spans) else None,
                index=index,
                code=_rejection_code(code),
                message=message,
            )
            for index, code, message in failures
        ]

        fresh, duplicates = await self._filter_duplicates(normalized)

        messages: list[BusMessageEnvelope] = []
        for item in fresh:
            messages.append(
                BusMessageEnvelope(
                    topic=Topics.SPANS,
                    # Partitioning by trace keeps one trace's spans on one
                    # consumer, which the roll-up depends on for consistency.
                    partition_key=item.span.trace_id,
                    payload=encode_span_message(item),
                    headers={"organization_id": item.span.organization_id},
                )
            )

        if messages:
            try:
                await self._bus.publish_batch(messages)
            except Exception:
                # The de-duplication keys were claimed before publishing so that
                # two concurrent deliveries of the same span cannot both be
                # queued. If publishing then fails, those claims must be
                # released -- otherwise the client's retry is silently swallowed
                # as a "duplicate" and the span is lost for the whole
                # de-duplication window. Losing telemetry is strictly worse than
                # processing it twice, which the analytics store collapses.
                await self._release_dedup_claims(fresh)
                raise
            # The roll-up request is deliberately NOT published here. Publishing
            # it alongside the spans races the span writer: the roll-up consumer
            # can read a trace whose spans have not landed yet, find nothing,
            # and do nothing. The span processor publishes it *after* the write,
            # which makes the ordering causal instead of hopeful.

        response = IngestResponse(
            accepted=len(fresh),
            rejected=len(rejections),
            duplicates=duplicates,
            batch_id=batch_id,
            replayed=False,
            rejections=rejections[:100],
        )
        await self._record_batch(
            principal=principal,
            scope=scope,
            batch=batch,
            batch_id=batch_id,
            source=source,
            payload_bytes=payload_bytes,
            response=response,
        )
        if batch.idempotency_key:
            await self._remember_batch(principal, batch.idempotency_key, response)

        log.info(
            "ingest.accepted",
            batch_id=batch_id,
            accepted=response.accepted,
            rejected=response.rejected,
            duplicates=response.duplicates,
            source=source,
        )
        return IngestionResult(response=response, rate_limit=rate_limit)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _scope_for(self, principal: Principal, batch: IngestBatch) -> IngestScope:
        """Build the server-authoritative destination for this batch.

        The resource's ``environment`` field is *advisory only*; the real
        environment comes from the credential. A client cannot redirect its
        telemetry into another environment by setting an attribute.
        """
        return IngestScope(
            organization_id=principal.organization_id,
            project_id=principal.project_id or "",
            environment=principal.environment_name or "",
            environment_id=principal.environment_id or "",
            api_key_id=principal.id,
            sampling_rate=batch.sampling_rate if batch.sampling_rate is not None else 1.0,
            store_payloads=self._settings.ingest.store_payloads,
        )

    async def _filter_duplicates(
        self, normalized: Sequence[NormalizedSpan]
    ) -> tuple[list[NormalizedSpan], int]:
        """Drop spans already seen within the de-duplication window."""
        ttl = self._settings.kv.dedup_ttl_seconds
        fresh: list[NormalizedSpan] = []
        duplicates = 0
        for item in normalized:
            key = f"span:{item.span.organization_id}:{item.span.content_hash}"
            try:
                claimed = await self._kv.set_if_absent(key, b"1", ttl_seconds=ttl)
            except Exception as exc:
                if not self._settings.kv.fail_open:
                    raise
                log.warning("ingest.dedup_unavailable", error=str(exc))
                # Fail open: the analytics store's ReplacingMergeTree collapses
                # whatever slips through.
                claimed = True
            if claimed:
                fresh.append(item)
            else:
                duplicates += 1
        return fresh, duplicates

    async def _release_dedup_claims(self, normalized: Sequence[NormalizedSpan]) -> None:
        """Undo de-duplication claims after a failed publish."""
        for item in normalized:
            key = f"span:{item.span.organization_id}:{item.span.content_hash}"
            try:
                await self._kv.delete(key)
            except Exception as exc:
                log.warning("ingest.dedup_release_failed", error=str(exc))

    async def _replay_if_seen(
        self, principal: Principal, batch: IngestBatch, batch_id: str
    ) -> IngestResponse | None:
        """Return the stored response if this idempotency key was already used."""
        if not batch.idempotency_key:
            return None
        key = f"idem:{principal.organization_id}:ingest:{batch.idempotency_key}"
        stored = await self._kv.get(key)
        if stored is None:
            return None
        import json

        try:
            payload = json.loads(stored)
        except ValueError:
            return None
        return IngestResponse(
            accepted=int(payload.get("accepted", 0)),
            rejected=int(payload.get("rejected", 0)),
            duplicates=int(payload.get("duplicates", 0)),
            batch_id=str(payload.get("batch_id", batch_id)),
            replayed=True,
        )

    async def _remember_batch(
        self, principal: Principal, idempotency_key: str, response: IngestResponse
    ) -> None:
        import json

        key = f"idem:{principal.organization_id}:ingest:{idempotency_key}"
        await self._kv.set(
            key,
            json.dumps(
                {
                    "accepted": response.accepted,
                    "rejected": response.rejected,
                    "duplicates": response.duplicates,
                    "batch_id": response.batch_id,
                }
            ).encode("utf-8"),
            ttl_seconds=self._settings.kv.dedup_ttl_seconds,
        )

    async def _record_batch(
        self,
        *,
        principal: Principal,
        scope: IngestScope,
        batch: IngestBatch,
        batch_id: str,
        source: str,
        payload_bytes: int,
        response: IngestResponse,
    ) -> None:
        async with self._database.session_scope() as session:
            session.add(
                IngestBatchRecord(
                    id=batch_id,
                    organization_id=scope.organization_id,
                    project_id=scope.project_id,
                    environment_id=scope.environment_id,
                    api_key_id=scope.api_key_id,
                    source=source,
                    received_at=self._clock.now(),
                    span_count=len(batch.spans),
                    accepted_count=response.accepted,
                    rejected_count=response.rejected,
                    duplicate_count=response.duplicates,
                    payload_bytes=payload_bytes,
                    idempotency_key=batch.idempotency_key,
                    sdk_name=batch.resource.sdk_name,
                    sdk_version=batch.resource.sdk_version,
                )
            )

    async def ingest_otlp_groups(
        self,
        *,
        principal: Principal,
        groups: Sequence[tuple[ResourceDescriptor, list[WireSpan]]],
        decode_rejections: Sequence[tuple[int, str, str]],
        source: str,
        payload_bytes: int,
    ) -> IngestionResult:
        """Ingest decoded OTLP groups, merging per-group results into one response."""
        accepted = duplicates = 0
        rejections: list[SpanRejection] = [
            SpanRejection(span_id=None, index=index, code=_rejection_code(code), message=message)
            for index, code, message in decode_rejections
        ]
        batch_id = generate_id(IdPrefix.INGEST_BATCH)
        rate_limit: RateLimitResult | None = None

        for resource, spans in groups:
            result = await self.ingest(
                principal=principal,
                batch=IngestBatch(resource=resource, spans=spans),
                source=source,
                payload_bytes=payload_bytes // max(len(groups), 1),
            )
            accepted += result.response.accepted
            duplicates += result.response.duplicates
            rejections.extend(result.response.rejections)
            rate_limit = result.rate_limit

        return IngestionResult(
            response=IngestResponse(
                accepted=accepted,
                rejected=len(rejections),
                duplicates=duplicates,
                batch_id=batch_id,
                rejections=rejections[:100],
            ),
            rate_limit=rate_limit,
        )


_REJECTION_CODES = {
    "invalid_span",
    "invalid_trace_id",
    "invalid_span_id",
    "clock_skew",
    "too_old",
    "quota_exceeded",
    "payload_too_large",
    "duplicate",
    "internal_error",
}


def _rejection_code(code: str) -> str:
    return code if code in _REJECTION_CODES else "invalid_span"


def _seconds_until_midnight(now: datetime) -> float:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds()
