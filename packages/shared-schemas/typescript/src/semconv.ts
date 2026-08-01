/**
 * Semantic convention constants.
 *
 * Generated from `aiobs_schemas.semconv` by `scripts/gen-ts-semconv.py`; the
 * contract test asserts the three copies (Python schemas, Python SDK, this)
 * stay identical. Do not edit by hand.
 */

export const AGENT_APPROVAL_REQUIRED = "aiobs.agent.approval.required" as const;
export const AGENT_APPROVAL_STATUS = "aiobs.agent.approval.status" as const;
export const AGENT_BRANCH_ID = "aiobs.agent.branch.id" as const;
export const AGENT_DECISION_SUMMARY = "aiobs.agent.decision_summary" as const;
export const AGENT_GOAL = "aiobs.agent.goal" as const;
export const AGENT_HANDOFF_TARGET = "aiobs.agent.handoff.target" as const;
export const AGENT_ID = "aiobs.agent.id" as const;
export const AGENT_LOOP_ITERATION = "aiobs.agent.loop.iteration" as const;
export const AGENT_MAX_STEPS = "aiobs.agent.max_steps" as const;
export const AGENT_MEMORY_READ_KEYS = "aiobs.agent.memory.read_keys" as const;
export const AGENT_MEMORY_WRITE_KEYS = "aiobs.agent.memory.write_keys" as const;
export const AGENT_RETRY_OF = "aiobs.agent.retry_of" as const;
export const AGENT_STEP_NUMBER = "aiobs.agent.step.number" as const;
export const AGENT_STEP_PARENT = "aiobs.agent.step.parent" as const;
export const AGENT_STEP_TYPE = "aiobs.agent.step.type" as const;
export const AGENT_TERMINATION_REASON =
  "aiobs.agent.termination_reason" as const;
export const AGENT_TOOL_ARGUMENTS = "aiobs.agent.tool.arguments" as const;
export const AGENT_TOOL_NAME = "aiobs.agent.tool.name" as const;
export const AGENT_TOOL_RESULT_REF = "aiobs.agent.tool.result_ref" as const;
export const AGENT_TOOL_STATUS = "aiobs.agent.tool.status" as const;
export const AGENT_VERSION = "aiobs.agent.version" as const;
export const COST_CURRENCY = "aiobs.cost.currency" as const;
export const COST_ESTIMATED = "aiobs.cost.estimated" as const;
export const COST_PRICE_BOOK_VERSION = "aiobs.cost.price_book_version" as const;
export const COST_TOTAL = "aiobs.cost.total" as const;
export const DATASET_NAME = "aiobs.dataset.name" as const;
export const DATASET_RECORD_ID = "aiobs.dataset.record_id" as const;
export const DATASET_VERSION_ID = "aiobs.dataset.version_id" as const;
export const DB_QUERY_TEXT = "db.query.text" as const;
export const DB_SYSTEM = "db.system.name" as const;
export const DEPLOYMENT_ENVIRONMENT = "deployment.environment.name" as const;
export const EVENT_EXCEPTION = "exception" as const;
export const EVENT_FIRST_TOKEN = "aiobs.first_token" as const;
export const EVENT_HUMAN_APPROVAL = "aiobs.human_approval" as const;
export const EVENT_LOG = "aiobs.log" as const;
export const EVENT_RETRY = "aiobs.retry" as const;
export const EVENT_STREAM_CHUNK = "aiobs.stream_chunk" as const;
export const EVENT_TRUNCATION = "aiobs.truncation" as const;
export const EXCEPTION_MESSAGE = "exception.message" as const;
export const EXCEPTION_STACKTRACE = "exception.stacktrace" as const;
export const EXCEPTION_TYPE = "exception.type" as const;
export const EXPERIMENT_ID = "aiobs.experiment.id" as const;
export const EXPERIMENT_RUN_ID = "aiobs.experiment.run_id" as const;
export const GEN_AI_AGENT_ID = "gen_ai.agent.id" as const;
export const GEN_AI_AGENT_NAME = "gen_ai.agent.name" as const;
export const GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id" as const;
export const GEN_AI_OPERATION_NAME = "gen_ai.operation.name" as const;
export const GEN_AI_REQUEST_ENCODING_FORMATS =
  "gen_ai.request.encoding_formats" as const;
export const GEN_AI_REQUEST_FREQUENCY_PENALTY =
  "gen_ai.request.frequency_penalty" as const;
export const GEN_AI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens" as const;
export const GEN_AI_REQUEST_MODEL = "gen_ai.request.model" as const;
export const GEN_AI_REQUEST_PRESENCE_PENALTY =
  "gen_ai.request.presence_penalty" as const;
export const GEN_AI_REQUEST_SEED = "gen_ai.request.seed" as const;
export const GEN_AI_REQUEST_STOP_SEQUENCES =
  "gen_ai.request.stop_sequences" as const;
export const GEN_AI_REQUEST_TEMPERATURE = "gen_ai.request.temperature" as const;
export const GEN_AI_REQUEST_TOP_K = "gen_ai.request.top_k" as const;
export const GEN_AI_REQUEST_TOP_P = "gen_ai.request.top_p" as const;
export const GEN_AI_RESPONSE_FINISH_REASONS =
  "gen_ai.response.finish_reasons" as const;
export const GEN_AI_RESPONSE_ID = "gen_ai.response.id" as const;
export const GEN_AI_RESPONSE_MODEL = "gen_ai.response.model" as const;
export const GEN_AI_SYSTEM = "gen_ai.system" as const;
export const GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id" as const;
export const GEN_AI_TOOL_NAME = "gen_ai.tool.name" as const;
export const GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens" as const;
export const GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens" as const;
export const GIT_COMMIT = "aiobs.git.commit" as const;
export const GUARDRAIL_NAME = "aiobs.guardrail.name" as const;
export const GUARDRAIL_OUTCOME = "aiobs.guardrail.outcome" as const;
export const GUARDRAIL_SCORE = "aiobs.guardrail.score" as const;
export const HTTP_REQUEST_METHOD = "http.request.method" as const;
export const HTTP_RESPONSE_STATUS_CODE = "http.response.status_code" as const;
export const INPUT_BYTES = "aiobs.input.bytes" as const;
export const INPUT_REF = "aiobs.input.ref" as const;
export const INPUT_TRUNCATED = "aiobs.input.truncated" as const;
export const INPUT_VALUE = "aiobs.input.value" as const;
export const KNOWLEDGE_BASE_VERSION = "aiobs.knowledge_base.version" as const;
export const LATENCY_PROVIDER_MS = "aiobs.latency.provider_ms" as const;
export const LATENCY_QUEUE_MS = "aiobs.latency.queue_ms" as const;
export const LATENCY_STREAM_MS = "aiobs.latency.stream_ms" as const;
export const LATENCY_TIME_TO_FIRST_TOKEN_MS =
  "aiobs.latency.time_to_first_token_ms" as const;
export const MESSAGING_DESTINATION_NAME = "messaging.destination.name" as const;
export const MESSAGING_SYSTEM = "messaging.system" as const;
export const MODEL_CONFIG_HASH = "aiobs.model.config_hash" as const;
export const MODEL_CONFIG_ID = "aiobs.model.config_id" as const;
export const MODEL_DEPLOYMENT = "aiobs.model.deployment" as const;
export const MODEL_FAMILY = "aiobs.model.family" as const;
export const MODEL_REGION = "aiobs.model.region" as const;
export const MODEL_SYSTEM_FINGERPRINT =
  "aiobs.model.system_fingerprint" as const;
export const OUTPUT_BYTES = "aiobs.output.bytes" as const;
export const OUTPUT_REF = "aiobs.output.ref" as const;
export const OUTPUT_TRUNCATED = "aiobs.output.truncated" as const;
export const OUTPUT_VALUE = "aiobs.output.value" as const;
export const PROJECT_ID = "aiobs.project.id" as const;
export const PROMPT_HASH = "aiobs.prompt.hash" as const;
export const PROMPT_NAME = "aiobs.prompt.name" as const;
export const PROMPT_VARIABLES = "aiobs.prompt.variables" as const;
export const PROMPT_VERSION_ID = "aiobs.prompt.version_id" as const;
export const PROMPT_VERSION_LABEL = "aiobs.prompt.version_label" as const;
export const REDACTED_KEYS = "aiobs.redacted.keys" as const;
export const RELEASE = "aiobs.release" as const;
export const RETRIEVAL_CONTEXT_TOKENS =
  "aiobs.retrieval.context_tokens" as const;
export const RETRIEVAL_CONTEXT_TRUNCATED =
  "aiobs.retrieval.context_truncated" as const;
export const RETRIEVAL_DOCUMENTS = "aiobs.retrieval.documents" as const;
export const RETRIEVAL_EMBEDDING_DIMENSIONS =
  "aiobs.retrieval.embedding.dimensions" as const;
export const RETRIEVAL_EMBEDDING_LATENCY_MS =
  "aiobs.retrieval.embedding.latency_ms" as const;
export const RETRIEVAL_EMBEDDING_MODEL =
  "aiobs.retrieval.embedding.model" as const;
export const RETRIEVAL_FILTERS = "aiobs.retrieval.filters" as const;
export const RETRIEVAL_LATENCY_MS = "aiobs.retrieval.latency_ms" as const;
export const RETRIEVAL_QUERY = "aiobs.retrieval.query" as const;
export const RETRIEVAL_RERANKER_LATENCY_MS =
  "aiobs.retrieval.reranker.latency_ms" as const;
export const RETRIEVAL_RERANKER_MODEL =
  "aiobs.retrieval.reranker.model" as const;
export const RETRIEVAL_RESULT_COUNT = "aiobs.retrieval.result_count" as const;
export const RETRIEVAL_RETRIEVER_NAME =
  "aiobs.retrieval.retriever.name" as const;
export const RETRIEVAL_RETRIEVER_VERSION =
  "aiobs.retrieval.retriever.version" as const;
export const RETRIEVAL_REWRITTEN_QUERY =
  "aiobs.retrieval.rewritten_query" as const;
export const RETRIEVAL_SEARCH_TYPE = "aiobs.retrieval.search_type" as const;
export const RETRIEVAL_TOP_K = "aiobs.retrieval.top_k" as const;
export const SAMPLING_DECISION = "aiobs.sampling.decision" as const;
export const SAMPLING_RATE = "aiobs.sampling.rate" as const;
export const SDK_NAME = "aiobs.sdk.name" as const;
export const SDK_VERSION = "aiobs.sdk.version" as const;
export const SERVICE_INSTANCE_ID = "service.instance.id" as const;
export const SERVICE_NAME = "service.name" as const;
export const SERVICE_VERSION = "service.version" as const;
export const SESSION_ID = "aiobs.session.id" as const;
export const SPAN_CATEGORY = "aiobs.span.category" as const;
export const SUBJECT_ID = "aiobs.subject.id" as const;
export const TAGS = "aiobs.tags" as const;
export const TELEMETRY_SDK_LANGUAGE = "telemetry.sdk.language" as const;
export const TELEMETRY_SDK_NAME = "telemetry.sdk.name" as const;
export const TELEMETRY_SDK_VERSION = "telemetry.sdk.version" as const;
export const TENANT_ID = "aiobs.tenant.id" as const;
export const TRACE_NAME = "aiobs.trace.name" as const;
export const URL_FULL = "url.full" as const;
export const USAGE_AUDIO_INPUT_SECONDS =
  "aiobs.usage.audio_input_seconds" as const;
export const USAGE_AUDIO_OUTPUT_SECONDS =
  "aiobs.usage.audio_output_seconds" as const;
export const USAGE_CACHED_INPUT_TOKENS =
  "aiobs.usage.cached_input_tokens" as const;
export const USAGE_CACHE_WRITE_TOKENS =
  "aiobs.usage.cache_write_tokens" as const;
export const USAGE_IMAGE_INPUT_COUNT = "aiobs.usage.image_input_count" as const;
export const USAGE_IMAGE_OUTPUT_COUNT =
  "aiobs.usage.image_output_count" as const;
export const USAGE_INPUT_TOKENS = "aiobs.usage.input_tokens" as const;
export const USAGE_OUTPUT_TOKENS = "aiobs.usage.output_tokens" as const;
export const USAGE_RAW = "aiobs.usage.raw" as const;
export const USAGE_REASONING_TOKENS = "aiobs.usage.reasoning_tokens" as const;
export const USAGE_SOURCE = "aiobs.usage.source" as const;
export const USAGE_TOTAL_TOKENS = "aiobs.usage.total_tokens" as const;

/** Every constant defined here, for the parity test. */
export const ALL_ATTRIBUTES: ReadonlySet<string> = new Set([
  AGENT_APPROVAL_REQUIRED,
  AGENT_APPROVAL_STATUS,
  AGENT_BRANCH_ID,
  AGENT_DECISION_SUMMARY,
  AGENT_GOAL,
  AGENT_HANDOFF_TARGET,
  AGENT_ID,
  AGENT_LOOP_ITERATION,
  AGENT_MAX_STEPS,
  AGENT_MEMORY_READ_KEYS,
  AGENT_MEMORY_WRITE_KEYS,
  AGENT_RETRY_OF,
  AGENT_STEP_NUMBER,
  AGENT_STEP_PARENT,
  AGENT_STEP_TYPE,
  AGENT_TERMINATION_REASON,
  AGENT_TOOL_ARGUMENTS,
  AGENT_TOOL_NAME,
  AGENT_TOOL_RESULT_REF,
  AGENT_TOOL_STATUS,
  AGENT_VERSION,
  COST_CURRENCY,
  COST_ESTIMATED,
  COST_PRICE_BOOK_VERSION,
  COST_TOTAL,
  DATASET_NAME,
  DATASET_RECORD_ID,
  DATASET_VERSION_ID,
  DB_QUERY_TEXT,
  DB_SYSTEM,
  DEPLOYMENT_ENVIRONMENT,
  EVENT_EXCEPTION,
  EVENT_FIRST_TOKEN,
  EVENT_HUMAN_APPROVAL,
  EVENT_LOG,
  EVENT_RETRY,
  EVENT_STREAM_CHUNK,
  EVENT_TRUNCATION,
  EXCEPTION_MESSAGE,
  EXCEPTION_STACKTRACE,
  EXCEPTION_TYPE,
  EXPERIMENT_ID,
  EXPERIMENT_RUN_ID,
  GEN_AI_AGENT_ID,
  GEN_AI_AGENT_NAME,
  GEN_AI_CONVERSATION_ID,
  GEN_AI_OPERATION_NAME,
  GEN_AI_REQUEST_ENCODING_FORMATS,
  GEN_AI_REQUEST_FREQUENCY_PENALTY,
  GEN_AI_REQUEST_MAX_TOKENS,
  GEN_AI_REQUEST_MODEL,
  GEN_AI_REQUEST_PRESENCE_PENALTY,
  GEN_AI_REQUEST_SEED,
  GEN_AI_REQUEST_STOP_SEQUENCES,
  GEN_AI_REQUEST_TEMPERATURE,
  GEN_AI_REQUEST_TOP_K,
  GEN_AI_REQUEST_TOP_P,
  GEN_AI_RESPONSE_FINISH_REASONS,
  GEN_AI_RESPONSE_ID,
  GEN_AI_RESPONSE_MODEL,
  GEN_AI_SYSTEM,
  GEN_AI_TOOL_CALL_ID,
  GEN_AI_TOOL_NAME,
  GEN_AI_USAGE_INPUT_TOKENS,
  GEN_AI_USAGE_OUTPUT_TOKENS,
  GIT_COMMIT,
  GUARDRAIL_NAME,
  GUARDRAIL_OUTCOME,
  GUARDRAIL_SCORE,
  HTTP_REQUEST_METHOD,
  HTTP_RESPONSE_STATUS_CODE,
  INPUT_BYTES,
  INPUT_REF,
  INPUT_TRUNCATED,
  INPUT_VALUE,
  KNOWLEDGE_BASE_VERSION,
  LATENCY_PROVIDER_MS,
  LATENCY_QUEUE_MS,
  LATENCY_STREAM_MS,
  LATENCY_TIME_TO_FIRST_TOKEN_MS,
  MESSAGING_DESTINATION_NAME,
  MESSAGING_SYSTEM,
  MODEL_CONFIG_HASH,
  MODEL_CONFIG_ID,
  MODEL_DEPLOYMENT,
  MODEL_FAMILY,
  MODEL_REGION,
  MODEL_SYSTEM_FINGERPRINT,
  OUTPUT_BYTES,
  OUTPUT_REF,
  OUTPUT_TRUNCATED,
  OUTPUT_VALUE,
  PROJECT_ID,
  PROMPT_HASH,
  PROMPT_NAME,
  PROMPT_VARIABLES,
  PROMPT_VERSION_ID,
  PROMPT_VERSION_LABEL,
  REDACTED_KEYS,
  RELEASE,
  RETRIEVAL_CONTEXT_TOKENS,
  RETRIEVAL_CONTEXT_TRUNCATED,
  RETRIEVAL_DOCUMENTS,
  RETRIEVAL_EMBEDDING_DIMENSIONS,
  RETRIEVAL_EMBEDDING_LATENCY_MS,
  RETRIEVAL_EMBEDDING_MODEL,
  RETRIEVAL_FILTERS,
  RETRIEVAL_LATENCY_MS,
  RETRIEVAL_QUERY,
  RETRIEVAL_RERANKER_LATENCY_MS,
  RETRIEVAL_RERANKER_MODEL,
  RETRIEVAL_RESULT_COUNT,
  RETRIEVAL_RETRIEVER_NAME,
  RETRIEVAL_RETRIEVER_VERSION,
  RETRIEVAL_REWRITTEN_QUERY,
  RETRIEVAL_SEARCH_TYPE,
  RETRIEVAL_TOP_K,
  SAMPLING_DECISION,
  SAMPLING_RATE,
  SDK_NAME,
  SDK_VERSION,
  SERVICE_INSTANCE_ID,
  SERVICE_NAME,
  SERVICE_VERSION,
  SESSION_ID,
  SPAN_CATEGORY,
  SUBJECT_ID,
  TAGS,
  TELEMETRY_SDK_LANGUAGE,
  TELEMETRY_SDK_NAME,
  TELEMETRY_SDK_VERSION,
  TENANT_ID,
  TRACE_NAME,
  URL_FULL,
  USAGE_AUDIO_INPUT_SECONDS,
  USAGE_AUDIO_OUTPUT_SECONDS,
  USAGE_CACHED_INPUT_TOKENS,
  USAGE_CACHE_WRITE_TOKENS,
  USAGE_IMAGE_INPUT_COUNT,
  USAGE_IMAGE_OUTPUT_COUNT,
  USAGE_INPUT_TOKENS,
  USAGE_OUTPUT_TOKENS,
  USAGE_RAW,
  USAGE_REASONING_TOKENS,
  USAGE_SOURCE,
  USAGE_TOTAL_TOKENS,
]);
