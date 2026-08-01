"""Application configuration.

Every knob the platform has is declared here, read from the environment with an
``AIOBS_`` prefix, and validated *once at startup*. A process that boots has, by
construction, a coherent configuration: there is no lazy `os.environ["..."]`
lookup buried in a request handler waiting to fail at 3am.

Three rules shape this module:

1. **Fail fast and loudly.** :meth:`Settings.validate_for_runtime` refuses to
   start a production process with development defaults (a known JWT secret,
   permissive CORS, an in-memory event bus). Misconfiguration in production is a
   security incident, not a warning.
2. **Secrets never render.** Anything sensitive is a
   :class:`~pydantic.SecretStr`, so an accidental ``repr(settings)`` in a log
   line or an exception traceback prints ``**********``.
3. **Drivers are swappable.** Storage back-ends are selected by enum, not by
   URL sniffing, so ``AIOBS_ANALYTICS__DRIVER=sqlite`` is an explicit,
   reviewable decision rather than an accident of a malformed DSN.

See ``docs/operations/configuration.md`` for the full reference table.
"""

from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AnyHttpUrl,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "AnalyticsDriver",
    "AnalyticsSettings",
    "AuthSettings",
    "BusDriver",
    "BusSettings",
    "DatabaseSettings",
    "Environment",
    "IngestSettings",
    "KeyValueDriver",
    "KeyValueSettings",
    "ObjectStoreDriver",
    "ObjectStoreSettings",
    "RetentionSettings",
    "SecuritySettings",
    "Settings",
    "TelemetrySettings",
    "get_settings",
    "reset_settings_cache",
]


class Environment(str, Enum):
    """Deployment environment of the *platform itself* (not of traced apps)."""

    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_production_like(self) -> bool:
        return self in {Environment.STAGING, Environment.PRODUCTION}


class AnalyticsDriver(str, Enum):
    """Back-end serving the high-volume span/trace analytics workload."""

    CLICKHOUSE = "clickhouse"
    #: Single-file store used for local development, CI and unit tests. It
    #: implements the identical AnalyticsStore protocol and is held to the same
    #: behaviour by the shared conformance suite -- see ADR-0013.
    SQLITE = "sqlite"


class ObjectStoreDriver(str, Enum):
    S3 = "s3"
    FILESYSTEM = "filesystem"


class KeyValueDriver(str, Enum):
    REDIS = "redis"
    MEMORY = "memory"


class BusDriver(str, Enum):
    KAFKA = "kafka"
    #: Durable, single-node queue backed by the relational database. Provides
    #: the same consumer-group, retry and dead-letter semantics as Kafka.
    DATABASE = "database"


class LogFormat(str, Enum):
    JSON = "json"
    CONSOLE = "console"


class _Section(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True)


class DatabaseSettings(_Section):
    """PostgreSQL (production) or SQLite (development/test) metadata store."""

    url: str = Field(
        default="sqlite+aiosqlite:///./.aiobs/metadata.db",
        description="SQLAlchemy async DSN. Use postgresql+asyncpg://... in production.",
    )
    pool_size: int = Field(default=10, ge=1, le=200)
    max_overflow: int = Field(default=20, ge=0, le=200)
    pool_timeout_seconds: float = Field(default=10.0, gt=0)
    pool_recycle_seconds: int = Field(default=1800, gt=0)
    statement_timeout_ms: int = Field(default=15_000, gt=0)
    echo: bool = False

    @field_validator("url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        if not value.startswith(("postgresql+asyncpg://", "sqlite+aiosqlite://")):
            raise ValueError(
                "database url must use an async driver: 'postgresql+asyncpg://' "
                f"or 'sqlite+aiosqlite://', got {value.split('://', 1)[0]!r}"
            )
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.url.startswith("sqlite")

    @property
    def sync_url(self) -> str:
        """Synchronous DSN, for tooling that cannot drive an async engine."""
        return self.url.replace("postgresql+asyncpg://", "postgresql://").replace(
            "sqlite+aiosqlite://", "sqlite://"
        )


class AnalyticsSettings(_Section):
    """Trace/span analytics store."""

    driver: AnalyticsDriver = AnalyticsDriver.SQLITE
    #: ClickHouse HTTP interface, e.g. http://clickhouse:8123
    url: str = "http://localhost:58123"
    database: str = "aiobs"
    username: str = "aiobs"
    password: SecretStr = SecretStr("")
    #: Path used when driver == sqlite.
    sqlite_path: Path = Path("./.aiobs/analytics.db")
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    query_timeout_seconds: float = Field(default=30.0, gt=0)
    #: Spans are flushed to the analytics store in batches. Larger batches mean
    #: fewer, larger ClickHouse parts (which merge better) at the cost of
    #: ingest-to-query latency.
    insert_batch_size: int = Field(default=5_000, ge=1, le=200_000)
    insert_flush_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    #: Guard against a single query scanning the whole cluster.
    max_result_rows: int = Field(default=50_000, ge=1)


class ObjectStoreSettings(_Section):
    """Storage for payloads too large to keep in a span attribute."""

    driver: ObjectStoreDriver = ObjectStoreDriver.FILESYSTEM
    bucket: str = "aiobs-payloads"
    #: S3-compatible endpoint. Leave unset for real AWS S3.
    endpoint_url: str | None = None
    region: str = "us-east-1"
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None
    #: Root directory used when driver == filesystem.
    root_path: Path = Path("./.aiobs/objects")
    #: Anything larger than this is offloaded instead of being inlined.
    inline_threshold_bytes: int = Field(default=8_192, ge=0, le=1_048_576)
    #: Hard ceiling on a single stored object.
    max_object_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)
    #: Presigned download URLs expire after this long.
    signed_url_ttl_seconds: int = Field(default=300, ge=30, le=86_400)


class KeyValueSettings(_Section):
    """Redis (or an in-process equivalent) for rate limits, locks and dedup."""

    driver: KeyValueDriver = KeyValueDriver.MEMORY
    url: str = "redis://localhost:56379/0"
    #: How long an ingested span id is remembered for duplicate suppression.
    dedup_ttl_seconds: int = Field(default=3_600, ge=60, le=86_400 * 7)
    connect_timeout_seconds: float = Field(default=2.0, gt=0)
    #: Redis is a cache, never the source of truth: if it is down, ingestion
    #: continues and relies on the analytics store's own de-duplication.
    fail_open: bool = True


class BusSettings(_Section):
    """Durable event bus between the API and the worker."""

    driver: BusDriver = BusDriver.DATABASE
    brokers: str = "localhost:59092"
    topic_prefix: str = "aiobs"
    consumer_group: str = "aiobs-worker"
    #: Attempts before a message is parked in the dead-letter topic.
    max_delivery_attempts: int = Field(default=5, ge=1, le=100)
    retry_base_delay_seconds: float = Field(default=0.5, gt=0)
    retry_max_delay_seconds: float = Field(default=60.0, gt=0)
    #: Full jitter is applied to every backoff so that a fleet of consumers
    #: recovering from an outage does not stampede the downstream store.
    retry_jitter: bool = True
    poll_interval_seconds: float = Field(default=0.25, gt=0, le=10)
    max_poll_records: int = Field(default=500, ge=1, le=10_000)
    #: Compression for Kafka producer batches.
    compression: Literal["none", "gzip", "snappy", "lz4", "zstd"] = "lz4"

    @model_validator(mode="after")
    def _check_backoff(self) -> BusSettings:
        if self.retry_max_delay_seconds < self.retry_base_delay_seconds:
            raise ValueError("retry_max_delay_seconds must be >= retry_base_delay_seconds")
        return self


class AuthSettings(_Section):
    """Authentication and session policy."""

    #: HMAC key for locally-issued access tokens. MUST be overridden outside
    #: development; validate_for_runtime() enforces that.
    jwt_secret: SecretStr = SecretStr("dev-only-insecure-secret-change-me")
    jwt_algorithm: Literal["HS256", "HS512", "RS256"] = "HS256"
    jwt_issuer: str = "aiobs"
    jwt_audience: str = "aiobs-api"
    access_token_ttl_seconds: int = Field(default=3_600, ge=60, le=86_400)
    refresh_token_ttl_seconds: int = Field(default=30 * 86_400, ge=3_600)
    #: Password hashing cost. Argon2id defaults tuned for ~100ms on a modern core.
    argon2_time_cost: int = Field(default=3, ge=1, le=10)
    argon2_memory_cost_kib: int = Field(default=65_536, ge=8_192)
    argon2_parallelism: int = Field(default=2, ge=1, le=16)
    #: Local username/password auth. Disable it once OIDC is wired up.
    enable_local_auth: bool = True
    #: OIDC discovery document; when set, bearer tokens are validated against
    #: the provider's JWKS in addition to locally-issued tokens.
    oidc_issuer: AnyHttpUrl | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_jwks_cache_seconds: int = Field(default=3_600, ge=60)
    #: Failed logins per account before temporary lockout.
    max_failed_logins: int = Field(default=10, ge=3, le=100)
    lockout_seconds: int = Field(default=900, ge=60)
    #: API keys older than this are refused even if not explicitly expired.
    api_key_max_age_days: int | None = Field(default=365, ge=1)

    @model_validator(mode="after")
    def _check_oidc(self) -> AuthSettings:
        if self.oidc_issuer is not None and not self.oidc_client_id:
            raise ValueError("oidc_client_id is required when oidc_issuer is set")
        if not self.enable_local_auth and self.oidc_issuer is None:
            raise ValueError(
                "no authentication method is enabled: set enable_local_auth=true "
                "or configure oidc_issuer"
            )
        return self


class SecuritySettings(_Section):
    """Transport and application security policy."""

    #: Development defaults only. ``localhost`` and ``127.0.0.1`` are distinct
    #: origins to a browser even though they are the same host, and a developer
    #: who types the wrong one gets a CORS failure that looks like a broken
    #: login. Both are listed so that confusion never happens locally; a
    #: deployment must set this explicitly and startup validation rejects a
    #: production configuration that still allows a localhost origin.
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:53000", "http://127.0.0.1:53000"]
    )
    cors_allow_credentials: bool = True
    #: Emitted on HTML responses served by the API (the docs UI).
    content_security_policy: str = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    hsts_max_age_seconds: int = Field(default=31_536_000, ge=0)
    #: Cookies are Secure outside development; SameSite=Lax keeps the login
    #: redirect flow working while blocking cross-site POSTs.
    cookie_secure: bool = True
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cookie_domain: str | None = None
    #: Requests larger than this are rejected before the body is read.
    max_request_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    #: Per-API-key ingest budget.
    ingest_rate_limit_per_minute: int = Field(default=6_000, ge=1)
    ingest_burst: int = Field(default=1_000, ge=1)
    #: Per-principal read API budget.
    api_rate_limit_per_minute: int = Field(default=600, ge=1)
    api_burst: int = Field(default=120, ge=1)
    #: Hosts the export/webhook fetcher may contact. Empty means "deny all
    #: outbound fetches", which is the safe default against SSRF.
    outbound_allowed_hosts: list[str] = Field(default_factory=list)
    #: Trusted proxy hops for X-Forwarded-For parsing. 0 means do not trust it.
    trusted_proxy_hops: int = Field(default=0, ge=0, le=10)


class IngestSettings(_Section):
    """Ingestion pipeline behaviour."""

    #: Reject spans whose start time is further in the future than this.
    max_clock_skew_future_seconds: int = Field(default=300, ge=0, le=86_400)
    #: Accept but flag spans older than this.
    max_backfill_age_seconds: int = Field(default=7 * 86_400, ge=3_600)
    #: A trace with no root span after this long is finalised as 'incomplete'.
    trace_completion_grace_seconds: int = Field(default=300, ge=10, le=86_400)
    #: Store inline span payloads at all. Turning this off keeps only references.
    store_payloads: bool = True
    #: Default redaction mode applied server-side, on top of SDK redaction.
    redact_by_default: bool = True
    #: Attribute keys always stripped, regardless of tenant configuration.
    always_redact_keys: list[str] = Field(
        default_factory=lambda: [
            "authorization",
            "proxy-authorization",
            "cookie",
            "set-cookie",
            "x-api-key",
            "api_key",
            "apikey",
            "password",
            "secret",
            "token",
            "access_token",
            "refresh_token",
            "private_key",
            "client_secret",
        ]
    )
    #: Cap on distinct tag values remembered per project, to bound cardinality.
    max_distinct_tag_values: int = Field(default=10_000, ge=100)
    #: OTLP endpoints enabled on the API process.
    enable_otlp_http: bool = True
    #: Accept spans without authentication. Only ever true in local demos.
    allow_anonymous_ingest: bool = False


class RetentionSettings(_Section):
    """Default data-retention policy; per-project policies override these."""

    raw_span_days: int = Field(default=30, ge=1, le=3_650)
    aggregate_days: int = Field(default=395, ge=1, le=3_650)
    payload_days: int = Field(default=14, ge=1, le=3_650)
    audit_days: int = Field(default=395, ge=30, le=3_650)
    #: How often the retention worker runs.
    sweep_interval_seconds: int = Field(default=3_600, ge=60)
    #: Rows deleted per sweep iteration, to keep locks short.
    sweep_batch_size: int = Field(default=10_000, ge=100)

    @model_validator(mode="after")
    def _check_ordering(self) -> RetentionSettings:
        if self.payload_days > self.raw_span_days:
            raise ValueError(
                "payload_days must not exceed raw_span_days: payloads would be "
                "referenced by spans that no longer exist"
            )
        if self.raw_span_days > self.aggregate_days:
            raise ValueError("aggregate_days must be >= raw_span_days")
        return self


class TelemetrySettings(_Section):
    """Self-observability of the platform.

    The platform traces itself with OpenTelemetry. To avoid the obvious
    infinite regress (the trace describing the ingestion of a trace is itself
    ingested, producing another trace...) self-telemetry is exported to a
    *separate* endpoint by default and internal HTTP paths are excluded.
    """

    enable_metrics: bool = True
    metrics_path: str = "/internal/metrics"
    enable_tracing: bool = False
    otlp_endpoint: str | None = None
    otlp_headers: str | None = None
    sample_ratio: float = Field(default=0.05, ge=0.0, le=1.0)
    #: URL paths never traced, preventing telemetry-about-telemetry loops.
    excluded_paths: list[str] = Field(
        default_factory=lambda: ["/internal/metrics", "/live", "/ready", "/health"]
    )
    #: Refuse to export self-telemetry to our own ingest endpoint.
    forbid_self_export: bool = True


class Settings(BaseSettings):
    """Root configuration object.

    Nested sections are populated from double-underscore delimited variables,
    e.g. ``AIOBS_DATABASE__URL`` maps to ``settings.database.url``.
    """

    model_config = SettingsConfigDict(
        env_prefix="AIOBS_",
        env_nested_delimiter="__",
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = Environment.DEVELOPMENT
    service_name: str = "aiobs-api"
    version: str = "0.1.0"
    #: Populated by the container build; surfaces in /health and every log line.
    git_commit: str = "unknown"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: LogFormat = LogFormat.JSON
    #: Base URL the API is reachable at; used to build absolute links.
    public_url: str = "http://localhost:58000"
    #: Base URL of the web UI; used for OIDC redirects and share links.
    web_url: str = "http://localhost:53000"
    #: Seconds allowed for in-flight requests to finish during shutdown.
    shutdown_grace_seconds: float = Field(default=20.0, ge=0, le=300)

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    analytics: AnalyticsSettings = Field(default_factory=AnalyticsSettings)
    objects: ObjectStoreSettings = Field(default_factory=ObjectStoreSettings)
    kv: KeyValueSettings = Field(default_factory=KeyValueSettings)
    bus: BusSettings = Field(default_factory=BusSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    ingest: IngestSettings = Field(default_factory=IngestSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)

    @field_validator("public_url", "web_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    def validate_for_runtime(self) -> list[str]:
        """Return a list of fatal misconfigurations for this environment.

        Called from the application lifespan. An empty list means the process
        may serve traffic; anything else aborts startup with the list rendered
        into the error message. Keeping this separate from field validation
        means tests can construct a deliberately-insecure ``Settings`` object
        without tripping the guard.
        """
        problems: list[str] = []
        if not self.environment.is_production_like:
            return problems

        if self.auth.jwt_secret.get_secret_value() == "dev-only-insecure-secret-change-me":
            problems.append(
                "AIOBS_AUTH__JWT_SECRET is still the development default; "
                "set a unique high-entropy value"
            )
        if self.auth.jwt_algorithm.startswith("HS") and (
            len(self.auth.jwt_secret.get_secret_value()) < 32
        ):
            problems.append(
                "AIOBS_AUTH__JWT_SECRET must be at least 32 characters for HMAC signing"
            )
        if self.database.is_sqlite:
            problems.append(
                "SQLite is not supported for the metadata store outside development; "
                "set AIOBS_DATABASE__URL to a postgresql+asyncpg DSN"
            )
        if self.analytics.driver is AnalyticsDriver.SQLITE:
            problems.append(
                "the SQLite analytics driver is a development/CI facility; "
                "set AIOBS_ANALYTICS__DRIVER=clickhouse"
            )
        if self.kv.driver is KeyValueDriver.MEMORY:
            problems.append(
                "the in-memory key-value driver cannot enforce rate limits across "
                "replicas; set AIOBS_KV__DRIVER=redis"
            )
        if self.objects.driver is ObjectStoreDriver.FILESYSTEM:
            problems.append(
                "the filesystem object store is node-local and will lose payloads on "
                "rescheduling; set AIOBS_OBJECTS__DRIVER=s3"
            )
        if "*" in self.security.cors_allow_origins:
            problems.append("wildcard CORS origin is not permitted outside development")
        # No exemption for loopback: a production deployment that still trusts
        # http://localhost is either misconfigured or someone left a development
        # default in place, and both are worth failing on. Credentialed CORS
        # over plaintext hands the session token to anyone on the path.
        insecure = [
            origin for origin in self.security.cors_allow_origins if origin.startswith("http://")
        ]
        if self.security.cors_allow_credentials and insecure:
            problems.append(
                "credentialed CORS origins must use https; found " + ", ".join(sorted(insecure))
            )
        if not self.security.cookie_secure:
            problems.append("AIOBS_SECURITY__COOKIE_SECURE must be true outside development")
        if self.ingest.allow_anonymous_ingest:
            problems.append("anonymous ingest must be disabled outside development")
        if self.telemetry.enable_tracing and self.telemetry.forbid_self_export:
            endpoint = (self.telemetry.otlp_endpoint or "").rstrip("/")
            if endpoint and endpoint.startswith(self.public_url):
                problems.append(
                    "self-telemetry OTLP endpoint points at this API's own ingest "
                    "endpoint, which would create a telemetry feedback loop"
                )
        return problems

    def describe(self) -> dict[str, Any]:
        """Redacted configuration summary, safe to log and to expose on /health."""
        return {
            "environment": self.environment.value,
            "service_name": self.service_name,
            "version": self.version,
            "git_commit": self.git_commit,
            "database": "sqlite" if self.database.is_sqlite else "postgresql",
            "analytics_driver": self.analytics.driver.value,
            "object_store_driver": self.objects.driver.value,
            "kv_driver": self.kv.driver.value,
            "bus_driver": self.bus.driver.value,
            "local_auth": self.auth.enable_local_auth,
            "oidc": self.auth.oidc_issuer is not None,
            "otlp_http_ingest": self.ingest.enable_otlp_http,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached because constructing ``Settings`` reads files and the environment;
    doing that per request would be both slow and a source of inconsistency if
    the environment mutated mid-process.
    """
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache. Test-only: production configuration is immutable."""
    get_settings.cache_clear()


def load_settings_from(overrides: dict[str, str]) -> Settings:
    """Build a ``Settings`` object from an explicit environment mapping.

    Used by tests and by the admin CLI to evaluate a candidate configuration
    without mutating the current process environment permanently.
    """
    previous = dict(os.environ)
    try:
        os.environ.update(overrides)
        return Settings()
    finally:
        os.environ.clear()
        os.environ.update(previous)


#: Type alias used in FastAPI dependency signatures.
SettingsDep = Annotated[Settings, "injected settings"]
