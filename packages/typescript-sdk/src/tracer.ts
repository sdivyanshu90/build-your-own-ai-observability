/**
 * The tracing API: `Client`, `Trace` and `Span`.
 *
 * Two calling styles are supported, and both matter:
 *
 * **Callback** (`client.trace(name, async (t) => ...)`) -- the span always
 * ends, even when the body throws, because the SDK owns the `finally`.
 *
 * **Manual** (`const span = client.startSpan(...); ... span.end()`) -- required
 * when the work does not fit a single lexical scope, such as a streamed
 * response whose span must outlive the function that started it.
 *
 * Every public method is guarded: an SDK bug produces a console warning, never
 * an exception in the caller's request path.
 */

import {
  semconv,
  type SpanCategory,
  type SpanKind,
  type SpanStatus,
  type WireSpan,
} from "@aiobs/schemas";

import { canExport, fromEnv, type Config, type ConfigInput } from "./config.js";
import {
  childContext,
  getCurrentContext,
  inject,
  isSampled,
  newRootContext,
  withContext,
  type SpanContext,
} from "./context.js";
import {
  BatchExporter,
  HttpTransport,
  type ExporterStats,
  type Transport,
} from "./exporter.js";
import { Redactor } from "./redaction.js";

const NS_PER_MS = 1_000_000n;

function nowNanos(): number {
  // Date.now() is millisecond resolution; that is enough for span boundaries and
  // avoids depending on a high-resolution clock that differs across runtimes.
  return Number(BigInt(Date.now()) * NS_PER_MS);
}

function guard<T>(label: string, action: () => T, fallback: T): T {
  try {
    return action();
  } catch (error) {
    console.warn(`aiobs: ${label} failed:`, error);
    return fallback;
  }
}

export interface SpanOptions {
  kind?: SpanKind;
  category?: SpanCategory;
  attributes?: Record<string, unknown>;
  parent?: SpanContext | null;
}

export interface TraceOptions extends SpanOptions {
  sessionId?: string;
  /** A *pseudonymous* identifier. Never an email: it is stored unredacted so
   *  that per-user cost attribution works. */
  subjectId?: string;
  tags?: string[];
  release?: string;
}

export interface UsageInput {
  inputTokens?: number;
  outputTokens?: number;
  totalTokens?: number;
  cachedInputTokens?: number;
  cacheWriteTokens?: number;
  reasoningTokens?: number;
  /** `estimated` when the numbers came from a local tokeniser, not the provider. */
  source?: "provider" | "estimated" | "reconciled" | "missing";
  raw?: Record<string, unknown>;
}

export interface ModelInput {
  provider: string;
  model: string;
  operation?: string;
  temperature?: number;
  topP?: number;
  maxTokens?: number;
  seed?: number;
  responseModel?: string;
  finishReasons?: string[];
  systemFingerprint?: string;
}

export interface RetrievalInput {
  query?: string;
  rewrittenQuery?: string;
  documents?: Record<string, unknown>[];
  retrieverName?: string;
  retrieverVersion?: string;
  knowledgeBaseVersion?: string;
  searchType?: string;
  topK?: number;
  filters?: Record<string, unknown>;
  embeddingModel?: string;
  embeddingDimensions?: number;
  embeddingLatencyMs?: number;
  rerankerModel?: string;
  rerankerLatencyMs?: number;
  retrievalLatencyMs?: number;
  contextTokens?: number;
  contextTruncated?: boolean;
}

export interface AgentStepInput {
  agentId: string;
  stepNumber: number;
  stepType?: string;
  agentVersion?: string;
  goal?: string;
  parentStep?: number;
  decisionSummary?: string;
  toolName?: string;
  toolArguments?: Record<string, unknown>;
  toolResultRef?: string;
  toolStatus?: string;
  handoffTarget?: string;
  memoryReadKeys?: string[];
  memoryWriteKeys?: string[];
  retryOf?: number;
  branchId?: string;
  loopIteration?: number;
  approvalRequired?: boolean;
  approvalStatus?: string;
  terminationReason?: string;
  maxSteps?: number;
}

export interface LineageInput {
  promptName?: string;
  promptVersionId?: string;
  promptVersionLabel?: string;
  promptVariables?: Record<string, unknown>;
  modelConfigId?: string;
  datasetName?: string;
  datasetVersionId?: string;
  datasetRecordId?: string;
  knowledgeBaseVersion?: string;
  experimentId?: string;
  experimentRunId?: string;
}

export class Span {
  readonly context: SpanContext;
  name: string;
  private category: SpanCategory;
  private readonly kind: SpanKind;
  private readonly parentId: string | null;
  private readonly startNanos: number;
  private endNanos: number | null = null;
  private status: SpanStatus = "unset";
  private statusMessage: string | null = null;
  private readonly attributes: Record<string, unknown>;
  private readonly events: {
    name: string;
    time_unix_nano: number;
    attributes: Record<string, unknown>;
  }[] = [];
  private readonly links: {
    trace_id: string;
    span_id: string;
    attributes: Record<string, unknown>;
  }[] = [];
  private usage: Record<string, unknown> | null = null;
  private retrieval: Record<string, unknown> | null = null;
  private agentStep: Record<string, unknown> | null = null;
  private lineage: Record<string, unknown> = {};
  protected traceFields: Record<string, unknown> = {};
  private ended = false;
  private firstTokenAt: number | null = null;

  constructor(
    protected readonly client: Client,
    name: string,
    context: SpanContext,
    parentId: string | null,
    options: SpanOptions,
  ) {
    this.context = context;
    this.name = name.slice(0, 512);
    this.kind = options.kind ?? "internal";
    this.category = options.category ?? "custom";
    this.parentId = parentId;
    this.startNanos = nowNanos();
    this.attributes = { ...(options.attributes ?? {}) };
  }

  get traceId(): string {
    return this.context.traceId;
  }

  get spanId(): string {
    return this.context.spanId;
  }

  /** Propagation headers for an outbound HTTP request or queue message. */
  headers(): Record<string, string> {
    return inject(this.context);
  }

  setAttribute(key: string, value: unknown): this {
    return guard(
      "setAttribute",
      () => {
        this.attributes[key] = value;
        return this;
      },
      this,
    );
  }

  setAttributes(attributes: Record<string, unknown>): this {
    return guard(
      "setAttributes",
      () => {
        Object.assign(this.attributes, attributes);
        return this;
      },
      this,
    );
  }

  addEvent(name: string, attributes: Record<string, unknown> = {}): this {
    return guard(
      "addEvent",
      () => {
        this.events.push({ name, time_unix_nano: nowNanos(), attributes });
        return this;
      },
      this,
    );
  }

  /** Relate this span to one that is not its parent (retry, fan-in, sub-graph). */
  addLink(
    context: SpanContext,
    attributes: Record<string, unknown> = {},
  ): this {
    return guard(
      "addLink",
      () => {
        this.links.push({
          trace_id: context.traceId,
          span_id: context.spanId,
          attributes,
        });
        return this;
      },
      this,
    );
  }

  setStatus(status: SpanStatus, message?: string): this {
    this.status = status;
    if (message) this.statusMessage = message.slice(0, 4000);
    return this;
  }

  recordException(error: unknown): this {
    return guard(
      "recordException",
      () => {
        const err = error instanceof Error ? error : new Error(String(error));
        this.events.push({
          name: "exception",
          time_unix_nano: nowNanos(),
          attributes: {
            [semconv.EXCEPTION_TYPE]: err.name,
            [semconv.EXCEPTION_MESSAGE]: err.message.slice(0, 4000),
            [semconv.EXCEPTION_STACKTRACE]: (err.stack ?? "").slice(0, 16000),
          },
        });
        this.attributes[semconv.EXCEPTION_TYPE] ??= err.name;
        this.attributes[semconv.EXCEPTION_MESSAGE] ??= err.message.slice(
          0,
          4000,
        );
        return this.setStatus("error", err.message);
      },
      this,
    );
  }

  setInput(value: unknown): this {
    return guard(
      "setInput",
      () => {
        if (!this.client.config.capturePayloads) return this;
        const text = typeof value === "string" ? value : JSON.stringify(value);
        this.attributes[semconv.INPUT_VALUE] =
          this.client.redactor.payload(text);
        this.attributes[semconv.INPUT_BYTES] = new TextEncoder().encode(
          text,
        ).length;
        return this;
      },
      this,
    );
  }

  setOutput(value: unknown): this {
    return guard(
      "setOutput",
      () => {
        if (!this.client.config.capturePayloads) return this;
        const text = typeof value === "string" ? value : JSON.stringify(value);
        this.attributes[semconv.OUTPUT_VALUE] =
          this.client.redactor.payload(text);
        this.attributes[semconv.OUTPUT_BYTES] = new TextEncoder().encode(
          text,
        ).length;
        return this;
      },
      this,
    );
  }

  recordModel(input: ModelInput): this {
    return guard(
      "recordModel",
      () => {
        this.attributes[semconv.GEN_AI_SYSTEM] = input.provider;
        this.attributes[semconv.GEN_AI_REQUEST_MODEL] = input.model;
        this.attributes[semconv.GEN_AI_OPERATION_NAME] =
          input.operation ?? "chat";
        if (input.responseModel)
          this.attributes[semconv.GEN_AI_RESPONSE_MODEL] = input.responseModel;
        if (input.temperature !== undefined)
          this.attributes[semconv.GEN_AI_REQUEST_TEMPERATURE] =
            input.temperature;
        if (input.topP !== undefined)
          this.attributes[semconv.GEN_AI_REQUEST_TOP_P] = input.topP;
        if (input.maxTokens !== undefined)
          this.attributes[semconv.GEN_AI_REQUEST_MAX_TOKENS] = input.maxTokens;
        if (input.seed !== undefined)
          this.attributes[semconv.GEN_AI_REQUEST_SEED] = input.seed;
        if (input.systemFingerprint)
          this.attributes[semconv.MODEL_SYSTEM_FINGERPRINT] =
            input.systemFingerprint;
        if (input.finishReasons?.length) {
          this.attributes[semconv.GEN_AI_RESPONSE_FINISH_REASONS] =
            input.finishReasons;
        }
        if (this.category === "custom") this.category = "chat_completion";
        return this;
      },
      this,
    );
  }

  recordUsage(input: UsageInput): this {
    return guard(
      "recordUsage",
      () => {
        const usage: Record<string, unknown> = {
          source: input.source ?? "provider",
        };
        const mapping: [string, number | undefined][] = [
          ["input_tokens", input.inputTokens],
          ["output_tokens", input.outputTokens],
          ["total_tokens", input.totalTokens],
          ["cached_input_tokens", input.cachedInputTokens],
          ["cache_write_tokens", input.cacheWriteTokens],
          ["reasoning_tokens", input.reasoningTokens],
        ];
        for (const [key, value] of mapping) {
          if (value !== undefined) usage[key] = Math.trunc(value);
        }
        if (input.raw) usage["raw"] = input.raw;
        this.usage = usage;
        return this;
      },
      this,
    );
  }

  /** Mark the first streamed token: the latency a user actually perceives. */
  recordFirstToken(): this {
    return guard(
      "recordFirstToken",
      () => {
        if (this.firstTokenAt !== null) return this;
        this.firstTokenAt = nowNanos();
        this.attributes[semconv.LATENCY_TIME_TO_FIRST_TOKEN_MS] =
          (this.firstTokenAt - this.startNanos) / 1_000_000;
        this.events.push({
          name: semconv.EVENT_FIRST_TOKEN,
          time_unix_nano: this.firstTokenAt,
          attributes: {},
        });
        return this;
      },
      this,
    );
  }

  recordRetrieval(input: RetrievalInput): this {
    return guard(
      "recordRetrieval",
      () => {
        const documents = (input.documents ?? []).map((document, index) => {
          const item: Record<string, unknown> = { ...document };
          item["rank"] ??= index;
          item["document_id"] ??= String(item["id"] ?? `doc-${index}`);
          delete item["id"];
          if (
            this.client.config.capturePayloads &&
            typeof item["content"] === "string"
          ) {
            item["content"] = this.client.redactor.payload(
              item["content"] as string,
            );
          } else {
            delete item["content"];
          }
          return item;
        });

        const payload: Record<string, unknown> = {
          documents,
          context_truncated: input.contextTruncated ?? false,
        };
        const optional: [string, unknown][] = [
          [
            "query",
            input.query ? this.client.redactor.payload(input.query) : undefined,
          ],
          [
            "rewritten_query",
            input.rewrittenQuery
              ? this.client.redactor.payload(input.rewrittenQuery)
              : undefined,
          ],
          ["retriever_name", input.retrieverName],
          ["retriever_version", input.retrieverVersion],
          ["knowledge_base_version", input.knowledgeBaseVersion],
          ["search_type", input.searchType],
          ["top_k", input.topK],
          ["embedding_model", input.embeddingModel],
          ["embedding_dimensions", input.embeddingDimensions],
          ["embedding_latency_ms", input.embeddingLatencyMs],
          ["reranker_model", input.rerankerModel],
          ["reranker_latency_ms", input.rerankerLatencyMs],
          ["retrieval_latency_ms", input.retrievalLatencyMs],
          ["context_tokens", input.contextTokens],
        ];
        for (const [key, value] of optional) {
          if (value !== undefined) payload[key] = value;
        }
        if (input.filters) payload["filters"] = input.filters;
        this.retrieval = payload;
        if (this.category === "custom") this.category = "retrieval";
        return this;
      },
      this,
    );
  }

  recordAgentStep(input: AgentStepInput): this {
    return guard(
      "recordAgentStep",
      () => {
        const step: Record<string, unknown> = {
          agent_id: input.agentId,
          step_number: input.stepNumber,
          step_type: input.stepType ?? "observation",
          approval_required: input.approvalRequired ?? false,
        };
        const optional: [string, unknown][] = [
          ["agent_version", input.agentVersion],
          [
            "goal",
            input.goal ? this.client.redactor.payload(input.goal) : undefined,
          ],
          ["parent_step", input.parentStep],
          [
            "decision_summary",
            input.decisionSummary
              ? this.client.redactor.payload(input.decisionSummary)
              : undefined,
          ],
          ["tool_name", input.toolName],
          ["tool_result_ref", input.toolResultRef],
          ["tool_status", input.toolStatus],
          ["handoff_target", input.handoffTarget],
          ["retry_of", input.retryOf],
          ["branch_id", input.branchId],
          ["loop_iteration", input.loopIteration],
          ["approval_status", input.approvalStatus],
          ["termination_reason", input.terminationReason],
          ["max_steps", input.maxSteps],
        ];
        for (const [key, value] of optional) {
          if (value !== undefined) step[key] = value;
        }
        if (input.toolArguments)
          step["tool_arguments"] = this.client.redactor.attributes(
            input.toolArguments,
          );
        if (input.memoryReadKeys?.length)
          step["memory_read_keys"] = input.memoryReadKeys;
        if (input.memoryWriteKeys?.length)
          step["memory_write_keys"] = input.memoryWriteKeys;
        this.agentStep = step;
        if (this.category === "custom") {
          this.category =
            input.stepType === "tool_call" ? "tool_call" : "agent_decision";
        }
        return this;
      },
      this,
    );
  }

  setLineage(input: LineageInput): this {
    return guard(
      "setLineage",
      () => {
        const mapping: [string, unknown][] = [
          ["prompt_name", input.promptName],
          ["prompt_version_id", input.promptVersionId],
          ["prompt_version_label", input.promptVersionLabel],
          ["model_config_id", input.modelConfigId],
          ["dataset_name", input.datasetName],
          ["dataset_version_id", input.datasetVersionId],
          ["dataset_record_id", input.datasetRecordId],
          ["knowledge_base_version", input.knowledgeBaseVersion],
          ["experiment_id", input.experimentId],
          ["experiment_run_id", input.experimentRunId],
        ];
        for (const [key, value] of mapping) {
          if (value !== undefined) this.lineage[key] = value;
        }
        if (input.promptVariables) {
          this.lineage["prompt_variables"] = this.client.redactor.attributes(
            input.promptVariables,
          );
        }
        return this;
      },
      this,
    );
  }

  /** Start a child span, optionally scoping a callback to it. */
  span<T>(
    name: string,
    options: SpanOptions,
    body: (span: Span) => Promise<T> | T,
  ): Promise<T>;
  span(name: string, options?: SpanOptions): Span;
  span<T>(
    name: string,
    options: SpanOptions = {},
    body?: (span: Span) => Promise<T> | T,
  ): Span | Promise<T> {
    const child = this.client.startSpan(name, {
      ...options,
      parent: this.context,
    });
    if (!body) return child;
    return runInSpan(child, body);
  }

  end(): void {
    guard(
      "end",
      () => {
        if (this.ended) return;
        this.ended = true;
        this.endNanos = nowNanos();
        if (this.status === "unset") this.status = "ok";
        this.client.submit(this);
      },
      undefined,
    );
  }

  toWire(): WireSpan {
    const redactor = this.client.redactor;
    const wire: WireSpan = {
      trace_id: this.traceId,
      span_id: this.spanId,
      parent_span_id: this.parentId,
      name: this.name,
      kind: this.kind,
      category: this.category,
      start_time_unix_nano: this.startNanos,
      end_time_unix_nano: this.endNanos,
      status: this.status,
      attributes: redactor.attributes(
        this.attributes,
      ) as unknown as WireSpan["attributes"],
    };
    if (this.statusMessage) wire.status_message = this.statusMessage;
    if (this.events.length)
      wire.events = this.events as unknown as WireSpan["events"];
    if (this.links.length)
      wire.links = this.links as unknown as WireSpan["links"];
    if (this.usage) wire.usage = this.usage as unknown as WireSpan["usage"];
    if (this.retrieval)
      wire.retrieval = this.retrieval as unknown as WireSpan["retrieval"];
    if (this.agentStep)
      wire.agent_step = this.agentStep as unknown as WireSpan["agent_step"];
    if (Object.keys(this.lineage).length)
      wire.lineage = this.lineage as unknown as WireSpan["lineage"];
    Object.assign(wire, this.traceFields);
    return wire;
  }
}

export class Trace extends Span {
  setSession(sessionId: string): this {
    this.traceFields["session_id"] = sessionId;
    return this;
  }

  setSubject(subjectId: string): this {
    this.traceFields["subject_id"] = subjectId;
    return this;
  }

  setTags(...tags: string[]): this {
    const existing = (this.traceFields["tags"] as string[] | undefined) ?? [];
    this.traceFields["tags"] = [...new Set([...existing, ...tags])];
    return this;
  }
}

async function runInSpan<T>(
  span: Span,
  body: (span: Span) => Promise<T> | T,
): Promise<T> {
  return withContext(span.context, async () => {
    try {
      return await body(span);
    } catch (error) {
      span.recordException(error);
      throw error;
    } finally {
      // The `finally` is the whole point of the callback form: the span ends
      // whether the body returns, throws or is cancelled.
      span.end();
    }
  });
}

export class Client {
  readonly config: Config;
  readonly redactor: Redactor;
  readonly exporter: BatchExporter;

  constructor(input: ConfigInput = {}, transport?: Transport) {
    this.config = fromEnv(input);
    this.redactor = new Redactor({
      redactKeys: this.config.redactKeys,
      allowedKeys: this.config.allowedKeys,
      maxChars: this.config.maxPayloadChars,
    });
    this.exporter = new BatchExporter(
      this.config,
      transport ?? new HttpTransport(this.config),
      {
        service_name: this.config.serviceName,
        service_version: this.config.serviceVersion ?? null,
        service_instance_id: this.config.serviceInstanceId ?? null,
        environment: this.config.environment ?? null,
        sdk_name: "aiobs-typescript",
        sdk_version: "0.1.0",
        sdk_language: "typescript",
        attributes: this.config.resourceAttributes,
      },
    );
    if (this.config.enabled) this.exporter.start();
  }

  /** Start a trace, optionally scoping a callback to it. */
  trace<T>(
    name: string,
    options: TraceOptions,
    body: (trace: Trace) => Promise<T> | T,
  ): Promise<T>;
  trace<T>(name: string, body: (trace: Trace) => Promise<T> | T): Promise<T>;
  trace(name: string, options?: TraceOptions): Trace;
  trace<T>(
    name: string,
    optionsOrBody: TraceOptions | ((trace: Trace) => Promise<T> | T) = {},
    maybeBody?: (trace: Trace) => Promise<T> | T,
  ): Trace | Promise<T> {
    const options = typeof optionsOrBody === "function" ? {} : optionsOrBody;
    const body =
      typeof optionsOrBody === "function" ? optionsOrBody : maybeBody;

    // Joining an existing context rather than always starting a root: a "trace"
    // begun inside a distributed request must belong to that request.
    const inherited = options.parent ?? getCurrentContext();
    const context = inherited ? childContext(inherited) : this.rootContext();
    const trace = new Trace(this, name, context, inherited?.spanId ?? null, {
      kind: options.kind ?? "server",
      category: options.category ?? "workflow_step",
      attributes: options.attributes ?? {},
    });
    trace.setAttribute(semconv.TRACE_NAME, name);
    if (options.sessionId) trace.setSession(options.sessionId);
    if (options.subjectId) trace.setSubject(options.subjectId);
    if (options.tags?.length) trace.setTags(...options.tags);
    const release = options.release ?? this.config.release;
    if (release) trace.setLineage({});

    if (!body) return trace;
    return runInSpan(trace, body as (span: Span) => Promise<T> | T);
  }

  startSpan(name: string, options: SpanOptions = {}): Span {
    const parent = options.parent ?? getCurrentContext();
    const context = parent ? childContext(parent) : this.rootContext();
    return new Span(this, name, context, parent?.spanId ?? null, options);
  }

  /** Start a span and scope a callback to it. */
  span<T>(
    name: string,
    options: SpanOptions,
    body: (span: Span) => Promise<T> | T,
  ): Promise<T> {
    return runInSpan(this.startSpan(name, options), body);
  }

  /**
   * Head sampling, decided once per trace and inherited by every child.
   * Sampling per span would produce traces with holes, which are worse than
   * no trace at all.
   */
  private rootContext(): SpanContext {
    const sampled =
      this.config.sampleRate >= 1 || Math.random() < this.config.sampleRate;
    return newRootContext(sampled);
  }

  submit(span: Span): void {
    if (!this.config.enabled || !isSampled(span.context)) return;
    this.exporter.submit(span.toWire());
  }

  flush(): Promise<void> {
    return this.exporter.flush();
  }

  shutdown(): Promise<void> {
    return this.exporter.shutdown();
  }

  stats(): ExporterStats {
    return this.exporter.stats();
  }

  get canExport(): boolean {
    return canExport(this.config);
  }
}

let defaultClient: Client | null = null;

/** Create and install the process-wide client. */
export function init(input: ConfigInput = {}, transport?: Transport): Client {
  if (defaultClient) void defaultClient.shutdown();
  defaultClient = new Client(input, transport);
  return defaultClient;
}

export function getClient(): Client {
  defaultClient ??= new Client();
  return defaultClient;
}

export async function shutdown(): Promise<void> {
  if (defaultClient) {
    await defaultClient.shutdown();
    defaultClient = null;
  }
}
