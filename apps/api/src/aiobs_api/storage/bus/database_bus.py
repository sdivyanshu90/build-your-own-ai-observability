"""Database-backed event bus.

A complete implementation of the :class:`EventBus` contract using three tables:
an append-only log (``bus_messages``), per-consumer-group progress markers with
leases (``bus_offsets``), and a dead-letter table.

It exists so that ``make dev`` starts a working platform without a Kafka
cluster, and so that the ingestion pipeline's retry, backoff, dead-letter and
replay behaviour can be tested deterministically in-process. It is genuinely
durable -- messages survive a restart -- but it is single-cluster and polls, so
throughput is bounded by the database rather than by disk sequential writes.

Leasing is what gives at-least-once delivery: a consumer claims an offset range
for ``lease_seconds``; if it dies, the lease lapses and another consumer re-reads
the same range. Handlers must be idempotent, and on the ingestion path they are.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, update

from ...core.errors import DependencyUnavailableError
from ...core.logging import get_logger
from ...core.timeutil import Clock, SystemClock
from ..postgres.models.operations import BusDeadLetter, BusMessage, BusOffset
from ..postgres.session import Database
from .protocol import BusMessageEnvelope, EventBus

__all__ = ["DatabaseEventBus"]

log = get_logger(__name__)

_PARTITION_COUNT = 8


def _partition_for(key: str) -> int:
    """Stable partition assignment.

    A trace's spans must all land on one partition so a single consumer sees
    them in order. Python's ``hash()`` is randomised per process, so an explicit
    stable hash is required -- using ``hash()`` here would scatter one trace's
    spans across partitions on every restart.
    """
    digest = 0
    for byte in key.encode("utf-8"):
        digest = (digest * 131 + byte) & 0xFFFFFFFF
    return digest % _PARTITION_COUNT


class DatabaseEventBus(EventBus):
    """Durable queue backed by the relational metadata store."""

    def __init__(
        self,
        database: Database,
        *,
        topic_prefix: str = "aiobs",
        lease_seconds: int = 60,
        message_ttl_seconds: int = 7 * 86_400,
        clock: Clock | None = None,
        owner: str = "worker",
    ) -> None:
        self._database = database
        self._prefix = topic_prefix
        self._lease_seconds = lease_seconds
        self._message_ttl_seconds = message_ttl_seconds
        self._clock = clock or SystemClock()
        self._owner = owner
        self._closed = False
        #: Messages scheduled for delayed re-delivery, keyed by wake time.
        self._retry_queue: list[tuple[float, BusMessageEnvelope]] = []

    def _topic(self, topic: str) -> str:
        return f"{self._prefix}.{topic}"

    async def start(self) -> None:
        self._closed = False

    async def close(self) -> None:
        self._closed = True

    async def check_health(self) -> None:
        await self._database.check_health()

    # ------------------------------------------------------------------
    # producing
    # ------------------------------------------------------------------

    async def publish(
        self,
        topic: str,
        *,
        partition_key: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        schema_version: str = "1.0",
    ) -> None:
        await self.publish_batch(
            [
                BusMessageEnvelope(
                    topic=topic,
                    partition_key=partition_key,
                    payload=payload,
                    headers=headers or {},
                    schema_version=schema_version,
                )
            ]
        )

    async def publish_batch(self, messages: list[BusMessageEnvelope]) -> int:
        if not messages:
            return 0
        now = self._clock.now()
        expires_at = now + timedelta(seconds=self._message_ttl_seconds)
        rows = [
            BusMessage(
                topic=self._topic(message.topic),
                partition=_partition_for(message.partition_key),
                partition_key=message.partition_key,
                payload=message.payload,
                headers=message.headers,
                schema_version=message.schema_version,
                created_at=now,
                expires_at=expires_at,
            )
            for message in messages
        ]
        try:
            async with self._database.session_scope() as session:
                session.add_all(rows)
        except Exception as exc:
            raise DependencyUnavailableError("event-bus", cause=str(exc)) from exc
        return len(rows)

    async def publish_in_transaction(self, session: Any, messages: list[BusMessageEnvelope]) -> int:
        """Publish inside a caller-supplied transaction.

        This is what makes the outbox pattern unnecessary for the simple cases:
        when the bus and the domain state share a database, the event and the
        state change commit atomically without a relay.
        """
        now = self._clock.now()
        expires_at = now + timedelta(seconds=self._message_ttl_seconds)
        session.add_all(
            [
                BusMessage(
                    topic=self._topic(message.topic),
                    partition=_partition_for(message.partition_key),
                    partition_key=message.partition_key,
                    payload=message.payload,
                    headers=message.headers,
                    schema_version=message.schema_version,
                    created_at=now,
                    expires_at=expires_at,
                )
                for message in messages
            ]
        )
        return len(messages)

    # ------------------------------------------------------------------
    # consuming
    # ------------------------------------------------------------------

    async def consume(
        self,
        topic: str,
        *,
        group: str,
        max_records: int = 100,
        poll_interval: float = 0.25,
    ) -> AsyncIterator[list[BusMessageEnvelope]]:
        qualified = self._topic(topic)
        while not self._closed:
            batch = await self._drain_retry_queue()
            if not batch:
                batch = await self._claim(qualified, group=group, max_records=max_records)
            if batch:
                yield batch
            else:
                await asyncio.sleep(poll_interval)

    async def _drain_retry_queue(self) -> list[BusMessageEnvelope]:
        """Return messages whose backoff has elapsed."""
        if not self._retry_queue:
            return []
        now = self._clock.now_unix_seconds()
        ready = [envelope for wake_at, envelope in self._retry_queue if wake_at <= now]
        if ready:
            self._retry_queue = [
                (wake_at, envelope) for wake_at, envelope in self._retry_queue if wake_at > now
            ]
        return ready

    async def _claim(
        self, qualified_topic: str, *, group: str, max_records: int
    ) -> list[BusMessageEnvelope]:
        """Lease the next range of messages for this consumer group."""
        now = self._clock.now()
        lease_until = now + timedelta(seconds=self._lease_seconds)
        claimed: list[BusMessageEnvelope] = []

        async with self._database.session_scope() as session:
            for partition in range(_PARTITION_COUNT):
                offset_row = (
                    await session.execute(
                        select(BusOffset).where(
                            BusOffset.consumer_group == group,
                            BusOffset.topic == qualified_topic,
                            BusOffset.partition == partition,
                        )
                    )
                ).scalar_one_or_none()

                if offset_row is None:
                    from aiobs_schemas.ids import IdPrefix, generate_id

                    offset_row = BusOffset(
                        id=generate_id(IdPrefix.INGEST_BATCH),
                        consumer_group=group,
                        topic=qualified_topic,
                        partition=partition,
                        committed_offset=0,
                        updated_at=now,
                    )
                    session.add(offset_row)
                    await session.flush()
                elif (
                    offset_row.lease_owner is not None
                    and offset_row.lease_expires_at is not None
                    and offset_row.lease_expires_at > now
                    and offset_row.lease_owner != self._owner
                ):
                    # Another consumer holds this partition.
                    continue

                remaining = max_records - len(claimed)
                if remaining <= 0:
                    break

                rows = (
                    (
                        await session.execute(
                            select(BusMessage)
                            .where(
                                BusMessage.topic == qualified_topic,
                                BusMessage.partition == partition,
                                BusMessage.id > offset_row.committed_offset,
                            )
                            .order_by(BusMessage.id.asc())
                            .limit(remaining)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not rows:
                    continue

                offset_row.lease_owner = self._owner
                offset_row.lease_expires_at = lease_until
                offset_row.updated_at = now

                for row in rows:
                    claimed.append(
                        BusMessageEnvelope(
                            topic=_strip_prefix(row.topic, self._prefix),
                            partition_key=row.partition_key,
                            payload=dict(row.payload),
                            headers=dict(row.headers or {}),
                            schema_version=row.schema_version,
                            offset=int(row.id),
                            partition=int(row.partition),
                            created_at=row.created_at,
                            ack_token=(qualified_topic, int(row.partition), int(row.id)),
                        )
                    )
        return claimed

    async def commit(self, messages: list[BusMessageEnvelope], *, group: str) -> None:
        if not messages:
            return
        # Commit the highest contiguous offset per partition. Committing a
        # non-contiguous maximum would silently skip the messages in between.
        highest: dict[tuple[str, int], int] = {}
        for message in messages:
            if message.ack_token is None:
                continue
            topic, partition, offset = message.ack_token
            key = (topic, partition)
            highest[key] = max(highest.get(key, 0), offset)

        now = self._clock.now()
        async with self._database.session_scope() as session:
            for (topic, partition), offset in highest.items():
                await session.execute(
                    update(BusOffset)
                    .where(
                        BusOffset.consumer_group == group,
                        BusOffset.topic == topic,
                        BusOffset.partition == partition,
                        BusOffset.committed_offset < offset,
                    )
                    .values(
                        committed_offset=offset,
                        lease_owner=None,
                        lease_expires_at=None,
                        updated_at=now,
                    )
                )

    async def retry_later(
        self, message: BusMessageEnvelope, *, group: str, delay_seconds: float
    ) -> None:
        message.attempt += 1
        wake_at = self._clock.now_unix_seconds() + max(delay_seconds, 0.0)
        self._retry_queue.append((wake_at, message))

    async def dead_letter(
        self,
        message: BusMessageEnvelope,
        *,
        group: str,
        error_type: str,
        error_message: str,
    ) -> None:
        from aiobs_schemas.ids import IdPrefix, generate_id

        now = self._clock.now()
        async with self._database.session_scope() as session:
            session.add(
                BusDeadLetter(
                    id=generate_id(IdPrefix.INGEST_BATCH),
                    topic=self._topic(message.topic),
                    consumer_group=group,
                    original_offset=message.offset,
                    partition_key=message.partition_key,
                    payload=message.payload,
                    headers=message.headers,
                    attempts=message.attempt,
                    error_type=error_type[:128],
                    error_message=error_message[:8_000],
                    first_failed_at=message.created_at or now,
                    dead_lettered_at=now,
                )
            )
        log.warning(
            "bus.dead_lettered",
            topic=message.topic,
            partition_key=message.partition_key,
            attempts=message.attempt,
            error_type=error_type,
        )

    async def consumer_lag(self, topic: str, *, group: str) -> int:
        qualified = self._topic(topic)
        async with self._database.session_scope() as session:
            offsets = (
                (
                    await session.execute(
                        select(BusOffset.partition, BusOffset.committed_offset).where(
                            BusOffset.consumer_group == group,
                            BusOffset.topic == qualified,
                        )
                    )
                )
                .tuples()
                .all()
            )
            committed = dict(offsets)
            total = 0
            heads = (
                (
                    await session.execute(
                        select(BusMessage.partition, func.max(BusMessage.id))
                        .where(BusMessage.topic == qualified)
                        .group_by(BusMessage.partition)
                    )
                )
                .tuples()
                .all()
            )
            for partition, head in heads:
                total += max(int(head) - committed.get(partition, 0), 0)
            return total

    async def replay_dead_letters(self, topic: str, *, group: str, limit: int = 100) -> int:
        qualified = self._topic(topic)
        now = self._clock.now()
        replayed: list[BusMessageEnvelope] = []
        async with self._database.session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(BusDeadLetter)
                        .where(
                            BusDeadLetter.topic == qualified,
                            BusDeadLetter.consumer_group == group,
                            BusDeadLetter.replayed_at.is_(None),
                        )
                        .order_by(BusDeadLetter.dead_lettered_at.asc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                replayed.append(
                    BusMessageEnvelope(
                        topic=topic,
                        partition_key=row.partition_key,
                        payload=dict(row.payload),
                        headers={**dict(row.headers or {}), "x-replayed": "true"},
                    )
                )
                row.replayed_at = now
        if replayed:
            await self.publish_batch(replayed)
        return len(replayed)

    async def purge_expired(self) -> int:
        """Trim messages past their TTL that every group has consumed."""
        now = self._clock.now()
        async with self._database.session_scope() as session:
            minimum_offsets = (
                (
                    await session.execute(
                        select(
                            BusOffset.topic,
                            BusOffset.partition,
                            func.min(BusOffset.committed_offset),
                        ).group_by(BusOffset.topic, BusOffset.partition)
                    )
                )
                .tuples()
                .all()
            )
            removed = 0
            for topic, partition, committed in minimum_offsets:
                result = await session.execute(
                    delete(BusMessage).where(
                        BusMessage.topic == topic,
                        BusMessage.partition == partition,
                        BusMessage.id <= committed,
                        BusMessage.expires_at < now,
                    )
                )
                removed += result.rowcount or 0
            return removed


def _strip_prefix(topic: str, prefix: str) -> str:
    marker = f"{prefix}."
    return topic[len(marker) :] if topic.startswith(marker) else topic


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
