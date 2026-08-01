"""Derived analyses: retrieval diagnostics and agent trajectory graphs.

Pure functions over stored rows. Keeping them out of the query layer means they
can be unit-tested against hand-written fixtures, and reused by the export path
and the SDK's test utilities.

Formulas, assumptions and limitations are documented inline and in
``docs/concepts/retrieval-observability.md`` -- an unlabelled "NDCG: 0.72" is
worse than no number, because two implementations of NDCG disagree on the
handling of missing labels and neither is wrong.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from itertools import pairwise

from ..storage.analytics.rows import AgentStepRow, RetrievalDocumentRow, SpanRow

__all__ = [
    "AgentGraph",
    "AgentGraphEdge",
    "AgentGraphNode",
    "RetrievalDiagnostics",
    "build_agent_graph",
    "ndcg_at_k",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
    "retrieval_diagnostics",
]


# ---------------------------------------------------------------------------
# retrieval
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RetrievalDiagnostics:
    """Quality signals computed from one retrieval step's documents."""

    document_count: int = 0
    selected_count: int = 0
    unused_count: int = 0
    #: Documents retrieved but not selected into the final context. The single
    #: most actionable retrieval signal: a high ratio means the retriever is
    #: over-fetching or the context assembler is dropping relevant material.
    unused_ratio: float = 0.0
    score_min: float | None = None
    score_max: float | None = None
    score_mean: float | None = None
    score_stddev: float | None = None
    #: Gap between the best and second-best score. A tiny gap means the ranking
    #: is close to arbitrary.
    score_margin: float | None = None
    reranked: bool = False
    #: Mean absolute rank movement caused by reranking.
    mean_rank_movement: float | None = None
    #: Documents the reranker promoted into the selected set.
    rerank_promotions: int = 0
    rerank_demotions: int = 0
    duplicate_document_ids: tuple[str, ...] = ()
    near_duplicate_pairs: tuple[tuple[str, str], ...] = ()
    context_tokens: int = 0
    truncated_count: int = 0
    missing_source_count: int = 0
    empty_result: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "document_count": self.document_count,
            "selected_count": self.selected_count,
            "unused_count": self.unused_count,
            "unused_ratio": round(self.unused_ratio, 4),
            "score_min": self.score_min,
            "score_max": self.score_max,
            "score_mean": self.score_mean,
            "score_stddev": self.score_stddev,
            "score_margin": self.score_margin,
            "reranked": self.reranked,
            "mean_rank_movement": self.mean_rank_movement,
            "rerank_promotions": self.rerank_promotions,
            "rerank_demotions": self.rerank_demotions,
            "duplicate_document_ids": list(self.duplicate_document_ids),
            "near_duplicate_pairs": [list(pair) for pair in self.near_duplicate_pairs],
            "context_tokens": self.context_tokens,
            "truncated_count": self.truncated_count,
            "missing_source_count": self.missing_source_count,
            "empty_result": self.empty_result,
        }


def retrieval_diagnostics(
    documents: Sequence[RetrievalDocumentRow],
    *,
    near_duplicate_threshold: float = 0.9,
) -> RetrievalDiagnostics:
    """Compute diagnostics for one retrieval step.

    ``near_duplicate_threshold`` is a Jaccard similarity over word shingles of
    the stored content preview. It is a cheap heuristic operating on a
    *truncated* preview, so it detects obvious duplication (the same chunk
    indexed twice) and will miss semantic paraphrase. That limitation is
    surfaced in the UI rather than hidden.
    """
    result = RetrievalDiagnostics(document_count=len(documents))
    if not documents:
        result.empty_result = True
        return result

    scores = [document.score for document in documents if document.score is not None]
    if scores:
        result.score_min = min(scores)
        result.score_max = max(scores)
        result.score_mean = sum(scores) / len(scores)
        if len(scores) > 1:
            mean = result.score_mean
            variance = sum((value - mean) ** 2 for value in scores) / (len(scores) - 1)
            result.score_stddev = math.sqrt(variance)
            ordered = sorted(scores, reverse=True)
            result.score_margin = ordered[0] - ordered[1]

    result.selected_count = sum(1 for document in documents if document.selected)
    result.unused_count = result.document_count - result.selected_count
    result.unused_ratio = (
        result.unused_count / result.document_count if result.document_count else 0.0
    )
    result.context_tokens = sum(
        document.token_count or 0 for document in documents if document.selected
    )
    result.truncated_count = sum(1 for document in documents if document.truncated)
    result.missing_source_count = sum(1 for document in documents if not document.source)

    movements = [
        abs(document.rank - document.rerank_rank)
        for document in documents
        if document.rerank_rank is not None
    ]
    if movements:
        result.reranked = True
        result.mean_rank_movement = sum(movements) / len(movements)
        result.rerank_promotions = sum(
            1
            for document in documents
            if document.rerank_rank is not None and document.rerank_rank < document.rank
        )
        result.rerank_demotions = sum(
            1
            for document in documents
            if document.rerank_rank is not None and document.rerank_rank > document.rank
        )

    counts = Counter(document.document_id for document in documents if document.document_id)
    result.duplicate_document_ids = tuple(
        sorted(identifier for identifier, count in counts.items() if count > 1)
    )
    result.near_duplicate_pairs = _near_duplicates(documents, near_duplicate_threshold)
    return result


def _shingles(text: str, size: int = 5) -> frozenset[str]:
    words = text.lower().split()
    if len(words) < size:
        return frozenset({" ".join(words)}) if words else frozenset()
    return frozenset(
        " ".join(words[index : index + size]) for index in range(len(words) - size + 1)
    )


def _near_duplicates(
    documents: Sequence[RetrievalDocumentRow], threshold: float
) -> tuple[tuple[str, str], ...]:
    """Pairwise Jaccard similarity over shingles of the content preview.

    Quadratic in the document count, which is fine because retrieval steps
    return tens of documents, and bounded explicitly for the pathological case.
    """
    candidates = [
        (document, _shingles(document.content_preview))
        for document in documents
        if document.content_preview
    ][:64]
    pairs: list[tuple[str, str]] = []
    for index, (left, left_shingles) in enumerate(candidates):
        for right, right_shingles in candidates[index + 1 :]:
            if not left_shingles or not right_shingles:
                continue
            intersection = len(left_shingles & right_shingles)
            union = len(left_shingles | right_shingles)
            if union and intersection / union >= threshold:
                pairs.append(
                    (
                        left.chunk_id or left.document_id,
                        right.chunk_id or right.document_id,
                    )
                )
    return tuple(pairs)


# ---------------------------------------------------------------------------
# ranking metrics
# ---------------------------------------------------------------------------


def precision_at_k(ranked_ids: Sequence[str], relevant_ids: Iterable[str], k: int) -> float | None:
    """Fraction of the top-k results that are relevant.

    Returns ``None`` when no relevance labels exist -- reporting 0.0 would be
    indistinguishable from "retrieved nothing relevant", which is a completely
    different fact.
    """
    relevant = set(relevant_ids)
    if not relevant or k <= 0:
        return None
    top = ranked_ids[:k]
    if not top:
        return 0.0
    return sum(1 for identifier in top if identifier in relevant) / len(top)


def recall_at_k(ranked_ids: Sequence[str], relevant_ids: Iterable[str], k: int) -> float | None:
    """Fraction of all relevant documents that appear in the top k."""
    relevant = set(relevant_ids)
    if not relevant or k <= 0:
        return None
    return sum(1 for identifier in ranked_ids[:k] if identifier in relevant) / len(relevant)


def reciprocal_rank(ranked_ids: Sequence[str], relevant_ids: Iterable[str]) -> float | None:
    """Reciprocal of the 1-based rank of the first relevant result.

    Zero when no relevant document appears at any rank, which is the standard
    convention for mean reciprocal rank.
    """
    relevant = set(relevant_ids)
    if not relevant:
        return None
    for position, identifier in enumerate(ranked_ids, start=1):
        if identifier in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at_k(ranked_ids: Sequence[str], gains: Mapping[str, float], k: int) -> float | None:
    """Normalised discounted cumulative gain at k.

    Uses the standard formulation ``DCG = sum(gain / log2(rank + 1))`` with a
    binary or graded gain supplied by the caller, normalised against the ideal
    ranking of the *labelled* set.

    Assumptions worth stating: unlabelled documents contribute zero gain (they
    are treated as non-relevant, not as unknown), and the ideal DCG is computed
    over the labels provided rather than over the whole corpus. Both are
    conventional and both mean NDCG is only comparable across runs that share a
    label set.
    """
    if not gains or k <= 0:
        return None
    discounted = sum(
        gains.get(identifier, 0.0) / math.log2(position + 1)
        for position, identifier in enumerate(ranked_ids[:k], start=1)
    )
    ideal_gains = sorted(gains.values(), reverse=True)[:k]
    ideal = sum(
        gain / math.log2(position + 1) for position, gain in enumerate(ideal_gains, start=1)
    )
    if ideal == 0:
        return 0.0
    return discounted / ideal


# ---------------------------------------------------------------------------
# agent trajectories
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AgentGraphNode:
    """One node in the trajectory graph."""

    id: str
    step_number: int
    agent_id: str
    step_type: str
    label: str
    status: str
    span_id: str
    duration_ms: float | None = None
    tool_name: str = ""
    tool_status: str = ""
    decision_summary: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_total: str | None = None
    branch_id: str = ""
    loop_iteration: int | None = None
    is_retry: bool = False
    approval_status: str = ""
    termination_reason: str = ""
    error_message: str = ""
    #: True when the node lies on the trajectory's longest-duration chain.
    on_critical_path: bool = False


@dataclass(slots=True)
class AgentGraphEdge:
    """A directed relationship between two trajectory nodes."""

    source: str
    target: str
    #: 'sequence' | 'branch' | 'retry' | 'handoff' | 'loop'
    kind: str = "sequence"
    label: str = ""


@dataclass(slots=True)
class AgentGraph:
    """A renderable agent trajectory."""

    nodes: list[AgentGraphNode] = field(default_factory=list)
    edges: list[AgentGraphEdge] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    #: Distinct branches observed, for the branch selector in the UI.
    branches: list[str] = field(default_factory=list)
    max_steps: int | None = None
    termination_reason: str = ""
    total_steps: int = 0
    retry_count: int = 0
    loop_detected: bool = False
    handoff_count: int = 0
    #: True when the trajectory exceeded the node budget and was summarised.
    truncated: bool = False

    def as_dict(self) -> dict[str, object]:
        # asdict() rather than __dict__: these dataclasses use slots=True for
        # memory, and a slotted instance has no __dict__ at all.
        return {
            "nodes": [asdict(node) for node in self.nodes],
            "edges": [asdict(edge) for edge in self.edges],
            "agents": self.agents,
            "branches": self.branches,
            "max_steps": self.max_steps,
            "termination_reason": self.termination_reason,
            "total_steps": self.total_steps,
            "retry_count": self.retry_count,
            "loop_detected": self.loop_detected,
            "handoff_count": self.handoff_count,
            "truncated": self.truncated,
        }


#: Beyond this many steps a force-directed graph stops being readable and starts
#: being expensive. Larger trajectories are summarised rather than rendered raw;
#: the step list remains fully paginated.
MAX_GRAPH_NODES = 500


def build_agent_graph(steps: Sequence[AgentStepRow], spans: Sequence[SpanRow] = ()) -> AgentGraph:
    """Assemble a directed graph from recorded agent steps.

    Edges come from three sources, in priority order:

    1. ``parent_step`` -- explicit structure the application declared.
    2. ``retry_of`` -- a retry edge back to the attempt it replaces.
    3. sequential order within an agent -- the fallback when the application
       recorded only step numbers.

    Handoffs produce an edge from the handing-off step to the first step of the
    receiving agent, which is what makes multi-agent trajectories legible
    instead of appearing as unrelated chains.
    """
    graph = AgentGraph()
    if not steps:
        return graph

    ordered = sorted(
        steps, key=lambda step: (step.agent_id, step.step_number, step.start_unix_nano)
    )
    if len(ordered) > MAX_GRAPH_NODES:
        ordered = ordered[:MAX_GRAPH_NODES]
        graph.truncated = True

    by_key: dict[tuple[str, int], AgentGraphNode] = {}
    per_agent: dict[str, list[AgentStepRow]] = defaultdict(list)

    for step in ordered:
        node_id = f"{step.agent_id}#{step.step_number}"
        node = AgentGraphNode(
            id=node_id,
            step_number=step.step_number,
            agent_id=step.agent_id,
            step_type=step.step_type,
            label=step.tool_name or step.step_type or f"step {step.step_number}",
            status=step.status,
            span_id=step.span_id,
            duration_ms=None if step.duration_ns is None else step.duration_ns / 1e6,
            tool_name=step.tool_name,
            tool_status=step.tool_status,
            decision_summary=step.decision_summary,
            input_tokens=step.input_tokens,
            output_tokens=step.output_tokens,
            cost_total=None if step.cost_total is None else format(step.cost_total, "f"),
            branch_id=step.branch_id,
            loop_iteration=step.loop_iteration,
            is_retry=step.retry_of is not None,
            approval_status=step.approval_status,
            termination_reason=step.termination_reason,
            error_message=step.error_message,
        )
        graph.nodes.append(node)
        by_key[(step.agent_id, step.step_number)] = node
        per_agent[step.agent_id].append(step)
        if step.termination_reason:
            graph.termination_reason = step.termination_reason
        if step.max_steps is not None:
            graph.max_steps = step.max_steps

    graph.agents = sorted(per_agent)
    graph.branches = sorted({step.branch_id for step in ordered if step.branch_id})
    graph.total_steps = len(graph.nodes)
    graph.retry_count = sum(1 for node in graph.nodes if node.is_retry)

    seen_edges: set[tuple[str, str, str]] = set()

    def add_edge(source: str, target: str, kind: str, label: str = "") -> None:
        key = (source, target, kind)
        if source == target or key in seen_edges:
            return
        seen_edges.add(key)
        graph.edges.append(AgentGraphEdge(source=source, target=target, kind=kind, label=label))

    for step in ordered:
        node_id = f"{step.agent_id}#{step.step_number}"
        if step.retry_of is not None:
            source = by_key.get((step.agent_id, step.retry_of))
            if source is not None:
                add_edge(source.id, node_id, "retry", f"retry of {step.retry_of}")
                continue
        if step.parent_step is not None:
            parent = by_key.get((step.agent_id, step.parent_step))
            if parent is not None:
                kind = "branch" if step.branch_id else "sequence"
                add_edge(parent.id, node_id, kind, step.branch_id)
                continue

    # Sequential fallback within each agent, only where no explicit edge exists.
    targets_with_incoming = {edge.target for edge in graph.edges}
    for agent_id, agent_steps in per_agent.items():
        agent_steps.sort(key=lambda step: step.step_number)
        for previous, current in pairwise(agent_steps):
            current_id = f"{agent_id}#{current.step_number}"
            if current_id in targets_with_incoming:
                continue
            add_edge(f"{agent_id}#{previous.step_number}", current_id, "sequence")

    # Handoff edges into the receiving agent's first step.
    for step in ordered:
        if not step.handoff_target:
            continue
        graph.handoff_count += 1
        receiving = per_agent.get(step.handoff_target)
        if not receiving:
            continue
        first = min(receiving, key=lambda item: item.step_number)
        add_edge(
            f"{step.agent_id}#{step.step_number}",
            f"{step.handoff_target}#{first.step_number}",
            "handoff",
            step.handoff_target,
        )

    graph.loop_detected = _detect_loop(ordered)
    _mark_critical_path(graph)
    return graph


def _detect_loop(steps: Sequence[AgentStepRow]) -> bool:
    """Whether the trajectory repeated the same action enough to look stuck.

    Heuristic: the same ``(tool, step_type)`` pair three or more times within a
    single agent, or an explicit ``loop_iteration`` above two. It is advisory --
    a legitimate map-over-documents agent will trip it -- so the UI labels it
    "possible loop" rather than asserting a defect.
    """
    if any((step.loop_iteration or 0) > 2 for step in steps):
        return True
    signature_counts: Counter[tuple[str, str, str]] = Counter(
        (step.agent_id, step.tool_name, step.step_type) for step in steps if step.tool_name
    )
    return any(count >= 3 for count in signature_counts.values())


def _mark_critical_path(graph: AgentGraph) -> None:
    """Mark the longest-duration path through the graph.

    Longest *duration*, not longest hop count: the useful question is which
    chain of steps accounts for the elapsed time. Computed with a topological
    relaxation over the edge set; cycles (from retry edges) are broken by
    visiting nodes in step order.
    """
    if not graph.nodes:
        return
    by_id = {node.id: node for node in graph.nodes}
    successors: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        successors[edge.source].append(edge.target)

    ordered_ids = [
        node.id for node in sorted(graph.nodes, key=lambda item: (item.step_number, item.agent_id))
    ]
    best: dict[str, float] = {node_id: by_id[node_id].duration_ms or 0.0 for node_id in ordered_ids}
    predecessor: dict[str, str | None] = dict.fromkeys(ordered_ids)
    position = {node_id: index for index, node_id in enumerate(ordered_ids)}

    for node_id in ordered_ids:
        for successor in successors.get(node_id, []):
            if successor not in best or position.get(successor, -1) <= position[node_id]:
                # Skip backward edges so a retry cycle cannot loop forever.
                continue
            candidate = best[node_id] + (by_id[successor].duration_ms or 0.0)
            if candidate > best[successor]:
                best[successor] = candidate
                predecessor[successor] = node_id

    if not best:
        return
    terminal = max(best, key=lambda node_id: best[node_id])
    cursor: str | None = terminal
    guard = 0
    while cursor is not None and guard <= len(graph.nodes):
        by_id[cursor].on_critical_path = True
        cursor = predecessor.get(cursor)
        guard += 1
