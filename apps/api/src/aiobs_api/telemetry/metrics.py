"""Platform self-observability metrics.

The platform instruments itself with Prometheus metrics. Cardinality is the
main design constraint: a label whose value comes from user data (a trace id, a
model name a customer invented) multiplies series without bound and will take
down the metrics backend long before it takes down the API.

So: tenant-level labels are deliberately absent from high-frequency counters.
Per-tenant volume is answered from the ``ingest_batches`` table, which is built
for exactly that question and does not live in memory.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "INGEST_SPANS",
    "PROCESSING_DURATION",
    "refresh_runtime_metrics",
]

# --- ingestion -------------------------------------------------------------

INGEST_REQUESTS = Counter(
    "aiobs_ingest_requests_total",
    "Ingest requests received.",
    ["source", "outcome"],
)

INGEST_SPANS = Counter(
    "aiobs_ingest_spans_total",
    "Spans seen at the ingestion boundary.",
    # 'outcome' is accepted | rejected | duplicate -- three values, not one per
    # rejection reason, which would grow with every new validation rule.
    ["source", "outcome"],
)

INGEST_PAYLOAD_BYTES = Histogram(
    "aiobs_ingest_payload_bytes",
    "Uncompressed ingest request size.",
    buckets=(1_024, 8_192, 65_536, 262_144, 1_048_576, 4_194_304, 8_388_608),
)

INGEST_LAG_SECONDS = Histogram(
    "aiobs_ingest_lag_seconds",
    "Delay between a span's start time and its arrival at the API.",
    buckets=(0.1, 0.5, 1, 5, 15, 60, 300, 3_600, 86_400),
)

# --- processing ------------------------------------------------------------

PROCESSING_DURATION = Histogram(
    "aiobs_processing_duration_seconds",
    "Worker batch processing time.",
    ["consumer"],
    buckets=(0.005, 0.025, 0.1, 0.5, 1, 2.5, 5, 10, 30),
)

PROCESSING_BATCH_SIZE = Histogram(
    "aiobs_processing_batch_size",
    "Messages per processed batch.",
    ["consumer"],
    buckets=(1, 10, 50, 100, 250, 500, 1_000),
)

PROCESSING_FAILURES = Counter(
    "aiobs_processing_failures_total",
    "Messages that failed processing.",
    ["consumer", "kind"],
)

DEAD_LETTERED = Counter(
    "aiobs_dead_lettered_total",
    "Messages parked in the dead-letter topic.",
    ["topic"],
)

# --- storage ---------------------------------------------------------------

ANALYTICS_QUERY_DURATION = Histogram(
    "aiobs_analytics_query_duration_seconds",
    "Analytics store query latency.",
    ["operation"],
    buckets=(0.005, 0.025, 0.1, 0.5, 1, 2.5, 5, 15, 30),
)

ANALYTICS_INSERT_FAILURES = Counter(
    "aiobs_analytics_insert_failures_total",
    "Failed analytics inserts.",
    ["table"],
)

OBJECT_STORE_ERRORS = Counter(
    "aiobs_object_store_errors_total",
    "Object storage operation failures.",
    ["operation"],
)

CACHE_OPERATIONS = Counter(
    "aiobs_cache_operations_total",
    "Key-value store operations.",
    ["operation", "result"],
)

# --- auth / limits ---------------------------------------------------------

AUTH_FAILURES = Counter(
    "aiobs_auth_failures_total",
    "Authentication failures.",
    ["reason"],
)

RATE_LIMITED = Counter(
    "aiobs_rate_limited_total",
    "Requests rejected by a rate limiter.",
    ["scope"],
)

# --- gauges ----------------------------------------------------------------

QUEUE_LAG = Gauge(
    "aiobs_queue_lag_messages",
    "Messages published but not yet committed by the worker.",
    ["topic"],
)

BACKGROUND_JOB_LAST_SUCCESS = Gauge(
    "aiobs_background_job_last_success_timestamp",
    "Unix timestamp of a background job's last successful run.",
    ["job"],
)

EXPORT_FAILURES = Counter(
    "aiobs_export_failures_total",
    "Export jobs that failed.",
)


async def refresh_runtime_metrics(container: Any) -> None:
    """Populate gauges that must be sampled rather than incremented.

    Called from the metrics endpoint so the numbers are fresh at scrape time
    rather than being maintained by a background task that could itself stall.
    Failures are swallowed: a metrics endpoint must never be the reason a
    scrape, or a health check, fails.
    """
    try:
        from ..storage.bus.protocol import Topics

        group = container.settings.bus.consumer_group
        for topic in Topics.ALL:
            QUEUE_LAG.labels(topic=topic).set(await container.bus.consumer_lag(topic, group=group))
    except Exception:
        return
