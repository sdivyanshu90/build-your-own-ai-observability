import { afterEach, describe, expect, it } from "vitest";

import { createTestClient, type TestHarness } from "../src/testing";
import { semconv } from "@aiobs/schemas";

let harness: TestHarness | null = null;

function client(overrides = {}): TestHarness {
  harness = createTestClient(overrides);
  return harness;
}

afterEach(async () => {
  await harness?.client.shutdown().catch(() => undefined);
  harness = null;
});

describe("tracing", () => {
  it("records a trace and its children under one trace id", async () => {
    const { client: sdk, captured } = client();

    await sdk.trace("checkout", async (trace) => {
      await sdk.span(
        "retrieve",
        { category: "retrieval" },
        async () => undefined,
      );
      await sdk.span(
        "generate",
        { category: "chat_completion" },
        async () => undefined,
      );
      trace.setStatus("ok");
    });
    await sdk.flush();

    captured.assertWellFormed();
    expect(captured.traceIds().size).toBe(1);
    expect(captured.roots()).toHaveLength(1);
    expect(captured.named("retrieve")).toHaveLength(1);
  });

  it("parents child spans to the enclosing span, not to the root", async () => {
    const { client: sdk, captured } = client();

    await sdk.trace("outer", async () => {
      await sdk.span("middle", {}, async () => {
        await sdk.span("inner", {}, async () => undefined);
      });
    });
    await sdk.flush();

    const middle = captured.named("middle")[0]!;
    const inner = captured.named("inner")[0]!;
    expect(inner.parent_span_id).toBe(middle.span_id);
  });

  it("marks a span as errored when the body throws, and re-raises", async () => {
    const { client: sdk, captured } = client();

    await expect(
      sdk.trace("failing", async () => {
        throw new Error("upstream exploded");
      }),
    ).rejects.toThrow("upstream exploded");
    await sdk.flush();

    const errors = captured.errors();
    expect(errors).toHaveLength(1);
    // Instrumentation must never swallow the application's exception.
    expect(errors[0]!.status_message).toContain("upstream exploded");
  });

  it("never lets instrumentation break the application", async () => {
    const { client: sdk } = client();
    const span = sdk.startSpan("resilient");
    // A value that cannot be serialised must not throw out of the SDK.
    const circular: Record<string, unknown> = {};
    circular.self = circular;
    expect(() => span.setAttribute("bad", circular)).not.toThrow();
    span.end();
  });

  it("produces propagation headers that round-trip", async () => {
    const { client: sdk } = client();
    const span = sdk.startSpan("outbound");
    const headers = span.headers();
    expect(headers.traceparent).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$/);
    span.end();
  });
});

describe("usage and cost attributes", () => {
  it("records token counts under the platform namespace", async () => {
    const { client: sdk, captured } = client();

    await sdk.trace("generate", async (trace) => {
      trace.recordModel({ provider: "openai", model: "gpt-4o" });
      trace.recordUsage({
        inputTokens: 1200,
        outputTokens: 340,
        source: "provider",
      });
    });
    await sdk.flush();

    const span = captured.all[0]!;
    expect(span.usage?.input_tokens).toBe(1200);
    expect(span.usage?.output_tokens).toBe(340);
    expect(span.usage?.source).toBe("provider");
  });

  it("keeps token counts intact through redaction", async () => {
    // Regression: a substring rule on "token" once zeroed every usage count
    // and every time-to-first-token measurement.
    const { client: sdk, captured } = client();

    await sdk.trace("generate", async (trace) => {
      trace.recordUsage({ inputTokens: 999, outputTokens: 111 });
      trace.setAttribute(semconv.LATENCY_TIME_TO_FIRST_TOKEN_MS, 123.4);
    });
    await sdk.flush();

    const span = captured.all[0]!;
    expect(span.usage?.input_tokens).toBe(999);
    expect(span.attributes[semconv.LATENCY_TIME_TO_FIRST_TOKEN_MS]).toBe(123.4);
  });
});

describe("redaction", () => {
  it("removes an application secret from attributes", async () => {
    const { client: sdk, captured } = client();

    await sdk.trace("call", async (trace) => {
      trace.setAttribute(
        "http.request.header.authorization",
        "Bearer super-secret-value",
      );
      trace.setAttribute("app.api_key", "sk-live-1234567890");
      trace.setAttribute("app.user_count", 42);
    });
    await sdk.flush();

    const attributes = captured.all[0]!.attributes;
    expect(JSON.stringify(attributes)).not.toContain("super-secret-value");
    expect(JSON.stringify(attributes)).not.toContain("sk-live-1234567890");
    // Ordinary attributes are untouched.
    expect(attributes["app.user_count"]).toBe(42);
  });

  it("scrubs high-confidence secrets found inside free text", async () => {
    const { client: sdk, captured } = client();

    await sdk.trace("call", async (trace) => {
      trace.setAttribute("app.note", "key AKIAIOSFODNN7EXAMPLE was rotated");
    });
    await sdk.flush();

    expect(String(captured.all[0]!.attributes["app.note"])).not.toContain(
      "AKIAIOSFODNN7EXAMPLE",
    );
  });
});

describe("sampling", () => {
  it("exports nothing when the sample rate is zero", async () => {
    const { client: sdk, captured } = client({ sampleRate: 0 });

    await sdk.trace("dropped", async () => undefined);
    await sdk.flush();

    expect(captured.length).toBe(0);
  });

  it("keeps a sampled trace whole rather than sampling per span", async () => {
    const { client: sdk, captured } = client({ sampleRate: 1 });

    await sdk.trace("kept", async () => {
      await sdk.span("child-a", {}, async () => undefined);
      await sdk.span("child-b", {}, async () => undefined);
    });
    await sdk.flush();

    expect(captured.length).toBe(3);
    expect(captured.traceIds().size).toBe(1);
  });

  it("exports nothing at all when disabled", async () => {
    const { client: sdk, captured } = client({ enabled: false });
    await sdk.trace("ignored", async () => undefined);
    await sdk.flush();
    expect(captured.length).toBe(0);
  });
});

describe("retrieval and agent instrumentation", () => {
  it("records ranked documents with their selection state", async () => {
    const { client: sdk, captured } = client();

    await sdk.trace("rag", async (trace) => {
      trace.recordRetrieval({
        query: "refund policy",
        retrieverName: "pgvector",
        documents: [
          { documentId: "doc-1", rank: 0, score: 0.91, selected: true },
          { documentId: "doc-2", rank: 1, score: 0.42, selected: false },
        ],
      });
    });
    await sdk.flush();

    const retrieval = captured.all[0]!.retrieval;
    expect(retrieval?.documents).toHaveLength(2);
    expect(retrieval?.documents?.[0]?.selected).toBe(true);
    expect(retrieval?.documents?.[1]?.selected).toBe(false);
  });

  it("records an agent step without demanding hidden reasoning", async () => {
    const { client: sdk, captured } = client();

    await sdk.trace("agent", async (trace) => {
      trace.recordAgentStep({
        stepNumber: 1,
        stepType: "tool_call",
        agentId: "planner",
        toolName: "search_docs",
        decisionSummary: "Look up the refund policy before answering.",
      });
    });
    await sdk.flush();

    const step = captured.all[0]!.agent_step;
    expect(step?.step_number).toBe(1);
    expect(step?.tool_name).toBe("search_docs");
    // The field exists, is short, and is supplied by the application: the SDK
    // has no notion of chain-of-thought to capture.
    expect(step?.decision_summary).toContain("refund policy");
  });
});
