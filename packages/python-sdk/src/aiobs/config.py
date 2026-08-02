"""SDK configuration.

Reads from the environment by default so that instrumenting an application is a
deploy-time concern rather than a code change, and so the same binary can point
at a local platform in development and a production one in production without
recompilation.

The design rule throughout: **an SDK must never break the application it
instruments.** Every failure mode here degrades to "telemetry is lost" rather
than "the request fails". A misconfigured endpoint, an unreachable collector, a
full buffer -- all of them drop spans and log a warning.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

__all__ = ["Config", "from_env"]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        return default


def _env_list(name: str, default: Sequence[str]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return tuple(default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(slots=True)
class Config:
    """Everything the SDK needs to send telemetry."""

    #: Base URL of the platform, e.g. ``https://aiobs.example.com``.
    endpoint: str = "http://localhost:58000"
    #: API key. Without one the SDK runs in local mode: spans are built and can
    #: be inspected by tests, but nothing is sent.
    api_key: str | None = None
    service_name: str = "unknown_service"
    service_version: str | None = None
    service_instance_id: str | None = None
    environment: str | None = None
    release: str | None = None
    git_commit: str | None = None

    # --- batching ----------------------------------------------------------
    #: Spans are buffered and flushed together. Larger batches mean fewer
    #: round trips at the cost of more memory and more loss if the process dies.
    max_batch_size: int = 200
    #: Flush at least this often even if the batch is not full, so a
    #: low-traffic service still reports promptly.
    flush_interval_seconds: float = 2.0
    #: Hard cap on buffered spans. When full, the *oldest* spans are dropped:
    #: recent telemetry is what an engineer is looking at during an incident.
    max_queue_size: int = 10_000
    #: Seconds allowed for the final flush during shutdown.
    shutdown_timeout_seconds: float = 5.0

    # --- transport ---------------------------------------------------------
    timeout_seconds: float = 10.0
    max_retries: int = 3
    retry_base_delay_seconds: float = 0.25
    retry_max_delay_seconds: float = 8.0
    #: gzip the request body. Worth it above a few kilobytes, which a batch of
    #: spans with payloads always is.
    compress: bool = True

    # --- sampling ----------------------------------------------------------
    #: Head sampling rate in [0, 1]. Applied per *trace*, never per span, so a
    #: sampled trace is complete rather than a scatter of orphaned spans.
    sample_rate: float = 1.0

    # --- privacy -----------------------------------------------------------
    #: Capture prompt and completion text at all.
    capture_payloads: bool = True
    #: Truncate captured payloads to this many characters.
    max_payload_chars: int = 8_192
    #: Additional attribute keys to redact, on top of the built-in patterns.
    redact_keys: tuple[str, ...] = ()
    #: When set, ONLY these attribute keys are sent.
    allowed_keys: tuple[str, ...] = ()

    # --- behaviour ---------------------------------------------------------
    #: Turn the SDK off entirely without removing instrumentation.
    enabled: bool = True
    #: Log the payload of every export. Development only; it is noisy.
    debug: bool = False
    #: Called with any exception the exporter raises. Defaults to a log line.
    on_error: Callable[[BaseException], None] | None = None
    #: Extra resource attributes attached to every span.
    resource_attributes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.endpoint = self.endpoint.rstrip("/")
        # Clamp rather than raise: an out-of-range sample rate in a deployment
        # variable should not crash the application at import time.
        self.sample_rate = min(max(self.sample_rate, 0.0), 1.0)
        self.max_batch_size = max(1, self.max_batch_size)
        self.max_queue_size = max(self.max_batch_size, self.max_queue_size)

    @property
    def ingest_url(self) -> str:
        return f"{self.endpoint}/v1/ingest/spans"

    @property
    def otlp_url(self) -> str:
        return f"{self.endpoint}/v1/traces"

    @property
    def can_export(self) -> bool:
        """Whether the SDK has enough configuration to send anything."""
        return bool(self.enabled and self.api_key and self.endpoint)


def from_env(**overrides: object) -> Config:
    """Build a :class:`Config` from ``AIOBS_*`` environment variables.

    Explicit keyword arguments win over the environment, which is what makes a
    library's own defaults overridable by its host application.
    """
    config = Config(
        endpoint=os.environ.get("AIOBS_ENDPOINT", "http://localhost:58000"),
        api_key=os.environ.get("AIOBS_API_KEY"),
        service_name=os.environ.get("AIOBS_SERVICE_NAME", "unknown_service"),
        service_version=os.environ.get("AIOBS_SERVICE_VERSION"),
        service_instance_id=os.environ.get("AIOBS_SERVICE_INSTANCE_ID"),
        environment=os.environ.get("AIOBS_ENVIRONMENT"),
        release=os.environ.get("AIOBS_RELEASE"),
        git_commit=os.environ.get("AIOBS_GIT_COMMIT"),
        max_batch_size=_env_int("AIOBS_MAX_BATCH_SIZE", 200),
        flush_interval_seconds=_env_float("AIOBS_FLUSH_INTERVAL_SECONDS", 2.0),
        max_queue_size=_env_int("AIOBS_MAX_QUEUE_SIZE", 10_000),
        shutdown_timeout_seconds=_env_float("AIOBS_SHUTDOWN_TIMEOUT_SECONDS", 5.0),
        timeout_seconds=_env_float("AIOBS_TIMEOUT_SECONDS", 10.0),
        max_retries=_env_int("AIOBS_MAX_RETRIES", 3),
        compress=_env_bool("AIOBS_COMPRESS", True),
        sample_rate=_env_float("AIOBS_SAMPLE_RATE", 1.0),
        capture_payloads=_env_bool("AIOBS_CAPTURE_PAYLOADS", True),
        max_payload_chars=_env_int("AIOBS_MAX_PAYLOAD_CHARS", 8_192),
        redact_keys=_env_list("AIOBS_REDACT_KEYS", ()),
        allowed_keys=_env_list("AIOBS_ALLOWED_KEYS", ()),
        enabled=_env_bool("AIOBS_ENABLED", True),
        debug=_env_bool("AIOBS_DEBUG", False),
    )
    for key, value in overrides.items():
        if value is not None and hasattr(config, key):
            setattr(config, key, value)
    config.__post_init__()
    return config
