/**
 * Wire and API types.
 *
 * Mirrors the Pydantic models in `aiobs_schemas.wire` and the response models
 * in `aiobs_api.http.schemas`. The contract test compares these against the
 * generated OpenAPI schema, so a backend change that is not reflected here
 * fails CI rather than surfacing as a runtime `undefined` in the UI.
 */

// ---------------------------------------------------------------------------
// closed vocabularies
// ---------------------------------------------------------------------------

export const SPAN_KINDS = [
  "internal",
  "server",
  "client",
  "producer",
  "consumer",
] as const;
export type SpanKind = (typeof SPAN_KINDS)[number];

export const SPAN_STATUSES = ["unset", "ok", "error"] as const;
export type SpanStatus = (typeof SPAN_STATUSES)[number];

export const TRACE_STATUSES = ["ok", "error", "incomplete"] as const;
export type TraceStatus = (typeof TRACE_STATUSES)[number];

export const SPAN_CATEGORIES = [
  "llm_generation",
  "chat_completion",
  "embedding",
  "retrieval",
  "rerank",
  "prompt_render",
  "guardrail",
  "tool_call",
  "agent_decision",
  "agent_handoff",
  "workflow_step",
  "db_query",
  "http_request",
  "queue_operation",
  "custom",
] as const;
export type SpanCategory = (typeof SPAN_CATEGORIES)[number];

/**
 * Where a token count came from. Never conflate these: `missing` and a count of
 * zero are completely different facts, and an estimate must never be presented
 * as a billing-grade number.
 */
export const USAGE_SOURCES = [
  "provider",
  "estimated",
  "reconciled",
  "missing",
] as const;
export type UsageSource = (typeof USAGE_SOURCES)[number];

export const COST_STATUSES = ["final", "estimated", "unpriced"] as const;
export type CostEstimationStatus = (typeof COST_STATUSES)[number];

export type AttributeValue =
  | string
  | number
  | boolean
  | string[]
  | number[]
  | null;

// ---------------------------------------------------------------------------
// ingest wire format
// ---------------------------------------------------------------------------

export interface TokenUsage {
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  cached_input_tokens?: number | null;
  cache_write_tokens?: number | null;
  reasoning_tokens?: number | null;
  source?: UsageSource;
  raw?: Record<string, unknown> | null;
}

export interface RetrievalDocument {
  document_id: string;
  chunk_id?: string | null;
  rank: number;
  score?: number | null;
  rerank_score?: number | null;
  rerank_rank?: number | null;
  source?: string | null;
  title?: string | null;
  content?: string | null;
  content_ref?: string | null;
  selected?: boolean;
  token_count?: number | null;
  truncated?: boolean;
  metadata?: Record<string, AttributeValue>;
}

export interface RetrievalPayload {
  query?: string | null;
  rewritten_query?: string | null;
  retriever_name?: string | null;
  retriever_version?: string | null;
  knowledge_base_version?: string | null;
  search_type?: string | null;
  filters?: Record<string, AttributeValue>;
  top_k?: number | null;
  embedding_model?: string | null;
  embedding_dimensions?: number | null;
  embedding_latency_ms?: number | null;
  reranker_model?: string | null;
  reranker_latency_ms?: number | null;
  retrieval_latency_ms?: number | null;
  context_tokens?: number | null;
  context_truncated?: boolean;
  documents?: RetrievalDocument[];
}

export interface AgentStepPayload {
  agent_id: string;
  step_number: number;
  step_type?: string;
  agent_version?: string | null;
  goal?: string | null;
  parent_step?: number | null;
  /**
   * A short, deliberately-published rationale. There is no field for private
   * chain-of-thought and the platform does not collect it.
   */
  decision_summary?: string | null;
  tool_name?: string | null;
  tool_arguments?: Record<string, unknown> | null;
  tool_result_ref?: string | null;
  tool_status?: string | null;
  handoff_target?: string | null;
  memory_read_keys?: string[];
  memory_write_keys?: string[];
  retry_of?: number | null;
  branch_id?: string | null;
  loop_iteration?: number | null;
  approval_required?: boolean;
  approval_status?: string | null;
  termination_reason?: string | null;
  max_steps?: number | null;
}

export interface LineagePayload {
  prompt_name?: string | null;
  prompt_version_id?: string | null;
  prompt_version_label?: string | null;
  prompt_variables?: Record<string, unknown> | null;
  model_config_id?: string | null;
  dataset_name?: string | null;
  dataset_version_id?: string | null;
  dataset_record_id?: string | null;
  knowledge_base_version?: string | null;
  experiment_id?: string | null;
  experiment_run_id?: string | null;
  release?: string | null;
  git_commit?: string | null;
}

export interface SpanEvent {
  name: string;
  time_unix_nano: number;
  attributes?: Record<string, AttributeValue>;
}

export interface SpanLink {
  trace_id: string;
  span_id: string;
  attributes?: Record<string, AttributeValue>;
}

export interface WireSpan {
  trace_id: string;
  span_id: string;
  parent_span_id?: string | null;
  name: string;
  kind?: SpanKind;
  category?: SpanCategory;
  start_time_unix_nano: number;
  end_time_unix_nano?: number | null;
  status?: SpanStatus;
  status_message?: string | null;
  attributes?: Record<string, AttributeValue>;
  events?: SpanEvent[];
  links?: SpanLink[];
  usage?: TokenUsage;
  retrieval?: RetrievalPayload;
  agent_step?: AgentStepPayload;
  lineage?: LineagePayload;
  trace_name?: string | null;
  session_id?: string | null;
  subject_id?: string | null;
  tags?: string[];
}

export interface ResourceDescriptor {
  service_name: string;
  service_version?: string | null;
  service_instance_id?: string | null;
  environment?: string | null;
  sdk_name?: string | null;
  sdk_version?: string | null;
  sdk_language?: string | null;
  attributes?: Record<string, AttributeValue>;
}

export interface IngestBatch {
  resource: ResourceDescriptor;
  spans: WireSpan[];
  idempotency_key?: string | null;
  sampling_rate?: number | null;
}

export interface SpanRejection {
  span_id: string | null;
  index: number;
  code: string;
  message: string;
}

export interface IngestResponse {
  accepted: number;
  rejected: number;
  duplicates: number;
  batch_id: string;
  replayed: boolean;
  rejections: SpanRejection[];
}

// ---------------------------------------------------------------------------
// API responses
// ---------------------------------------------------------------------------

export interface ErrorResponse {
  code: string;
  message: string;
  request_id: string;
  details: { location: string; message: string; reason?: string | null }[];
  retry_after_seconds?: number | null;
  context: Record<string, unknown>;
  documentation_url?: string | null;
}

export interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface Trace {
  trace_id: string;
  name: string;
  environment: string;
  status: TraceStatus;
  start_time: string;
  end_time: string | null;
  duration_ms: number | null;
  span_count: number;
  error_count: number;
  session_id: string;
  subject_id: string;
  release: string;
  tags: string[];
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cached_input_tokens: number;
  usage_source: UsageSource;
  /** Decimal string, never a number: JSON numbers cannot represent money exactly. */
  cost: string | null;
  cost_currency: string;
  cost_status: CostEstimationStatus;
  time_to_first_token_ms: number | null;
  models: string[];
  providers: string[];
  prompt_version_ids: string[];
  model_config_ids: string[];
  dataset_version_ids: string[];
  service_names: string[];
  llm_call_count: number;
  retrieval_count: number;
  tool_call_count: number;
  agent_step_count: number;
  complete: boolean;
}

export interface Span {
  span_id: string;
  trace_id: string;
  parent_span_id: string | null;
  name: string;
  kind: SpanKind;
  category: SpanCategory;
  status: SpanStatus;
  status_message: string;
  error_type: string;
  error_message: string;
  start_time: string;
  end_time: string | null;
  duration_ms: number | null;
  self_time_ms: number | null;
  on_critical_path: boolean;
  service_name: string;
  service_version: string;
  provider: string;
  model: string;
  prompt_name: string;
  prompt_version_id: string;
  model_config_id: string;
  dataset_version_id: string;
  knowledge_base_version: string;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  cached_input_tokens: number | null;
  reasoning_tokens: number | null;
  usage_source: UsageSource;
  cost: string | null;
  cost_currency: string;
  cost_status: CostEstimationStatus;
  time_to_first_token_ms: number | null;
  agent_id: string;
  tool_name: string;
  tool_status: string;
  retriever_name: string;
  input_preview: string;
  output_preview: string;
  input_ref: string;
  output_ref: string;
  attributes: Record<string, unknown>;
  links: {
    trace_id: string;
    span_id: string;
    attributes?: Record<string, unknown>;
  }[];
  late_arrival: boolean;
}

export interface SpanEventOut {
  span_id: string;
  name: string;
  time: string;
  sequence: number;
  attributes: Record<string, unknown>;
}

export interface CostRecord {
  span_id: string;
  provider: string;
  model: string;
  currency: string;
  total: string;
  price_book_version: string;
  estimation_status: CostEstimationStatus;
  usage_source: UsageSource;
  components: {
    category: string;
    quantity: string;
    unit_quantity: number;
    unit_price: string;
    amount: string;
    currency: string;
  }[];
  formula: string;
}

export interface TraceDetail {
  trace: Trace;
  spans: Span[];
  events: SpanEventOut[];
  cost_records: CostRecord[];
  critical_path: string[];
  children: Record<string, string[]>;
  orphan_span_ids: string[];
  services: string[];
  retry_groups: Record<string, string[]>;
}

export interface RetrievalDiagnostics {
  document_count: number;
  selected_count: number;
  unused_count: number;
  unused_ratio: number;
  score_min: number | null;
  score_max: number | null;
  score_mean: number | null;
  score_stddev: number | null;
  score_margin: number | null;
  reranked: boolean;
  mean_rank_movement: number | null;
  rerank_promotions: number;
  rerank_demotions: number;
  duplicate_document_ids: string[];
  near_duplicate_pairs: string[][];
  context_tokens: number;
  truncated_count: number;
  missing_source_count: number;
  empty_result: boolean;
}

export interface RetrievalStage {
  span_id: string;
  span_name: string;
  query: string;
  rewritten_query: string;
  retriever_name: string;
  knowledge_base_version: string;
  embedding_model: string;
  search_type: string;
  latency_ms: number | null;
  embedding_latency_ms: number | null;
  reranker_latency_ms: number | null;
  reranker_model: string;
  stages: {
    stage: string;
    label: string;
    detail: string;
    latency_ms: number | null;
    present: boolean;
  }[];
  documents: (RetrievalDocument & { rank_delta?: number | null })[];
  diagnostics: RetrievalDiagnostics;
}

export interface AgentGraphNode {
  id: string;
  step_number: number;
  agent_id: string;
  step_type: string;
  label: string;
  status: string;
  span_id: string;
  duration_ms: number | null;
  tool_name: string;
  tool_status: string;
  decision_summary: string;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_total: string | null;
  branch_id: string;
  loop_iteration: number | null;
  is_retry: boolean;
  approval_status: string;
  termination_reason: string;
  error_message: string;
  on_critical_path: boolean;
}

export interface AgentGraphEdge {
  source: string;
  target: string;
  kind: "sequence" | "branch" | "retry" | "handoff" | "loop";
  label: string;
}

export interface AgentGraph {
  nodes: AgentGraphNode[];
  edges: AgentGraphEdge[];
  agents: string[];
  branches: string[];
  max_steps: number | null;
  termination_reason: string;
  total_steps: number;
  retry_count: number;
  loop_detected: boolean;
  handoff_count: number;
  truncated: boolean;
}

export interface PercentileResult {
  keys: string[];
  count: number;
  p50: number | null;
  p75: number | null;
  p90: number | null;
  p95: number | null;
  p99: number | null;
  avg: number | null;
  max: number | null;
}

export interface OverviewSummary {
  request_count: number;
  error_count: number;
  error_rate: number;
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  total_cost: string | null;
  cost_currency: string;
  cost_is_partial: boolean;
  latency: PercentileResult | null;
  time_to_first_token: PercentileResult | null;
  previous: OverviewSummary | null;
}

export interface MetricPoint {
  bucket: string;
  value: number | string | null;
  count: number;
}

export interface MetricGroup {
  keys: string[];
  total: number | string | null;
  count: number;
  points: MetricPoint[];
}

export interface DashboardSeries {
  metric: string;
  aggregation: string;
  interval: string;
  unit: string;
  /** Buckets still filling. Rendered differently so nobody reports a false drop. */
  partial_buckets: string[];
  groups: MetricGroup[];
}

export interface Project {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  default_sampling_rate: number;
  environments: {
    id: string;
    name: string;
    is_production: boolean;
    settings: Record<string, unknown>;
  }[];
  created_at: string;
}

export interface PromptVersion {
  id: string;
  prompt_id: string;
  version_number: number;
  label: string;
  content_hash: string;
  messages: { role: string; content: string }[];
  variable_schema: Record<string, unknown>;
  default_variables: Record<string, unknown>;
  template_engine: string;
  release_stage: string;
  parent_version_id: string | null;
  commit_message: string | null;
  created_at: string;
  published_at: string | null;
}
