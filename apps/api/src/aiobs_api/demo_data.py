"""Deterministic demo telemetry generator.

Produces a realistic mix of traces -- simple completions, RAG pipelines,
multi-step agents and distributed requests -- so that a freshly-installed
platform has something to look at, and so the end-to-end tests have fixtures
whose numbers are known in advance.

Everything is seeded from an explicit integer. The same seed always produces
byte-identical spans, which is what lets a test assert an exact cost rather than
"greater than zero".

It writes through the *real* normalisation, costing and roll-up code rather than
inserting rows directly. A seeded database therefore exercises the same paths
production telemetry does, and a bug in normalisation shows up here first.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from aiobs_schemas.enums import SpanCategory, SpanKind, SpanStatus
from aiobs_schemas.ids import generate_span_id, generate_trace_id
from aiobs_schemas.wire import (
    AgentStepPayload,
    LineagePayload,
    ResourceDescriptor,
    RetrievalDocument,
    RetrievalPayload,
    SpanEvent,
    TokenUsage,
    WireSpan,
)

from .core.logging import get_logger
from .ingest.normalizer import IngestScope
from .services.bundle import ServiceBundle

__all__ = ["generate_demo_data"]

log = get_logger(__name__)

_MODELS = (
    ("mock", "mock-model-v1", 1.0),
    ("openai", "gpt-4o", 0.9),
    ("openai", "gpt-4o-mini", 0.4),
    ("anthropic", "claude-sonnet-4", 1.1),
    ("anthropic", "claude-haiku-4-5", 0.5),
)

_REQUEST_NAMES = (
    "customer-support-request",
    "document-summarisation",
    "code-review-assistant",
    "sales-email-drafting",
    "knowledge-base-search",
)

_TOOLS = ("search_orders", "lookup_customer", "issue_refund", "escalate_to_human", "send_email")

_DOCUMENT_TITLES = (
    "Refund policy",
    "Shipping timelines",
    "Warranty coverage",
    "Account recovery",
    "Subscription tiers",
    "Data retention policy",
    "Enterprise SLA",
    "Returns process",
)


@dataclass(frozen=True, slots=True)
class _RegistryFixtures:
    """Real registry ids for the demo traces to reference.

    Without these the generator emits invented lineage ids like ``pmv_DEMO0001``
    that resolve to nothing, and the registry screens sit empty while every
    trace claims a prompt version. Seeding the registries for real means the
    lineage links in the UI actually go somewhere, and a version comparison has
    two versions to compare.
    """

    #: prompt name -> ordered version ids, oldest first
    prompt_versions: dict[str, tuple[str, ...]]
    #: "provider/model" -> configuration version id
    model_configs: dict[str, str]
    #: ordered dataset version ids
    dataset_versions: tuple[str, ...]

    def prompt_version(self, name: str, index: int) -> str | None:
        versions = self.prompt_versions.get(name)
        return versions[index % len(versions)] if versions else None

    def model_config(self, provider: str, model: str) -> str | None:
        return self.model_configs.get(f"{provider}/{model}")

    def dataset_version(self, index: int) -> str | None:
        if not self.dataset_versions:
            return None
        return self.dataset_versions[index % len(self.dataset_versions)]

    @classmethod
    def empty(cls) -> _RegistryFixtures:
        return cls(prompt_versions={}, model_configs={}, dataset_versions=())


@dataclass(slots=True)
class _Builder:
    """Accumulates the spans of one synthetic trace."""

    trace_id: str
    start: datetime
    spans: list[WireSpan]
    registry: _RegistryFixtures

    def add(self, span: WireSpan) -> WireSpan:
        self.spans.append(span)
        return span


def _nano(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000_000)


async def generate_demo_data(
    *,
    services: ServiceBundle,
    project_id: str,
    environment: str = "development",
    traces: int = 120,
    seed: int = 1234,
    organization_id: str | None = None,
) -> dict[str, int]:
    """Generate and persist ``traces`` synthetic traces. Returns counters."""
    rng = random.Random(seed)
    container = services.container

    resolved_organization = organization_id
    if resolved_organization is None:
        from sqlalchemy import select

        from .storage.postgres.models import Project

        async with container.database.session_scope() as session:
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            if project is None:
                raise ValueError(f"project {project_id!r} does not exist")
            resolved_organization = project.organization_id

    scope = IngestScope(
        organization_id=resolved_organization,
        project_id=project_id,
        environment=environment,
        environment_id="",
        sampling_rate=1.0,
        store_payloads=True,
    )

    fixtures = await _seed_registries(
        services=services,
        organization_id=resolved_organization,
        project_id=project_id,
    )

    now = datetime.now(timezone.utc)
    span_total = 0

    for index in range(traces):
        # Spread traces over the last 24 hours so the dashboards have shape.
        offset = timedelta(seconds=rng.uniform(0, 24 * 3600))
        started = now - offset
        shape = rng.choices(("simple", "rag", "agent", "distributed"), weights=(35, 30, 25, 10))[0]
        builder = _Builder(trace_id=generate_trace_id(), start=started, spans=[], registry=fixtures)

        if shape == "simple":
            _build_simple(builder, rng, index)
        elif shape == "rag":
            _build_rag(builder, rng, index)
        elif shape == "agent":
            _build_agent(builder, rng, index)
        else:
            _build_distributed(builder, rng, index)

        resource = ResourceDescriptor(
            service_name={"distributed": "api-gateway"}.get(shape, "ai-application"),
            service_version="1.4.2",
            service_instance_id=f"instance-{index % 3}",
            environment=environment,
            sdk_name="aiobs-demo-generator",
            sdk_version="0.1.0",
            sdk_language="python",
        )
        span_total += await _persist(services, builder.spans, resource, scope)

    log.info("demo.seeded", traces=traces, spans=span_total, project_id=project_id)
    return {"traces": traces, "spans": span_total}


_DEMO_PROMPTS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "support-reply",
        "Answers a customer support question in the brand voice.",
        (
            (
                "You are a support agent for {company}. Answer in at most three "
                "sentences. If you are not certain, say so and offer to escalate.",
                "initial version",
            ),
            (
                "You are a support agent for {company}. Answer in at most three "
                "sentences. Never promise a refund; instead describe the policy and "
                "offer to escalate. If you are not certain, say so.",
                "stop promising refunds (incident 2026-05-14)",
            ),
            (
                "You are a support agent for {company}. Answer in at most three "
                "sentences using only the supplied context. Never promise a refund; "
                "describe the policy and offer to escalate. Cite the section you "
                "used. If the context does not answer the question, say so.",
                "require citations",
            ),
        ),
    ),
    (
        "rag-answer",
        "Answers from retrieved knowledge-base passages.",
        (
            (
                "Answer the question using only the passages below.\n\n"
                "Passages:\n{context}\n\nQuestion: {question}",
                "initial version",
            ),
            (
                "Answer the question using only the passages below. Quote the "
                "passage number you relied on. If the passages do not contain the "
                "answer, reply exactly: I don't have that information.\n\n"
                "Passages:\n{context}\n\nQuestion: {question}",
                "add refusal path and citations",
            ),
        ),
    ),
)


async def _seed_registries(
    *,
    services: ServiceBundle,
    organization_id: str,
    project_id: str,
) -> _RegistryFixtures:
    """Register the prompts, models and datasets the demo traces refer to.

    Idempotent: re-seeding converges on the same versions because every registry
    is content-addressed, so running the seeder twice does not double the
    version history.
    """
    from sqlalchemy import select

    from .domain.principal import Principal
    from .domain.rbac import Role
    from .services.registry import DatasetFileSpec, PromptVersionInput
    from .storage.postgres.models import Membership

    async with services.container.database.session_scope() as session:
        membership = (
            (
                await session.execute(
                    select(Membership)
                    .where(Membership.organization_id == organization_id)
                    .order_by(Membership.created_at)
                )
            )
            .scalars()
            .first()
        )
    if membership is None:
        log.warning("demo.no_member", organization_id=organization_id)
        return _RegistryFixtures.empty()

    principal = Principal.for_user(
        user_id=membership.user_id,
        email="seed@localhost",
        organization_id=organization_id,
        role=Role.OWNER,
    )

    prompt_versions: dict[str, tuple[str, ...]] = {}
    for name, description, revisions in _DEMO_PROMPTS:
        prompt = await _get_or_create_prompt(
            services=services,
            principal=principal,
            project_id=project_id,
            name=name,
            description=description,
        )
        if prompt is None:
            continue
        version_ids: list[str] = []
        for text, message in revisions:
            version, _ = await services.prompts.create_version(
                principal=principal,
                prompt_id=prompt.id,
                spec=PromptVersionInput(
                    messages=[
                        {"role": "system", "content": text},
                        {"role": "user", "content": "{question}"},
                    ],
                    variable_schema={
                        "company": {"type": "string"},
                        "question": {"type": "string"},
                        **({"context": {"type": "string"}} if "{context}" in text else {}),
                    },
                    template_engine="fstring",
                    commit_message=message,
                ),
                publish=True,
            )
            version_ids.append(version.id)
        prompt_versions[name] = tuple(version_ids)
        if version_ids:
            # The alias points at the *previous* version, not the newest: that
            # is what a real registry looks like mid-rollout, and it gives the
            # UI a rollback target to display.
            await services.prompts.promote_alias(
                principal=principal,
                prompt_id=prompt.id,
                alias="production",
                target_version_id=version_ids[-2] if len(version_ids) > 1 else version_ids[-1],
            )

    model_configs: dict[str, str] = {}
    for provider, model, _factor in _MODELS:
        definition = await services.models.ensure_model(
            principal=principal,
            provider=provider,
            model_identifier=model,
            project_id=project_id,
            endpoint_kind="chat",
        )
        version, _ = await services.models.create_version(
            principal=principal,
            model_id=definition.id,
            config={
                "temperature": 0.2,
                "top_p": 1.0,
                "max_tokens": 2048,
                "stop_sequences": [],
                "region": "us-east-1",
            },
        )
        model_configs[f"{provider}/{model}"] = version.id

    dataset_versions: tuple[str, ...] = ()
    dataset = await _get_or_create_dataset(
        services=services,
        principal=principal,
        project_id=project_id,
        name="support-eval",
    )
    if dataset is not None:
        version, _ = await services.datasets.create_version(
            principal=principal,
            dataset_id=dataset.id,
            files=[
                DatasetFileSpec(
                    sequence=0,
                    # A demo manifest: the platform records the manifest, it does
                    # not claim to have the bytes.
                    object_key=f"datasets/{dataset.id}/support-eval-v1.jsonl",
                    size_bytes=184_320,
                    checksum="sha256:" + "0" * 64,
                    row_count=240,
                    split="test",
                )
            ],
            splits={"test": 240},
            labels=["helpfulness", "policy_compliance"],
            source="synthetic demo manifest",
            commit_message="initial evaluation set",
        )
        dataset_versions = (version.id,)

    log.info(
        "demo.registries_seeded",
        prompts=len(prompt_versions),
        models=len(model_configs),
        datasets=len(dataset_versions),
    )
    return _RegistryFixtures(
        prompt_versions=prompt_versions,
        model_configs=model_configs,
        dataset_versions=dataset_versions,
    )


async def _get_or_create_prompt(
    *,
    services: ServiceBundle,
    principal: Any,
    project_id: str,
    name: str,
    description: str,
) -> Any:
    """Create the prompt, or return the existing one on a re-run."""
    from .core.errors import ConflictError

    try:
        return await services.prompts.create_prompt(
            principal=principal, project_id=project_id, name=name, description=description
        )
    except ConflictError:
        prompts = await services.prompts.list_prompts(
            organization_id=principal.organization_id, project_id=project_id
        )
        return next((prompt for prompt in prompts if prompt.name == name), None)


async def _get_or_create_dataset(
    *, services: ServiceBundle, principal: Any, project_id: str, name: str
) -> Any:
    from .core.errors import ConflictError

    try:
        return await services.datasets.create_dataset(
            principal=principal,
            project_id=project_id,
            name=name,
            description="Held-out support questions with reviewed answers.",
            license_text="CC-BY-4.0",
            # Synthetic, but the flag defaults to "assume yes" and the demo
            # should not model turning that off casually.
            contains_sensitive_data=False,
        )
    except ConflictError:
        datasets = await services.datasets.list_datasets(
            organization_id=principal.organization_id, project_id=project_id
        )
        return next((dataset for dataset in datasets if dataset.name == name), None)


async def _persist(
    services: ServiceBundle,
    spans: Sequence[WireSpan],
    resource: ResourceDescriptor,
    scope: IngestScope,
) -> int:
    """Normalise, cost and store one trace's spans."""
    from .ingest.normalizer import normalize_batch
    from .ingest.rollup import build_trace_rollup

    normalizer = services.ingestion._normalizer
    normalized, failures = normalize_batch(normalizer, list(spans), resource, scope)
    if failures:
        log.warning("demo.normalization_failures", failures=failures[:3])

    calculator = await services.pricing.calculator_for(scope.organization_id)
    rows = []
    events = []
    documents = []
    steps = []
    costs = []
    for item in normalized:
        cost_row = services.span_processor._apply_cost(item.span, calculator)
        if cost_row is not None:
            costs.append(cost_row)
        rows.append(item.span)
        events.extend(item.events)
        documents.extend(item.retrieval_documents)
        for step in item.agent_steps:
            step.cost_total = item.span.cost_total
            steps.append(step)

    analytics = services.container.analytics
    await analytics.insert_spans(rows)
    if events:
        await analytics.insert_span_events(events)
    if documents:
        await analytics.insert_retrieval_documents(documents)
    if steps:
        await analytics.insert_agent_steps(steps)
    if costs:
        await analytics.insert_cost_records(costs)

    rollup = build_trace_rollup(rows, clock=services.container.clock)
    if rollup is not None:
        await analytics.upsert_traces([rollup])
    return len(rows)


def _pick_model(rng: random.Random) -> tuple[str, str, float]:
    return rng.choice(_MODELS)


def _usage(rng: random.Random, *, cached: bool = False) -> TokenUsage:
    input_tokens = rng.randint(180, 3_200)
    output_tokens = rng.randint(40, 900)
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=rng.randint(0, input_tokens // 2) if cached else None,
        raw={"prompt_tokens": input_tokens, "completion_tokens": output_tokens},
    )


def _root(
    builder: _Builder, name: str, duration_ms: float, *, status: SpanStatus = SpanStatus.OK
) -> WireSpan:
    span_id = generate_span_id()
    return builder.add(
        WireSpan(
            trace_id=builder.trace_id,
            span_id=span_id,
            parent_span_id=None,
            name=name,
            kind=SpanKind.SERVER,
            category=SpanCategory.WORKFLOW_STEP,
            start_time_unix_nano=_nano(builder.start),
            end_time_unix_nano=_nano(builder.start + timedelta(milliseconds=duration_ms)),
            status=status,
            trace_name=name,
            session_id=f"session-{abs(hash(builder.trace_id)) % 500}",
            subject_id=f"user-{abs(hash(builder.trace_id)) % 2000}",
            tags=["demo", name.split("-")[0]],
            lineage=LineagePayload(release="1.4.2", git_commit="a1b2c3d4"),
        )
    )


def _child(
    builder: _Builder,
    parent: WireSpan,
    name: str,
    category: SpanCategory,
    offset_ms: float,
    duration_ms: float,
    **kwargs: Any,
) -> WireSpan:
    start = builder.start + timedelta(milliseconds=offset_ms)
    return builder.add(
        WireSpan(
            trace_id=builder.trace_id,
            span_id=generate_span_id(),
            parent_span_id=parent.span_id,
            name=name,
            kind=kwargs.pop("kind", SpanKind.CLIENT),
            category=category,
            start_time_unix_nano=_nano(start),
            end_time_unix_nano=_nano(start + timedelta(milliseconds=duration_ms)),
            **kwargs,
        )
    )


def _model_attributes(provider: str, model: str, temperature: float = 0.2) -> dict[str, Any]:
    from aiobs_schemas import semconv

    return {
        semconv.GEN_AI_SYSTEM: provider,
        semconv.GEN_AI_REQUEST_MODEL: model,
        semconv.GEN_AI_RESPONSE_MODEL: model,
        semconv.GEN_AI_OPERATION_NAME: "chat",
        semconv.GEN_AI_REQUEST_TEMPERATURE: temperature,
        semconv.GEN_AI_REQUEST_MAX_TOKENS: 2048,
    }


def _build_simple(builder: _Builder, rng: random.Random, index: int) -> None:
    provider, model, factor = _pick_model(rng)
    total = rng.uniform(400, 2_600) * factor
    failed = rng.random() < 0.06
    root = _root(
        builder,
        rng.choice(_REQUEST_NAMES),
        total,
        status=SpanStatus.ERROR if failed else SpanStatus.OK,
    )
    _child(
        builder,
        root,
        "render-prompt",
        SpanCategory.PROMPT_RENDER,
        5,
        rng.uniform(0.4, 3.0),
        lineage=LineagePayload(
            prompt_name="support-reply",
            prompt_version_id=builder.registry.prompt_version("support-reply", index),
            prompt_version_label=f"v{index % 3 + 1}",
        ),
    )
    generation = _child(
        builder,
        root,
        f"{provider}.chat",
        SpanCategory.CHAT_COMPLETION,
        12,
        total - 20,
        usage=_usage(rng, cached=rng.random() < 0.3),
        attributes={
            **_model_attributes(provider, model),
            "aiobs.latency.time_to_first_token_ms": round(rng.uniform(120, 900), 1),
        },
        status=SpanStatus.ERROR if failed else SpanStatus.OK,
        status_message="provider returned 529 overloaded" if failed else None,
    )
    generation.events.append(
        SpanEvent(
            name="aiobs.first_token",
            time_unix_nano=_nano(builder.start + timedelta(milliseconds=180)),
            attributes={"index": 0},
        )
    )


def _build_rag(builder: _Builder, rng: random.Random, index: int) -> None:
    provider, model, factor = _pick_model(rng)
    total = rng.uniform(900, 4_200) * factor
    root = _root(builder, "knowledge-base-search", total)

    _child(
        builder,
        root,
        "rewrite-query",
        SpanCategory.LLM_GENERATION,
        8,
        rng.uniform(80, 260),
        usage=TokenUsage(input_tokens=rng.randint(20, 90), output_tokens=rng.randint(8, 40)),
        attributes=_model_attributes("openai", "gpt-4o-mini"),
    )
    _child(
        builder,
        root,
        "embed-query",
        SpanCategory.EMBEDDING,
        70,
        rng.uniform(20, 90),
        usage=TokenUsage(input_tokens=rng.randint(20, 90), output_tokens=0),
        attributes={
            "gen_ai.system": "openai",
            "gen_ai.request.model": "text-embedding-3-small",
            "gen_ai.operation.name": "embeddings",
        },
    )

    document_count = rng.randint(4, 10)
    selected = rng.randint(2, min(5, document_count))
    documents: list[RetrievalDocument] = []
    for rank in range(document_count):
        score = round(0.94 - rank * rng.uniform(0.02, 0.08), 4)
        rerank_rank = rank
        if rng.random() < 0.4:
            rerank_rank = max(0, min(document_count - 1, rank + rng.choice((-2, -1, 1, 2))))
        documents.append(
            RetrievalDocument(
                document_id=f"doc-{(index + rank) % 40:03d}",
                chunk_id=f"doc-{(index + rank) % 40:03d}#chunk-{rank}",
                rank=rank,
                score=score,
                rerank_score=round(score + rng.uniform(-0.05, 0.05), 4),
                rerank_rank=rerank_rank,
                source=f"https://docs.example.com/{(index + rank) % 40}",
                title=_DOCUMENT_TITLES[(index + rank) % len(_DOCUMENT_TITLES)],
                content=(
                    f"{_DOCUMENT_TITLES[(index + rank) % len(_DOCUMENT_TITLES)]}: "
                    "customers may request a refund within 30 days of delivery, "
                    "provided the item is unused and in its original packaging."
                ),
                selected=rank < selected,
                token_count=rng.randint(90, 420),
                metadata={"section": f"{rank + 1}", "language": "en"},
            )
        )

    _child(
        builder,
        root,
        "vector-search",
        SpanCategory.RETRIEVAL,
        160,
        rng.uniform(40, 220),
        retrieval=RetrievalPayload(
            query="how do refunds work for damaged items?",
            rewritten_query="refund policy damaged items 30 days",
            retriever_name="pgvector-primary",
            retriever_version="2024.11",
            knowledge_base_version="kb-2026-07",
            search_type="hybrid",
            top_k=document_count,
            embedding_model="text-embedding-3-small",
            embedding_dimensions=1536,
            embedding_latency_ms=round(rng.uniform(18, 70), 2),
            reranker_model="mock-reranker-v1",
            reranker_latency_ms=round(rng.uniform(30, 120), 2),
            retrieval_latency_ms=round(rng.uniform(35, 190), 2),
            context_tokens=sum(
                document.token_count or 0 for document in documents if document.selected
            ),
            context_truncated=rng.random() < 0.15,
            documents=documents,
        ),
    )

    _child(
        builder,
        root,
        f"{provider}.chat",
        SpanCategory.CHAT_COMPLETION,
        420,
        total - 460,
        usage=_usage(rng, cached=True),
        attributes={
            **_model_attributes(provider, model),
            "aiobs.latency.time_to_first_token_ms": round(rng.uniform(200, 1_100), 1),
        },
        lineage=LineagePayload(
            prompt_name="rag-answer",
            prompt_version_id=builder.registry.prompt_version("rag-answer", index),
            model_config_id=builder.registry.model_config(provider, model),
            dataset_name="support-eval",
            dataset_version_id=builder.registry.dataset_version(index),
            knowledge_base_version="kb-2026-07",
        ),
    )


def _build_agent(builder: _Builder, rng: random.Random, index: int) -> None:
    provider, model, factor = _pick_model(rng)
    steps = rng.randint(3, 7)
    total = rng.uniform(1_800, 9_000) * factor
    root = _root(builder, "multi-step-agent", total)
    agent_id = f"support-agent-{index % 3}"
    offset = 20.0
    terminated = "completed"

    for step_number in range(steps):
        tool = rng.choice(_TOOLS)
        failing = rng.random() < 0.18
        duration = rng.uniform(120, 900)

        _child(
            builder,
            root,
            f"agent.decide[{step_number}]",
            SpanCategory.AGENT_DECISION,
            offset,
            rng.uniform(150, 700),
            usage=_usage(rng),
            attributes=_model_attributes(provider, model, temperature=0.0),
            agent_step=AgentStepPayload(
                agent_id=agent_id,
                agent_version="2.1.0",
                goal="Resolve the customer's refund request",
                step_number=step_number,
                parent_step=step_number - 1 if step_number else None,
                step_type="decision",
                decision_summary=f"Call {tool} to gather the missing information",
                max_steps=8,
            ),
        )
        offset += 60

        _child(
            builder,
            root,
            f"tool.{tool}",
            SpanCategory.TOOL_CALL,
            offset,
            duration,
            status=SpanStatus.ERROR if failing else SpanStatus.OK,
            status_message="tool timed out after 5s" if failing else None,
            agent_step=AgentStepPayload(
                agent_id=agent_id,
                step_number=step_number,
                step_type="tool_call",
                tool_name=tool,
                tool_status="timeout" if failing else "ok",
                tool_arguments={"order_id": f"ORD-{index:05d}"},
                max_steps=8,
            ),
        )
        offset += duration + 20

        if failing:
            # Retry the same tool once, which is what the graph's retry edge
            # renders from.
            _child(
                builder,
                root,
                f"tool.{tool}",
                SpanCategory.TOOL_CALL,
                offset,
                duration * 0.6,
                agent_step=AgentStepPayload(
                    agent_id=agent_id,
                    step_number=step_number + 100,
                    step_type="tool_call",
                    tool_name=tool,
                    tool_status="ok",
                    retry_of=step_number,
                    max_steps=8,
                ),
            )
            offset += duration * 0.6 + 20

        if tool == "escalate_to_human":
            terminated = "approval_timeout" if rng.random() < 0.4 else "completed"
            _child(
                builder,
                root,
                "human-approval",
                SpanCategory.AGENT_DECISION,
                offset,
                rng.uniform(500, 4_000),
                agent_step=AgentStepPayload(
                    agent_id=agent_id,
                    step_number=step_number + 200,
                    step_type="approval",
                    approval_required=True,
                    approval_status="timeout" if terminated == "approval_timeout" else "approved",
                    max_steps=8,
                ),
            )
            break

    _child(
        builder,
        root,
        "agent.terminate",
        SpanCategory.AGENT_DECISION,
        offset + 30,
        5,
        agent_step=AgentStepPayload(
            agent_id=agent_id,
            step_number=999,
            step_type="terminate",
            termination_reason=terminated,
            max_steps=8,
        ),
    )


def _build_distributed(builder: _Builder, rng: random.Random, index: int) -> None:
    """A trace spanning three services, with a queue hop and a span link."""
    provider, model, factor = _pick_model(rng)
    total = rng.uniform(1_200, 5_000) * factor
    root = _root(builder, "distributed-ai-request", total)

    gateway = _child(
        builder,
        root,
        "POST /api/assist",
        SpanCategory.HTTP_REQUEST,
        2,
        total - 10,
        kind=SpanKind.SERVER,
        attributes={
            "http.request.method": "POST",
            "http.response.status_code": 200,
            "service.name": "api-gateway",
        },
    )
    enqueue = _child(
        builder,
        gateway,
        "publish assist.requests",
        SpanCategory.QUEUE_OPERATION,
        20,
        rng.uniform(2, 15),
        kind=SpanKind.PRODUCER,
        attributes={
            "messaging.system": "redpanda",
            "messaging.destination.name": "assist.requests",
        },
    )
    worker = _child(
        builder,
        enqueue,
        "consume assist.requests",
        SpanCategory.QUEUE_OPERATION,
        60,
        total - 120,
        kind=SpanKind.CONSUMER,
        attributes={
            "messaging.system": "redpanda",
            "messaging.destination.name": "assist.requests",
            "service.name": "inference-worker",
        },
    )
    _child(
        builder,
        worker,
        f"{provider}.chat",
        SpanCategory.CHAT_COMPLETION,
        140,
        total - 260,
        usage=_usage(rng),
        attributes={
            **_model_attributes(provider, model),
            "service.name": "inference-worker",
        },
    )
