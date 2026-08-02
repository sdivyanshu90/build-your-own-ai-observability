"""Retrieval and agent instrumentation helpers.

Generic wrappers rather than integrations with specific frameworks: retrieval
pipelines and agent loops are usually hand-written, and the ones that are not
differ enough that a per-framework adapter would be mostly guesswork. These
helpers make the hand-written case a few lines.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from ..tracer import Client, Span, get_client

__all__ = ["AgentRecorder", "RetrievalRecorder", "agent_span", "retrieval_span"]


@dataclass(slots=True)
class RetrievalRecorder:
    """Accumulates a retrieval pipeline's stages, then records them together.

    Retrieval is several timed steps (embed, search, rerank, select) whose
    individual latencies are the whole point of the view. Timing them
    separately and recording once keeps the span count low without losing the
    breakdown.

        with retrieval_span("vector-search", retriever_name="pgvector") as rec:
            with rec.time_embedding():
                vector = embed(question)
            with rec.time_retrieval():
                hits = index.search(vector, k=10)
            rec.documents(hits)
            rec.select(hits[:3])
    """

    span: Span
    retriever_name: str | None = None
    retriever_version: str | None = None
    knowledge_base_version: str | None = None
    embedding_model: str | None = None
    reranker_model: str | None = None
    search_type: str | None = None
    query: str | None = None
    rewritten_query: str | None = None
    top_k: int | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    _documents: list[dict[str, Any]] = field(default_factory=list)
    _selected: set[str] = field(default_factory=set)
    _embedding_ms: float | None = None
    _retrieval_ms: float | None = None
    _rerank_ms: float | None = None
    _context_tokens: int | None = None
    _truncated: bool = False

    @contextmanager
    def time_embedding(self) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self._embedding_ms = (time.perf_counter() - started) * 1000

    @contextmanager
    def time_retrieval(self) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self._retrieval_ms = (time.perf_counter() - started) * 1000

    @contextmanager
    def time_rerank(self) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self._rerank_ms = (time.perf_counter() - started) * 1000

    def documents(self, documents: Iterable[Mapping[str, Any]]) -> RetrievalRecorder:
        """Record the ranked results. Accepts dicts or objects with attributes."""
        self._documents = [
            _normalise_document(document, index) for index, document in enumerate(documents)
        ]
        return self

    def rerank(self, reranked: Sequence[Mapping[str, Any]]) -> RetrievalRecorder:
        """Record post-rerank positions against the already-recorded documents.

        Rank movement is the signal that tells you whether the reranker is
        earning its latency, and it can only be computed by keeping both
        orderings.
        """
        positions = {
            _document_key(_normalise_document(document, index)): index
            for index, document in enumerate(reranked)
        }
        for document in self._documents:
            key = _document_key(document)
            if key in positions:
                document["rerank_rank"] = positions[key]
        return self

    def select(self, selected: Iterable[Mapping[str, Any]]) -> RetrievalRecorder:
        """Mark which documents reached the model's context."""
        self._selected = {
            _document_key(_normalise_document(document, index))
            for index, document in enumerate(selected)
        }
        for document in self._documents:
            document["selected"] = _document_key(document) in self._selected
        return self

    def context(self, *, tokens: int | None = None, truncated: bool = False) -> RetrievalRecorder:
        self._context_tokens = tokens
        self._truncated = truncated
        return self

    def flush(self) -> None:
        """Write everything onto the span. Called automatically on block exit."""
        self.span.record_retrieval(
            query=self.query,
            rewritten_query=self.rewritten_query,
            documents=self._documents,
            retriever_name=self.retriever_name,
            retriever_version=self.retriever_version,
            knowledge_base_version=self.knowledge_base_version,
            search_type=self.search_type,
            top_k=self.top_k,
            filters=self.filters or None,
            embedding_model=self.embedding_model,
            embedding_latency_ms=self._embedding_ms,
            reranker_model=self.reranker_model,
            reranker_latency_ms=self._rerank_ms,
            retrieval_latency_ms=self._retrieval_ms,
            context_tokens=self._context_tokens,
            context_truncated=self._truncated,
        )


@contextmanager
def retrieval_span(
    name: str = "retrieval",
    *,
    client: Client | None = None,
    query: str | None = None,
    **options: Any,
) -> Iterator[RetrievalRecorder]:
    """Open a retrieval span and yield a recorder for its stages."""
    tracer = client or get_client()
    with tracer.span(name, kind="client", category="retrieval") as span:
        recorder = RetrievalRecorder(span=span, query=query, **options)
        try:
            yield recorder
        finally:
            recorder.flush()


@dataclass(slots=True)
class AgentRecorder:
    """Tracks an agent loop, numbering steps and enforcing a step budget.

    agent = AgentRecorder(agent_id="support", goal=goal, max_steps=8)
    while not done:
        with agent.step("decide") as span:
            choice = decide()
            span.record_agent_step(**agent.decision(summary=choice.reason))
        with agent.step(f"tool.{choice.tool}") as span:
            span.record_agent_step(**agent.tool_call(tool=choice.tool, args=choice.args))
    agent.terminate("completed")
    """

    agent_id: str
    goal: str | None = None
    agent_version: str | None = None
    max_steps: int | None = None
    client: Client | None = None
    _step: int = 0
    _branch: str | None = None
    _terminated: bool = False

    @property
    def current_step(self) -> int:
        return self._step

    @property
    def budget_exhausted(self) -> bool:
        """Whether the configured step budget has been reached.

        An agent with no step cap is an agent that can bill indefinitely; the
        recorder exposes the check so the loop condition is explicit.
        """
        return self.max_steps is not None and self._step >= self.max_steps

    def _tracer(self) -> Client:
        return self.client or get_client()

    @contextmanager
    def step(self, name: str, *, category: str = "agent_decision") -> Iterator[Span]:
        """Open a span for the next step."""
        self._step += 1
        with self._tracer().span(name, kind="internal", category=category) as span:
            yield span

    def _base(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "agent_id": self.agent_id,
            "step_number": self._step,
            "max_steps": self.max_steps,
        }
        if self.agent_version:
            payload["agent_version"] = self.agent_version
        if self.goal:
            payload["goal"] = self.goal
        if self._branch:
            payload["branch_id"] = self._branch
        if self._step > 1:
            payload["parent_step"] = self._step - 1
        return payload

    def decision(self, *, summary: str | None = None) -> dict[str, Any]:
        return {**self._base(), "step_type": "decision", "decision_summary": summary}

    def tool_call(
        self,
        *,
        tool: str,
        args: Mapping[str, Any] | None = None,
        status: str = "ok",
        retry_of: int | None = None,
        result_ref: str | None = None,
    ) -> dict[str, Any]:
        return {
            **self._base(),
            "step_type": "tool_call",
            "tool_name": tool,
            "tool_arguments": dict(args or {}),
            "tool_status": status,
            "retry_of": retry_of,
            "tool_result_ref": result_ref,
        }

    def handoff(self, *, target: str, summary: str | None = None) -> dict[str, Any]:
        return {
            **self._base(),
            "step_type": "handoff",
            "handoff_target": target,
            "decision_summary": summary,
        }

    def approval(self, *, status: str) -> dict[str, Any]:
        return {
            **self._base(),
            "step_type": "approval",
            "approval_required": True,
            "approval_status": status,
        }

    def memory(self, *, read: Iterable[str] = (), write: Iterable[str] = ()) -> dict[str, Any]:
        return {
            **self._base(),
            "step_type": "memory",
            "memory_read_keys": list(read),
            "memory_write_keys": list(write),
        }

    def branch(self, branch_id: str | None) -> AgentRecorder:
        self._branch = branch_id
        return self

    def terminate(self, reason: str) -> None:
        """Record why the trajectory ended.

        Always call this, including on the failure paths. "Why did the agent
        stop?" is the first question asked about any agent trace, and an
        unterminated trajectory cannot answer it.
        """
        if self._terminated:
            return
        self._terminated = True
        self._step += 1
        with self._tracer().span("agent.terminate", category="agent_decision") as span:
            span.record_agent_step(
                agent_id=self.agent_id,
                step_number=self._step,
                step_type="terminate",
                termination_reason=reason,
                max_steps=self.max_steps,
            )


@contextmanager
def agent_span(
    name: str, *, client: Client | None = None, category: str = "agent_decision"
) -> Iterator[Span]:
    """Open a single agent span without the loop bookkeeping."""
    tracer = client or get_client()
    with tracer.span(name, kind="internal", category=category) as span:
        yield span


def _normalise_document(document: Mapping[str, Any] | Any, index: int) -> dict[str, Any]:
    """Accept dicts, dataclasses and ORM-ish objects uniformly."""
    if isinstance(document, Mapping):
        item = dict(document)
    else:
        item = {
            key: getattr(document, key)
            for key in (
                "document_id",
                "id",
                "chunk_id",
                "score",
                "rerank_score",
                "source",
                "title",
                "content",
                "text",
                "token_count",
                "metadata",
            )
            if hasattr(document, key)
        }
    if "document_id" not in item:
        item["document_id"] = str(item.pop("id", f"doc-{index}"))
    if "content" not in item and "text" in item:
        item["content"] = item.pop("text")
    item.setdefault("rank", index)
    return item


def _document_key(document: Mapping[str, Any]) -> str:
    return str(document.get("chunk_id") or document.get("document_id"))
