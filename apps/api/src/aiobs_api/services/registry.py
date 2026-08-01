"""Prompt, model-configuration and dataset registries.

All three follow the same pattern -- mutable container, immutable
content-addressed versions, movable aliases -- so they share their invariants:

* A published version is never updated. Attempting it raises
  :class:`ImmutableResourceError`, and the check is in the service layer rather
  than relying on callers being careful.
* Publishing identical content converges on the existing version instead of
  creating a duplicate. That makes ``create_version`` naturally idempotent: a
  retried deploy does not fork history.
* The content hash covers *semantic* content only. Editorial metadata
  (description, commit message, evaluation results) is excluded, so fixing a
  typo in a changelog does not manufacture a new behavioural version.
* Rollback is repointing an alias. Nothing is ever mutated or deleted, so the
  trace that ran three months ago still resolves to exactly what it ran.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from aiobs_schemas.canonical import canonical_json_str, content_hash
from aiobs_schemas.enums import ReleaseStage
from aiobs_schemas.ids import IdPrefix, generate_id, version_id

from ..core.errors import (
    ConflictError,
    ImmutableResourceError,
    NotFoundError,
    ValidationFailedError,
)
from ..core.logging import get_logger
from ..core.timeutil import Clock
from ..domain.principal import Principal
from ..storage.postgres.models import (
    Dataset,
    DatasetFile,
    DatasetVersion,
    ModelDefinition,
    ModelVersion,
    Prompt,
    PromptAlias,
    PromptVersion,
)
from ..storage.postgres.session import Database

__all__ = [
    "DatasetRegistry",
    "ModelRegistry",
    "PromptDiff",
    "PromptRegistry",
    "PromptVersionInput",
    "diff_prompt_versions",
]

log = get_logger(__name__)

_TEMPLATE_ENGINES = {"fstring", "jinja2", "mustache", "none"}


def _scoped_version_id(prefix: str, parent_id: str, digest: str) -> str:
    """Derive a version identifier from its parent *and* its content hash.

    Deriving the identifier from the content hash alone is tempting -- it makes
    the id reproducible -- but it is wrong: two different prompts that happen to
    contain the same text would collide on the primary key, and the second one
    would fail to publish. Mixing in the parent id keeps reproducibility
    (identical content under the same prompt always yields the same id) while
    scoping uniqueness where it belongs.

    ``content_hash`` itself remains the *pure* content digest, so the UI can
    still recognise that two prompts share identical text.
    """
    return version_id(prefix, content_hash({"parent": parent_id, "content": digest}))


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromptVersionInput:
    """The semantic content of a prompt version."""

    messages: list[dict[str, Any]]
    variable_schema: dict[str, Any] = field(default_factory=dict)
    default_variables: dict[str, Any] = field(default_factory=dict)
    template_engine: str = "fstring"
    commit_message: str | None = None
    label: str | None = None

    def hashable(self) -> dict[str, Any]:
        """The subset of fields that defines behavioural identity."""
        return {
            "messages": self.messages,
            "variable_schema": self.variable_schema,
            "default_variables": self.default_variables,
            "template_engine": self.template_engine,
        }


class PromptRegistry:
    """Manages prompts, their immutable versions and their aliases."""

    def __init__(self, *, database: Database, clock: Clock) -> None:
        self._database = database
        self._clock = clock

    async def create_prompt(
        self,
        *,
        principal: Principal,
        project_id: str,
        name: str,
        description: str | None = None,
        tags: Sequence[str] = (),
    ) -> Prompt:
        async with self._database.session_scope() as session:
            existing = (
                await session.execute(
                    select(Prompt).where(
                        Prompt.project_id == project_id,
                        Prompt.name == name,
                        Prompt.organization_id == principal.organization_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ConflictError(f"a prompt named {name!r} already exists in this project")
            prompt = Prompt(
                id=generate_id(IdPrefix.PROMPT),
                organization_id=principal.organization_id,
                project_id=project_id,
                name=name,
                description=description,
                tags=list(tags),
                created_by=principal.id,
            )
            session.add(prompt)
            await session.flush()
            session.expunge(prompt)
            return prompt

    async def create_version(
        self,
        *,
        principal: Principal,
        prompt_id: str,
        spec: PromptVersionInput,
        publish: bool = True,
    ) -> tuple[PromptVersion, bool]:
        """Create a version. Returns ``(version, created)``.

        ``created`` is ``False`` when identical content already existed -- the
        caller gets the pre-existing version rather than a duplicate, which is
        what makes repeated deploys of unchanged prompts a no-op.
        """
        self._validate_messages(spec.messages)
        if spec.template_engine not in _TEMPLATE_ENGINES:
            raise ValidationFailedError(
                f"unknown template engine {spec.template_engine!r}; "
                f"expected one of {sorted(_TEMPLATE_ENGINES)}"
            )

        digest = content_hash(spec.hashable())
        now = self._clock.now()

        async with self._database.session_scope() as session:
            prompt = await self._require_prompt(session, principal, prompt_id)

            existing = (
                await session.execute(
                    select(PromptVersion).where(
                        PromptVersion.prompt_id == prompt_id,
                        PromptVersion.content_hash == digest,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                session.expunge(existing)
                return existing, False

            highest = (
                await session.execute(
                    select(func.max(PromptVersion.version_number)).where(
                        PromptVersion.prompt_id == prompt_id
                    )
                )
            ).scalar_one_or_none()
            number = int(highest or 0) + 1
            parent = (
                await session.execute(
                    select(PromptVersion.id)
                    .where(PromptVersion.prompt_id == prompt_id)
                    .order_by(PromptVersion.version_number.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            version = PromptVersion(
                # Derived from the content hash, so the identifier is itself
                # reproducible: the same prompt content always yields the same id.
                id=_scoped_version_id(IdPrefix.PROMPT_VERSION, prompt_id, digest),
                prompt_id=prompt_id,
                organization_id=prompt.organization_id,
                project_id=prompt.project_id,
                version_number=number,
                label=spec.label or f"v{number}",
                content_hash=digest,
                messages=spec.messages,
                variable_schema=spec.variable_schema,
                default_variables=spec.default_variables,
                template_engine=spec.template_engine,
                release_stage=(
                    ReleaseStage.PUBLISHED.value if publish else ReleaseStage.DRAFT.value
                ),
                parent_version_id=parent,
                commit_message=spec.commit_message,
                author_id=principal.id,
                created_at=now,
                published_at=now if publish else None,
            )
            session.add(version)
            await session.flush()
            session.expunge(version)
            return version, True

    async def update_draft(
        self, *, principal: Principal, version_id_value: str, spec: PromptVersionInput
    ) -> PromptVersion:
        """Edit a draft version. Refuses once published."""
        async with self._database.session_scope() as session:
            version = await self._require_version(session, principal, version_id_value)
            if version.release_stage != ReleaseStage.DRAFT.value:
                raise ImmutableResourceError(
                    f"prompt version {version.label!r} is {version.release_stage} and cannot be "
                    "modified; create a new version instead"
                )
            version.messages = spec.messages
            version.variable_schema = spec.variable_schema
            version.default_variables = spec.default_variables
            version.template_engine = spec.template_engine
            version.commit_message = spec.commit_message
            version.content_hash = content_hash(spec.hashable())
            await session.flush()
            session.expunge(version)
            return version

    async def publish(self, *, principal: Principal, version_id_value: str) -> PromptVersion:
        async with self._database.session_scope() as session:
            version = await self._require_version(session, principal, version_id_value)
            if version.release_stage == ReleaseStage.PUBLISHED.value:
                session.expunge(version)
                return version
            version.release_stage = ReleaseStage.PUBLISHED.value
            version.published_at = self._clock.now()
            await session.flush()
            session.expunge(version)
            return version

    async def promote_alias(
        self, *, principal: Principal, prompt_id: str, alias: str, target_version_id: str
    ) -> PromptAlias:
        """Point ``alias`` at ``target_version_id``.

        This is the deployment primitive. Rolling back is the same operation
        with an older version id, which is why it is atomic and audited: the
        previous target is retained on the row so the audit trail shows what
        was replaced.
        """
        async with self._database.session_scope() as session:
            version = await self._require_version(session, principal, target_version_id)
            if version.prompt_id != prompt_id:
                raise ValidationFailedError("version does not belong to this prompt")
            if version.release_stage == ReleaseStage.DRAFT.value:
                raise ValidationFailedError("a draft version cannot be promoted; publish it first")

            record = (
                await session.execute(
                    select(PromptAlias).where(
                        PromptAlias.prompt_id == prompt_id, PromptAlias.name == alias
                    )
                )
            ).scalar_one_or_none()
            now = self._clock.now()
            if record is None:
                record = PromptAlias(
                    id=generate_id(IdPrefix.PROMPT),
                    organization_id=principal.organization_id,
                    prompt_id=prompt_id,
                    name=alias,
                    version_id=target_version_id,
                    promoted_by=principal.id,
                    promoted_at=now,
                )
                session.add(record)
            else:
                record.previous_version_id = record.version_id
                record.version_id = target_version_id
                record.promoted_by = principal.id
                record.promoted_at = now
            await session.flush()
            session.expunge(record)
            return record

    async def resolve_alias(
        self, *, organization_id: str, project_id: str, prompt_name: str, alias: str
    ) -> PromptVersion:
        """Resolve ``project/prompt@alias`` to a concrete version.

        This is the SDK's read path at application start-up, so it is a single
        join rather than three round trips.
        """
        async with self._database.session_scope() as session:
            row = (
                await session.execute(
                    select(PromptVersion)
                    .join(PromptAlias, PromptAlias.version_id == PromptVersion.id)
                    .join(Prompt, Prompt.id == PromptAlias.prompt_id)
                    .where(
                        Prompt.organization_id == organization_id,
                        Prompt.project_id == project_id,
                        Prompt.name == prompt_name,
                        PromptAlias.name == alias,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("prompt alias", f"{prompt_name}@{alias}")
            session.expunge(row)
            return row

    async def list_prompts(
        self, *, organization_id: str, project_id: str, limit: int = 100
    ) -> list[Prompt]:
        async with self._database.session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(Prompt)
                        .where(
                            Prompt.organization_id == organization_id,
                            Prompt.project_id == project_id,
                            Prompt.archived_at.is_(None),
                        )
                        .order_by(Prompt.name)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

    async def list_versions(
        self, *, organization_id: str, prompt_id: str, limit: int = 100
    ) -> list[PromptVersion]:
        async with self._database.session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(PromptVersion)
                        .where(
                            PromptVersion.prompt_id == prompt_id,
                            PromptVersion.organization_id == organization_id,
                        )
                        .order_by(PromptVersion.version_number.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

    async def list_aliases(self, *, organization_id: str, prompt_id: str) -> list[PromptAlias]:
        async with self._database.session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(PromptAlias).where(
                            PromptAlias.prompt_id == prompt_id,
                            PromptAlias.organization_id == organization_id,
                        )
                    )
                )
                .scalars()
                .all()
            )

    async def get_version(self, *, organization_id: str, version_id_value: str) -> PromptVersion:
        async with self._database.session_scope() as session:
            version = (
                await session.execute(
                    select(PromptVersion).where(
                        PromptVersion.id == version_id_value,
                        PromptVersion.organization_id == organization_id,
                    )
                )
            ).scalar_one_or_none()
            if version is None:
                raise NotFoundError("prompt version", version_id_value)
            session.expunge(version)
            return version

    async def _require_prompt(self, session: Any, principal: Principal, prompt_id: str) -> Prompt:
        prompt = (
            await session.execute(
                select(Prompt).where(
                    Prompt.id == prompt_id,
                    Prompt.organization_id == principal.organization_id,
                )
            )
        ).scalar_one_or_none()
        if prompt is None:
            raise NotFoundError("prompt", prompt_id)
        principal.require_project(prompt.project_id)
        return prompt

    async def _require_version(
        self, session: Any, principal: Principal, version_id_value: str
    ) -> PromptVersion:
        version = (
            await session.execute(
                select(PromptVersion).where(
                    PromptVersion.id == version_id_value,
                    PromptVersion.organization_id == principal.organization_id,
                )
            )
        ).scalar_one_or_none()
        if version is None:
            raise NotFoundError("prompt version", version_id_value)
        principal.require_project(version.project_id)
        return version

    def _validate_messages(self, messages: Sequence[dict[str, Any]]) -> None:
        if not messages:
            raise ValidationFailedError("a prompt version must contain at least one message")
        if len(messages) > 200:
            raise ValidationFailedError("a prompt version may contain at most 200 messages")
        allowed_roles = {"system", "user", "assistant", "tool", "developer"}
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                raise ValidationFailedError(f"message {index} is not an object")
            role = message.get("role")
            if role not in allowed_roles:
                raise ValidationFailedError(
                    f"message {index} has unknown role {role!r}; "
                    f"expected one of {sorted(allowed_roles)}"
                )
            if "content" not in message:
                raise ValidationFailedError(f"message {index} is missing 'content'")
            content = message["content"]
            if isinstance(content, str) and len(content) > 500_000:
                raise ValidationFailedError(f"message {index} exceeds the 500,000 character limit")


@dataclass(frozen=True, slots=True)
class PromptDiff:
    """Structured difference between two prompt versions."""

    identical: bool
    message_changes: tuple[dict[str, Any], ...]
    variable_changes: dict[str, Any]
    engine_changed: bool
    left_hash: str
    right_hash: str


def diff_prompt_versions(left: PromptVersion, right: PromptVersion) -> PromptDiff:
    """Compare two versions message by message.

    A message-level diff rather than a character-level one: a prompt is a list
    of role-tagged messages, and "the system message changed" is the useful
    unit of change, not "character 412 differs". The UI renders a text diff
    within a changed message.
    """
    left_messages = list(left.messages or [])
    right_messages = list(right.messages or [])
    changes: list[dict[str, Any]] = []

    for index in range(max(len(left_messages), len(right_messages))):
        old = left_messages[index] if index < len(left_messages) else None
        new = right_messages[index] if index < len(right_messages) else None
        if old == new:
            continue
        if old is None:
            changes.append({"index": index, "change": "added", "after": new})
        elif new is None:
            changes.append({"index": index, "change": "removed", "before": old})
        else:
            changes.append(
                {
                    "index": index,
                    "change": "modified",
                    "before": old,
                    "after": new,
                    "role_changed": old.get("role") != new.get("role"),
                }
            )

    left_variables = dict(left.variable_schema or {})
    right_variables = dict(right.variable_schema or {})
    variable_changes = {
        "added": sorted(set(right_variables) - set(left_variables)),
        "removed": sorted(set(left_variables) - set(right_variables)),
        "modified": sorted(
            key
            for key in set(left_variables) & set(right_variables)
            if canonical_json_str(left_variables[key]) != canonical_json_str(right_variables[key])
        ),
    }

    return PromptDiff(
        identical=left.content_hash == right.content_hash,
        message_changes=tuple(changes),
        variable_changes=variable_changes,
        engine_changed=left.template_engine != right.template_engine,
        left_hash=left.content_hash,
        right_hash=right.content_hash,
    )


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


class ModelRegistry:
    """Registers models and their immutable configuration versions."""

    #: Fields that define a configuration's behaviour and therefore its hash.
    _HASHED_FIELDS = (
        "deployment_name",
        "region",
        "api_version",
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "stop_sequences",
        "seed",
        "tool_config",
        "response_format",
        "safety_settings",
    )

    def __init__(self, *, database: Database, clock: Clock) -> None:
        self._database = database
        self._clock = clock

    async def ensure_model(
        self,
        *,
        principal: Principal,
        provider: str,
        model_identifier: str,
        project_id: str | None = None,
        family: str | None = None,
        endpoint_kind: str = "chat",
    ) -> ModelDefinition:
        """Get or create the model definition. Idempotent by (org, provider, model)."""
        async with self._database.session_scope() as session:
            existing = (
                await session.execute(
                    select(ModelDefinition).where(
                        ModelDefinition.organization_id == principal.organization_id,
                        ModelDefinition.provider == provider,
                        ModelDefinition.model_identifier == model_identifier,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                session.expunge(existing)
                return existing
            definition = ModelDefinition(
                id=generate_id(IdPrefix.MODEL),
                organization_id=principal.organization_id,
                project_id=project_id,
                provider=provider,
                model_identifier=model_identifier,
                family=family,
                endpoint_kind=endpoint_kind,
            )
            session.add(definition)
            await session.flush()
            session.expunge(definition)
            return definition

    async def create_version(
        self, *, principal: Principal, model_id: str, config: dict[str, Any]
    ) -> tuple[ModelVersion, bool]:
        """Create a configuration version, converging on identical configs."""
        hashable = {
            key: config.get(key) for key in self._HASHED_FIELDS if config.get(key) is not None
        }
        digest = content_hash(hashable)
        now = self._clock.now()

        async with self._database.session_scope() as session:
            definition = (
                await session.execute(
                    select(ModelDefinition).where(
                        ModelDefinition.id == model_id,
                        ModelDefinition.organization_id == principal.organization_id,
                    )
                )
            ).scalar_one_or_none()
            if definition is None:
                raise NotFoundError("model", model_id)

            existing = (
                await session.execute(
                    select(ModelVersion).where(
                        ModelVersion.model_id == model_id, ModelVersion.config_hash == digest
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                session.expunge(existing)
                return existing, False

            highest = (
                await session.execute(
                    select(func.max(ModelVersion.version_number)).where(
                        ModelVersion.model_id == model_id
                    )
                )
            ).scalar_one_or_none()
            number = int(highest or 0) + 1

            version = ModelVersion(
                id=_scoped_version_id(IdPrefix.MODEL_VERSION, model_id, digest),
                model_id=model_id,
                organization_id=definition.organization_id,
                project_id=definition.project_id,
                version_number=number,
                label=str(config.get("label") or f"v{number}"),
                config_hash=digest,
                deployment_name=config.get("deployment_name"),
                region=config.get("region"),
                api_version=config.get("api_version"),
                adapter_version=config.get("adapter_version"),
                temperature=config.get("temperature"),
                top_p=config.get("top_p"),
                top_k=config.get("top_k"),
                max_tokens=config.get("max_tokens"),
                stop_sequences=list(config.get("stop_sequences") or []),
                seed=config.get("seed"),
                tool_config=config.get("tool_config") or {},
                response_format=config.get("response_format") or {},
                safety_settings=config.get("safety_settings") or {},
                retry_policy=config.get("retry_policy") or {},
                timeout_policy=config.get("timeout_policy") or {},
                provider_extras=config.get("provider_extras") or {},
                # Observed, not an input: excluded from the hash on purpose.
                system_fingerprint=config.get("system_fingerprint"),
                author_id=principal.id,
                created_at=now,
            )
            session.add(version)
            await session.flush()
            session.expunge(version)
            return version, True

    async def list_models(
        self, *, organization_id: str, project_id: str | None = None
    ) -> list[ModelDefinition]:
        async with self._database.session_scope() as session:
            statement = select(ModelDefinition).where(
                ModelDefinition.organization_id == organization_id,
                ModelDefinition.archived_at.is_(None),
            )
            if project_id:
                statement = statement.where(
                    (ModelDefinition.project_id == project_id)
                    | (ModelDefinition.project_id.is_(None))
                )
            return list(
                (await session.execute(statement.order_by(ModelDefinition.provider)))
                .scalars()
                .all()
            )

    async def list_versions(
        self, *, organization_id: str, model_id: str, limit: int = 100
    ) -> list[ModelVersion]:
        async with self._database.session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(ModelVersion)
                        .where(
                            ModelVersion.model_id == model_id,
                            ModelVersion.organization_id == organization_id,
                        )
                        .order_by(ModelVersion.version_number.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )


def diff_model_versions(left: ModelVersion, right: ModelVersion) -> dict[str, Any]:
    """Field-by-field configuration difference."""
    fields = (
        "deployment_name",
        "region",
        "api_version",
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "stop_sequences",
        "seed",
        "tool_config",
        "response_format",
        "safety_settings",
        "retry_policy",
        "timeout_policy",
    )
    changes: dict[str, Any] = {}
    for name in fields:
        before = getattr(left, name)
        after = getattr(right, name)
        if canonical_json_str(before) != canonical_json_str(after):
            changes[name] = {"before": before, "after": after}
    return {
        "identical": left.config_hash == right.config_hash,
        "changes": changes,
        "left_hash": left.config_hash,
        "right_hash": right.config_hash,
    }


# ---------------------------------------------------------------------------
# datasets
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatasetFileSpec:
    """One chunk of a dataset version, already written to object storage."""

    sequence: int
    object_key: str
    size_bytes: int
    checksum: str
    row_count: int
    content_type: str = "application/jsonl"
    split: str | None = None


class DatasetRegistry:
    """Registers datasets and their immutable, manifest-hashed versions."""

    def __init__(self, *, database: Database, clock: Clock) -> None:
        self._database = database
        self._clock = clock

    async def create_dataset(
        self,
        *,
        principal: Principal,
        project_id: str,
        name: str,
        description: str | None = None,
        license_text: str | None = None,
        contains_sensitive_data: bool = True,
        tags: Sequence[str] = (),
    ) -> Dataset:
        async with self._database.session_scope() as session:
            existing = (
                await session.execute(
                    select(Dataset).where(
                        Dataset.project_id == project_id,
                        Dataset.name == name,
                        Dataset.organization_id == principal.organization_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ConflictError(f"a dataset named {name!r} already exists in this project")
            dataset = Dataset(
                id=generate_id(IdPrefix.DATASET),
                organization_id=principal.organization_id,
                project_id=project_id,
                name=name,
                description=description,
                license=license_text,
                contains_sensitive_data=contains_sensitive_data,
                tags=list(tags),
                created_by=principal.id,
            )
            session.add(dataset)
            await session.flush()
            session.expunge(dataset)
            return dataset

    async def create_version(
        self,
        *,
        principal: Principal,
        dataset_id: str,
        files: Sequence[DatasetFileSpec],
        record_schema: dict[str, Any] | None = None,
        splits: dict[str, Any] | None = None,
        labels: Sequence[str] = (),
        source: str | None = None,
        quality_summary: dict[str, Any] | None = None,
        commit_message: str | None = None,
    ) -> tuple[DatasetVersion, bool]:
        """Register an immutable dataset version from its file manifest.

        The version hash is computed over the *sorted manifest* -- each file's
        sequence, checksum, size and row count -- not over the bytes. That makes
        verification cheap (hash a few kilobytes of manifest) while still being
        a strong identity: changing any byte changes a file checksum, which
        changes the manifest, which changes the version hash.
        """
        if not files:
            raise ValidationFailedError("a dataset version must reference at least one file")
        manifest = sorted(
            (
                {
                    "sequence": item.sequence,
                    "object_key": item.object_key,
                    "checksum": item.checksum,
                    "size_bytes": item.size_bytes,
                    "row_count": item.row_count,
                    "split": item.split,
                }
                for item in files
            ),
            key=lambda entry: entry["sequence"],
        )
        digest = content_hash({"manifest": manifest, "schema": record_schema or {}})
        now = self._clock.now()

        async with self._database.session_scope() as session:
            dataset = (
                await session.execute(
                    select(Dataset).where(
                        Dataset.id == dataset_id,
                        Dataset.organization_id == principal.organization_id,
                    )
                )
            ).scalar_one_or_none()
            if dataset is None:
                raise NotFoundError("dataset", dataset_id)
            principal.require_project(dataset.project_id)

            existing = (
                await session.execute(
                    select(DatasetVersion).where(
                        DatasetVersion.dataset_id == dataset_id,
                        DatasetVersion.dataset_hash == digest,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                session.expunge(existing)
                return existing, False

            highest = (
                await session.execute(
                    select(func.max(DatasetVersion.version_number)).where(
                        DatasetVersion.dataset_id == dataset_id
                    )
                )
            ).scalar_one_or_none()
            number = int(highest or 0) + 1
            parent = (
                await session.execute(
                    select(DatasetVersion.id)
                    .where(DatasetVersion.dataset_id == dataset_id)
                    .order_by(DatasetVersion.version_number.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            version = DatasetVersion(
                id=_scoped_version_id(IdPrefix.DATASET_VERSION, dataset_id, digest),
                dataset_id=dataset_id,
                organization_id=dataset.organization_id,
                project_id=dataset.project_id,
                version_number=number,
                label=f"v{number}",
                dataset_hash=digest,
                record_schema=record_schema or {},
                row_count=sum(item.row_count for item in files),
                total_bytes=sum(item.size_bytes for item in files),
                source=source,
                splits=splits or {},
                labels=list(labels),
                quality_summary=quality_summary or {},
                parent_version_id=parent,
                author_id=principal.id,
                commit_message=commit_message,
                created_at=now,
            )
            session.add(version)
            await session.flush()
            for item in files:
                session.add(
                    DatasetFile(
                        id=generate_id(IdPrefix.OBJECT),
                        organization_id=dataset.organization_id,
                        dataset_version_id=version.id,
                        sequence=item.sequence,
                        object_key=item.object_key,
                        content_type=item.content_type,
                        size_bytes=item.size_bytes,
                        checksum=item.checksum,
                        row_count=item.row_count,
                        split=item.split,
                    )
                )
            await session.flush()
            session.expunge(version)
            return version, True

    async def list_datasets(self, *, organization_id: str, project_id: str) -> list[Dataset]:
        async with self._database.session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(Dataset)
                        .where(
                            Dataset.organization_id == organization_id,
                            Dataset.project_id == project_id,
                            Dataset.archived_at.is_(None),
                        )
                        .order_by(Dataset.name)
                    )
                )
                .scalars()
                .all()
            )

    async def list_versions(
        self, *, organization_id: str, dataset_id: str, limit: int = 100
    ) -> list[DatasetVersion]:
        async with self._database.session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(DatasetVersion)
                        .where(
                            DatasetVersion.dataset_id == dataset_id,
                            DatasetVersion.organization_id == organization_id,
                        )
                        .order_by(DatasetVersion.version_number.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

    async def get_version_files(
        self, *, organization_id: str, dataset_version_id: str
    ) -> tuple[DatasetVersion, list[DatasetFile]]:
        async with self._database.session_scope() as session:
            version = (
                await session.execute(
                    select(DatasetVersion).where(
                        DatasetVersion.id == dataset_version_id,
                        DatasetVersion.organization_id == organization_id,
                    )
                )
            ).scalar_one_or_none()
            if version is None:
                raise NotFoundError("dataset version", dataset_version_id)
            if version.payload_deleted_at is not None:
                from ..core.errors import GoneError

                raise GoneError(
                    "dataset payload has passed its retention horizon; "
                    "the manifest is retained for lineage but the rows are gone",
                    context={"deleted_at": version.payload_deleted_at.isoformat()},
                )
            files = list(
                (
                    await session.execute(
                        select(DatasetFile)
                        .where(DatasetFile.dataset_version_id == dataset_version_id)
                        .order_by(DatasetFile.sequence)
                    )
                )
                .scalars()
                .all()
            )
            session.expunge(version)
            for item in files:
                session.expunge(item)
            return version, files
