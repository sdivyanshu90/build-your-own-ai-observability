/**
 * AI Observability Platform -- TypeScript SDK.
 *
 * ```ts
 * import { init } from '@aiobs/sdk';
 *
 * const client = init({ serviceName: 'support-bot' });
 *
 * await client.trace('customer-support-request', async (trace) => {
 *   await trace.span('retrieve-context', { category: 'retrieval' }, async (span) => {
 *     span.recordRetrieval({ query, documents: hits, retrieverName: 'pgvector' });
 *   });
 *
 *   await trace.span('generate', { category: 'chat_completion' }, async (span) => {
 *     span.recordModel({ provider: 'anthropic', model: 'claude-sonnet-4' });
 *     const answer = await callModel(prompt);
 *     span.setOutput(answer);
 *     span.recordUsage({ inputTokens: 1200, outputTokens: 340 });
 *   });
 * });
 * ```
 *
 * Configuration comes from `AIOBS_ENDPOINT` and `AIOBS_API_KEY` by default.
 * Without an API key the SDK still builds spans -- which the test utilities
 * inspect -- but sends nothing, so instrumented code is safe to run anywhere.
 *
 * The SDK never throws into your application: every public method is wrapped so
 * an SDK bug produces a console warning rather than a failed request.
 */

export * from "./config.js";
export * from "./context.js";
export * from "./exporter.js";
export * from "./tracer.js";
export * from "./testing.js";
export { semconv } from "@aiobs/schemas";

export const VERSION = "0.1.0";
export const SDK_NAME = "aiobs-typescript";
