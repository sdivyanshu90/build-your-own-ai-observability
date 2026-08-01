"""Trace roll-up computation.

A trace roll-up is the denormalised summary the explorer lists and the
dashboards aggregate: duration, status, token totals, cost, which models and
prompt versions took part.

It is **recomputed from scratch** every time any of a trace's spans is ingested,
never incrementally updated. That choice is the reason the pipeline survives its
own failure modes:

* a duplicate delivery recomputes the same numbers -- an ``+=`` would double them;
* a late-arriving span produces a corrected roll-up rather than a skewed one;
* a dead-letter replay converges instead of compounding;
* a worker crash mid-batch leaves a stale roll-up that the next pass fixes.

The cost is re-reading a trace's spans (typically tens, occasionally thousands)
on each update. That is a bounded, indexed read, and it buys idempotency for the
entire ingestion path.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal

from aiobs_schemas.enums import CostEstimationStatus, SpanCategory, TraceStatus, UsageSource

from ..core.timeutil import Clock
from ..domain.usage import NormalizedUsage, UsageAccumulator
from ..storage.analytics.rows import SpanRow, TraceRow

__all__ = ["build_trace_rollup", "critical_path", "trace_status_for"]


def trace_status_for(spans: Sequence[SpanRow]) -> TraceStatus:
    """Derive a trace's status from its spans.

    ``INCOMPLETE`` is a first-class outcome, not an error: it means the root
    span has not been seen or some span never reported an end time. Reporting it
    as ``OK`` would hide broken instrumentation, and as ``ERROR`` would create
    false alerts for traces that are merely still running.
    """
    if not spans:
        return TraceStatus.INCOMPLETE
    if any(span.status == "error" for span in spans):
        return TraceStatus.ERROR
    has_root = any(span.is_root for span in spans)
    all_closed = all(span.end_unix_nano is not None for span in spans)
    if has_root and all_closed:
        return TraceStatus.OK
    return TraceStatus.INCOMPLETE


def _unique(values: Iterable[str], limit: int = 32) -> list[str]:
    """Ordered de-duplication, bounded to keep array columns small."""
    seen: dict[str, None] = {}
    for value in values:
        if value and value not in seen:
            seen[value] = None
            if len(seen) >= limit:
                break
    return list(seen)


def build_trace_rollup(
    spans: Sequence[SpanRow],
    *,
    clock: Clock,
    previous: TraceRow | None = None,
) -> TraceRow | None:
    """Compute the roll-up for one trace from all of its known spans."""
    if not spans:
        return None

    first = spans[0]
    root = next((span for span in spans if span.is_root), None)
    # Without a root span, the earliest span is the best available proxy for the
    # trace's start; it is corrected as soon as the root arrives.
    start_nano = min(span.start_unix_nano for span in spans)
    end_candidates = [span.end_unix_nano for span in spans if span.end_unix_nano is not None]
    end_nano = max(end_candidates) if end_candidates else None

    usage = UsageAccumulator()
    total_cost: Decimal | None = None
    currency = ""
    cost_statuses: set[str] = set()
    errors: list[str] = []

    llm_calls = retrievals = tool_calls = agent_steps = 0

    for span in spans:
        usage.add(
            NormalizedUsage(
                input_tokens=span.input_tokens,
                output_tokens=span.output_tokens,
                total_tokens=span.total_tokens,
                cached_input_tokens=span.cached_input_tokens,
                reasoning_tokens=span.reasoning_tokens,
                source=_usage_source(span.usage_source),
            )
        )
        if span.cost_total is not None:
            total_cost = (total_cost or Decimal("0")) + span.cost_total
            currency = currency or span.cost_currency
            cost_statuses.add(span.cost_estimation_status)
        if span.status == "error" and span.error_message:
            errors.append(f"{span.name}: {span.error_message}"[:512])

        category = SpanCategory.coerce(span.category)
        if category.is_model_call and category is not SpanCategory.EMBEDDING:
            llm_calls += 1
        if category is SpanCategory.RETRIEVAL:
            retrievals += 1
        if category is SpanCategory.TOOL_CALL:
            tool_calls += 1
        if category in {SpanCategory.AGENT_DECISION, SpanCategory.AGENT_HANDOFF}:
            agent_steps += 1

    status = trace_status_for(spans)
    ingested = max(
        (span.ingested_at for span in spans if span.ingested_at is not None),
        default=clock.now(),
    )

    # Time to first token belongs to the trace as a whole: take the earliest
    # model call that reported one, which is what a user perceives as latency.
    ttft = None
    ttft_candidates = [
        (span.start_unix_nano, span.time_to_first_token_ms)
        for span in spans
        if span.time_to_first_token_ms is not None
    ]
    if ttft_candidates:
        ttft = min(ttft_candidates, key=lambda item: item[0])[1]

    return TraceRow(
        organization_id=first.organization_id,
        project_id=first.project_id,
        environment=first.environment,
        trace_id=first.trace_id,
        name=_trace_name(root, spans),
        start_unix_nano=start_nano,
        end_unix_nano=end_nano,
        duration_ns=None if end_nano is None else end_nano - start_nano,
        status=status.value,
        error_summary=" | ".join(errors[:3])[:2_048],
        root_span_id=root.span_id if root else "",
        span_count=len(spans),
        error_count=sum(1 for span in spans if span.status == "error"),
        session_id=_first_non_empty(span.session_id for span in spans),
        subject_id=_first_non_empty(span.subject_id for span in spans),
        release=_first_non_empty(span.release for span in spans),
        git_commit=_first_non_empty(span.git_commit for span in spans),
        tags=_unique(tag for span in spans for tag in span.tags),
        total_input_tokens=usage.input_tokens,
        total_output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        total_cached_input_tokens=usage.cached_input_tokens,
        total_reasoning_tokens=usage.reasoning_tokens,
        usage_source=usage.source.value,
        total_cost=total_cost,
        cost_currency=currency,
        cost_estimation_status=_worst_cost_status(cost_statuses),
        time_to_first_token_ms=ttft,
        models=_unique(span.model for span in spans),
        providers=_unique(span.provider for span in spans),
        prompt_version_ids=_unique(span.prompt_version_id for span in spans),
        model_config_ids=_unique(span.model_config_id for span in spans),
        dataset_version_ids=_unique(span.dataset_version_id for span in spans),
        service_names=_unique(span.service_name for span in spans),
        llm_call_count=llm_calls,
        retrieval_count=retrievals,
        tool_call_count=tool_calls,
        agent_step_count=agent_steps,
        sdk_name=_first_non_empty(span.sdk_name for span in spans),
        sdk_version=_first_non_empty(span.sdk_version for span in spans),
        sampling_rate=first.sampling_rate,
        ingested_at=ingested,
        # Version strictly increases so a stale concurrent recomputation can
        # never overwrite a newer one in the ReplacingMergeTree.
        ingest_version=max(
            int(ingested.timestamp() * 1_000_000),
            (previous.ingest_version + 1) if previous else 0,
        ),
        complete=status is not TraceStatus.INCOMPLETE,
        previous_start_unix_nano=previous.start_unix_nano if previous else None,
    )


def _trace_name(root: SpanRow | None, spans: Sequence[SpanRow]) -> str:
    if root is not None:
        return root.name
    # No root yet: use the earliest span's name so the trace is at least
    # identifiable in the explorer while it is still assembling.
    return min(spans, key=lambda span: span.start_unix_nano).name


def _first_non_empty(values: Iterable[str]) -> str:
    for value in values:
        if value:
            return value
    return ""


def _usage_source(value: str) -> UsageSource:
    try:
        return UsageSource(value)
    except ValueError:
        return UsageSource.MISSING


def _worst_cost_status(statuses: set[str]) -> str:
    """A trace's cost is only final if every priced span's cost was final."""
    if not statuses:
        return CostEstimationStatus.UNPRICED.value
    for candidate in (
        CostEstimationStatus.UNPRICED,
        CostEstimationStatus.ESTIMATED,
        CostEstimationStatus.FINAL,
    ):
        if candidate.value in statuses:
            return candidate.value
    return CostEstimationStatus.UNPRICED.value


def critical_path(spans: Sequence[SpanRow]) -> list[str]:
    """Return the span ids on the trace's critical path, root first.

    The critical path is the chain of spans that determines total latency: from
    the root, repeatedly descend into the child whose *own* completion is
    latest. Optimising anything off this path cannot make the request faster,
    which is exactly the question a latency investigation is trying to answer.

    Concurrent siblings are handled correctly: only the last-finishing child
    constrains the parent, so a slow-but-parallel sibling is not on the path.
    Spans without an end time are skipped -- they cannot be shown to constrain
    anything.
    """
    if not spans:
        return []
    by_parent: dict[str, list[SpanRow]] = {}
    by_id: dict[str, SpanRow] = {}
    for span in spans:
        by_id[span.span_id] = span
        by_parent.setdefault(span.parent_span_id or "", []).append(span)

    root = next((span for span in spans if span.is_root), None)
    if root is None:
        return []

    path = [root.span_id]
    current = root
    # Bound the walk: a malformed parent chain could otherwise cycle.
    for _ in range(len(spans)):
        children = [
            child for child in by_parent.get(current.span_id, []) if child.end_unix_nano is not None
        ]
        if not children:
            break
        # The child that finishes last is the one the parent waited for.
        current = max(children, key=lambda child: child.end_unix_nano or 0)
        if current.span_id in path:
            break
        path.append(current.span_id)
    return path


def self_time_ns(spans: Sequence[SpanRow]) -> dict[str, int]:
    """Time spent in each span excluding its children.

    Total duration attributes a slow child to its parent too, which makes a
    waterfall misleading: an orchestration span looks expensive when the cost is
    actually one nested provider call. Self time answers "where did the
    milliseconds actually go".

    Overlapping children are merged before subtraction, so concurrent work is
    counted once rather than over-subtracted into a negative self time.
    """
    by_parent: dict[str, list[SpanRow]] = {}
    for span in spans:
        by_parent.setdefault(span.parent_span_id or "", []).append(span)

    result: dict[str, int] = {}
    for span in spans:
        if span.duration_ns is None:
            continue
        intervals = sorted(
            (child.start_unix_nano, child.end_unix_nano)
            for child in by_parent.get(span.span_id, [])
            if child.end_unix_nano is not None
        )
        merged_total = 0
        current_start: int | None = None
        current_end: int | None = None
        for start, end in intervals:
            if current_end is None or start > current_end:
                if current_start is not None and current_end is not None:
                    merged_total += current_end - current_start
                current_start, current_end = start, end
            else:
                current_end = max(current_end, end or 0)
        if current_start is not None and current_end is not None:
            merged_total += current_end - current_start
        result[span.span_id] = max(span.duration_ns - merged_total, 0)
    return result
