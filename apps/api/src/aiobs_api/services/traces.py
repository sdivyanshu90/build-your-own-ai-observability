"""Trace query service.

Assembles the read models the UI needs: the explorer list, the trace detail with
its span tree and critical path, the retrieval pipeline view, the agent
trajectory graph, and the two-trace comparison.

Every method takes a :class:`Principal` and builds its
:class:`AnalyticsScope` from it, so the tenant predicate is never a parameter a
caller could forget.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ..core.errors import NotFoundError, ValidationFailedError
from ..core.logging import get_logger
from ..core.query import FilterCondition, Page, PageRequest, SortTerm
from ..core.timeutil import Clock, unix_nano_to_datetime
from ..domain.analysis import (
    AgentGraph,
    RetrievalDiagnostics,
    build_agent_graph,
    retrieval_diagnostics,
)
from ..domain.principal import Principal
from ..domain.rbac import Permission
from ..ingest.rollup import critical_path, self_time_ns
from ..storage.analytics.protocol import AnalyticsStore
from ..storage.analytics.rows import (
    AgentStepRow,
    AnalyticsScope,
    CostRecordRow,
    RetrievalDocumentRow,
    SpanEventRow,
    SpanRow,
    TraceRow,
)

__all__ = ["RetrievalStageView", "TraceComparison", "TraceDetail", "TraceService"]

log = get_logger(__name__)


@dataclass(slots=True)
class TraceDetail:
    """Everything the trace detail page renders."""

    trace: TraceRow
    spans: list[SpanRow]
    events: list[SpanEventRow]
    cost_records: list[CostRecordRow]
    critical_path: list[str]
    self_time_ns: dict[str, int]
    #: Adjacency for the waterfall's collapse controls.
    children: dict[str, list[str]]
    orphan_span_ids: list[str] = field(default_factory=list)
    #: Distinct services, in first-seen order, for the service-boundary bands.
    services: list[str] = field(default_factory=list)
    retry_groups: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class RetrievalStageView:
    """One retrieval span rendered as a pipeline stage plus its documents."""

    span_id: str
    span_name: str
    query: str
    rewritten_query: str
    retriever_name: str
    knowledge_base_version: str
    embedding_model: str
    search_type: str
    latency_ms: float | None
    embedding_latency_ms: float | None
    reranker_latency_ms: float | None
    reranker_model: str
    documents: list[RetrievalDocumentRow]
    diagnostics: RetrievalDiagnostics
    #: Ordered stage descriptors: query rewrite, embed, retrieve, rerank, select.
    stages: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class TraceComparison:
    """Side-by-side difference between two traces."""

    left: TraceRow
    right: TraceRow
    summary_deltas: dict[str, Any]
    #: Spans matched by name and depth; unmatched ones appear on one side only.
    matched_spans: list[dict[str, Any]]
    only_in_left: list[str]
    only_in_right: list[str]
    lineage_differences: dict[str, Any]


class TraceService:
    """Read-side queries over the analytics store."""

    def __init__(self, *, analytics: AnalyticsStore, clock: Clock) -> None:
        self._analytics = analytics
        self._clock = clock

    def _scope(
        self, principal: Principal, *, project_id: str, environment: str | None = None
    ) -> AnalyticsScope:
        principal.require_project(project_id)
        return AnalyticsScope(
            organization_id=principal.organization_id,
            project_id=project_id,
            environment=environment,
        )

    async def search(
        self,
        *,
        principal: Principal,
        project_id: str,
        environment: str | None,
        start: datetime,
        end: datetime,
        filters: Sequence[FilterCondition] = (),
        sort: Sequence[SortTerm] = (),
        page: PageRequest | None = None,
        text_query: str | None = None,
    ) -> Page[TraceRow]:
        principal.require(Permission.TRACE_READ)
        if end <= start:
            raise ValidationFailedError("the end of the time range must be after its start")
        if (end - start) > timedelta(days=400):
            raise ValidationFailedError(
                "time ranges longer than 400 days are not supported; "
                "narrow the range or use an export"
            )
        return await self._analytics.search_traces(
            self._scope(principal, project_id=project_id, environment=environment),
            start=start,
            end=end,
            filters=filters,
            sort=sort,
            page=page,
            text_query=text_query,
        )

    async def get_detail(
        self,
        *,
        principal: Principal,
        project_id: str,
        trace_id: str,
        environment: str | None = None,
    ) -> TraceDetail:
        principal.require(Permission.TRACE_READ)
        scope = self._scope(principal, project_id=project_id, environment=environment)
        trace = await self._analytics.get_trace(scope, trace_id)
        if trace is None:
            raise NotFoundError("trace", trace_id)

        spans = await self._analytics.get_spans(scope, trace_id)
        events = await self._analytics.get_span_events(scope, trace_id)
        costs = await self._analytics.get_cost_records(scope, trace_id)

        known_ids = {span.span_id for span in spans}
        children: dict[str, list[str]] = {}
        orphans: list[str] = []
        for span in spans:
            parent = span.parent_span_id or ""
            if parent and parent not in known_ids:
                # The parent has not arrived (or was sampled away). Re-root the
                # span so the waterfall still renders it rather than hiding it.
                orphans.append(span.span_id)
                parent = ""
            children.setdefault(parent, []).append(span.span_id)

        services: list[str] = []
        for span in spans:
            if span.service_name and span.service_name not in services:
                services.append(span.service_name)

        return TraceDetail(
            trace=trace,
            spans=spans,
            events=events,
            cost_records=costs,
            critical_path=critical_path(spans),
            self_time_ns=self_time_ns(spans),
            children=children,
            orphan_span_ids=orphans,
            services=services,
            retry_groups=_retry_groups(spans),
        )

    async def get_span(
        self, *, principal: Principal, project_id: str, trace_id: str, span_id: str
    ) -> tuple[SpanRow, list[SpanEventRow]]:
        principal.require(Permission.SPAN_READ)
        scope = self._scope(principal, project_id=project_id)
        span = await self._analytics.get_span(scope, trace_id, span_id)
        if span is None:
            raise NotFoundError("span", span_id)
        events = await self._analytics.get_span_events(scope, trace_id, span_id)
        return span, events

    async def get_retrieval(
        self, *, principal: Principal, project_id: str, trace_id: str
    ) -> list[RetrievalStageView]:
        """Assemble the retrieval pipeline view for a trace."""
        principal.require(Permission.TRACE_READ)
        scope = self._scope(principal, project_id=project_id)
        documents = await self._analytics.get_retrieval_documents(scope, trace_id)
        spans = await self._analytics.get_spans(scope, trace_id)
        spans_by_id = {span.span_id: span for span in spans}

        grouped: dict[str, list[RetrievalDocumentRow]] = {}
        for document in documents:
            grouped.setdefault(document.span_id, []).append(document)

        # Retrieval spans with zero documents still matter: an empty retrieval
        # is one of the most important failures to surface.
        for span in spans:
            if span.category in {"retrieval", "rerank"} and span.span_id not in grouped:
                grouped[span.span_id] = []

        views: list[RetrievalStageView] = []
        for span_id, span_documents in grouped.items():
            # Deliberately a new name: `span` is already bound above with a
            # non-optional type, and reusing it hides the None case from the
            # type checker.
            retrieval_span = spans_by_id.get(span_id)
            if retrieval_span is None:
                continue
            span_documents.sort(key=lambda document: document.rank)
            first = span_documents[0] if span_documents else None
            attributes = retrieval_span.attributes or {}
            view = RetrievalStageView(
                span_id=span_id,
                span_name=retrieval_span.name,
                query=(first.query if first else "")
                or str(attributes.get("aiobs.retrieval.query", "")),
                rewritten_query=(first.rewritten_query if first else ""),
                retriever_name=retrieval_span.retriever_name
                or (first.retriever_name if first else ""),
                knowledge_base_version=(first.knowledge_base_version if first else "")
                or retrieval_span.knowledge_base_version,
                embedding_model=(first.embedding_model if first else ""),
                search_type=(first.search_type if first else ""),
                latency_ms=retrieval_span.duration_ms,
                embedding_latency_ms=_float_attr(
                    attributes, "aiobs.retrieval.embedding.latency_ms"
                ),
                reranker_latency_ms=_float_attr(attributes, "aiobs.retrieval.reranker.latency_ms"),
                reranker_model=str(attributes.get("aiobs.retrieval.reranker.model", "")),
                documents=span_documents,
                diagnostics=retrieval_diagnostics(span_documents),
            )
            view.stages = _retrieval_stages(view)
            views.append(view)

        views.sort(key=lambda item: spans_by_id[item.span_id].start_unix_nano)
        return views

    async def get_trajectory(
        self, *, principal: Principal, project_id: str, trace_id: str
    ) -> tuple[AgentGraph, list[AgentStepRow]]:
        principal.require(Permission.TRACE_READ)
        scope = self._scope(principal, project_id=project_id)
        steps = await self._analytics.get_agent_steps(scope, trace_id)
        spans = await self._analytics.get_spans(scope, trace_id)
        return build_agent_graph(steps, spans), steps

    async def compare(
        self,
        *,
        principal: Principal,
        project_id: str,
        left_trace_id: str,
        right_trace_id: str,
    ) -> TraceComparison:
        """Compare two traces structurally and numerically.

        Spans are matched by ``(depth, name, ordinal)`` rather than by span id,
        because two runs of the same request have different ids but the same
        shape. Where the shapes diverge, the unmatched spans are reported
        explicitly -- that divergence is usually the answer to "why is this run
        different".
        """
        principal.require(Permission.TRACE_READ)
        scope = self._scope(principal, project_id=project_id)
        traces = await self._analytics.get_traces(scope, [left_trace_id, right_trace_id])
        by_id = {trace.trace_id: trace for trace in traces}
        left = by_id.get(left_trace_id)
        right = by_id.get(right_trace_id)
        if left is None:
            raise NotFoundError("trace", left_trace_id)
        if right is None:
            raise NotFoundError("trace", right_trace_id)

        left_spans = await self._analytics.get_spans(scope, left_trace_id)
        right_spans = await self._analytics.get_spans(scope, right_trace_id)

        left_keyed = _structural_keys(left_spans)
        right_keyed = _structural_keys(right_spans)

        matched: list[dict[str, Any]] = []
        for key, left_span in left_keyed.items():
            right_span = right_keyed.get(key)
            if right_span is None:
                continue
            matched.append(
                {
                    "key": "/".join(str(part) for part in key),
                    "name": left_span.name,
                    "left_span_id": left_span.span_id,
                    "right_span_id": right_span.span_id,
                    "left_duration_ms": left_span.duration_ms,
                    "right_duration_ms": right_span.duration_ms,
                    "duration_delta_ms": _delta(left_span.duration_ms, right_span.duration_ms),
                    "left_tokens": left_span.total_tokens,
                    "right_tokens": right_span.total_tokens,
                    "token_delta": _delta(left_span.total_tokens, right_span.total_tokens),
                    "left_status": left_span.status,
                    "right_status": right_span.status,
                    "status_changed": left_span.status != right_span.status,
                    "left_model": left_span.model,
                    "right_model": right_span.model,
                    "model_changed": left_span.model != right_span.model,
                }
            )

        return TraceComparison(
            left=left,
            right=right,
            summary_deltas={
                "duration_ms": _delta(_ns_to_ms(left.duration_ns), _ns_to_ms(right.duration_ns)),
                "span_count": _delta(left.span_count, right.span_count),
                "total_tokens": _delta(left.total_tokens, right.total_tokens),
                "input_tokens": _delta(left.total_input_tokens, right.total_input_tokens),
                "output_tokens": _delta(left.total_output_tokens, right.total_output_tokens),
                "cost": _decimal_delta(left.total_cost, right.total_cost),
                "error_count": _delta(left.error_count, right.error_count),
                "time_to_first_token_ms": _delta(
                    left.time_to_first_token_ms, right.time_to_first_token_ms
                ),
            },
            matched_spans=matched,
            only_in_left=[span.name for key, span in left_keyed.items() if key not in right_keyed],
            only_in_right=[span.name for key, span in right_keyed.items() if key not in left_keyed],
            lineage_differences={
                "prompt_version_ids": {
                    "left": left.prompt_version_ids,
                    "right": right.prompt_version_ids,
                    "changed": set(left.prompt_version_ids) != set(right.prompt_version_ids),
                },
                "model_config_ids": {
                    "left": left.model_config_ids,
                    "right": right.model_config_ids,
                    "changed": set(left.model_config_ids) != set(right.model_config_ids),
                },
                "dataset_version_ids": {
                    "left": left.dataset_version_ids,
                    "right": right.dataset_version_ids,
                    "changed": set(left.dataset_version_ids) != set(right.dataset_version_ids),
                },
                "models": {
                    "left": left.models,
                    "right": right.models,
                    "changed": set(left.models) != set(right.models),
                },
                "release": {
                    "left": left.release,
                    "right": right.release,
                    "changed": left.release != right.release,
                },
            },
        )


def _retry_groups(spans: Sequence[SpanRow]) -> dict[str, list[str]]:
    """Group spans that represent retries of the same logical operation.

    Detected from span links tagged ``retry_of``; falls back to identical
    ``(parent, name)`` pairs appearing more than once, which is what a naive
    retry loop produces.
    """
    groups: dict[str, list[str]] = {}
    for span in spans:
        for link in span.links or []:
            if (link.get("attributes") or {}).get("aiobs.retry_of"):
                groups.setdefault(str(link.get("span_id")), []).append(span.span_id)
    if groups:
        return groups
    seen: dict[tuple[str, str], list[str]] = {}
    for span in spans:
        seen.setdefault((span.parent_span_id or "", span.name), []).append(span.span_id)
    return {ids[0]: ids[1:] for ids in seen.values() if len(ids) > 1}


def _structural_keys(spans: Sequence[SpanRow]) -> dict[tuple[int, str, int], SpanRow]:
    """Key spans by ``(depth, name, ordinal)`` for cross-trace matching."""
    by_id = {span.span_id: span for span in spans}
    depths: dict[str, int] = {}

    def depth_of(span: SpanRow, guard: int = 0) -> int:
        if span.span_id in depths:
            return depths[span.span_id]
        if not span.parent_span_id or span.parent_span_id not in by_id or guard > 64:
            depths[span.span_id] = 0
            return 0
        value = depth_of(by_id[span.parent_span_id], guard + 1) + 1
        depths[span.span_id] = value
        return value

    counters: dict[tuple[int, str], int] = {}
    result: dict[tuple[int, str, int], SpanRow] = {}
    for span in sorted(spans, key=lambda item: item.start_unix_nano):
        depth = depth_of(span)
        ordinal = counters.get((depth, span.name), 0)
        counters[(depth, span.name)] = ordinal + 1
        result[(depth, span.name, ordinal)] = span
    return result


def _retrieval_stages(view: RetrievalStageView) -> list[dict[str, Any]]:
    """Describe the retrieval pipeline as ordered, selectable stages."""
    selected = sum(1 for document in view.documents if document.selected)
    return [
        {
            "stage": "query",
            "label": "User query",
            "detail": view.query[:200],
            "latency_ms": None,
            "present": bool(view.query),
        },
        {
            "stage": "rewrite",
            "label": "Query rewrite",
            "detail": view.rewritten_query[:200],
            "latency_ms": None,
            "present": bool(view.rewritten_query and view.rewritten_query != view.query),
        },
        {
            "stage": "embedding",
            "label": f"Embedding ({view.embedding_model})" if view.embedding_model else "Embedding",
            "detail": view.embedding_model,
            "latency_ms": view.embedding_latency_ms,
            "present": bool(view.embedding_model),
        },
        {
            "stage": "retrieval",
            "label": f"{view.search_type or 'vector'} retrieval",
            "detail": f"{len(view.documents)} documents",
            "latency_ms": view.latency_ms,
            "present": True,
        },
        {
            "stage": "rerank",
            "label": f"Rerank ({view.reranker_model})" if view.reranker_model else "Rerank",
            "detail": (
                f"mean movement {view.diagnostics.mean_rank_movement:.1f}"
                if view.diagnostics.mean_rank_movement is not None
                else ""
            ),
            "latency_ms": view.reranker_latency_ms,
            "present": view.diagnostics.reranked,
        },
        {
            "stage": "selection",
            "label": "Context selection",
            "detail": f"{selected} of {len(view.documents)} selected, "
            f"{view.diagnostics.context_tokens} tokens",
            "latency_ms": None,
            "present": True,
        },
    ]


def _float_attr(attributes: dict[str, Any], key: str) -> float | None:
    value = attributes.get(key)
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _ns_to_ms(value: int | None) -> float | None:
    return None if value is None else value / 1e6


def _delta(left: float | int | None, right: float | int | None) -> dict[str, Any]:
    """Absolute and relative change from ``left`` to ``right``."""
    if left is None or right is None:
        return {"left": left, "right": right, "absolute": None, "relative": None}
    absolute = right - left
    relative = (absolute / left) if left else None
    return {
        "left": left,
        "right": right,
        "absolute": absolute,
        "relative": relative,
    }


def _decimal_delta(left: Decimal | None, right: Decimal | None) -> dict[str, Any]:
    if left is None or right is None:
        return {
            "left": None if left is None else str(left),
            "right": None if right is None else str(right),
            "absolute": None,
            "relative": None,
        }
    absolute = right - left
    return {
        "left": str(left),
        "right": str(right),
        "absolute": str(absolute),
        "relative": float(absolute / left) if left else None,
    }


def trace_started_at(trace: TraceRow) -> datetime:
    return unix_nano_to_datetime(trace.start_unix_nano)
