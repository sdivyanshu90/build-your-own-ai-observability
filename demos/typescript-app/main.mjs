#!/usr/bin/env node
/**
 * TypeScript SDK demo.
 *
 * The Node counterpart of demos/rag-application: one trace containing a
 * retrieval step, a model call and an agent step, using the callback style so
 * every span closes even when the body throws.
 *
 * Run:
 *   AIOBS_ENDPOINT=http://localhost:58000 AIOBS_API_KEY=aiobs_... \
 *     node demos/typescript-app/main.mjs
 *
 * Without an API key it still runs and reports what it would have sent.
 */

import { init } from '../../packages/typescript-sdk/dist/index.js';

/** Deterministic stand-in for a model provider: no keys, no network, no cost. */
function mockComplete(messages) {
  const prompt = messages.map((m) => m.content).join('\n');
  const answer =
    'Refunds are available within 30 days of delivery, provided the item is unused.';
  return {
    id: `mock-${prompt.length.toString(16)}`,
    content: answer,
    usage: {
      prompt_tokens: Math.max(1, Math.floor(prompt.length / 4)),
      completion_tokens: Math.max(1, Math.floor(answer.length / 4)),
    },
  };
}

const DOCUMENTS = [
  { id: 'refund-policy', score: 0.94, content: 'Refunds within 30 days of delivery.', token_count: 24 },
  { id: 'damaged-items', score: 0.88, content: 'Damaged items are replaced at no cost.', token_count: 22 },
  { id: 'returns-process', score: 0.71, content: 'Start a return from the Orders page.', token_count: 20 },
  { id: 'shipping', score: 0.42, content: 'Standard shipping takes 3-5 business days.', token_count: 21 },
];

async function main() {
  const client = init({ serviceName: 'typescript-demo', serviceVersion: '1.0.0' });
  console.log(
    `endpoint=${client.config.endpoint} authenticated=${Boolean(client.config.apiKey)}`,
  );

  const traceIds = [];

  for (let index = 0; index < 3; index += 1) {
    const question = `How do refunds work? (request ${index})`;

    await client.trace(
      'typescript-support-request',
      { subjectId: `user-${index}`, tags: ['demo', 'typescript'] },
      async (trace) => {
        traceIds.push(trace.traceId);
        trace.setInput(question);

        // Retrieval, with pre- and post-rerank positions so the UI can show
        // what the reranker did.
        const selected = await trace.span(
          'vector-search',
          { kind: 'client', category: 'retrieval' },
          async (span) => {
            const reranked = [...DOCUMENTS]
              .map((document, rank) => ({ ...document, rank }))
              .sort((left, right) => right.score - left.score)
              .map((document, rerankRank) => ({ ...document, rerank_rank: rerankRank }));
            const chosen = reranked.slice(0, 2).map((document) => ({ ...document, selected: true }));
            const rest = reranked.slice(2);

            span.recordRetrieval({
              query: question,
              rewrittenQuery: 'refund policy 30 days',
              documents: [...chosen, ...rest],
              retrieverName: 'demo-hybrid',
              knowledgeBaseVersion: 'kb-demo-2026-07',
              searchType: 'hybrid',
              topK: DOCUMENTS.length,
              embeddingModel: 'mock-embedding-v1',
              embeddingLatencyMs: 18.4,
              rerankerModel: 'mock-reranker-v1',
              rerankerLatencyMs: 31.2,
              retrievalLatencyMs: 44.9,
              contextTokens: chosen.reduce((total, item) => total + item.token_count, 0),
            });
            return chosen;
          },
        );

        // Generation.
        const answer = await trace.span(
          'generate',
          { kind: 'client', category: 'chat_completion' },
          async (span) => {
            const messages = [
              { role: 'system', content: 'Answer using only the provided context.' },
              {
                role: 'user',
                content: `Context:\n${selected.map((d) => d.content).join('\n')}\n\nQ: ${question}`,
              },
            ];
            span.recordModel({ provider: 'mock', model: 'mock-model-v1', temperature: 0.1 });
            span.setInput(messages);
            span.setLineage({
              promptName: 'rag-answer',
              promptVersionId: 'pmv_DEMO_TS_V1',
              knowledgeBaseVersion: 'kb-demo-2026-07',
            });

            const response = mockComplete(messages);
            span.recordFirstToken();
            span.setOutput(response.content);
            span.recordUsage({
              inputTokens: response.usage.prompt_tokens,
              outputTokens: response.usage.completion_tokens,
              raw: response.usage,
            });
            return response.content;
          },
        );

        // One agent step, so the trajectory view has something to render.
        await trace.span('agent.decide', { category: 'agent_decision' }, async (span) => {
          span.recordAgentStep({
            agentId: 'ts-support-agent',
            stepNumber: 1,
            stepType: 'decision',
            decisionSummary: 'Answer directly from the retrieved policy',
            maxSteps: 4,
          });
        });

        trace.setOutput(answer);
        console.log(`  request ${index}: ${answer.slice(0, 56)}...`);
      },
    );
  }

  await client.shutdown();
  console.log(`\ntrace ids: ${traceIds.map((id) => id.slice(0, 16)).join(', ')}`);
  console.log(`exporter: ${JSON.stringify(client.stats())}`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
