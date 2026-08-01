"""Durable event bus interface.

The API accepts telemetry and returns ``202 Accepted`` in single-digit
milliseconds; the expensive work -- normalisation, cost calculation, roll-up
recomputation, analytics writes -- happens in the worker. The bus is what makes
that split safe rather than lossy.

Semantics every driver must provide:

* **At-least-once delivery.** A consumer that crashes mid-batch sees those
  messages again. Handlers are therefore required to be idempotent, which the
  ingestion path achieves through content-hash de-duplication and
  ``ReplacingMergeTree`` semantics.
* **Ordered per partition key.** All spans of one trace share a partition key,
  so one consumer sees them in order and the roll-up is computed from a
  consistent view.
* **Bounded retries with jittered backoff.** A transient downstream failure is
  retried; a permanent one is parked in the dead-letter topic after
  ``max_delivery_attempts`` instead of blocking the partition forever.
* **Poison-message isolation.** A message that fails deserialisation is
  dead-lettered immediately -- retrying it can only fail identically.

Two drivers implement this: Kafka/Redpanda for production, and a
database-backed queue for single-node development. Both are exercised by the
same test suite.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = [
    "BusMessageEnvelope",
    "EventBus",
    "Topics",
    "backoff_delay",
]


class Topics:
    """Topic names. Prefixed per deployment so environments can share a cluster."""

    SPANS = "spans"
    #: Trace ids whose roll-up must be recomputed.
    TRACE_ROLLUP = "trace-rollup"
    #: Deferred maintenance: retention sweeps, export jobs, reconciliation.
    MAINTENANCE = "maintenance"
    DEAD_LETTER_SUFFIX = "dlq"

    ALL: tuple[str, ...] = (SPANS, TRACE_ROLLUP, MAINTENANCE)


@dataclass(slots=True)
class BusMessageEnvelope:
    """One message plus the metadata a consumer needs to act on it."""

    topic: str
    partition_key: str
    payload: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    #: Wire-schema version. A consumer that does not understand the major
    #: version dead-letters rather than guessing.
    schema_version: str = "1.0"
    offset: int = 0
    partition: int = 0
    #: Delivery attempt number, starting at 1.
    attempt: int = 1
    created_at: datetime | None = None
    #: Opaque driver handle used to acknowledge this specific message.
    ack_token: Any = None

    @property
    def trace_context(self) -> str | None:
        """W3C traceparent propagated from the producing request, if any."""
        return self.headers.get("traceparent")


def backoff_delay(
    attempt: int,
    *,
    base_seconds: float,
    max_seconds: float,
    jitter: bool = True,
) -> float:
    """Exponential backoff with full jitter.

    Full jitter (``uniform(0, backoff)``) rather than the more common
    ``backoff ± 10%``: after a downstream outage every consumer retries at the
    same moment, and a narrow jitter band reproduces the thundering herd that
    caused the outage. Full jitter spreads retries across the whole window.
    """
    exponential = min(base_seconds * (2 ** max(attempt - 1, 0)), max_seconds)
    if not jitter:
        return exponential
    return random.uniform(0.0, exponential)


class EventBus(ABC):
    """Durable, partitioned message transport."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def check_health(self) -> None: ...

    @abstractmethod
    async def publish(
        self,
        topic: str,
        *,
        partition_key: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        schema_version: str = "1.0",
    ) -> None:
        """Publish one message. Must not return until the broker has accepted it."""

    @abstractmethod
    async def publish_batch(self, messages: list[BusMessageEnvelope]) -> int:
        """Publish many messages, returning the number accepted."""

    @abstractmethod
    def consume(
        self,
        topic: str,
        *,
        group: str,
        max_records: int = 100,
        poll_interval: float = 0.25,
    ) -> AsyncIterator[list[BusMessageEnvelope]]:
        """Yield batches of messages for ``group`` until cancelled."""

    @abstractmethod
    async def commit(self, messages: list[BusMessageEnvelope], *, group: str) -> None:
        """Mark messages as durably processed."""

    @abstractmethod
    async def dead_letter(
        self,
        message: BusMessageEnvelope,
        *,
        group: str,
        error_type: str,
        error_message: str,
    ) -> None:
        """Park a message that has exhausted its delivery attempts."""

    @abstractmethod
    async def retry_later(
        self, message: BusMessageEnvelope, *, group: str, delay_seconds: float
    ) -> None:
        """Re-deliver ``message`` after ``delay_seconds``."""

    @abstractmethod
    async def consumer_lag(self, topic: str, *, group: str) -> int:
        """Messages published but not yet committed by ``group``.

        The single most useful operational number the platform has: it answers
        "is ingestion keeping up?" without needing to reason about throughput.
        """

    @abstractmethod
    async def replay_dead_letters(self, topic: str, *, group: str, limit: int = 100) -> int:
        """Re-publish parked messages after an operator has fixed the handler."""
