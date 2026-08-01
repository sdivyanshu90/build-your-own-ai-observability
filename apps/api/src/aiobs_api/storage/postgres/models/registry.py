"""Immutable version registries for prompts, models and datasets.

Every table here follows the same three-part pattern, and the pattern is the
point:

``<Thing>``
    A mutable container: a name, a description, ownership. Renaming a prompt is
    an editorial act and must not disturb history.

``<Thing>Version``
    An **immutable, content-addressed** record. Its ``content_hash`` is the
    SHA-256 of the RFC 8785 canonical serialisation of its semantic content,
    and its ``id`` is derived from that hash. Publishing identical content twice
    therefore converges on one row instead of forking history. Once
    ``release_stage`` leaves ``draft``, a database trigger-equivalent check in
    the repository layer refuses every UPDATE.

``<Thing>Alias``
    A *mutable pointer* -- ``production``, ``staging``, ``champion`` -- into the
    immutable history. Rollback is repointing an alias, which is atomic,
    instantaneous and fully audited. Nothing is ever edited in place.

That separation is what makes a trace trustworthy months later: the trace
records the version id, so "what exactly ran" has one answer forever, even
though the alias that selected it has since moved.

See ``docs/concepts/prompt-versioning.md`` for the full rationale.
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
    "Dataset",
    "DatasetFile",
    "DatasetVersion",
    "Experiment",
    "ExperimentRun",
    "ModelDefinition",
    "ModelVersion",
    "Prompt",
    "PromptAlias",
    "PromptVersion",
]

_RELEASE_STAGES = "('draft','published','deprecated','archived')"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


class Prompt(Base, TenantScopedMixin, TimestampMixin):
    """A named prompt whose versions form an append-only history."""

    __tablename__ = "prompts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    tags: Mapped[list[str]] = mapped_column(JSONColumn, nullable=False, default=list)
    created_by: Mapped[str | None] = mapped_column(String(40), default=None)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    versions: Mapped[list[PromptVersion]] = relationship(
        back_populates="prompt", cascade="all, delete-orphan", lazy="raise"
    )
    aliases: Mapped[list[PromptAlias]] = relationship(
        back_populates="prompt", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="unique_prompt_name_per_project"),
    )


class PromptVersion(Base, TenantScopedMixin):
    """One immutable revision of a prompt template.

    ``content_hash`` covers exactly the fields that change model behaviour --
    the messages, the variable schema, the template engine -- and deliberately
    excludes editorial metadata such as ``description`` and ``author``. Two
    versions with the same hash produce the same rendered prompt; fixing a typo
    in the changelog does not manufacture a new "version" of the behaviour.
    """

    __tablename__ = "prompt_versions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    prompt_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("prompts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    #: Monotonic per-prompt counter, for human-friendly labels ("v7").
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    #: Ordered chat messages: [{"role": "system", "content": "..."}].
    messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONColumn, nullable=False)
    #: JSON Schema describing the permitted template variables.
    variable_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    default_variables: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    template_engine: Mapped[str] = mapped_column(String(32), nullable=False, default="fstring")
    release_stage: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    #: Content hash of the version this one was derived from; NULL for the root.
    parent_version_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    commit_message: Mapped[str | None] = mapped_column(Text, default=None)
    author_id: Mapped[str | None] = mapped_column(String(40), default=None)
    #: Free-form evaluation results attached after the fact. Appending here does
    #: not violate immutability because it is not part of ``content_hash``.
    evaluation: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    prompt: Mapped[Prompt] = relationship(back_populates="versions", lazy="raise")

    __table_args__ = (
        UniqueConstraint("prompt_id", "version_number", name="unique_prompt_version_number"),
        UniqueConstraint("prompt_id", "content_hash", name="unique_prompt_content_hash"),
        CheckConstraint(f"release_stage IN {_RELEASE_STAGES}", name="known_release_stage"),
        CheckConstraint("version_number > 0", name="positive_version_number"),
        Index("ix_prompt_versions_project_stage", "project_id", "release_stage"),
    )

    @property
    def is_immutable(self) -> bool:
        """Whether this version has left ``draft`` and may no longer be edited."""
        return self.release_stage != "draft"


class PromptAlias(Base, TenantScopedMixin, TimestampMixin):
    """A movable pointer from an environment-facing name to a fixed version."""

    __tablename__ = "prompt_aliases"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    prompt_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("prompts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: 'production', 'staging', 'development', or any team-chosen label.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("prompt_versions.id", ondelete="RESTRICT"), nullable=False
    )
    #: Retained so the audit trail and the UI can show "rolled back from v9".
    previous_version_id: Mapped[str | None] = mapped_column(String(40), default=None)
    promoted_by: Mapped[str | None] = mapped_column(String(40), default=None)
    promoted_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    prompt: Mapped[Prompt] = relationship(back_populates="aliases", lazy="raise")

    __table_args__ = (UniqueConstraint("prompt_id", "name", name="unique_alias_per_prompt"),)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ModelDefinition(Base, TenantScopedMixin, TimestampMixin):
    """A provider/model pair the tenant uses, e.g. ``anthropic/claude-sonnet-4``."""

    __tablename__ = "model_definitions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_identifier: Mapped[str] = mapped_column(String(256), nullable=False)
    family: Mapped[str | None] = mapped_column(String(64), default=None)
    #: 'chat', 'completion', 'embedding', 'rerank', 'image', 'audio'.
    endpoint_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="chat")
    description: Mapped[str | None] = mapped_column(Text, default=None)
    #: Provider-reported capabilities that do not belong in the canonical schema.
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    versions: Mapped[list[ModelVersion]] = relationship(
        back_populates="model", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "provider",
            "model_identifier",
            name="unique_model_per_org",
        ),
    )


class ModelVersion(Base, TenantScopedMixin):
    """An immutable, fully-specified model *configuration*.

    A "model version" here is not the vendor's weights version -- we cannot
    observe that -- but the complete set of knobs that determine behaviour:
    temperature, top-p, max tokens, stop sequences, seed, tool configuration,
    response format, safety settings, timeouts. Change any one of them and you
    get a different configuration hash, because you have changed the
    experiment.

    ``system_fingerprint`` records what the provider *said* it ran, when it
    tells us. It is stored but excluded from the hash: it is an observation,
    not an input.
    """

    __tablename__ = "model_versions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    model_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("model_definitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(80), nullable=False, index=True)

    deployment_name: Mapped[str | None] = mapped_column(String(256), default=None)
    region: Mapped[str | None] = mapped_column(String(64), default=None)
    api_version: Mapped[str | None] = mapped_column(String(64), default=None)
    adapter_version: Mapped[str | None] = mapped_column(String(64), default=None)

    temperature: Mapped[float | None] = mapped_column(default=None)
    top_p: Mapped[float | None] = mapped_column(default=None)
    top_k: Mapped[int | None] = mapped_column(Integer, default=None)
    max_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    stop_sequences: Mapped[list[str]] = mapped_column(JSONColumn, nullable=False, default=list)
    seed: Mapped[int | None] = mapped_column(BigInteger, default=None)
    tool_config: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    response_format: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    safety_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    retry_policy: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    timeout_policy: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    #: Everything provider-specific that must not pollute the canonical fields.
    provider_extras: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    system_fingerprint: Mapped[str | None] = mapped_column(String(128), default=None)

    release_stage: Mapped[str] = mapped_column(String(16), nullable=False, default="published")
    parent_version_id: Mapped[str | None] = mapped_column(String(40), default=None)
    author_id: Mapped[str | None] = mapped_column(String(40), default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)

    model: Mapped[ModelDefinition] = relationship(back_populates="versions", lazy="raise")

    __table_args__ = (
        UniqueConstraint("model_id", "config_hash", name="unique_model_config_hash"),
        UniqueConstraint("model_id", "version_number", name="unique_model_version_number"),
        CheckConstraint(f"release_stage IN {_RELEASE_STAGES}", name="known_model_release_stage"),
        CheckConstraint(
            "temperature IS NULL OR (temperature >= 0 AND temperature <= 2)",
            name="temperature_range",
        ),
        CheckConstraint("top_p IS NULL OR (top_p >= 0 AND top_p <= 1)", name="top_p_range"),
    )


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


class Dataset(Base, TenantScopedMixin, TimestampMixin):
    """A named evaluation or reference dataset."""

    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    #: Free text, e.g. "CC-BY-4.0" or "internal use only". Surfaced prominently
    #: in the UI so nobody exports a dataset they are not licensed to move.
    license: Mapped[str | None] = mapped_column(String(256), default=None)
    #: When true, sample records are visible only to developer role and above.
    contains_sensitive_data: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tags: Mapped[list[str]] = mapped_column(JSONColumn, nullable=False, default=list)
    created_by: Mapped[str | None] = mapped_column(String(40), default=None)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    versions: Mapped[list[DatasetVersion]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="unique_dataset_name_per_project"),
    )


class DatasetVersion(Base, TenantScopedMixin):
    """An immutable snapshot of a dataset.

    Dataset *content* never enters PostgreSQL. Rows live in object storage as
    one or more content-addressed files; this table stores the **manifest**:
    per-file checksums, sizes, row counts, and the ``dataset_hash`` computed
    over the sorted manifest. Verifying a dataset therefore costs one hash of a
    small manifest plus, if you want the strong guarantee, a re-hash of the
    files themselves -- and neither requires the database to hold gigabytes.
    """

    __tablename__ = "dataset_versions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Hash over the canonical manifest, not over the raw bytes: it is stable
    #: under re-chunking and cheap to recompute.
    dataset_hash: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    #: JSON Schema of one record.
    record_schema: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    source: Mapped[str | None] = mapped_column(String(512), default=None)
    #: {"train": 800, "validation": 100, "test": 100}
    splits: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    labels: Mapped[list[str]] = mapped_column(JSONColumn, nullable=False, default=list)
    #: Null-rate, distinct counts, length percentiles -- computed at import.
    quality_summary: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    release_stage: Mapped[str] = mapped_column(String(16), nullable=False, default="published")
    parent_version_id: Mapped[str | None] = mapped_column(String(40), default=None)
    author_id: Mapped[str | None] = mapped_column(String(40), default=None)
    commit_message: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    #: Set when the payload has been purged by retention while the manifest
    #: (and therefore the lineage) is deliberately kept.
    payload_deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    dataset: Mapped[Dataset] = relationship(back_populates="versions", lazy="raise")
    files: Mapped[list[DatasetFile]] = relationship(
        back_populates="version", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("dataset_id", "dataset_hash", name="unique_dataset_hash"),
        UniqueConstraint("dataset_id", "version_number", name="unique_dataset_version_number"),
        CheckConstraint(f"release_stage IN {_RELEASE_STAGES}", name="known_dataset_release_stage"),
        CheckConstraint("row_count >= 0", name="non_negative_row_count"),
    )


class DatasetFile(Base, TenantScopedMixin):
    """One chunk of a dataset version, stored in object storage."""

    __tablename__ = "dataset_files"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    dataset_version_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("dataset_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Ordinal within the version; chunks are concatenated in this order.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(128), nullable=False, default="application/jsonl"
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: SHA-256 of the file bytes, prefixed. Verified on read.
    checksum: Mapped[str] = mapped_column(String(80), nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    split: Mapped[str | None] = mapped_column(String(64), default=None)

    version: Mapped[DatasetVersion] = relationship(back_populates="files", lazy="raise")

    __table_args__ = (
        UniqueConstraint("dataset_version_id", "sequence", name="unique_file_sequence"),
        Index("ix_dataset_files_object_key", "object_key"),
    )


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------


class Experiment(Base, TenantScopedMixin, TimestampMixin):
    """A named comparison of prompt/model/dataset combinations."""

    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[str | None] = mapped_column(String(40), default=None)

    runs: Mapped[list[ExperimentRun]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="unique_experiment_name_per_project"),
    )


class ExperimentRun(Base, TenantScopedMixin):
    """One execution of an experiment against a fixed set of versions."""

    __tablename__ = "experiment_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prompt_version_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    model_version_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    dataset_version_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, default=None)

    experiment: Mapped[Experiment] = relationship(back_populates="runs", lazy="raise")

    __table_args__ = (
        CheckConstraint(
            "status IN ('running','completed','failed','cancelled')", name="known_run_status"
        ),
    )
