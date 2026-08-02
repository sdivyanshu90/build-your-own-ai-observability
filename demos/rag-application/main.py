#!/usr/bin/env python
"""Demo 2: an instrumented RAG pipeline.

Produces the trace shape the retrieval visualisation is built around:

    user query -> query rewrite -> embed -> hybrid search -> rerank
               -> context selection -> generation

Every stage is a span, every stage's latency is recorded, and the retrieval step
carries its ranked documents with pre- and post-rerank positions so the UI can
show what the reranker actually did.

Also exercises the cases that break naive instrumentation: an empty retrieval, a
reranker failure, and context truncation.

Run::

    python demos/rag-application/main.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiobs  # noqa: E402
from aiobs.integrations.retrieval import retrieval_span  # noqa: E402
from _shared.knowledge_base import KNOWLEDGE_BASE_VERSION, build_knowledge_base  # noqa: E402
from _shared.mock_provider import (  # noqa: E402
    MockEmbeddings,
    MockProvider,
    MockReranker,
    ProviderError,
)

PROMPT_NAME = "rag-answer"
PROMPT_VERSION_ID = "pmv_DEMO_RAG_V2"
DATASET_NAME = "support-eval"
DATASET_VERSION_ID = "dsv_DEMO_SUPPORT_V1"

#: Deliberately includes a question the corpus cannot answer, so the demo
#: produces at least one empty retrieval.
QUESTIONS = (
    "How do refunds work for damaged items?",
    "How long does express shipping take?",
    "What does the warranty cover?",
    "What is the airspeed velocity of an unladen swallow?",
)

#: Small enough that some answers must drop documents, which is what makes the
#: "retrieved but unused" diagnostic show something.
CONTEXT_TOKEN_BUDGET = 220


def rewrite_query(client: aiobs.Client, question: str) -> str:
    """Expand the question into search terms.

    A cheap model call in its own right, and one whose cost is easy to forget:
    tracing it separately is how a team discovers that query rewriting is 30%
    of their bill.
    """
    with client.span("rewrite-query", kind="client", category="llm_generation") as span:
        span.record_model(provider="mock", model="mock-model-v1", operation="chat")
        span.set_input(question)
        rewritten = " ".join(
            word
            for word in question.lower().rstrip("?").split()
            if word not in {"how", "do", "does", "what", "is", "the", "a", "an", "for", "of"}
        )
        span.set_output(rewritten)
        span.record_usage(
            input_tokens=max(1, len(question) // 4),
            output_tokens=max(1, len(rewritten) // 4),
        )
        return rewritten


def select_context(hits: list[dict], budget: int) -> tuple[list[dict], bool]:
    """Fill the context budget greedily, reporting whether anything was cut."""
    selected: list[dict] = []
    used = 0
    truncated = False
    for hit in hits:
        tokens = int(hit.get("token_count") or 0)
        if used + tokens > budget:
            truncated = True
            continue
        selected.append(hit)
        used += tokens
    return selected, truncated


def answer(
    client: aiobs.Client,
    question: str,
    *,
    knowledge_base,
    embeddings: MockEmbeddings,
    reranker: MockReranker,
    provider: MockProvider,
    force_empty: bool = False,
) -> str:
    with client.trace(
        "knowledge-base-search",
        subject_id=f"user-{abs(hash(question)) % 1000}",
        tags=["demo", "rag"],
    ) as trace:
        trace.set_input(question)
        rewritten = rewrite_query(client, question)

        with retrieval_span(
            "hybrid-retrieval",
            query=question,
            retriever_name="demo-hybrid",
            retriever_version="2026.07",
            knowledge_base_version=KNOWLEDGE_BASE_VERSION,
            search_type="hybrid",
            top_k=6,
            embedding_model=embeddings.model,
            reranker_model=reranker.model,
        ) as recorder:
            recorder.rewritten_query = rewritten

            with recorder.time_embedding():
                vector = embeddings.embed(rewritten)

            with recorder.time_retrieval():
                hits = (
                    knowledge_base.search_empty()
                    if force_empty
                    else knowledge_base.search(vector, top_k=6)
                )
            recorder.documents(hits)

            if hits:
                try:
                    with recorder.time_rerank():
                        reranked = reranker.rerank(rewritten, hits)
                    recorder.rerank(reranked)
                    ordered = reranked
                except ProviderError:
                    # A reranker failure must degrade to the base ranking, not
                    # fail the request. The span records that it happened.
                    recorder.span.add_event("aiobs.reranker_failed", model=reranker.model)
                    recorder.span.set_attribute("aiobs.retrieval.reranker_failed", True)
                    ordered = hits
            else:
                ordered = []

            selected, truncated = select_context(ordered, CONTEXT_TOKEN_BUDGET)
            recorder.select(selected)
            recorder.context(
                tokens=sum(int(item.get("token_count") or 0) for item in selected),
                truncated=truncated,
            )
            recorder.span.set_lineage(
                dataset_name=DATASET_NAME,
                dataset_version_id=DATASET_VERSION_ID,
                knowledge_base_version=KNOWLEDGE_BASE_VERSION,
            )

        if not selected:
            # No context: answer honestly rather than hallucinating, and record
            # that this is why.
            with trace.span("no-context-response", category="workflow_step") as span:
                span.set_attribute("aiobs.rag.no_context", True)
                span.set_status("ok", "no relevant documents were retrieved")
            trace.set_output("I could not find anything about that in the knowledge base.")
            trace.set_tags("empty-retrieval")
            return "I could not find anything about that in the knowledge base."

        context = "\n\n".join(
            f"[{item['document_id']}] {item['content']}" for item in selected
        )
        messages = [
            {
                "role": "system",
                "content": "Answer using only the provided context. Cite document ids.",
            },
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"},
        ]

        with trace.span("generate", kind="client", category="chat_completion") as span:
            span.record_model(provider=provider.provider, model=provider.model, temperature=0.1)
            span.set_input(messages)
            span.set_lineage(
                prompt_name=PROMPT_NAME,
                prompt_version_id=PROMPT_VERSION_ID,
                dataset_name=DATASET_NAME,
                dataset_version_id=DATASET_VERSION_ID,
                knowledge_base_version=KNOWLEDGE_BASE_VERSION,
            )
            response = provider.complete(messages, temperature=0.1)
            span.record_first_token()
            span.set_output(response.content)
            span.record_usage(
                input_tokens=response.usage["prompt_tokens"],
                output_tokens=response.usage["completion_tokens"],
                cached_input_tokens=response.usage.get("cached_tokens") or None,
                raw=response.usage,
            )
            span.set_attribute("aiobs.rag.context_documents", len(selected))

        trace.set_output(response.content)
        return response.content


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Instrumented RAG demo")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--reranker-failure-rate", type=float, default=0.2)
    arguments = parser.parse_args(argv)

    client = aiobs.init(service_name="rag-demo", service_version="2.0.0")
    embeddings = MockEmbeddings()
    knowledge_base = build_knowledge_base(embeddings)
    reranker = MockReranker(failure_rate=arguments.reranker_failure_rate)
    provider = MockProvider(cache_hit_rate=0.4)

    print(
        f"endpoint={client.config.endpoint} "
        f"authenticated={bool(client.config.api_key)} "
        f"corpus={knowledge_base.size} documents ({KNOWLEDGE_BASE_VERSION})"
    )

    for _ in range(arguments.iterations):
        for index, question in enumerate(QUESTIONS):
            text = answer(
                client,
                question,
                knowledge_base=knowledge_base,
                embeddings=embeddings,
                reranker=reranker,
                provider=provider,
                # The last question has no answer in the corpus; force the
                # empty path so the demo always produces one.
                force_empty=index == len(QUESTIONS) - 1,
            )
            print(f"  {question[:48]:50} -> {text[:56]}...")

    client.shutdown()
    print(f"\nexporter: {client.stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
