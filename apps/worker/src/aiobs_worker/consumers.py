"""Bus consumers.

One consumer per topic, each a loop over ``bus.consume`` that hands batches to a
processor and then decides, per message, between commit / retry / dead-letter.

The retry decision is where correctness lives:

* **Permanent failures** (malformed payload, unknown schema version) go straight
  to the dead-letter topic. Retrying cannot change the outcome and would block
  the partition.
* **Transient failures** (storage unavailable) are retried with full-jitter
  exponential backoff up to ``max_delivery_attempts``, then dead-lettered so an
  operator sees them.
* **Successes** are committed only after the processor's writes have returned.
  A crash before the commit re-delivers the batch, which is safe because every
  handler is idempotent.

Committing before processing would be faster and would lose data on every crash.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from aiobs_api.core.config import Settings
from aiobs_api.core.context import RequestContext, use_context
from aiobs_api.core.logging import get_logger
from aiobs_api.services.processing import ProcessingOutcome
from aiobs_api.storage.bus.protocol import BusMessageEnvelope, EventBus, backoff_delay
from aiobs_api.telemetry.metrics import (
    DEAD_LETTERED,
    PROCESSING_BATCH_SIZE,
    PROCESSING_DURATION,
    PROCESSING_FAILURES,
)

__all__ = ["Consumer", "ConsumerStats"]

log = get_logger(__name__)

#: ``(outcome, permanent_failures, retryable)``
BatchHandler = Callable[
    [Sequence[BusMessageEnvelope]],
    Awaitable[tuple[ProcessingOutcome, list[BusMessageEnvelope], list[BusMessageEnvelope]]],
]


@dataclass(slots=True)
class ConsumerStats:
    """Cumulative counters, surfaced by the worker's status endpoint."""

    batches: int = 0
    messages: int = 0
    committed: int = 0
    retried: int = 0
    dead_lettered: int = 0
    last_batch_at: float = 0.0
    last_error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "batches": self.batches,
            "messages": self.messages,
            "committed": self.committed,
            "retried": self.retried,
            "dead_lettered": self.dead_lettered,
            "last_batch_at": self.last_batch_at,
            "last_error": self.last_error,
        }


class Consumer:
    """Drives one topic through a batch handler."""

    def __init__(
        self,
        *,
        name: str,
        topic: str,
        bus: EventBus,
        handler: BatchHandler,
        settings: Settings,
    ) -> None:
        self._name = name
        self._topic = topic
        self._bus = bus
        self._handler = handler
        self._settings = settings
        self._stopping = asyncio.Event()
        self.stats = ConsumerStats()

    @property
    def name(self) -> str:
        return self._name

    def request_stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        """Consume until stopped. Never raises: a consumer that dies is an outage."""
        group = self._settings.bus.consumer_group
        log.info("consumer.started", consumer=self._name, topic=self._topic, group=group)
        try:
            async for batch in self._bus.consume(
                self._topic,
                group=group,
                max_records=self._settings.bus.max_poll_records,
                poll_interval=self._settings.bus.poll_interval_seconds,
            ):
                if self._stopping.is_set():
                    break
                await self._process(batch, group)
        except asyncio.CancelledError:
            log.info("consumer.cancelled", consumer=self._name)
            raise
        except Exception as exc:
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            log.error("consumer.crashed", consumer=self._name, error=str(exc), exc_info=True)
            raise
        finally:
            log.info("consumer.stopped", consumer=self._name, **self.stats.as_dict())

    async def _process(self, batch: list[BusMessageEnvelope], group: str) -> None:
        started = time.perf_counter()
        # Each batch gets a correlation id so its log lines can be tied together
        # even though there is no HTTP request behind it.
        context = RequestContext(
            request_id=f"wrk_{self._name}_{int(started * 1000) % 10**10}",
            principal_type="worker",
            route=f"consumer:{self._topic}",
        )
        with use_context(context):
            self.stats.batches += 1
            self.stats.messages += len(batch)
            PROCESSING_BATCH_SIZE.labels(consumer=self._name).observe(len(batch))

            try:
                outcome, permanent, retryable = await self._handler(batch)
            except Exception as exc:
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                PROCESSING_FAILURES.labels(consumer=self._name, kind="unhandled").inc(len(batch))
                log.error(
                    "consumer.handler_failed",
                    consumer=self._name,
                    size=len(batch),
                    error=str(exc),
                    exc_info=True,
                )
                await self._retry_all(batch, group)
                return

            PROCESSING_DURATION.labels(consumer=self._name).observe(time.perf_counter() - started)
            self.stats.last_batch_at = time.time()

            for message in permanent:
                await self._dead_letter(message, group, "permanent", self.stats.last_error)
            for message in retryable:
                await self._retry_one(message, group)

            failed = {id(message) for message in permanent} | {id(message) for message in retryable}
            committed = [message for message in batch if id(message) not in failed]
            if committed:
                await self._bus.commit(committed, group=group)
                self.stats.committed += len(committed)

            if outcome.spans_written or outcome.traces_touched:
                log.info(
                    "consumer.batch_processed",
                    consumer=self._name,
                    messages=len(batch),
                    spans=outcome.spans_written,
                    traces=outcome.traces_touched,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )

    async def _retry_all(self, batch: Sequence[BusMessageEnvelope], group: str) -> None:
        for message in batch:
            await self._retry_one(message, group)

    async def _retry_one(self, message: BusMessageEnvelope, group: str) -> None:
        if message.attempt >= self._settings.bus.max_delivery_attempts:
            await self._dead_letter(
                message,
                group,
                "max_attempts",
                f"exhausted {message.attempt} delivery attempts",
            )
            return
        delay = backoff_delay(
            message.attempt,
            base_seconds=self._settings.bus.retry_base_delay_seconds,
            max_seconds=self._settings.bus.retry_max_delay_seconds,
            jitter=self._settings.bus.retry_jitter,
        )
        self.stats.retried += 1
        PROCESSING_FAILURES.labels(consumer=self._name, kind="transient").inc()
        await self._bus.retry_later(message, group=group, delay_seconds=delay)

    async def _dead_letter(
        self, message: BusMessageEnvelope, group: str, kind: str, detail: str
    ) -> None:
        self.stats.dead_lettered += 1
        DEAD_LETTERED.labels(topic=message.topic).inc()
        PROCESSING_FAILURES.labels(consumer=self._name, kind=kind).inc()
        await self._bus.dead_letter(
            message, group=group, error_type=kind, error_message=detail or kind
        )
        # Commit it: the message is durably parked in the DLQ, so re-delivering
        # it would only block the partition behind a message that cannot succeed.
        await self._bus.commit([message], group=group)
