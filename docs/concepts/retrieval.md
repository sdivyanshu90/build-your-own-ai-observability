# Retrieval

## What is recorded

A retrieval span records the pipeline it ran and every document it saw:

```
query                 what the user asked
rewritten_query       what was actually embedded, if it was rewritten
retriever_name        pgvector-primary, elastic-hybrid, …
knowledge_base_version   which index snapshot
search_type           vector | keyword | hybrid
embedding_model, embedding_dimensions
reranker_model
retrieval_latency_ms, embedding_latency_ms, reranker_latency_ms
context_tokens, context_truncated
documents[]           the ranked list
```

Each document:

```
document_id, chunk_id
rank                  position before reranking
rerank_rank           position after reranking
score, rerank_score
title, content (or content_ref), source
token_count
selected              did this actually reach the model
truncated
```

## Why `selected` matters more than it looks

`selected` is the difference between "retrieved" and "used". A pipeline that
fetches 20 documents and puts 3 in the context has a 85% unused ratio, and that
is the single most actionable retrieval signal there is: either you are paying
embedding and rerank latency for nothing, or your context selection is dropping
documents that would have answered the question.

The UI shows unused documents rather than hiding them, for exactly this reason.

## Rank movement

Showing the post-rerank order alone tells you what the reranker decided. Showing
the _movement_ — rank 5 promoted to rank 1, rank 1 demoted to rank 6 — tells you
whether it earned its latency. A reranker that never moves anything is a
90 ms tax; one that moves everything is either doing the real work or is
uncorrelated with the retriever, and the score margins tell you which.

## Diagnostics

The platform computes these per retrieval step:

| Diagnostic               | What it means when it fires                                                                                                                             |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `empty_result`           | The model answered with no context at all. Almost always a bug or an index outage.                                                                      |
| `unused_ratio` high      | Over-fetching, or context selection is too aggressive                                                                                                   |
| `score_margin` tiny      | The top results are nearly tied; the ranking is close to arbitrary and small index changes will reorder it                                              |
| `duplicate_document_ids` | The same document retrieved twice — usually a chunking or dedup bug                                                                                     |
| `near_duplicate_pairs`   | Chunks with near-identical text. The context is spending tokens repeating itself. Detection compares truncated previews, so paraphrase is _not_ caught. |
| `truncated_count`        | Documents cut to fit the context budget. The model saw a fragment.                                                                                      |
| `missing_source_count`   | Documents with no source. Answers citing them cannot be verified.                                                                                       |
| `mean_rank_movement`     | How much the reranker changed the order                                                                                                                 |

Each is a signal, not a verdict. A high unused ratio is correct for a pipeline
that deliberately over-fetches and filters; the point is that you can see it.

## Instrumenting

```python
with trace.retrieval_span("vector-search") as span:
    documents = retriever.search(question, k=10)
    selected = select_context(documents, budget=4000)

    span.record_retrieval(
        query=question,
        rewritten_query=rewritten,
        retriever_name="pgvector-primary",
        knowledge_base_version="kb-2026-07",
        search_type="hybrid",
        documents=[
            RetrievalDocument(
                document_id=d.id,
                rank=i,
                score=d.score,
                rerank_rank=d.rerank_rank,
                rerank_score=d.rerank_score,
                title=d.title,
                content=d.text,
                source=d.url,
                token_count=d.tokens,
                selected=d in selected,
            )
            for i, d in enumerate(documents)
        ],
    )
```

`aiobs.retrieval.documents` is a **sensitive** attribute: retrieved text is
redacted before storage unless the environment is configured to keep payloads.
The derived `retrieval_documents` rows — ranks, scores, ids, selection — are
built _before_ redaction, so the diagnostics work regardless.

## See also

- [Data model](data-model.md#derived-rows)
- [Sampling and retention](sampling-and-retention.md)
- [Tutorial: instrumenting a RAG pipeline](../tutorials/instrumenting-rag.md)
