"""AI observability semantic conventions.

The platform deliberately does **not** invent a proprietary tracing data model.
A trace is an OpenTelemetry trace; a span is an OpenTelemetry span. Everything
AI-specific is expressed as span attributes drawn from two namespaces:

``gen_ai.*``
    The upstream OpenTelemetry *Generative AI* semantic conventions. Where an
    upstream attribute exists we use it verbatim so that traces produced by
    third-party OpenTelemetry instrumentation (LangChain, the OpenAI
    instrumentation packages, vendor SDKs) are understood without translation.

``aiobs.*``
    Platform-specific extensions for concepts the upstream conventions do not
    yet cover: immutable prompt/model/dataset version lineage, retrieval
    document ranking, agent trajectory steps, and cost attribution.

Every attribute is registered in :data:`REGISTRY` with its type, stability and
a human description. The registry is the single source of truth: it is exported
to JSON for the TypeScript SDK, rendered into the documentation, and asserted
against by conformance tests. Adding an attribute anywhere else is a bug.

See ``docs/concepts/opentelemetry-and-otlp.md`` for the rationale and
``docs/architecture/data-model.md`` for how attributes are projected onto the
analytics tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Literal

__all__ = [
    "REGISTRY",
    "AttributeSpec",
    "AttributeType",
    "Stability",
    "attribute_names",
    "is_registered",
    "lookup",
    "registry_as_dict",
]


class AttributeType(str, Enum):
    """Value types permitted for span attributes (mirrors the OTLP AnyValue subset we accept)."""

    STRING = "string"
    INT = "int"
    DOUBLE = "double"
    BOOLEAN = "boolean"
    STRING_ARRAY = "string[]"
    INT_ARRAY = "int[]"
    DOUBLE_ARRAY = "double[]"


class Stability(str, Enum):
    """Lifecycle guarantee for an attribute name."""

    #: Defined by an upstream OpenTelemetry specification; we track upstream.
    OTEL = "otel"
    #: Platform extension we intend to keep; removal requires a major version.
    STABLE = "stable"
    #: Platform extension that may change; guarded behind documentation warnings.
    EXPERIMENTAL = "experimental"


@dataclass(frozen=True, slots=True)
class AttributeSpec:
    """Description of a single semantic-convention attribute."""

    name: str
    type: AttributeType
    stability: Stability
    brief: str
    #: Logical grouping used for documentation and UI rendering.
    group: str
    #: Whether the value may contain user content and is therefore subject to redaction.
    sensitive: bool = False
    examples: tuple[str, ...] = field(default_factory=tuple)


def _spec(
    name: str,
    type_: AttributeType,
    stability: Stability,
    group: str,
    brief: str,
    *,
    sensitive: bool = False,
    examples: tuple[str, ...] = (),
) -> AttributeSpec:
    return AttributeSpec(
        name=name,
        type=type_,
        stability=stability,
        brief=brief,
        group=group,
        sensitive=sensitive,
        examples=examples,
    )


_S = AttributeType.STRING
_I = AttributeType.INT
_D = AttributeType.DOUBLE
_B = AttributeType.BOOLEAN
_SA = AttributeType.STRING_ARRAY
_DA = AttributeType.DOUBLE_ARRAY
_IA = AttributeType.INT_ARRAY

_OTEL = Stability.OTEL
_STABLE = Stability.STABLE
_EXP = Stability.EXPERIMENTAL


# ---------------------------------------------------------------------------
# Upstream OpenTelemetry GenAI conventions
# ---------------------------------------------------------------------------

GEN_AI_SYSTEM: Final = "gen_ai.system"
GEN_AI_OPERATION_NAME: Final = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL: Final = "gen_ai.request.model"
GEN_AI_REQUEST_TEMPERATURE: Final = "gen_ai.request.temperature"
GEN_AI_REQUEST_TOP_P: Final = "gen_ai.request.top_p"
GEN_AI_REQUEST_TOP_K: Final = "gen_ai.request.top_k"
GEN_AI_REQUEST_MAX_TOKENS: Final = "gen_ai.request.max_tokens"
GEN_AI_REQUEST_STOP_SEQUENCES: Final = "gen_ai.request.stop_sequences"
GEN_AI_REQUEST_FREQUENCY_PENALTY: Final = "gen_ai.request.frequency_penalty"
GEN_AI_REQUEST_PRESENCE_PENALTY: Final = "gen_ai.request.presence_penalty"
GEN_AI_REQUEST_SEED: Final = "gen_ai.request.seed"
GEN_AI_REQUEST_ENCODING_FORMATS: Final = "gen_ai.request.encoding_formats"
GEN_AI_RESPONSE_ID: Final = "gen_ai.response.id"
GEN_AI_RESPONSE_MODEL: Final = "gen_ai.response.model"
GEN_AI_RESPONSE_FINISH_REASONS: Final = "gen_ai.response.finish_reasons"
GEN_AI_USAGE_INPUT_TOKENS: Final = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS: Final = "gen_ai.usage.output_tokens"
GEN_AI_TOOL_NAME: Final = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID: Final = "gen_ai.tool.call.id"
GEN_AI_AGENT_ID: Final = "gen_ai.agent.id"
GEN_AI_AGENT_NAME: Final = "gen_ai.agent.name"
GEN_AI_CONVERSATION_ID: Final = "gen_ai.conversation.id"

# Resource attributes (OpenTelemetry resource semantic conventions)
SERVICE_NAME: Final = "service.name"
SERVICE_VERSION: Final = "service.version"
SERVICE_INSTANCE_ID: Final = "service.instance.id"
DEPLOYMENT_ENVIRONMENT: Final = "deployment.environment.name"
TELEMETRY_SDK_NAME: Final = "telemetry.sdk.name"
TELEMETRY_SDK_VERSION: Final = "telemetry.sdk.version"
TELEMETRY_SDK_LANGUAGE: Final = "telemetry.sdk.language"

# Generic conventions reused for non-AI spans
HTTP_REQUEST_METHOD: Final = "http.request.method"
HTTP_RESPONSE_STATUS_CODE: Final = "http.response.status_code"
URL_FULL: Final = "url.full"
DB_SYSTEM: Final = "db.system.name"
DB_QUERY_TEXT: Final = "db.query.text"
MESSAGING_SYSTEM: Final = "messaging.system"
MESSAGING_DESTINATION_NAME: Final = "messaging.destination.name"
EXCEPTION_TYPE: Final = "exception.type"
EXCEPTION_MESSAGE: Final = "exception.message"
EXCEPTION_STACKTRACE: Final = "exception.stacktrace"


# ---------------------------------------------------------------------------
# Platform extensions -- aiobs.*
# ---------------------------------------------------------------------------

# -- span classification ----------------------------------------------------
SPAN_CATEGORY: Final = "aiobs.span.category"

# -- trace level ------------------------------------------------------------
TRACE_NAME: Final = "aiobs.trace.name"
SESSION_ID: Final = "aiobs.session.id"
SUBJECT_ID: Final = "aiobs.subject.id"
TENANT_ID: Final = "aiobs.tenant.id"
PROJECT_ID: Final = "aiobs.project.id"
RELEASE: Final = "aiobs.release"
GIT_COMMIT: Final = "aiobs.git.commit"
TAGS: Final = "aiobs.tags"

# -- lineage ----------------------------------------------------------------
PROMPT_NAME: Final = "aiobs.prompt.name"
PROMPT_VERSION_ID: Final = "aiobs.prompt.version_id"
PROMPT_VERSION_LABEL: Final = "aiobs.prompt.version_label"
PROMPT_HASH: Final = "aiobs.prompt.hash"
PROMPT_VARIABLES: Final = "aiobs.prompt.variables"
MODEL_CONFIG_ID: Final = "aiobs.model.config_id"
MODEL_CONFIG_HASH: Final = "aiobs.model.config_hash"
MODEL_DEPLOYMENT: Final = "aiobs.model.deployment"
MODEL_REGION: Final = "aiobs.model.region"
MODEL_FAMILY: Final = "aiobs.model.family"
MODEL_SYSTEM_FINGERPRINT: Final = "aiobs.model.system_fingerprint"
DATASET_NAME: Final = "aiobs.dataset.name"
DATASET_VERSION_ID: Final = "aiobs.dataset.version_id"
DATASET_RECORD_ID: Final = "aiobs.dataset.record_id"
KNOWLEDGE_BASE_VERSION: Final = "aiobs.knowledge_base.version"
EXPERIMENT_ID: Final = "aiobs.experiment.id"
EXPERIMENT_RUN_ID: Final = "aiobs.experiment.run_id"

# -- payload references -----------------------------------------------------
INPUT_VALUE: Final = "aiobs.input.value"
INPUT_REF: Final = "aiobs.input.ref"
INPUT_BYTES: Final = "aiobs.input.bytes"
INPUT_TRUNCATED: Final = "aiobs.input.truncated"
OUTPUT_VALUE: Final = "aiobs.output.value"
OUTPUT_REF: Final = "aiobs.output.ref"
OUTPUT_BYTES: Final = "aiobs.output.bytes"
OUTPUT_TRUNCATED: Final = "aiobs.output.truncated"

# -- usage ------------------------------------------------------------------
USAGE_INPUT_TOKENS: Final = "aiobs.usage.input_tokens"
USAGE_OUTPUT_TOKENS: Final = "aiobs.usage.output_tokens"
USAGE_TOTAL_TOKENS: Final = "aiobs.usage.total_tokens"
USAGE_CACHED_INPUT_TOKENS: Final = "aiobs.usage.cached_input_tokens"
USAGE_CACHE_WRITE_TOKENS: Final = "aiobs.usage.cache_write_tokens"
USAGE_REASONING_TOKENS: Final = "aiobs.usage.reasoning_tokens"
USAGE_AUDIO_INPUT_SECONDS: Final = "aiobs.usage.audio_input_seconds"
USAGE_AUDIO_OUTPUT_SECONDS: Final = "aiobs.usage.audio_output_seconds"
USAGE_IMAGE_INPUT_COUNT: Final = "aiobs.usage.image_input_count"
USAGE_IMAGE_OUTPUT_COUNT: Final = "aiobs.usage.image_output_count"
USAGE_SOURCE: Final = "aiobs.usage.source"
USAGE_RAW: Final = "aiobs.usage.raw"

# -- latency ----------------------------------------------------------------
LATENCY_TIME_TO_FIRST_TOKEN_MS: Final = "aiobs.latency.time_to_first_token_ms"
LATENCY_QUEUE_MS: Final = "aiobs.latency.queue_ms"
LATENCY_PROVIDER_MS: Final = "aiobs.latency.provider_ms"
LATENCY_STREAM_MS: Final = "aiobs.latency.stream_ms"

# -- cost -------------------------------------------------------------------
COST_TOTAL: Final = "aiobs.cost.total"
COST_CURRENCY: Final = "aiobs.cost.currency"
COST_ESTIMATED: Final = "aiobs.cost.estimated"
COST_PRICE_BOOK_VERSION: Final = "aiobs.cost.price_book_version"

# -- retrieval --------------------------------------------------------------
RETRIEVAL_QUERY: Final = "aiobs.retrieval.query"
RETRIEVAL_REWRITTEN_QUERY: Final = "aiobs.retrieval.rewritten_query"
RETRIEVAL_RETRIEVER_NAME: Final = "aiobs.retrieval.retriever.name"
RETRIEVAL_RETRIEVER_VERSION: Final = "aiobs.retrieval.retriever.version"
RETRIEVAL_SEARCH_TYPE: Final = "aiobs.retrieval.search_type"
RETRIEVAL_FILTERS: Final = "aiobs.retrieval.filters"
RETRIEVAL_TOP_K: Final = "aiobs.retrieval.top_k"
RETRIEVAL_RESULT_COUNT: Final = "aiobs.retrieval.result_count"
RETRIEVAL_LATENCY_MS: Final = "aiobs.retrieval.latency_ms"
RETRIEVAL_EMBEDDING_MODEL: Final = "aiobs.retrieval.embedding.model"
RETRIEVAL_EMBEDDING_DIMENSIONS: Final = "aiobs.retrieval.embedding.dimensions"
RETRIEVAL_EMBEDDING_LATENCY_MS: Final = "aiobs.retrieval.embedding.latency_ms"
RETRIEVAL_RERANKER_MODEL: Final = "aiobs.retrieval.reranker.model"
RETRIEVAL_RERANKER_LATENCY_MS: Final = "aiobs.retrieval.reranker.latency_ms"
RETRIEVAL_CONTEXT_TOKENS: Final = "aiobs.retrieval.context_tokens"
RETRIEVAL_CONTEXT_TRUNCATED: Final = "aiobs.retrieval.context_truncated"
RETRIEVAL_DOCUMENTS: Final = "aiobs.retrieval.documents"

# -- agent ------------------------------------------------------------------
AGENT_ID: Final = "aiobs.agent.id"
AGENT_VERSION: Final = "aiobs.agent.version"
AGENT_GOAL: Final = "aiobs.agent.goal"
AGENT_STEP_NUMBER: Final = "aiobs.agent.step.number"
AGENT_STEP_PARENT: Final = "aiobs.agent.step.parent"
AGENT_STEP_TYPE: Final = "aiobs.agent.step.type"
AGENT_DECISION_SUMMARY: Final = "aiobs.agent.decision_summary"
AGENT_TOOL_NAME: Final = "aiobs.agent.tool.name"
AGENT_TOOL_ARGUMENTS: Final = "aiobs.agent.tool.arguments"
AGENT_TOOL_RESULT_REF: Final = "aiobs.agent.tool.result_ref"
AGENT_TOOL_STATUS: Final = "aiobs.agent.tool.status"
AGENT_HANDOFF_TARGET: Final = "aiobs.agent.handoff.target"
AGENT_MEMORY_READ_KEYS: Final = "aiobs.agent.memory.read_keys"
AGENT_MEMORY_WRITE_KEYS: Final = "aiobs.agent.memory.write_keys"
AGENT_RETRY_OF: Final = "aiobs.agent.retry_of"
AGENT_BRANCH_ID: Final = "aiobs.agent.branch.id"
AGENT_LOOP_ITERATION: Final = "aiobs.agent.loop.iteration"
AGENT_APPROVAL_REQUIRED: Final = "aiobs.agent.approval.required"
AGENT_APPROVAL_STATUS: Final = "aiobs.agent.approval.status"
AGENT_TERMINATION_REASON: Final = "aiobs.agent.termination_reason"
AGENT_MAX_STEPS: Final = "aiobs.agent.max_steps"

# -- guardrail --------------------------------------------------------------
GUARDRAIL_NAME: Final = "aiobs.guardrail.name"
GUARDRAIL_OUTCOME: Final = "aiobs.guardrail.outcome"
GUARDRAIL_SCORE: Final = "aiobs.guardrail.score"

# -- SDK / sampling ---------------------------------------------------------
SDK_NAME: Final = "aiobs.sdk.name"
SDK_VERSION: Final = "aiobs.sdk.version"
SAMPLING_RATE: Final = "aiobs.sampling.rate"
SAMPLING_DECISION: Final = "aiobs.sampling.decision"
REDACTED_KEYS: Final = "aiobs.redacted.keys"

# -- span event names -------------------------------------------------------
EVENT_EXCEPTION: Final = "exception"
EVENT_FIRST_TOKEN: Final = "aiobs.first_token"
EVENT_STREAM_CHUNK: Final = "aiobs.stream_chunk"
EVENT_RETRY: Final = "aiobs.retry"
EVENT_HUMAN_APPROVAL: Final = "aiobs.human_approval"
EVENT_TRUNCATION: Final = "aiobs.truncation"
EVENT_LOG: Final = "aiobs.log"


_SPECS: tuple[AttributeSpec, ...] = (
    # --- OTel GenAI --------------------------------------------------------
    _spec(
        GEN_AI_SYSTEM,
        _S,
        _OTEL,
        "model",
        "Provider identifier, e.g. 'openai', 'anthropic'.",
        examples=("openai",),
    ),
    _spec(
        GEN_AI_OPERATION_NAME, _S, _OTEL, "model", "Operation performed, e.g. 'chat', 'embeddings'."
    ),
    _spec(GEN_AI_REQUEST_MODEL, _S, _OTEL, "model", "Model identifier requested by the caller."),
    _spec(GEN_AI_REQUEST_TEMPERATURE, _D, _OTEL, "model", "Sampling temperature requested."),
    _spec(GEN_AI_REQUEST_TOP_P, _D, _OTEL, "model", "Nucleus sampling parameter."),
    _spec(GEN_AI_REQUEST_TOP_K, _I, _OTEL, "model", "Top-k sampling parameter."),
    _spec(GEN_AI_REQUEST_MAX_TOKENS, _I, _OTEL, "model", "Maximum tokens the model may generate."),
    _spec(
        GEN_AI_REQUEST_STOP_SEQUENCES,
        _SA,
        _OTEL,
        "model",
        "Stop sequences supplied with the request.",
    ),
    _spec(GEN_AI_REQUEST_FREQUENCY_PENALTY, _D, _OTEL, "model", "Frequency penalty."),
    _spec(GEN_AI_REQUEST_PRESENCE_PENALTY, _D, _OTEL, "model", "Presence penalty."),
    _spec(GEN_AI_REQUEST_SEED, _I, _OTEL, "model", "Deterministic sampling seed."),
    _spec(
        GEN_AI_REQUEST_ENCODING_FORMATS,
        _SA,
        _OTEL,
        "model",
        "Requested embedding encoding formats.",
    ),
    _spec(GEN_AI_RESPONSE_ID, _S, _OTEL, "model", "Provider-assigned response identifier."),
    _spec(GEN_AI_RESPONSE_MODEL, _S, _OTEL, "model", "Model identifier reported by the provider."),
    _spec(
        GEN_AI_RESPONSE_FINISH_REASONS, _SA, _OTEL, "model", "Finish reasons reported per choice."
    ),
    _spec(GEN_AI_USAGE_INPUT_TOKENS, _I, _OTEL, "usage", "Provider-reported prompt tokens."),
    _spec(GEN_AI_USAGE_OUTPUT_TOKENS, _I, _OTEL, "usage", "Provider-reported completion tokens."),
    _spec(GEN_AI_TOOL_NAME, _S, _OTEL, "agent", "Name of the tool being invoked."),
    _spec(GEN_AI_TOOL_CALL_ID, _S, _OTEL, "agent", "Provider tool-call identifier."),
    _spec(GEN_AI_AGENT_ID, _S, _OTEL, "agent", "Stable identifier of the agent."),
    _spec(GEN_AI_AGENT_NAME, _S, _OTEL, "agent", "Human readable agent name."),
    _spec(GEN_AI_CONVERSATION_ID, _S, _OTEL, "trace", "Conversation / thread identifier."),
    # --- resource ----------------------------------------------------------
    _spec(SERVICE_NAME, _S, _OTEL, "resource", "Logical service emitting the span."),
    _spec(SERVICE_VERSION, _S, _OTEL, "resource", "Version of the emitting service."),
    _spec(
        SERVICE_INSTANCE_ID, _S, _OTEL, "resource", "Instance identifier of the emitting service."
    ),
    _spec(DEPLOYMENT_ENVIRONMENT, _S, _OTEL, "resource", "Deployment environment name."),
    _spec(TELEMETRY_SDK_NAME, _S, _OTEL, "resource", "Telemetry SDK name."),
    _spec(TELEMETRY_SDK_VERSION, _S, _OTEL, "resource", "Telemetry SDK version."),
    _spec(TELEMETRY_SDK_LANGUAGE, _S, _OTEL, "resource", "Telemetry SDK language."),
    # --- generic -----------------------------------------------------------
    _spec(HTTP_REQUEST_METHOD, _S, _OTEL, "http", "HTTP request method."),
    _spec(HTTP_RESPONSE_STATUS_CODE, _I, _OTEL, "http", "HTTP response status code."),
    _spec(URL_FULL, _S, _OTEL, "http", "Absolute request URL.", sensitive=True),
    _spec(DB_SYSTEM, _S, _OTEL, "db", "Database system identifier."),
    _spec(DB_QUERY_TEXT, _S, _OTEL, "db", "Database query text.", sensitive=True),
    _spec(MESSAGING_SYSTEM, _S, _OTEL, "queue", "Messaging system identifier."),
    _spec(MESSAGING_DESTINATION_NAME, _S, _OTEL, "queue", "Destination topic or queue."),
    _spec(EXCEPTION_TYPE, _S, _OTEL, "error", "Exception class name."),
    _spec(EXCEPTION_MESSAGE, _S, _OTEL, "error", "Exception message.", sensitive=True),
    _spec(EXCEPTION_STACKTRACE, _S, _OTEL, "error", "Exception stack trace.", sensitive=True),
    # --- classification ----------------------------------------------------
    _spec(SPAN_CATEGORY, _S, _STABLE, "span", "Platform span category; see SpanCategory enum."),
    # --- trace -------------------------------------------------------------
    _spec(TRACE_NAME, _S, _STABLE, "trace", "Human readable name of the logical AI request."),
    _spec(SESSION_ID, _S, _STABLE, "trace", "Session grouping multiple traces."),
    _spec(
        SUBJECT_ID,
        _S,
        _STABLE,
        "trace",
        "Pseudonymous end-user identifier. Applications MUST supply an opaque "
        "id, never an email or name: it is stored unredacted so that per-user "
        "cost and usage attribution works.",
    ),
    _spec(TENANT_ID, _S, _STABLE, "trace", "Tenant the trace belongs to (server-authoritative)."),
    _spec(PROJECT_ID, _S, _STABLE, "trace", "Project the trace belongs to (server-authoritative)."),
    _spec(RELEASE, _S, _STABLE, "trace", "Application release identifier."),
    _spec(GIT_COMMIT, _S, _STABLE, "trace", "Git commit of the emitting application."),
    _spec(TAGS, _SA, _STABLE, "trace", "Free-form tags attached to the trace."),
    # --- lineage -----------------------------------------------------------
    _spec(PROMPT_NAME, _S, _STABLE, "lineage", "Registered prompt name."),
    _spec(PROMPT_VERSION_ID, _S, _STABLE, "lineage", "Immutable prompt version identifier."),
    _spec(PROMPT_VERSION_LABEL, _S, _STABLE, "lineage", "Human readable prompt version label."),
    _spec(PROMPT_HASH, _S, _STABLE, "lineage", "Content hash of the rendered prompt template."),
    _spec(
        PROMPT_VARIABLES, _S, _STABLE, "lineage", "JSON object of prompt variables.", sensitive=True
    ),
    _spec(
        MODEL_CONFIG_ID, _S, _STABLE, "lineage", "Immutable model configuration version identifier."
    ),
    _spec(MODEL_CONFIG_HASH, _S, _STABLE, "lineage", "Content hash of the model configuration."),
    _spec(MODEL_DEPLOYMENT, _S, _STABLE, "lineage", "Provider deployment name."),
    _spec(MODEL_REGION, _S, _STABLE, "lineage", "Provider region."),
    _spec(MODEL_FAMILY, _S, _STABLE, "lineage", "Model family, e.g. 'claude', 'gpt'."),
    _spec(
        MODEL_SYSTEM_FINGERPRINT, _S, _STABLE, "lineage", "Provider-reported system fingerprint."
    ),
    _spec(DATASET_NAME, _S, _STABLE, "lineage", "Registered dataset name."),
    _spec(DATASET_VERSION_ID, _S, _STABLE, "lineage", "Immutable dataset version identifier."),
    _spec(
        DATASET_RECORD_ID,
        _S,
        _STABLE,
        "lineage",
        "Identifier of the dataset record under evaluation.",
    ),
    _spec(KNOWLEDGE_BASE_VERSION, _S, _STABLE, "lineage", "Knowledge base / index version."),
    _spec(EXPERIMENT_ID, _S, _STABLE, "lineage", "Experiment identifier."),
    _spec(EXPERIMENT_RUN_ID, _S, _STABLE, "lineage", "Experiment run identifier."),
    # --- payloads ----------------------------------------------------------
    _spec(INPUT_VALUE, _S, _STABLE, "payload", "Inline span input payload.", sensitive=True),
    _spec(INPUT_REF, _S, _STABLE, "payload", "Object storage reference to the span input payload."),
    _spec(INPUT_BYTES, _I, _STABLE, "payload", "Size in bytes of the original input payload."),
    _spec(INPUT_TRUNCATED, _B, _STABLE, "payload", "Whether the inline input was truncated."),
    _spec(OUTPUT_VALUE, _S, _STABLE, "payload", "Inline span output payload.", sensitive=True),
    _spec(
        OUTPUT_REF, _S, _STABLE, "payload", "Object storage reference to the span output payload."
    ),
    _spec(OUTPUT_BYTES, _I, _STABLE, "payload", "Size in bytes of the original output payload."),
    _spec(OUTPUT_TRUNCATED, _B, _STABLE, "payload", "Whether the inline output was truncated."),
    # --- usage -------------------------------------------------------------
    _spec(USAGE_INPUT_TOKENS, _I, _STABLE, "usage", "Normalised input token count."),
    _spec(USAGE_OUTPUT_TOKENS, _I, _STABLE, "usage", "Normalised output token count."),
    _spec(USAGE_TOTAL_TOKENS, _I, _STABLE, "usage", "Normalised total token count."),
    _spec(
        USAGE_CACHED_INPUT_TOKENS,
        _I,
        _STABLE,
        "usage",
        "Input tokens served from a provider prompt cache.",
    ),
    _spec(
        USAGE_CACHE_WRITE_TOKENS,
        _I,
        _STABLE,
        "usage",
        "Tokens written into a provider prompt cache.",
    ),
    _spec(
        USAGE_REASONING_TOKENS, _I, _STABLE, "usage", "Reasoning tokens reported by the provider."
    ),
    _spec(USAGE_AUDIO_INPUT_SECONDS, _D, _EXP, "usage", "Audio input seconds billed."),
    _spec(USAGE_AUDIO_OUTPUT_SECONDS, _D, _EXP, "usage", "Audio output seconds billed."),
    _spec(USAGE_IMAGE_INPUT_COUNT, _I, _EXP, "usage", "Number of input images billed."),
    _spec(USAGE_IMAGE_OUTPUT_COUNT, _I, _EXP, "usage", "Number of generated images billed."),
    _spec(
        USAGE_SOURCE,
        _S,
        _STABLE,
        "usage",
        "One of 'provider', 'estimated', 'reconciled', 'missing'.",
    ),
    _spec(USAGE_RAW, _S, _STABLE, "usage", "Verbatim provider usage object encoded as JSON."),
    # --- latency -----------------------------------------------------------
    _spec(
        LATENCY_TIME_TO_FIRST_TOKEN_MS,
        _D,
        _STABLE,
        "latency",
        "Milliseconds until the first streamed token.",
    ),
    _spec(LATENCY_QUEUE_MS, _D, _STABLE, "latency", "Milliseconds spent queued before execution."),
    _spec(
        LATENCY_PROVIDER_MS, _D, _STABLE, "latency", "Milliseconds spent inside the provider call."
    ),
    _spec(
        LATENCY_STREAM_MS,
        _D,
        _STABLE,
        "latency",
        "Milliseconds between first and last streamed token.",
    ),
    # --- cost --------------------------------------------------------------
    _spec(COST_TOTAL, _S, _STABLE, "cost", "Total cost as a decimal string (never a float)."),
    _spec(COST_CURRENCY, _S, _STABLE, "cost", "ISO-4217 currency code."),
    _spec(COST_ESTIMATED, _B, _STABLE, "cost", "Whether the cost is an estimate."),
    _spec(
        COST_PRICE_BOOK_VERSION, _S, _STABLE, "cost", "Price book version used for the calculation."
    ),
    # --- retrieval ---------------------------------------------------------
    _spec(RETRIEVAL_QUERY, _S, _STABLE, "retrieval", "Original user query.", sensitive=True),
    _spec(
        RETRIEVAL_REWRITTEN_QUERY,
        _S,
        _STABLE,
        "retrieval",
        "Query after rewriting.",
        sensitive=True,
    ),
    _spec(RETRIEVAL_RETRIEVER_NAME, _S, _STABLE, "retrieval", "Retriever name."),
    _spec(
        RETRIEVAL_RETRIEVER_VERSION, _S, _STABLE, "retrieval", "Retriever configuration version."
    ),
    _spec(
        RETRIEVAL_SEARCH_TYPE, _S, _STABLE, "retrieval", "'vector', 'keyword', 'hybrid', or custom."
    ),
    _spec(RETRIEVAL_FILTERS, _S, _STABLE, "retrieval", "JSON object of retrieval filters."),
    _spec(RETRIEVAL_TOP_K, _I, _STABLE, "retrieval", "Requested result count."),
    _spec(RETRIEVAL_RESULT_COUNT, _I, _STABLE, "retrieval", "Returned result count."),
    _spec(RETRIEVAL_LATENCY_MS, _D, _STABLE, "retrieval", "Retrieval latency in milliseconds."),
    _spec(RETRIEVAL_EMBEDDING_MODEL, _S, _STABLE, "retrieval", "Embedding model identifier."),
    _spec(
        RETRIEVAL_EMBEDDING_DIMENSIONS, _I, _STABLE, "retrieval", "Embedding vector dimensionality."
    ),
    _spec(
        RETRIEVAL_EMBEDDING_LATENCY_MS,
        _D,
        _STABLE,
        "retrieval",
        "Embedding latency in milliseconds.",
    ),
    _spec(RETRIEVAL_RERANKER_MODEL, _S, _STABLE, "retrieval", "Reranker model identifier."),
    _spec(
        RETRIEVAL_RERANKER_LATENCY_MS, _D, _STABLE, "retrieval", "Reranker latency in milliseconds."
    ),
    _spec(
        RETRIEVAL_CONTEXT_TOKENS,
        _I,
        _STABLE,
        "retrieval",
        "Tokens contributed to the final context.",
    ),
    _spec(
        RETRIEVAL_CONTEXT_TRUNCATED,
        _B,
        _STABLE,
        "retrieval",
        "Whether context assembly truncated documents.",
    ),
    _spec(
        RETRIEVAL_DOCUMENTS,
        _S,
        _STABLE,
        "retrieval",
        "JSON array of retrieved documents.",
        sensitive=True,
    ),
    # --- agent -------------------------------------------------------------
    _spec(AGENT_ID, _S, _STABLE, "agent", "Agent identifier."),
    _spec(AGENT_VERSION, _S, _STABLE, "agent", "Agent version."),
    _spec(AGENT_GOAL, _S, _STABLE, "agent", "Goal assigned to the agent.", sensitive=True),
    _spec(AGENT_STEP_NUMBER, _I, _STABLE, "agent", "Monotonic step number within the trajectory."),
    _spec(
        AGENT_STEP_PARENT, _I, _STABLE, "agent", "Parent step number, for branching trajectories."
    ),
    _spec(
        AGENT_STEP_TYPE,
        _S,
        _STABLE,
        "agent",
        "'decision', 'tool_call', 'handoff', 'memory', 'approval', 'terminate'.",
    ),
    _spec(
        AGENT_DECISION_SUMMARY,
        _S,
        _STABLE,
        "agent",
        "Short, user-approved summary of the decision. Hidden chain-of-thought is never collected.",
        sensitive=True,
    ),
    _spec(AGENT_TOOL_NAME, _S, _STABLE, "agent", "Tool selected for this step."),
    _spec(
        AGENT_TOOL_ARGUMENTS, _S, _STABLE, "agent", "JSON encoded tool arguments.", sensitive=True
    ),
    _spec(
        AGENT_TOOL_RESULT_REF, _S, _STABLE, "agent", "Object storage reference to the tool result."
    ),
    _spec(AGENT_TOOL_STATUS, _S, _STABLE, "agent", "'ok', 'error', 'timeout', 'malformed'."),
    _spec(AGENT_HANDOFF_TARGET, _S, _STABLE, "agent", "Agent receiving control."),
    _spec(AGENT_MEMORY_READ_KEYS, _SA, _STABLE, "agent", "Memory keys read during the step."),
    _spec(AGENT_MEMORY_WRITE_KEYS, _SA, _STABLE, "agent", "Memory keys written during the step."),
    _spec(AGENT_RETRY_OF, _I, _STABLE, "agent", "Step number this step retries."),
    _spec(AGENT_BRANCH_ID, _S, _STABLE, "agent", "Branch identifier for conditional trajectories."),
    _spec(AGENT_LOOP_ITERATION, _I, _STABLE, "agent", "Loop iteration counter."),
    _spec(AGENT_APPROVAL_REQUIRED, _B, _STABLE, "agent", "Whether human approval was requested."),
    _spec(
        AGENT_APPROVAL_STATUS, _S, _STABLE, "agent", "'pending', 'approved', 'rejected', 'timeout'."
    ),
    _spec(AGENT_TERMINATION_REASON, _S, _STABLE, "agent", "Why the trajectory ended."),
    _spec(AGENT_MAX_STEPS, _I, _STABLE, "agent", "Configured maximum step budget."),
    # --- guardrail ---------------------------------------------------------
    _spec(GUARDRAIL_NAME, _S, _STABLE, "guardrail", "Guardrail identifier."),
    _spec(GUARDRAIL_OUTCOME, _S, _STABLE, "guardrail", "'pass', 'block', 'flag'."),
    _spec(GUARDRAIL_SCORE, _D, _STABLE, "guardrail", "Guardrail score."),
    # --- sdk ---------------------------------------------------------------
    _spec(SDK_NAME, _S, _STABLE, "sdk", "Platform SDK name."),
    _spec(SDK_VERSION, _S, _STABLE, "sdk", "Platform SDK version."),
    _spec(SAMPLING_RATE, _D, _STABLE, "sdk", "Configured head sampling rate in [0, 1]."),
    _spec(SAMPLING_DECISION, _S, _STABLE, "sdk", "'record_and_sample', 'drop', 'record_only'."),
    _spec(REDACTED_KEYS, _SA, _STABLE, "sdk", "Attribute keys removed by client-side redaction."),
)

REGISTRY: Final[dict[str, AttributeSpec]] = {spec.name: spec for spec in _SPECS}

#: Attribute names whose values may contain end-user content.
SENSITIVE_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    name for name, spec in REGISTRY.items() if spec.sensitive
)


def lookup(name: str) -> AttributeSpec | None:
    """Return the registry entry for ``name`` or ``None`` when unregistered."""
    return REGISTRY.get(name)


def is_registered(name: str) -> bool:
    """Whether ``name`` is a known semantic-convention attribute."""
    return name in REGISTRY


def attribute_names(group: str | None = None) -> tuple[str, ...]:
    """Return registered attribute names, optionally filtered by ``group``."""
    if group is None:
        return tuple(sorted(REGISTRY))
    return tuple(sorted(n for n, s in REGISTRY.items() if s.group == group))


def registry_as_dict() -> dict[str, dict[str, object]]:
    """Serialise the registry for JSON export and cross-language conformance tests."""
    return {
        name: {
            "name": spec.name,
            "type": spec.type.value,
            "stability": spec.stability.value,
            "group": spec.group,
            "brief": spec.brief,
            "sensitive": spec.sensitive,
            "examples": list(spec.examples),
        }
        for name, spec in sorted(REGISTRY.items())
    }


#: Prefixes owned by the platform. Attributes outside these prefixes and outside
#: the registry are still stored, but are treated as untrusted user attributes
#: (cardinality-limited, never used for authorisation).
OWNED_PREFIXES: Final[tuple[str, ...]] = ("aiobs.", "gen_ai.")

AttributeGroup = Literal[
    "model",
    "usage",
    "trace",
    "resource",
    "http",
    "db",
    "queue",
    "error",
    "span",
    "lineage",
    "payload",
    "latency",
    "cost",
    "retrieval",
    "agent",
    "guardrail",
    "sdk",
]
