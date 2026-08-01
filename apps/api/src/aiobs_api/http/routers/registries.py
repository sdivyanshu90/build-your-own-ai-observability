"""Prompt, model and dataset registry endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from ...domain.rbac import Permission
from ...services.audit import AuditAction
from ...services.registry import (
    DatasetFileSpec,
    PromptVersionInput,
    diff_model_versions,
    diff_prompt_versions,
)
from ..deps import PrincipalDep, ServicesDep
from ..schemas import (
    AliasPromoteRequest,
    DatasetCreate,
    DatasetOut,
    DatasetVersionOut,
    ModelOut,
    ModelVersionCreate,
    ModelVersionOut,
    PromptAliasOut,
    PromptCreate,
    PromptOut,
    PromptVersionCreate,
    PromptVersionOut,
)

__all__ = ["router"]

router = APIRouter()


def _prompt_out(prompt) -> PromptOut:  # type: ignore[no-untyped-def]
    return PromptOut(
        id=prompt.id,
        project_id=prompt.project_id,
        name=prompt.name,
        description=prompt.description,
        tags=list(prompt.tags or []),
        created_at=prompt.created_at,
    )


def _version_out(version) -> PromptVersionOut:  # type: ignore[no-untyped-def]
    return PromptVersionOut(
        id=version.id,
        prompt_id=version.prompt_id,
        version_number=version.version_number,
        label=version.label,
        content_hash=version.content_hash,
        messages=list(version.messages or []),
        variable_schema=dict(version.variable_schema or {}),
        default_variables=dict(version.default_variables or {}),
        template_engine=version.template_engine,
        release_stage=version.release_stage,
        parent_version_id=version.parent_version_id,
        commit_message=version.commit_message,
        created_at=version.created_at,
        published_at=version.published_at,
    )


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


@router.get("/prompts", response_model=list[PromptOut], tags=["prompts"], summary="List prompts")
async def list_prompts(
    principal: PrincipalDep,
    services: ServicesDep,
    project_id: Annotated[str, Query()],
) -> list[PromptOut]:
    principal.require(Permission.PROMPT_READ)
    principal.require_project(project_id)
    prompts = await services.prompts.list_prompts(
        organization_id=principal.organization_id, project_id=project_id
    )
    return [_prompt_out(prompt) for prompt in prompts]


@router.post(
    "/prompts",
    response_model=PromptOut,
    status_code=status.HTTP_201_CREATED,
    tags=["prompts"],
    summary="Create a prompt",
)
async def create_prompt(
    payload: PromptCreate, principal: PrincipalDep, services: ServicesDep
) -> PromptOut:
    principal.require(Permission.PROMPT_CREATE)
    principal.require_project(payload.project_id)
    prompt = await services.prompts.create_prompt(
        principal=principal,
        project_id=payload.project_id,
        name=payload.name,
        description=payload.description,
        tags=payload.tags,
    )
    await services.audit.record(
        principal=principal,
        action=AuditAction.PROMPT_CREATED,
        resource_type="prompt",
        resource_id=prompt.id,
        project_id=payload.project_id,
        metadata={"name": payload.name},
    )
    return _prompt_out(prompt)


@router.get(
    "/prompts/{prompt_id}/versions",
    response_model=list[PromptVersionOut],
    tags=["prompts"],
    summary="List a prompt's versions",
)
async def list_prompt_versions(
    prompt_id: str, principal: PrincipalDep, services: ServicesDep
) -> list[PromptVersionOut]:
    principal.require(Permission.PROMPT_READ)
    versions = await services.prompts.list_versions(
        organization_id=principal.organization_id, prompt_id=prompt_id
    )
    return [_version_out(version) for version in versions]


@router.post(
    "/prompts/{prompt_id}/versions",
    response_model=PromptVersionOut,
    status_code=status.HTTP_201_CREATED,
    tags=["prompts"],
    summary="Create an immutable prompt version",
    description=(
        "Publishing identical content returns the existing version rather than "
        "creating a duplicate, so repeated deploys of an unchanged prompt are "
        "a no-op."
    ),
)
async def create_prompt_version(
    prompt_id: str,
    payload: PromptVersionCreate,
    principal: PrincipalDep,
    services: ServicesDep,
) -> PromptVersionOut:
    principal.require(Permission.PROMPT_PUBLISH if payload.publish else Permission.PROMPT_CREATE)
    version, created = await services.prompts.create_version(
        principal=principal,
        prompt_id=prompt_id,
        spec=PromptVersionInput(
            messages=payload.messages,
            variable_schema=payload.variable_schema,
            default_variables=payload.default_variables,
            template_engine=payload.template_engine,
            commit_message=payload.commit_message,
            label=payload.label,
        ),
        publish=payload.publish,
    )
    if created and payload.publish:
        await services.audit.record(
            principal=principal,
            action=AuditAction.PROMPT_VERSION_PUBLISHED,
            resource_type="prompt_version",
            resource_id=version.id,
            metadata={"prompt_id": prompt_id, "content_hash": version.content_hash},
        )
    return _version_out(version)


@router.get(
    "/prompts/{prompt_id}/aliases",
    response_model=list[PromptAliasOut],
    tags=["prompts"],
    summary="List a prompt's aliases",
)
async def list_prompt_aliases(
    prompt_id: str, principal: PrincipalDep, services: ServicesDep
) -> list[PromptAliasOut]:
    principal.require(Permission.PROMPT_READ)
    aliases = await services.prompts.list_aliases(
        organization_id=principal.organization_id, prompt_id=prompt_id
    )
    return [
        PromptAliasOut(
            name=alias.name,
            version_id=alias.version_id,
            previous_version_id=alias.previous_version_id,
            promoted_at=alias.promoted_at,
        )
        for alias in aliases
    ]


@router.post(
    "/prompts/{prompt_id}/aliases",
    response_model=PromptAliasOut,
    tags=["prompts"],
    summary="Point an alias at a version",
    description="This is the deploy and rollback primitive: atomic and audited.",
)
async def promote_alias(
    prompt_id: str,
    payload: AliasPromoteRequest,
    principal: PrincipalDep,
    services: ServicesDep,
) -> PromptAliasOut:
    principal.require(Permission.PROMPT_PROMOTE)
    alias = await services.prompts.promote_alias(
        principal=principal,
        prompt_id=prompt_id,
        alias=payload.alias,
        target_version_id=payload.version_id,
    )
    await services.audit.record(
        principal=principal,
        action=AuditAction.PROMPT_ALIAS_PROMOTED,
        resource_type="prompt_alias",
        resource_id=f"{prompt_id}@{payload.alias}",
        metadata={
            "version_id": payload.version_id,
            "previous_version_id": alias.previous_version_id,
        },
    )
    return PromptAliasOut(
        name=alias.name,
        version_id=alias.version_id,
        previous_version_id=alias.previous_version_id,
        promoted_at=alias.promoted_at,
    )


@router.get(
    "/prompts/resolve",
    response_model=PromptVersionOut,
    tags=["prompts"],
    summary="Resolve project/prompt@alias to a concrete version",
    description="The SDK's start-up read path.",
)
async def resolve_prompt(
    principal: PrincipalDep,
    services: ServicesDep,
    project_id: Annotated[str, Query()],
    name: Annotated[str, Query(description="Prompt name")],
    alias: Annotated[str, Query()] = "production",
) -> PromptVersionOut:
    principal.require(Permission.PROMPT_READ)
    principal.require_project(project_id)
    version = await services.prompts.resolve_alias(
        organization_id=principal.organization_id,
        project_id=project_id,
        prompt_name=name,
        alias=alias,
    )
    return _version_out(version)


@router.get(
    "/prompts/versions/{version_id}/diff",
    tags=["prompts"],
    summary="Diff two prompt versions",
)
async def diff_prompts(
    version_id: str,
    principal: PrincipalDep,
    services: ServicesDep,
    against: Annotated[str, Query(description="Version id to compare against")],
) -> dict[str, object]:
    principal.require(Permission.PROMPT_READ)
    left = await services.prompts.get_version(
        organization_id=principal.organization_id, version_id_value=against
    )
    right = await services.prompts.get_version(
        organization_id=principal.organization_id, version_id_value=version_id
    )
    diff = diff_prompt_versions(left, right)
    return {
        "identical": diff.identical,
        "left_hash": diff.left_hash,
        "right_hash": diff.right_hash,
        "engine_changed": diff.engine_changed,
        "message_changes": list(diff.message_changes),
        "variable_changes": diff.variable_changes,
    }


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


@router.get("/models", response_model=list[ModelOut], tags=["models"], summary="List models")
async def list_models(
    principal: PrincipalDep,
    services: ServicesDep,
    project_id: Annotated[str | None, Query()] = None,
) -> list[ModelOut]:
    principal.require(Permission.MODEL_READ)
    models = await services.models.list_models(
        organization_id=principal.organization_id, project_id=project_id
    )
    return [
        ModelOut(
            id=model.id,
            provider=model.provider,
            model_identifier=model.model_identifier,
            family=model.family,
            endpoint_kind=model.endpoint_kind,
            created_at=model.created_at,
        )
        for model in models
    ]


def _model_version_out(version) -> ModelVersionOut:  # type: ignore[no-untyped-def]
    return ModelVersionOut(
        id=version.id,
        model_id=version.model_id,
        version_number=version.version_number,
        label=version.label,
        config_hash=version.config_hash,
        deployment_name=version.deployment_name,
        region=version.region,
        temperature=version.temperature,
        top_p=version.top_p,
        top_k=version.top_k,
        max_tokens=version.max_tokens,
        stop_sequences=list(version.stop_sequences or []),
        seed=version.seed,
        tool_config=dict(version.tool_config or {}),
        response_format=dict(version.response_format or {}),
        safety_settings=dict(version.safety_settings or {}),
        provider_extras=dict(version.provider_extras or {}),
        system_fingerprint=version.system_fingerprint,
        created_at=version.created_at,
    )


@router.post(
    "/models/versions",
    response_model=ModelVersionOut,
    status_code=status.HTTP_201_CREATED,
    tags=["models"],
    summary="Register a model configuration version",
    description=(
        "Creates the model definition if needed. Identical configurations "
        "converge on one version: the id is derived from the configuration hash."
    ),
)
async def create_model_version(
    payload: ModelVersionCreate, principal: PrincipalDep, services: ServicesDep
) -> ModelVersionOut:
    principal.require(Permission.MODEL_WRITE)
    definition = await services.models.ensure_model(
        principal=principal,
        provider=payload.provider,
        model_identifier=payload.model_identifier,
        project_id=payload.project_id,
        family=payload.family,
        endpoint_kind=payload.endpoint_kind,
    )
    version, created = await services.models.create_version(
        principal=principal, model_id=definition.id, config=payload.config
    )
    if created:
        await services.audit.record(
            principal=principal,
            action=AuditAction.MODEL_VERSION_CREATED,
            resource_type="model_version",
            resource_id=version.id,
            metadata={"provider": payload.provider, "model": payload.model_identifier},
        )
    return _model_version_out(version)


@router.get(
    "/models/{model_id}/versions",
    response_model=list[ModelVersionOut],
    tags=["models"],
    summary="List a model's configuration versions",
)
async def list_model_versions(
    model_id: str, principal: PrincipalDep, services: ServicesDep
) -> list[ModelVersionOut]:
    principal.require(Permission.MODEL_READ)
    versions = await services.models.list_versions(
        organization_id=principal.organization_id, model_id=model_id
    )
    return [_model_version_out(version) for version in versions]


@router.get(
    "/models/{model_id}/versions/diff",
    tags=["models"],
    summary="Diff two model configuration versions",
)
async def diff_models(
    model_id: str,
    principal: PrincipalDep,
    services: ServicesDep,
    left: Annotated[str, Query()],
    right: Annotated[str, Query()],
) -> dict[str, object]:
    principal.require(Permission.MODEL_READ)
    versions = {
        version.id: version
        for version in await services.models.list_versions(
            organization_id=principal.organization_id, model_id=model_id, limit=500
        )
    }
    from ...core.errors import NotFoundError

    if left not in versions:
        raise NotFoundError("model version", left)
    if right not in versions:
        raise NotFoundError("model version", right)
    return diff_model_versions(versions[left], versions[right])


# ---------------------------------------------------------------------------
# datasets
# ---------------------------------------------------------------------------


@router.get(
    "/datasets", response_model=list[DatasetOut], tags=["datasets"], summary="List datasets"
)
async def list_datasets(
    principal: PrincipalDep,
    services: ServicesDep,
    project_id: Annotated[str, Query()],
) -> list[DatasetOut]:
    principal.require(Permission.DATASET_READ)
    principal.require_project(project_id)
    datasets = await services.datasets.list_datasets(
        organization_id=principal.organization_id, project_id=project_id
    )
    return [
        DatasetOut(
            id=dataset.id,
            project_id=dataset.project_id,
            name=dataset.name,
            description=dataset.description,
            license=dataset.license,
            contains_sensitive_data=dataset.contains_sensitive_data,
            tags=list(dataset.tags or []),
            created_at=dataset.created_at,
        )
        for dataset in datasets
    ]


@router.post(
    "/datasets",
    response_model=DatasetOut,
    status_code=status.HTTP_201_CREATED,
    tags=["datasets"],
    summary="Create a dataset",
)
async def create_dataset(
    payload: DatasetCreate, principal: PrincipalDep, services: ServicesDep
) -> DatasetOut:
    principal.require(Permission.DATASET_WRITE)
    principal.require_project(payload.project_id)
    dataset = await services.datasets.create_dataset(
        principal=principal,
        project_id=payload.project_id,
        name=payload.name,
        description=payload.description,
        license_text=payload.license,
        contains_sensitive_data=payload.contains_sensitive_data,
        tags=payload.tags,
    )
    await services.audit.record(
        principal=principal,
        action=AuditAction.DATASET_CREATED,
        resource_type="dataset",
        resource_id=dataset.id,
        project_id=payload.project_id,
    )
    return DatasetOut(
        id=dataset.id,
        project_id=dataset.project_id,
        name=dataset.name,
        description=dataset.description,
        license=dataset.license,
        contains_sensitive_data=dataset.contains_sensitive_data,
        tags=list(dataset.tags or []),
        created_at=dataset.created_at,
    )


def _dataset_version_out(version) -> DatasetVersionOut:  # type: ignore[no-untyped-def]
    return DatasetVersionOut(
        id=version.id,
        dataset_id=version.dataset_id,
        version_number=version.version_number,
        label=version.label,
        dataset_hash=version.dataset_hash,
        row_count=version.row_count,
        total_bytes=version.total_bytes,
        record_schema=dict(version.record_schema or {}),
        splits=dict(version.splits or {}),
        labels=list(version.labels or []),
        quality_summary=dict(version.quality_summary or {}),
        parent_version_id=version.parent_version_id,
        created_at=version.created_at,
        payload_deleted_at=version.payload_deleted_at,
    )


@router.get(
    "/datasets/{dataset_id}/versions",
    response_model=list[DatasetVersionOut],
    tags=["datasets"],
    summary="List a dataset's versions",
)
async def list_dataset_versions(
    dataset_id: str, principal: PrincipalDep, services: ServicesDep
) -> list[DatasetVersionOut]:
    principal.require(Permission.DATASET_READ)
    versions = await services.datasets.list_versions(
        organization_id=principal.organization_id, dataset_id=dataset_id
    )
    return [_dataset_version_out(version) for version in versions]


@router.post(
    "/datasets/{dataset_id}/versions",
    response_model=DatasetVersionOut,
    status_code=status.HTTP_201_CREATED,
    tags=["datasets"],
    summary="Register a dataset version from an uploaded file manifest",
    description=(
        "Files must already be in object storage. The version hash covers the "
        "sorted manifest, so verification is cheap and identity is still strong."
    ),
)
async def create_dataset_version(
    dataset_id: str,
    principal: PrincipalDep,
    services: ServicesDep,
    files: list[dict[str, Any]],
    record_schema: dict[str, Any] | None = None,
    splits: dict[str, Any] | None = None,
    commit_message: str | None = None,
) -> DatasetVersionOut:
    principal.require(Permission.DATASET_WRITE)
    version, created = await services.datasets.create_version(
        principal=principal,
        dataset_id=dataset_id,
        files=[
            DatasetFileSpec(
                sequence=int(item["sequence"]),
                object_key=str(item["object_key"]),
                size_bytes=int(item["size_bytes"]),
                checksum=str(item["checksum"]),
                row_count=int(item.get("row_count", 0)),
                content_type=str(item.get("content_type", "application/jsonl")),
                split=item.get("split"),
            )
            for item in files
        ],
        record_schema=record_schema,
        splits=splits,
        commit_message=commit_message,
    )
    if created:
        await services.audit.record(
            principal=principal,
            action=AuditAction.DATASET_VERSION_CREATED,
            resource_type="dataset_version",
            resource_id=version.id,
            metadata={"dataset_id": dataset_id, "rows": version.row_count},
        )
    return _dataset_version_out(version)


@router.get(
    "/datasets/versions/{version_id}/files",
    tags=["datasets"],
    summary="List a dataset version's files",
    description=(
        "Returns the manifest. Reading the rows themselves additionally "
        "requires `dataset:read_samples`, and is audited."
    ),
)
async def list_dataset_files(
    version_id: str, principal: PrincipalDep, services: ServicesDep
) -> dict[str, object]:
    principal.require(Permission.DATASET_READ)
    version, files = await services.datasets.get_version_files(
        organization_id=principal.organization_id, dataset_version_id=version_id
    )
    return {
        "version": _dataset_version_out(version).model_dump(mode="json"),
        "files": [
            {
                "sequence": item.sequence,
                "object_key": item.object_key,
                "content_type": item.content_type,
                "size_bytes": item.size_bytes,
                "checksum": item.checksum,
                "row_count": item.row_count,
                "split": item.split,
            }
            for item in files
        ],
    }
