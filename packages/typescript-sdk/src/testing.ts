/**
 * Test utilities.
 *
 * Instrumented code should be testable without a running platform, and
 * assertions should be about what was *recorded*, not about whether an HTTP
 * call happened.
 *
 * ```ts
 * const { client, captured } = createTestClient();
 * await answerQuestion(client, 'how do refunds work?');
 * await client.flush();
 * expect(captured.ofCategory('chat_completion')[0].usage.input_tokens).toBeGreaterThan(0);
 * ```
 */

import type { WireSpan } from "@aiobs/schemas";

import type { ConfigInput } from "./config.js";
import { MemoryTransport } from "./exporter.js";
import { Client } from "./tracer.js";

export class CapturedSpans {
  constructor(private readonly transport: MemoryTransport) {}

  get all(): WireSpan[] {
    return this.transport.spans;
  }

  get length(): number {
    return this.transport.spans.length;
  }

  named(name: string): WireSpan[] {
    return this.all.filter((span) => span.name === name);
  }

  ofCategory(category: string): WireSpan[] {
    return this.all.filter((span) => span.category === category);
  }

  errors(): WireSpan[] {
    return this.all.filter((span) => span.status === "error");
  }

  roots(): WireSpan[] {
    return this.all.filter((span) => !span.parent_span_id);
  }

  traceIds(): Set<string> {
    return new Set(this.all.map((span) => span.trace_id));
  }

  clear(): void {
    this.transport.clear();
  }

  /**
   * Assert the structural invariants the platform enforces on ingest.
   *
   * Running this in a unit test catches broken instrumentation before it turns
   * into a batch of rejected spans in production.
   */
  assertWellFormed(): void {
    const spans = this.all;
    if (spans.length === 0) throw new Error("no spans were recorded");
    const byId = new Map(spans.map((span) => [span.span_id, span]));
    for (const span of spans) {
      if (!/^[0-9a-f]{32}$/.test(span.trace_id)) {
        throw new Error(`${span.name}: malformed trace id`);
      }
      if (!/^[0-9a-f]{16}$/.test(span.span_id)) {
        throw new Error(`${span.name}: malformed span id`);
      }
      if (
        span.end_time_unix_nano === null ||
        span.end_time_unix_nano === undefined
      ) {
        throw new Error(`${span.name}: span was never ended`);
      }
      if (span.end_time_unix_nano < span.start_time_unix_nano) {
        throw new Error(`${span.name}: negative duration`);
      }
      const parent = span.parent_span_id
        ? byId.get(span.parent_span_id)
        : undefined;
      if (parent && parent.trace_id !== span.trace_id) {
        throw new Error(
          `${span.name}: child and parent are in different traces`,
        );
      }
    }
  }
}

export interface TestHarness {
  client: Client;
  captured: CapturedSpans;
  transport: MemoryTransport;
}

export function createTestClient(overrides: ConfigInput = {}): TestHarness {
  const transport = new MemoryTransport();
  const client = new Client(
    {
      endpoint: "http://test.invalid",
      apiKey: "test-key",
      serviceName: "test-service",
      // Export inline so a test never has to wait on a timer.
      maxBatchSize: 1,
      flushIntervalMs: 10,
      ...overrides,
    },
    transport,
  );
  return { client, captured: new CapturedSpans(transport), transport };
}
