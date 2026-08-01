"""Tenancy, identity and access-control tables.

The shape of this schema encodes the platform's central security invariant:

    Every row of trace, prompt, model, dataset or cost data is reachable only
    through an ``organization_id`` that the caller has been granted a role on.

``Organization`` is the tenant boundary. ``Project`` partitions a tenant's work;
``Environment`` partitions a project by deployment stage. Credentials
(``ApiKey``, ``ServiceAccount``) are always issued *into* a specific
project/environment, never at the tenant root, so a leaked ingest key cannot be
replayed against production from a staging service.
"""

from __future__ import annotations

from datetime import datetime
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
from ..types import UtcDateTime

__all__ = [
    "ApiKey",
    "AuditEvent",
    "Environment",
    "Membership",
    "Organization",
    "Project",
    "ServiceAccount",
    "User",
]


class Organization(Base, TimestampMixin):
    """A tenant. The root of every authorisation decision."""

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    #: URL-safe unique handle, used in dashboard paths.
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    #: Soft-delete marker. Hard deletion runs asynchronously so that object
    #: storage and the analytics store can be purged transactionally-ish.
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    # --- quotas: the primary defence against a noisy neighbour -------------
    max_spans_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=50_000_000)
    max_projects: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    max_storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)

    projects: Mapped[list[Project]] = relationship(
        back_populates="organization", cascade="all, delete-orphan", lazy="raise"
    )
    memberships: Mapped[list[Membership]] = relationship(
        back_populates="organization", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        CheckConstraint("length(slug) >= 2", name="slug_min_length"),
        CheckConstraint("max_spans_per_day > 0", name="positive_span_quota"),
    )


class User(Base, TimestampMixin):
    """A human principal.

    Users are global rather than tenant-scoped: one person may belong to
    several organisations, and duplicating them per tenant would fork their
    credentials. Authorisation always happens through :class:`Membership`.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    #: Argon2id hash. NULL for users that authenticate only through OIDC.
    password_hash: Mapped[str | None] = mapped_column(Text, default=None)
    #: Stable subject claim from the OIDC provider, if federated.
    oidc_subject: Mapped[str | None] = mapped_column(String(256), default=None, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Brute-force protection state.
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    #: Invalidates every token issued before this instant. Bumped on password
    #: change, role change and explicit "sign out everywhere".
    token_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )

    # Email shape is validated by Pydantic's EmailStr at the API boundary. A
    # database CHECK for it would have to be written in dialect-specific SQL
    # (PostgreSQL's position(... in ...) is a syntax error to SQLite) for no
    # additional safety.
    __table_args__ = (Index("ix_users_oidc_subject_unique", "oidc_subject", unique=True),)


class Membership(Base, TimestampMixin):
    """Grants a user a role within an organisation.

    A user with no membership row for an organisation cannot see that it
    exists: listing endpoints join through this table rather than filtering
    after the fact.
    """

    __tablename__ = "memberships"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: One of aiobs_api.domain.rbac.Role.
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Optional narrowing: when non-empty the membership only applies to these
    #: project ids. Used for contractor-style access.
    project_scope: Mapped[list[str]] = mapped_column(JSONColumn, nullable=False, default=list)
    invited_by: Mapped[str | None] = mapped_column(String(40), default=None)

    organization: Mapped[Organization] = relationship(back_populates="memberships", lazy="raise")
    user: Mapped[User] = relationship(back_populates="memberships", lazy="raise")

    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="one_membership_per_user_per_org"),
        CheckConstraint(
            "role IN ('owner','administrator','developer','analyst','viewer')",
            name="known_role",
        ),
    )


class Project(Base, TenantScopedMixin, TimestampMixin):
    """A unit of work inside a tenant: one application, one service, one team."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    #: Head sampling rate suggested to SDKs for this project.
    default_sampling_rate: Mapped[float] = mapped_column(nullable=False, default=1.0)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)

    organization: Mapped[Organization] = relationship(back_populates="projects", lazy="raise")
    environments: Mapped[list[Environment]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "slug", name="unique_project_slug_per_org"),
        CheckConstraint(
            "default_sampling_rate >= 0 AND default_sampling_rate <= 1",
            name="sampling_rate_range",
        ),
    )


class Environment(Base, TenantScopedMixin, TimestampMixin):
    """A deployment stage of a project: development, staging, production.

    Environments are first-class rather than a free-text tag because they are
    an *authorisation* boundary: an API key is bound to one, and a developer
    role may be allowed to read production traces but not to write to them.
    """

    __tablename__ = "environments"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Production environments get stricter defaults: payload storage off,
    #: redaction on, longer retention.
    is_production: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)

    project: Mapped[Project] = relationship(back_populates="environments", lazy="raise")

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="unique_environment_name_per_project"),
    )


class ApiKey(Base, TenantScopedMixin, TimestampMixin):
    """A credential used by an SDK to send telemetry or read data.

    Only the hash is stored. The plaintext secret is returned exactly once, at
    creation, and is unrecoverable afterwards -- a database dump therefore
    contains no usable credentials.

    ``prefix`` is the first few characters of the plaintext, stored in the
    clear. It is what makes a key identifiable in the UI ("aiobs_live_7f3a...")
    and, crucially, what lets the auth path find the candidate row with an
    indexed lookup instead of hashing against every key in the table.
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    environment_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("environments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    #: Public, indexed identifier fragment: ``aiobs_<live|test>_<8 chars>``.
    prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    #: Argon2id hash of the full secret.
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    #: Coarse capability set: 'ingest', 'read', or both.
    scopes: Mapped[list[str]] = mapped_column(JSONColumn, nullable=False, default=list)
    created_by: Mapped[str | None] = mapped_column(String(40), default=None)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    revoked_by: Mapped[str | None] = mapped_column(String(40), default=None)
    #: Updated at most once a minute to avoid a write on every ingest request.
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    #: Per-key override of the tenant ingest budget.
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer, default=None)

    __table_args__ = (
        Index("ix_api_keys_project_active", "project_id", "revoked_at"),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at", name="revoked_after_created"
        ),
    )

    def is_usable(self, now: datetime) -> bool:
        """Whether the key may authenticate a request at ``now``."""
        if self.revoked_at is not None:
            return False
        return not (self.expires_at is not None and self.expires_at <= now)


class ServiceAccount(Base, TenantScopedMixin, TimestampMixin):
    """A non-human principal for CI systems and internal tooling.

    Separate from :class:`ApiKey` because a service account has a *role* (and
    therefore the full RBAC surface) rather than the two coarse ingest/read
    scopes an SDK key carries.
    """

    __tablename__ = "service_accounts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="viewer")
    prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    project_scope: Mapped[list[str]] = mapped_column(JSONColumn, nullable=False, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="unique_service_account_name_per_org"),
        CheckConstraint(
            "role IN ('owner','administrator','developer','analyst','viewer')",
            name="known_service_account_role",
        ),
    )


class AuditEvent(Base, TenantScopedMixin):
    """An append-only record of a security-relevant action.

    Deliberately *not* a general application log. Audit events answer "who
    changed what, when, and from where" for a compliance reviewer, so they are
    written in the same transaction as the change they describe, never updated,
    and retained on their own (longer) schedule.
    """

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    #: Dotted action name, e.g. ``api_key.revoked``, ``prompt_version.published``.
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    actor_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    actor_label: Mapped[str | None] = mapped_column(String(320), default=None)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    project_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    #: 'success' or 'denied'. Denied attempts are the interesting ones.
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="success")
    request_id: Mapped[str | None] = mapped_column(String(64), default=None)
    client_ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(512), default=None)
    #: Redacted before write; never contains secrets or prompt content.
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONColumn, nullable=False, default=dict
    )

    __table_args__ = (
        Index("ix_audit_events_org_occurred", "organization_id", "occurred_at"),
        Index("ix_audit_events_org_action_occurred", "organization_id", "action", "occurred_at"),
        CheckConstraint("outcome IN ('success','denied','error')", name="known_outcome"),
    )
