# Instrumenting a RAG pipeline

The goal is to be able to answer "why was this answer wrong?" without adding
print statements. That means recording the query as rewritten, every document
considered, and which ones actually reached the model.

## The shape

```python
from aiobs import Client

client = Client(service_name="support-rag")

def answer(question: str, session_id: str) -> str:
    with client.trace("answer-question", session_id=session_id) as trace:

        # 1. Rewrite — a separate span, because it is a separate model call
        with trace.span("rewrite-query", category="llm_generation") as span:
            span.record_model(provider="openai", model="gpt-4o-mini")
            rewritten = rewrite(question)
            span.record_usage(input_tokens=..., output_tokens=..., source="provider")

        # 2. Retrieve
        with trace.span("vector-search", category="retrieval") as span:
            candidates = index.search(rewritten, k=20)
            reranked = reranker.rank(rewritten, candidates)
            selected = fit_to_budget(reranked, tokens=4000)

            span.record_retrieval(
                query=question,
                rewritten_query=rewritten,
                retriever_name="pgvector-primary",
                knowledge_base_version="kb-2026-07",
                search_type="hybrid",
                embedding_model="text-embedding-3-small",
                reranker_model="rerank-v3",
                context_tokens=sum(d.tokens for d in selected),
                documents=[
                    {
                        "document_id": d.id,
                        "chunk_id": d.chunk_id,
                        "rank": d.original_rank,       # before reranking
                        "rerank_rank": d.rank,          # after
                        "score": d.score,
                        "rerank_score": d.rerank_score,
                        "title": d.title,
                        "content": d.text,
                        "source": d.url,
                        "token_count": d.tokens,
                        "selected": d in selected,      # did it reach the model
                    }
                    for d in reranked
                ],
            )

        # 3. Generate
        with trace.span("generate", category="chat_completion") as span:
            span.set_lineage(
                prompt_name="rag-answer",
                prompt_version_id=prompt.version_id,
            )
            span.record_model(provider="openai", model="gpt-4o")
            response = generate(prompt.render(context=selected, question=question))
            span.record_usage_from(response)
            return response.text
```

## The three fields that earn their keep

**`rank` and `rerank_rank` together.** Post-rerank order alone tells you what the
reranker decided. The _movement_ tells you whether it earned its 90 ms.

**`selected`.** The difference between retrieved and used. A pipeline fetching
20 and using 3 has an 85% unused ratio, and that single number is the most
actionable retrieval signal there is.

**`rewritten_query`.** When retrieval fails, the first question is whether the
query it actually ran resembles what the user asked. Recording only the original
makes that unanswerable.

## What the platform derives

Once the documents are recorded:

- Score distribution and **margin** — a tiny margin between the top results
  means the ranking is close to arbitrary
- **Rank movement** — how much reranking changed the order
- **Duplicates and near-duplicates** — the context spending tokens on repeated
  material
- **Missing sources** — documents whose answers cannot be verified
- **Unused ratio** and context token count

See [concepts/retrieval.md](../concepts/retrieval.md) for what each means when
it fires.

## Reading a bad answer

1. Open the trace. **Retrieval** tab.
2. Was anything retrieved? `empty_result` means the model answered with no
   context — an index outage or a query that embedded to nothing.
3. Is the right document in the list at all? If not, it is a retrieval problem:
   embedding, chunking or the index.
4. Is it in the list but not selected? A context-budget problem. Look at
   `truncated_count` and the token counts.
5. Is it selected and the answer is still wrong? A generation problem. Check the
   prompt version and compare against a trace that worked.

That sequence is the whole reason the pipeline is instrumented this way: each
step eliminates a layer.

## Cost

Both the rewrite and the generation are priced, because both have a provider, a
model and usage. Embedding calls too. The cost dashboard grouped by span
category shows what the pipeline actually spends where — and the rewrite step is
usually more than people expect.

## See also

- [Retrieval concepts](../concepts/retrieval.md)
- [Instrumenting an agent](instrumenting-agents.md)
