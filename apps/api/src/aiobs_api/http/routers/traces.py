"""Trace search, detail, retrieval visualisation, trajectory and comparison."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ...core.query import FilterCondition, SortTerm
from ...core.timeutil import unix_nano_to_datetime
from ...domain.rbac import Permission
from ...storage.analytics.schemas import TRACE_SCHEMA
from ..deps import PageDep, PrincipalDep, ServicesDep, TimeRangeDep, query_parser
from ..schemas import (
    AgentStepOut,
    CostRecordOut,
    CursorPage,
    RetrievalDocumentOut,
    RetrievalStageOut,
    SpanEventOut,
    SpanOut,
    TraceDetailOut,
    TraceOut,
    TrajectoryOut,
)

__all__ = ["router"]

router = APIRouter(prefix="/traces", tags=["traces"])

TraceQuery = Annotated[
    tuple[tuple[FilterCondition, ...], tuple[SortTerm, ...]],
    Depends(query_parser(TRACE_SCHEMA)),
]


@router.get(
    "",
    response_model=CursorPage[TraceOut],
    summary="Search traces",
    description=(
        "Keyset-paginated trace search. Combine `filter` parameters freely; "
        "for example `filter=status:eq:error&filter=duration_ms:gte:1000`."
    ),
)
async def search_traces(
    principal: PrincipalDep,
    services: ServicesDep,
    time_range: TimeRangeDep,
    page: PageDep,
    parsed: TraceQuery,
    project_id: Annotated[str, Query(description="Project to search within")],
    environment: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query(description="Free-text search over trace names and ids")] = None,
) -> CursorPage[TraceOut]:
    filters, sort = parsed
    result = await services.traces.search(
        principal=principal,
        project_id=project_id,
        environment=environment,
        start=time_range.start,
        end=time_range.end,
        filters=filters,
        sort=sort,
        page=page,
        text_query=q,
    )
    return CursorPage[TraceOut](
        items=[TraceOut.from_row(row) for row in result.items],
        next_cursor=result.next_cursor,
        has_more=result.has_more,
    )


@router.get(
    "/compare",
    summary="Compare two traces",
    description=(
        "Matches spans across the two traces by structural position "
        "(depth, name, ordinal) rather than by span id, so two runs of the "
        "same request line up."
    ),
)
async def compare_traces(
    principal: PrincipalDep,
    services: ServicesDep,
    project_id: Annotated[str, Query()],
    left: Annotated[str, Query(description="Trace id of the baseline run")],
    right: Annotated[str, Query(description="Trace id of the comparison run")],
) -> dict[str, object]:
    comparison = await services.traces.compare(
        principal=principal,
        project_id=project_id,
        left_trace_id=left,
        right_trace_id=right,
    )
    return {
        "left": TraceOut.from_row(comparison.left).model_dump(mode="json"),
        "right": TraceOut.from_row(comparison.right).model_dump(mode="json"),
        "summary_deltas": comparison.summary_deltas,
        "matched_spans": comparison.matched_spans,
        "only_in_left": comparison.only_in_left,
        "only_in_right": comparison.only_in_right,
        "lineage_differences": comparison.lineage_differences,
    }


@router.get("/{trace_id}", response_model=TraceDetailOut, summary="Get a trace with its spans")
async def get_trace(
    trace_id: str,
    principal: PrincipalDep,
    services: ServicesDep,
    project_id: Annotated[str, Query()],
    environment: Annotated[str | None, Query()] = None,
) -> TraceDetailOut:
    detail = await services.traces.get_detail(
        principal=principal,
        project_id=project_id,
        trace_id=trace_id,
        environment=environment,
    )
    critical = set(detail.critical_path)
    return TraceDetailOut(
        trace=TraceOut.from_row(detail.trace),
        spans=[
            SpanOut.from_row(
                span,
                self_time_ns=detail.self_time_ns.get(span.span_id),
                on_critical_path=span.span_id in critical,
            )
            for span in detail.spans
        ],
        events=[
            SpanEventOut(
                span_id=event.span_id,
                name=event.name,
                time=unix_nano_to_datetime(event.time_unix_nano),
                sequence=event.sequence,
                attributes=dict(event.attributes),
            )
            for event in detail.events
        ],
        cost_records=[
            CostRecordOut(
                span_id=record.span_id,
                provider=record.provider,
                model=record.model,
                currency=record.currency,
                total=str(record.total),
                price_book_version=record.price_book_version,
                estimation_status=record.estimation_status,
                usage_source=record.usage_source,
                components=list(record.components),
                formula=record.formula,
            )
            for record in detail.cost_records
        ],
        critical_path=detail.critical_path,
        children=detail.children,
        orphan_span_ids=detail.orphan_span_ids,
        services=detail.services,
        retry_groups=detail.retry_groups,
    )


@router.get(
    "/{trace_id}/spans",
    response_model=list[SpanOut],
    summary="List a trace's spans",
)
async def list_spans(
    trace_id: str,
    principal: PrincipalDep,
    services: ServicesDep,
    project_id: Annotated[str, Query()],
) -> list[SpanOut]:
    detail = await services.traces.get_detail(
        principal=principal, project_id=project_id, trace_id=trace_id
    )
    critical = set(detail.critical_path)
    return [
        SpanOut.from_row(
            span,
            self_time_ns=detail.self_time_ns.get(span.span_id),
            on_critical_path=span.span_id in critical,
        )
        for span in detail.spans
    ]


@router.get(
    "/{trace_id}/spans/{span_id}",
    summary="Get one span with its events",
)
async def get_span(
    trace_id: str,
    span_id: str,
    principal: PrincipalDep,
    services: ServicesDep,
    project_id: Annotated[str, Query()],
) -> dict[str, object]:
    span, events = await services.traces.get_span(
        principal=principal, project_id=project_id, trace_id=trace_id, span_id=span_id
    )
    return {
        "span": SpanOut.from_row(span).model_dump(mode="json"),
        "events": [
            SpanEventOut(
                span_id=event.span_id,
                name=event.name,
                time=unix_nano_to_datetime(event.time_unix_nano),
                sequence=event.sequence,
                attributes=dict(event.attributes),
            ).model_dump(mode="json")
            for event in events
        ],
    }


@router.get(
    "/{trace_id}/retrieval",
    response_model=list[RetrievalStageOut],
    summary="Retrieval pipeline view",
    description=(
        "One entry per retrieval span, with ranked documents, rerank movement "
        "and diagnostics (unused chunks, score distribution, near-duplicates)."
    ),
)
async def get_retrieval(
    trace_id: str,
    principal: PrincipalDep,
    services: ServicesDep,
    project_id: Annotated[str, Query()],
) -> list[RetrievalStageOut]:
    views = await services.traces.get_retrieval(
        principal=principal, project_id=project_id, trace_id=trace_id
    )
    show_content = principal.can(Permission.TRACE_READ_PAYLOADS)
    return [
        RetrievalStageOut(
            span_id=view.span_id,
            span_name=view.span_name,
            query=view.query,
            rewritten_query=view.rewritten_query,
            retriever_name=view.retriever_name,
            knowledge_base_version=view.knowledge_base_version,
            embedding_model=view.embedding_model,
            search_type=view.search_type,
            latency_ms=view.latency_ms,
            embedding_latency_ms=view.embedding_latency_ms,
            reranker_latency_ms=view.reranker_latency_ms,
            reranker_model=view.reranker_model,
            stages=view.stages,
            documents=[
                RetrievalDocumentOut(
                    document_id=document.document_id,
                    chunk_id=document.chunk_id,
                    rank=document.rank,
                    score=document.score,
                    rerank_score=document.rerank_score,
                    rerank_rank=document.rerank_rank,
                    rank_delta=document.rank_delta,
                    selected=document.selected,
                    token_count=document.token_count,
                    truncated=document.truncated,
                    source=document.source,
                    title=document.title,
                    # Chunk text is payload data; viewers without the payload
                    # permission get metadata and ranking only.
                    content_preview=document.content_preview if show_content else "",
                    content_ref=document.content_ref,
                    metadata=dict(document.metadata),
                )
                for document in view.documents
            ],
            diagnostics=view.diagnostics.as_dict(),
        )
        for view in views
    ]


@router.get(
    "/{trace_id}/trajectory",
    response_model=TrajectoryOut,
    summary="Agent trajectory graph",
    description=(
        "Directed graph of the agent's steps with sequence, branch, retry and "
        "handoff edges, plus the longest-duration path through it."
    ),
)
async def get_trajectory(
    trace_id: str,
    principal: PrincipalDep,
    services: ServicesDep,
    project_id: Annotated[str, Query()],
) -> TrajectoryOut:
    graph, steps = await services.traces.get_trajectory(
        principal=principal, project_id=project_id, trace_id=trace_id
    )
    return TrajectoryOut(
        graph=graph.as_dict(),
        steps=[
            AgentStepOut(
                span_id=step.span_id,
                agent_id=step.agent_id,
                agent_version=step.agent_version,
                step_number=step.step_number,
                parent_step=step.parent_step,
                step_type=step.step_type,
                decision_summary=step.decision_summary,
                tool_name=step.tool_name,
                tool_status=step.tool_status,
                handoff_target=step.handoff_target,
                retry_of=step.retry_of,
                branch_id=step.branch_id,
                loop_iteration=step.loop_iteration,
                approval_required=step.approval_required,
                approval_status=step.approval_status,
                termination_reason=step.termination_reason,
                duration_ms=None if step.duration_ns is None else step.duration_ns / 1e6,
                input_tokens=step.input_tokens,
                output_tokens=step.output_tokens,
                cost=None if step.cost_total is None else str(step.cost_total),
                status=step.status,
                error_message=step.error_message,
            )
            for step in steps
        ],
    )
