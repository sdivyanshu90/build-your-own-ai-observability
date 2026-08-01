"""ORM models for the relational metadata store.

Importing this package registers every table on :data:`Base.metadata`, which is
what Alembic autogenerate and ``create_all`` (used by the test fixtures) walk.
Adding a model file without importing it here means its table silently never
gets created -- so the import list below is deliberately explicit rather than a
directory scan.
"""

from __future__ import annotations

from ..base import Base
from .operations import (
    BusDeadLetter,
    BusMessage,
    BusOffset,
    ExportJob,
    IdempotencyRecord,
    IngestBatchRecord,
    OutboxMessage,
    PriceBook,
    PriceEntry,
    RetentionPolicy,
    SavedView,
    StoredObject,
)
from .organization import (
    ApiKey,
    AuditEvent,
    Environment,
    Membership,
    Organization,
    Project,
    ServiceAccount,
    User,
)
from .registry import (
    Dataset,
    DatasetFile,
    DatasetVersion,
    Experiment,
    ExperimentRun,
    ModelDefinition,
    ModelVersion,
    Prompt,
    PromptAlias,
    PromptVersion,
)

__all__ = [
    "ApiKey",
    "AuditEvent",
    "Base",
    "BusDeadLetter",
    "BusMessage",
    "BusOffset",
    "Dataset",
    "DatasetFile",
    "DatasetVersion",
    "Environment",
    "Experiment",
    "ExperimentRun",
    "ExportJob",
    "IdempotencyRecord",
    "IngestBatchRecord",
    "Membership",
    "ModelDefinition",
    "ModelVersion",
    "Organization",
    "OutboxMessage",
    "PriceBook",
    "PriceEntry",
    "Project",
    "Prompt",
    "PromptAlias",
    "PromptVersion",
    "RetentionPolicy",
    "SavedView",
    "ServiceAccount",
    "StoredObject",
    "User",
]

#: Tables that carry tenant data and must never be queried without an
#: ``organization_id`` predicate. Asserted by a test that walks the metadata.
TENANT_SCOPED_TABLES: frozenset[str] = frozenset(
    {
        "api_keys",
        "audit_events",
        "dataset_files",
        "dataset_versions",
        "datasets",
        "environments",
        "experiment_runs",
        "experiments",
        "export_jobs",
        "idempotency_records",
        "ingest_batches",
        "memberships",
        "model_definitions",
        "model_versions",
        "projects",
        "prompt_aliases",
        "prompt_versions",
        "prompts",
        "retention_policies",
        "saved_views",
        "service_accounts",
        "stored_objects",
    }
)
