"""Kafka / Redpanda event bus -- the production transport.

Producer configuration is chosen for durability over latency, because losing
telemetry silently is the failure this whole subsystem exists to prevent:

``acks="all"``
    The write is acknowledged only once every in-sync replica has it. With
    ``acks=1`` a leader failover loses recently-produced messages.

``enable_idempotence=True``
    A producer retry after a network timeout would otherwise write the batch
    twice. Idempotent production makes the broker de-duplicate by producer id
    and sequence number.

``max_in_flight_requests_per_connection=5``
    The maximum that still preserves ordering under the idempotent producer.

Consumers disable auto-commit. Offsets advance only after the handler has
durably written its results, which is what makes "crash after processing but
before acknowledgement" a duplicate (safe, handled) rather than a loss.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from ...core.errors import DependencyUnavailableError
from ...core.logging import get_logger
from .protocol import BusMessageEnvelope, EventBus

__all__ = ["KafkaEventBus"]

log = get_logger(__name__)


class KafkaEventBus(EventBus):
    """Kafka-compatible durable bus (Apache Kafka, Redpanda, Warpstream)."""

    def __init__(
        self,
        *,
        brokers: str,
        topic_prefix: str = "aiobs",
        compression: str = "lz4",
        client_id: str = "aiobs",
        max_delivery_attempts: int = 5,
    ) -> None:
        self._brokers = brokers
        self._prefix = topic_prefix
        self._compression = None if compression == "none" else compression
        self._client_id = client_id
        self._max_delivery_attempts = max_delivery_attempts
        self._producer: Any = None
        self._consumers: dict[tuple[str, str], Any] = {}
        self._closed = False

    def _topic(self, topic: str) -> str:
        return f"{self._prefix}.{topic}"

    def _dlq_topic(self, topic: str) -> str:
        return f"{self._prefix}.{topic}.dlq"

    async def start(self) -> None:
        if self._producer is not None:
            return
        try:
            from aiokafka import AIOKafkaProducer
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise DependencyUnavailableError("kafka", cause="aiokafka is not installed") from exc
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._brokers,
            client_id=self._client_id,
            acks="all",
            enable_idempotence=True,
            max_in_flight_requests_per_connection=5,
            compression_type=self._compression,
            linger_ms=20,
            request_timeout_ms=30_000,
            value_serializer=lambda value: json.dumps(value, default=str).encode("utf-8"),
            key_serializer=lambda value: value.encode("utf-8") if value else None,
        )
        try:
            await self._producer.start()
        except Exception as exc:
            self._producer = None
            raise DependencyUnavailableError("kafka", cause=str(exc)) from exc
        self._closed = False

    async def close(self) -> None:
        self._closed = True
        for consumer in list(self._consumers.values()):
            try:
                await consumer.stop()
            except Exception:
                log.warning("kafka.consumer_stop_failed", exc_info=True)
        self._consumers.clear()
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    def _require_producer(self) -> Any:
        if self._producer is None:
            raise DependencyUnavailableError("kafka", cause="bus used before start() was awaited")
        return self._producer

    async def check_health(self) -> None:
        producer = self._require_producer()
        try:
            await producer.client.fetch_all_metadata()
        except Exception as exc:
            raise DependencyUnavailableError("kafka", cause=str(exc)) from exc

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
        producer = self._require_producer()
        encoded_headers = [(key, value.encode("utf-8")) for key, value in (headers or {}).items()]
        encoded_headers.append(("schema-version", schema_version.encode("utf-8")))
        try:
            await producer.send_and_wait(
                self._topic(topic),
                value=payload,
                key=partition_key,
                headers=encoded_headers,
            )
        except Exception as exc:
            raise DependencyUnavailableError("kafka", cause=str(exc)) from exc

    async def publish_batch(self, messages: list[BusMessageEnvelope]) -> int:
        if not messages:
            return 0
        producer = self._require_producer()
        futures = []
        for message in messages:
            headers = [(key, value.encode("utf-8")) for key, value in message.headers.items()]
            headers.append(("schema-version", message.schema_version.encode("utf-8")))
            futures.append(
                await producer.send(
                    self._topic(message.topic),
                    value=message.payload,
                    key=message.partition_key,
                    headers=headers,
                )
            )
        try:
            # Waiting on every future is what makes publish_batch a durability
            # boundary: the caller may return 202 only once the broker has the
            # data.
            await asyncio.gather(*futures)
        except Exception as exc:
            raise DependencyUnavailableError("kafka", cause=str(exc)) from exc
        return len(messages)

    # ------------------------------------------------------------------
    # consuming
    # ------------------------------------------------------------------

    async def _consumer_for(self, topic: str, group: str) -> Any:
        key = (topic, group)
        existing = self._consumers.get(key)
        if existing is not None:
            return existing
        from aiokafka import AIOKafkaConsumer

        consumer = AIOKafkaConsumer(
            self._topic(topic),
            bootstrap_servers=self._brokers,
            group_id=group,
            client_id=self._client_id,
            # Offsets advance only after the handler has committed its work.
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            max_poll_records=500,
            # Long enough that a slow ClickHouse insert does not trigger a
            # rebalance, short enough that a hung consumer is evicted.
            max_poll_interval_ms=300_000,
            session_timeout_ms=45_000,
            heartbeat_interval_ms=3_000,
            value_deserializer=lambda raw: json.loads(raw.decode("utf-8")),
            key_deserializer=lambda raw: raw.decode("utf-8") if raw else "",
        )
        await consumer.start()
        self._consumers[key] = consumer
        return consumer

    async def consume(
        self,
        topic: str,
        *,
        group: str,
        max_records: int = 100,
        poll_interval: float = 0.25,
    ) -> AsyncIterator[list[BusMessageEnvelope]]:
        consumer = await self._consumer_for(topic, group)
        while not self._closed:
            try:
                partitions = await consumer.getmany(
                    timeout_ms=int(poll_interval * 1000), max_records=max_records
                )
            except Exception as exc:
                log.warning("kafka.poll_failed", error=str(exc))
                await asyncio.sleep(1.0)
                continue

            batch: list[BusMessageEnvelope] = []
            for topic_partition, records in partitions.items():
                for record in records:
                    headers = {key: value.decode("utf-8") for key, value in (record.headers or [])}
                    batch.append(
                        BusMessageEnvelope(
                            topic=topic,
                            partition_key=record.key or "",
                            payload=record.value,
                            headers=headers,
                            schema_version=headers.get("schema-version", "1.0"),
                            offset=record.offset,
                            partition=topic_partition.partition,
                            ack_token=(topic_partition, record.offset),
                        )
                    )
            if batch:
                yield batch
            else:
                await asyncio.sleep(poll_interval)

    async def commit(self, messages: list[BusMessageEnvelope], *, group: str) -> None:
        if not messages:
            return
        from aiokafka.structs import OffsetAndMetadata

        consumer = self._consumers.get((messages[0].topic, group))
        if consumer is None:
            return
        offsets: dict[Any, Any] = {}
        for message in messages:
            if message.ack_token is None:
                continue
            topic_partition, offset = message.ack_token
            current = offsets.get(topic_partition)
            # Kafka commits the *next* offset to read.
            candidate = offset + 1
            if current is None or candidate > current.offset:
                offsets[topic_partition] = OffsetAndMetadata(candidate, "")
        if offsets:
            await consumer.commit(offsets)

    async def retry_later(
        self, message: BusMessageEnvelope, *, group: str, delay_seconds: float
    ) -> None:
        """Re-publish with an incremented attempt counter after a delay.

        Kafka has no per-message delay primitive. Sleeping in the consumer would
        stall the partition, so the message is re-published to the tail of its
        own topic instead: it loses its place in the ordering, which is
        acceptable for a retry, and the partition keeps moving.
        """
        await asyncio.sleep(max(delay_seconds, 0.0))
        message.attempt += 1
        await self.publish(
            message.topic,
            partition_key=message.partition_key,
            payload=message.payload,
            headers={**message.headers, "x-attempt": str(message.attempt)},
            schema_version=message.schema_version,
        )

    async def dead_letter(
        self,
        message: BusMessageEnvelope,
        *,
        group: str,
        error_type: str,
        error_message: str,
    ) -> None:
        producer = self._require_producer()
        headers = [
            ("x-error-type", error_type.encode("utf-8")),
            ("x-error-message", error_message[:4_000].encode("utf-8")),
            ("x-attempts", str(message.attempt).encode("utf-8")),
            ("x-consumer-group", group.encode("utf-8")),
            ("x-original-topic", message.topic.encode("utf-8")),
        ]
        await producer.send_and_wait(
            self._dlq_topic(message.topic),
            value=message.payload,
            key=message.partition_key,
            headers=headers,
        )
        log.warning(
            "bus.dead_lettered",
            topic=message.topic,
            attempts=message.attempt,
            error_type=error_type,
        )

    async def consumer_lag(self, topic: str, *, group: str) -> int:
        consumer = self._consumers.get((topic, group))
        if consumer is None:
            return 0
        total = 0
        for topic_partition in consumer.assignment():
            try:
                position = await consumer.position(topic_partition)
                end_offsets = await consumer.end_offsets([topic_partition])
                total += max(end_offsets[topic_partition] - position, 0)
            except Exception:
                continue
        return total

    async def replay_dead_letters(self, topic: str, *, group: str, limit: int = 100) -> int:
        """Move parked messages back onto the primary topic.

        Reads from the DLQ with a dedicated, short-lived consumer group so that
        replaying does not disturb the primary consumer's offsets.
        """
        from aiokafka import AIOKafkaConsumer

        consumer = AIOKafkaConsumer(
            self._dlq_topic(topic),
            bootstrap_servers=self._brokers,
            group_id=f"{group}.dlq-replay",
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            value_deserializer=lambda raw: json.loads(raw.decode("utf-8")),
            key_deserializer=lambda raw: raw.decode("utf-8") if raw else "",
        )
        await consumer.start()
        replayed = 0
        try:
            partitions = await consumer.getmany(timeout_ms=5_000, max_records=limit)
            for records in partitions.values():
                for record in records:
                    await self.publish(
                        topic,
                        partition_key=record.key or "",
                        payload=record.value,
                        headers={"x-replayed": "true"},
                    )
                    replayed += 1
            if replayed:
                await consumer.commit()
        finally:
            await consumer.stop()
        return replayed
