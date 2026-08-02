"""Trace analysis: critical path, self time, retrieval diagnostics, agent graphs."""

from __future__ import annotations

import pytest

from aiobs_api.domain.analysis import (
    build_agent_graph,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    retrieval_diagnostics,
)
from aiobs_api.ingest.rollup import critical_path, self_time_ns, trace_status_for
from aiobs_api.storage.analytics.rows import AgentStepRow, RetrievalDocumentRow, SpanRow

BASE = 1_800_000_000_000_000_000  # a plausible nanosecond timestamp


def span(
    span_id: str,
    parent: str | None,
    start_offset_ms: float,
    duration_ms: float,
    *,
    status: str = "ok",
    name: str | None = None,
) -> SpanRow:
    start = BASE + int(start_offset_ms * 1e6)
    duration = int(duration_ms * 1e6)
    return SpanRow(
        organization_id="org",
        project_id="prj",
        environment="development",
        trace_id="t" * 32,
        span_id=span_id,
        parent_span_id=parent,
        name=name or span_id,
        kind="internal",
        category="custom",
        start_unix_nano=start,
        end_unix_nano=start + duration,
        duration_ns=duration,
        status=status,
    )


class TestCriticalPath:
    def test_follows_the_last_finishing_child(self) -> None:
        # root ──┬── fast (finishes at 20ms)
        #        └── slow (finishes at 90ms) ── leaf
        spans = [
            span("root", None, 0, 100),
            span("fast", "root", 5, 15),
            span("slow", "root", 5, 85),
            span("leaf", "slow", 10, 60),
        ]
        assert critical_path(spans) == ["root", "slow", "leaf"]

    def test_ignores_a_slow_but_earlier_finishing_sibling(self) -> None:
        """Concurrency: only the last finisher constrains the parent."""
        spans = [
            span("root", None, 0, 100),
            span("long-but-early", "root", 0, 50),  # finishes at 50ms
            span("short-but-late", "root", 60, 30),  # finishes at 90ms
        ]
        assert critical_path(spans) == ["root", "short-but-late"]

    def test_returns_empty_without_a_root(self) -> None:
        assert critical_path([span("orphan", "missing", 0, 10)]) == []

    def test_terminates_on_a_cycle(self) -> None:
        """A malformed parent chain must not hang the request."""
        a = span("a", "b", 0, 10)
        b = span("b", "a", 0, 10)
        root = span("root", None, 0, 20)
        assert len(critical_path([root, a, b])) <= 3


class TestSelfTime:
    def test_subtracts_children(self) -> None:
        spans = [span("root", None, 0, 100), span("child", "root", 10, 60)]
        result = self_time_ns(spans)
        assert result["root"] == int(40 * 1e6)
        assert result["child"] == int(60 * 1e6)

    def test_merges_overlapping_children(self) -> None:
        """Two concurrent children occupying 0-60ms consume 60ms of the parent,
        not 100ms -- otherwise self time goes negative."""
        spans = [
            span("root", None, 0, 100),
            span("a", "root", 0, 40),
            span("b", "root", 20, 40),  # overlaps a; union is 0-60
        ]
        assert self_time_ns(spans)["root"] == int(40 * 1e6)

    def test_never_goes_negative(self) -> None:
        spans = [span("root", None, 0, 10), span("child", "root", 0, 50)]
        assert self_time_ns(spans)["root"] == 0


class TestTraceStatus:
    def test_error_when_any_span_failed(self) -> None:
        spans = [span("root", None, 0, 10), span("child", "root", 1, 5, status="error")]
        assert trace_status_for(spans).value == "error"

    def test_incomplete_without_a_root(self) -> None:
        assert trace_status_for([span("child", "parent", 0, 5)]).value == "incomplete"

    def test_incomplete_when_a_span_never_ended(self) -> None:
        open_span = span("root", None, 0, 10)
        open_span.end_unix_nano = None
        assert trace_status_for([open_span]).value == "incomplete"

    def test_ok_when_rooted_and_closed(self) -> None:
        assert trace_status_for([span("root", None, 0, 10)]).value == "ok"


def document(
    rank: int,
    *,
    score: float | None = None,
    rerank_rank: int | None = None,
    selected: bool = False,
    document_id: str | None = None,
    content: str = "",
    source: str = "https://example.com/doc",
    tokens: int = 40,
) -> RetrievalDocumentRow:
    return RetrievalDocumentRow(
        organization_id="org",
        project_id="prj",
        environment="development",
        trace_id="t" * 32,
        span_id="s" * 16,
        time_unix_nano=BASE,
        document_id=document_id or f"doc-{rank}",
        rank=rank,
        score=score,
        rerank_rank=rerank_rank,
        selected=selected,
        token_count=tokens,
        source=source,
        content_preview=content,
    )


class TestRetrievalDiagnostics:
    def test_empty_retrieval_is_flagged(self) -> None:
        result = retrieval_diagnostics([])
        assert result.empty_result is True
        assert result.document_count == 0

    def test_counts_unused_documents(self) -> None:
        documents = [document(index, selected=index < 2) for index in range(5)]
        result = retrieval_diagnostics(documents)
        assert result.selected_count == 2
        assert result.unused_count == 3
        assert result.unused_ratio == pytest.approx(0.6)

    def test_measures_rank_movement(self) -> None:
        documents = [
            document(0, rerank_rank=2),
            document(1, rerank_rank=0),
            document(2, rerank_rank=1),
        ]
        result = retrieval_diagnostics(documents)
        assert result.reranked is True
        assert result.mean_rank_movement == pytest.approx(4 / 3)
        assert result.rerank_promotions == 2
        assert result.rerank_demotions == 1

    def test_computes_score_distribution(self) -> None:
        documents = [document(index, score=0.9 - index * 0.1) for index in range(4)]
        result = retrieval_diagnostics(documents)
        assert result.score_max == pytest.approx(0.9)
        assert result.score_min == pytest.approx(0.6)
        assert result.score_margin == pytest.approx(0.1)
        assert result.score_stddev is not None

    def test_detects_duplicate_document_ids(self) -> None:
        documents = [document(0, document_id="same"), document(1, document_id="same")]
        assert retrieval_diagnostics(documents).duplicate_document_ids == ("same",)

    def test_detects_near_duplicate_content(self) -> None:
        text = "refunds are available within thirty days of delivery for unused items"
        documents = [
            document(0, document_id="a", content=text),
            document(1, document_id="b", content=text),
            document(2, document_id="c", content="shipping takes three to five days"),
        ]
        pairs = retrieval_diagnostics(documents).near_duplicate_pairs
        assert len(pairs) == 1
        assert set(pairs[0]) == {"a", "b"}

    def test_counts_documents_missing_a_source(self) -> None:
        documents = [document(0, source=""), document(1)]
        assert retrieval_diagnostics(documents).missing_source_count == 1

    def test_context_tokens_count_only_selected_documents(self) -> None:
        documents = [document(0, selected=True, tokens=100), document(1, tokens=200)]
        assert retrieval_diagnostics(documents).context_tokens == 100


class TestRankingMetrics:
    def test_precision_at_k(self) -> None:
        assert precision_at_k(["a", "b", "c", "d"], {"a", "c"}, 2) == pytest.approx(0.5)

    def test_recall_at_k(self) -> None:
        assert recall_at_k(["a", "b", "c"], {"a", "c", "z"}, 3) == pytest.approx(2 / 3)

    def test_reciprocal_rank(self) -> None:
        assert reciprocal_rank(["x", "y", "a"], {"a"}) == pytest.approx(1 / 3)

    def test_reciprocal_rank_is_zero_when_nothing_relevant_is_found(self) -> None:
        assert reciprocal_rank(["x", "y"], {"a"}) == 0.0

    def test_metrics_return_none_without_labels(self) -> None:
        """None and 0.0 are different facts: 'unlabelled' versus 'nothing
        relevant was retrieved'."""
        assert precision_at_k(["a"], set(), 1) is None
        assert recall_at_k(["a"], set(), 1) is None
        assert reciprocal_rank(["a"], set()) is None
        assert ndcg_at_k(["a"], {}, 1) is None

    def test_ndcg_is_one_for_a_perfect_ranking(self) -> None:
        gains = {"a": 3.0, "b": 2.0, "c": 1.0}
        assert ndcg_at_k(["a", "b", "c"], gains, 3) == pytest.approx(1.0)

    def test_ndcg_penalises_a_reversed_ranking(self) -> None:
        gains = {"a": 3.0, "b": 2.0, "c": 1.0}
        assert ndcg_at_k(["c", "b", "a"], gains, 3) < 1.0


def step(
    number: int,
    *,
    agent: str = "agent-1",
    step_type: str = "decision",
    tool: str = "",
    parent: int | None = None,
    retry_of: int | None = None,
    handoff: str = "",
    termination: str = "",
    duration_ms: float = 10,
    branch: str = "",
) -> AgentStepRow:
    return AgentStepRow(
        organization_id="org",
        project_id="prj",
        environment="development",
        trace_id="t" * 32,
        span_id=f"{number:016x}",
        agent_id=agent,
        step_number=number,
        start_unix_nano=BASE + number * 1_000_000,
        duration_ns=int(duration_ms * 1e6),
        step_type=step_type,
        tool_name=tool,
        parent_step=parent,
        retry_of=retry_of,
        handoff_target=handoff,
        termination_reason=termination,
        branch_id=branch,
    )


class TestAgentGraph:
    def test_empty_input_produces_an_empty_graph(self) -> None:
        graph = build_agent_graph([])
        assert graph.nodes == [] and graph.edges == []

    def test_sequential_steps_are_chained(self) -> None:
        graph = build_agent_graph([step(1), step(2), step(3)])
        assert len(graph.nodes) == 3
        kinds = {edge.kind for edge in graph.edges}
        assert kinds == {"sequence"}
        assert len(graph.edges) == 2

    def test_retry_produces_a_retry_edge(self) -> None:
        graph = build_agent_graph([step(1, tool="search"), step(2, tool="search", retry_of=1)])
        retry_edges = [edge for edge in graph.edges if edge.kind == "retry"]
        assert len(retry_edges) == 1
        assert graph.retry_count == 1

    def test_handoff_links_to_the_receiving_agent(self) -> None:
        graph = build_agent_graph(
            [
                step(1, agent="support", handoff="billing"),
                step(1, agent="billing"),
                step(2, agent="billing"),
            ]
        )
        handoffs = [edge for edge in graph.edges if edge.kind == "handoff"]
        assert len(handoffs) == 1
        assert handoffs[0].target.startswith("billing#")
        assert set(graph.agents) == {"support", "billing"}
        assert graph.handoff_count == 1

    def test_explicit_parent_wins_over_sequence(self) -> None:
        graph = build_agent_graph([step(1), step(2), step(3, parent=1, branch="alt")])
        branch_edges = [edge for edge in graph.edges if edge.kind == "branch"]
        assert len(branch_edges) == 1
        assert branch_edges[0].source.endswith("#1")

    def test_records_the_termination_reason(self) -> None:
        graph = build_agent_graph(
            [step(1), step(2, step_type="terminate", termination="max_steps")]
        )
        assert graph.termination_reason == "max_steps"

    def test_detects_a_repeated_action_loop(self) -> None:
        steps = [step(index, tool="search_orders", step_type="tool_call") for index in range(1, 5)]
        assert build_agent_graph(steps).loop_detected is True

    def test_does_not_flag_varied_actions_as_a_loop(self) -> None:
        steps = [
            step(1, tool="a", step_type="tool_call"),
            step(2, tool="b", step_type="tool_call"),
            step(3, tool="c", step_type="tool_call"),
        ]
        assert build_agent_graph(steps).loop_detected is False

    def test_marks_a_critical_path(self) -> None:
        graph = build_agent_graph(
            [step(1, duration_ms=5), step(2, duration_ms=100), step(3, duration_ms=5)]
        )
        assert any(node.on_critical_path for node in graph.nodes)

    def test_truncates_an_enormous_trajectory(self) -> None:
        """A 5,000-node force-directed graph is unusable and expensive; the
        step list stays complete but the graph is capped."""
        steps = [step(index) for index in range(1, 700)]
        graph = build_agent_graph(steps)
        assert graph.truncated is True
        assert len(graph.nodes) <= 500

    def test_serialises_slotted_dataclasses(self) -> None:
        # Regression: slots=True dataclasses have no __dict__.
        payload = build_agent_graph([step(1)]).as_dict()
        assert payload["nodes"][0]["id"] == "agent-1#1"
