"""Operational tables: pricing, retention, payload references, messaging, exports.

Several of these tables exist to make *distributed* correctness properties hold
with only the relational database as a coordination point:

``outbox_messages``
    The transactional outbox. A request that must both change state and emit an
    event writes the event into this table **in the same transaction**. A relay
    publishes it afterwards. Without it, a crash between "commit" and "publish"
    silently drops the event, and a crash between "publish" and "commit"
    fabricates one.

``bus_messages`` / ``bus_offsets`` / ``bus_dead_letters``
    The database-backed event bus driver, which gives local development the
    same consumer-group, retry, backoff and dead-letter semantics as Kafka
    without requiring a broker.

``idempotency_records``
    Makes retried mutations safe. The response of the first attempt is stored
    and replayed, so a client that times out and retries never double-applies.

``stored_objects``
    Every byte in object storage has a row here. Retention and orphan detection
    both work from this table: object storage itself is not a queryable index.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, JSONColumn, TenantScopedMixin, TimestampMixin
from ..types import DecimalText, UtcDateTime

__all__ = [
    "BusDeadLetter",
    "BusMessage",
    "BusOffset",
    "ExportJob",
    "IdempotencyRecord",
    "IngestBatchRecord",
    "OutboxMessage",
    "PriceBook",
    "PriceEntry",
    "RetentionPolicy",
    "SavedView",
    "StoredObject",
]


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


class PriceBook(Base, TimestampMixin):
    """A versioned collection of price entries.

    Price books are versioned rather than edited because a cost figure must be
    reproducible: re-running last quarter's report has to use last quarter's
    prices, even though the provider has since changed them. Every cost record
    stores the price-book version it was computed against.

    ``organization_id`` is nullable: a NULL book is a platform-provided public
    price list, a non-NULL book is a tenant's negotiated enterprise pricing,
    and the tenant book wins when both match.
    """

    __tablename__ = "price_books"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("organizations.id", ondelete="CASCADE"), default=None, index=True
    )
    #: Human-readable version, e.g. "2026-02-public" or "acme-enterprise-v3".
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    source: Mapped[str | None] = mapped_column(String(512), default=None)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    published_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    published_by: Mapped[str | None] = mapped_column(String(40), default=None)
    #: Once entries exist and a cost has referenced this book, it is frozen.
    frozen_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    entries: Mapped[list[PriceEntry]] = relationship(
        back_populates="price_book", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "version", name="unique_price_book_version"),
        CheckConstraint("length(currency) = 3", name="currency_is_iso4217"),
    )


class PriceEntry(Base):
    """One effective-dated price for one usage category of one model.

    The composite of (provider, model, usage_category, tier, validity window)
    identifies a price. Overlapping windows for the same key are rejected at
    write time, so lookup is unambiguous: exactly zero or one entry matches a
    given timestamp and usage quantity.
    """

    __tablename__ = "price_entries"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    price_book_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("price_books.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_identifier: Mapped[str] = mapped_column(String(256), nullable=False)
    #: 'input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_write_tokens',
    #: 'reasoning_tokens', 'audio_input_seconds', 'image_input_count', 'request'.
    usage_category: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Quantity the price applies to, e.g. 1_000_000 for "per million tokens".
    unit_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1_000_000)
    unit_price: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    effective_from: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    #: NULL means "still in effect". Half-open interval [from, to).
    effective_to: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    #: Volume tiers. A usage quantity matches when tier_min <= q < tier_max.
    tier_min_units: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tier_max_units: Mapped[int | None] = mapped_column(BigInteger, default=None)
    #: Applied multiplicatively after the tier price, e.g. 0.10 for a 10% discount.
    discount_rate: Mapped[Decimal | None] = mapped_column(DecimalText, default=None)
    source_url: Mapped[str | None] = mapped_column(String(1024), default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    price_book: Mapped[PriceBook] = relationship(back_populates="entries", lazy="raise")

    __table_args__ = (
        Index(
            "ix_price_entries_lookup",
            "provider",
            "model_identifier",
            "usage_category",
            "effective_from",
        ),
        Index("ix_price_entries_book_model", "price_book_id", "provider", "model_identifier"),
        CheckConstraint("unit_quantity > 0", name="positive_unit_quantity"),
        CheckConstraint("tier_min_units >= 0", name="non_negative_tier_min"),
        CheckConstraint(
            "tier_max_units IS NULL OR tier_max_units > tier_min_units", name="tier_ordering"
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from", name="validity_window_ordering"
        ),
        CheckConstraint("length(currency) = 3", name="entry_currency_is_iso4217"),
    )


# ---------------------------------------------------------------------------
# Retention and payloads
# ---------------------------------------------------------------------------


class RetentionPolicy(Base, TenantScopedMixin, TimestampMixin):
    """Per-project override of the platform's default retention horizons."""

    __tablename__ = "retention_policies"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    environment_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    raw_span_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    aggregate_days: Mapped[int] = mapped_column(Integer, nullable=False, default=395)
    payload_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    #: Applied by the retention sweep as a hard delete rather than a tombstone.
    purge_on_expiry: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[str | None] = mapped_column(String(40), default=None)

    __table_args__ = (
        UniqueConstraint("project_id", "environment_id", name="one_policy_per_scope"),
        CheckConstraint("payload_days <= raw_span_days", name="payload_within_span_retention"),
        CheckConstraint("raw_span_days <= aggregate_days", name="spans_within_aggregate_retention"),
        CheckConstraint("payload_days >= 1", name="positive_payload_days"),
    )


class StoredObject(Base, TenantScopedMixin):
    """Index of every object the platform has written to object storage.

    Object storage has no usable secondary index and listing a prefix over
    millions of keys is prohibitive, so retention, orphan detection and access
    control all work from this table. The invariant the reconciliation job
    enforces is bidirectional: no row without an object, no object without a row.
    """

    __tablename__ = "stored_objects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    #: 'span_input', 'span_output', 'tool_result', 'dataset_file', 'export', 'attachment'.
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum: Mapped[str] = mapped_column(String(80), nullable=False)
    #: Backlink for lineage: which span or dataset version owns this payload.
    owner_type: Mapped[str | None] = mapped_column(String(32), default=None)
    owner_id: Mapped[str | None] = mapped_column(String(128), default=None, index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    #: When the retention sweep may delete it. NULL means "keep indefinitely".
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None, index=True)
    #: Set the moment the object is removed from storage. A reference whose
    #: target is deleted must return 410 Gone, never a signed URL to nothing.
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    __table_args__ = (
        Index("ix_stored_objects_expiry_sweep", "expires_at", "deleted_at"),
        CheckConstraint("size_bytes >= 0", name="non_negative_size"),
    )


# ---------------------------------------------------------------------------
# Messaging primitives
# ---------------------------------------------------------------------------


class OutboxMessage(Base):
    """Transactional outbox row.

    Written in the same transaction as the state change it describes; relayed
    to the event bus afterwards by a poller. ``published_at`` is set once the
    broker has acknowledged the message, making the relay itself idempotent
    across restarts.
    """

    __tablename__ = "outbox_messages"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    topic: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Partition key. Spans of one trace share a key so they land on one
    #: partition and are therefore processed in order by a single consumer.
    partition_key: Mapped[str] = mapped_column(String(256), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    #: Message schema version, so a consumer can reject what it cannot parse.
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    __table_args__ = (Index("ix_outbox_unpublished", "published_at", "created_at"),)


class BusMessage(Base):
    """A message in the database-backed event bus.

    Append-only. Consumers track progress in :class:`BusOffset` rather than
    mutating messages, which is what makes replay possible: resetting an offset
    re-delivers history exactly as Kafka would.
    """

    __tablename__ = "bus_messages"

    #: Monotonic offset within a topic partition.
    #:
    #: The variant is load-bearing: SQLite only auto-assigns a value to an
    #: `INTEGER PRIMARY KEY` (which aliases the rowid). A `BIGINT PRIMARY KEY`
    #: is an ordinary column there, so every insert fails a NOT NULL check.
    #: PostgreSQL still gets a 64-bit identity column.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    topic: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    partition: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    partition_key: Mapped[str] = mapped_column(String(256), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    #: Set by the retention sweep; consumed messages are trimmed, not deleted
    #: immediately, so a lagging consumer group can still catch up.
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    __table_args__ = (Index("ix_bus_messages_topic_partition_id", "topic", "partition", "id"),)


class BusOffset(Base):
    """Per-consumer-group progress marker, with in-flight leasing.

    ``lease_expires_at`` implements at-least-once delivery: a consumer claims a
    range, and if it dies before committing, the lease lapses and another
    consumer re-reads the same range. Handlers must therefore be idempotent --
    which they are, by construction, on the ingestion path.
    """

    __tablename__ = "bus_offsets"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    consumer_group: Mapped[str] = mapped_column(String(128), nullable=False)
    topic: Mapped[str] = mapped_column(String(128), nullable=False)
    partition: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Highest offset durably processed. Delivery resumes at committed + 1.
    committed_offset: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128), default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "consumer_group", "topic", "partition", name="one_offset_per_group_partition"
        ),
    )


class BusDeadLetter(Base):
    """A message that exhausted its delivery attempts.

    Parked rather than dropped, with the full original payload and the last
    error, so an operator can fix the handler and replay. A poison message must
    never be able to stall a partition indefinitely.
    """

    __tablename__ = "bus_dead_letters"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    topic: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    consumer_group: Mapped[str] = mapped_column(String(128), nullable=False)
    original_offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    partition_key: Mapped[str] = mapped_column(String(256), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    error_type: Mapped[str] = mapped_column(String(128), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    first_failed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    dead_lettered_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    replayed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    __table_args__ = (Index("ix_bus_dead_letters_pending", "topic", "replayed_at"),)


class IdempotencyRecord(Base, TenantScopedMixin):
    """Stored outcome of a mutating request, keyed by client idempotency key.

    ``request_fingerprint`` is a hash of the request body. Replaying the same
    key with a *different* body is a client bug and returns 409 rather than the
    stale response -- silently returning the first result would be worse than
    failing, because the caller would believe its second, different request had
    been applied.
    """

    __tablename__ = "idempotency_records"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(256), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(80), nullable=False)
    #: 'in_progress' | 'completed' | 'failed'
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="in_progress")
    response_status: Mapped[int | None] = mapped_column(Integer, default=None)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "endpoint", "idempotency_key", name="unique_idempotency_key"
        ),
        CheckConstraint("state IN ('in_progress','completed','failed')", name="known_idem_state"),
    )


class IngestBatchRecord(Base, TenantScopedMixin):
    """Accounting row for one accepted ingest request.

    Gives operators an answer to "did the SDK's batch actually arrive, and what
    happened to it" without trawling logs, and gives quota enforcement a cheap
    per-tenant volume series.
    """

    __tablename__ = "ingest_batches"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    environment_id: Mapped[str] = mapped_column(String(40), nullable=False)
    api_key_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    #: 'otlp_http_proto', 'otlp_http_json', 'native_json'.
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    span_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), default=None)
    sdk_name: Mapped[str | None] = mapped_column(String(128), default=None)
    sdk_version: Mapped[str | None] = mapped_column(String(64), default=None)

    __table_args__ = (Index("ix_ingest_batches_org_received", "organization_id", "received_at"),)


# ---------------------------------------------------------------------------
# User-facing conveniences
# ---------------------------------------------------------------------------


class SavedView(Base, TenantScopedMixin, TimestampMixin):
    """A named, shareable set of trace-explorer filters."""

    __tablename__ = "saved_views"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    #: The exact query string the UI reconstructs, so a view and a shared URL
    #: are the same thing.
    query: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    is_shared: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str] = mapped_column(String(40), nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "created_by", "name", name="unique_view_name_per_owner"),
    )


class ExportJob(Base, TenantScopedMixin):
    """An asynchronous data export.

    Exports are jobs rather than streaming responses because they can span
    millions of rows: a synchronous request would time out, and a retry would
    restart the whole scan. The result lands in object storage and is fetched
    through a short-lived signed URL.
    """

    __tablename__ = "export_jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    requested_by: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    #: 'traces' | 'spans' | 'costs' | 'audit_events' | 'dataset'
    resource: Mapped[str] = mapped_column(String(32), nullable=False)
    format: Mapped[str] = mapped_column(String(16), nullable=False, default="jsonl")
    query: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    #: True when the export was produced with redaction applied. Surfaced in
    #: the UI and embedded in the archive manifest so a downstream consumer
    #: knows the file is not the full fidelity data.
    redacted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    object_key: Mapped[str | None] = mapped_column(String(1024), default=None)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','completed','failed','expired')",
            name="known_export_status",
        ),
        CheckConstraint("format IN ('jsonl','csv','json')", name="known_export_format"),
        Index("ix_export_jobs_org_status", "organization_id", "status"),
    )
