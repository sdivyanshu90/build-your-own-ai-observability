/**
 * SDK configuration.
 *
 * Read from the environment by default so instrumenting an application is a
 * deploy-time concern, not a code change.
 *
 * The design rule throughout: an SDK must never break the application it
 * instruments. Every failure here degrades to "telemetry is lost", never to
 * "the request fails".
 */

export interface Config {
  /** Base URL of the platform, e.g. `https://aiobs.example.com`. */
  endpoint: string;
  /** API key. Without one the SDK builds spans but sends nothing. */
  apiKey?: string | undefined;
  serviceName: string;
  serviceVersion?: string | undefined;
  serviceInstanceId?: string | undefined;
  environment?: string | undefined;
  release?: string | undefined;
  gitCommit?: string | undefined;

  /** Spans per export request. */
  maxBatchSize: number;
  /** Flush at least this often, so a low-traffic service still reports. */
  flushIntervalMs: number;
  /** Buffer ceiling. When full, the OLDEST spans are dropped: during an
   *  incident the recent ones are what someone is looking at. */
  maxQueueSize: number;
  shutdownTimeoutMs: number;

  timeoutMs: number;
  maxRetries: number;
  retryBaseDelayMs: number;
  retryMaxDelayMs: number;

  /** Head sampling in [0,1], applied per *trace* so sampled traces are whole. */
  sampleRate: number;

  capturePayloads: boolean;
  maxPayloadChars: number;
  redactKeys: string[];
  allowedKeys: string[];

  enabled: boolean;
  debug: boolean;
  resourceAttributes: Record<string, string>;
}

export type ConfigInput = Partial<Config>;

function env(name: string): string | undefined {
  const value = globalThis.process?.env?.[name];
  return value === undefined || value === "" ? undefined : value;
}

function envBool(name: string, fallback: boolean): boolean {
  const value = env(name);
  if (value === undefined) return fallback;
  return ["1", "true", "yes", "on"].includes(value.toLowerCase());
}

function envNumber(name: string, fallback: number): number {
  const value = env(name);
  if (value === undefined) return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function envList(name: string): string[] {
  const value = env(name);
  return value
    ? value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean)
    : [];
}

export function fromEnv(overrides: ConfigInput = {}): Config {
  const config: Config = {
    endpoint: env("AIOBS_ENDPOINT") ?? "http://localhost:58000",
    apiKey: env("AIOBS_API_KEY"),
    serviceName: env("AIOBS_SERVICE_NAME") ?? "unknown_service",
    serviceVersion: env("AIOBS_SERVICE_VERSION"),
    serviceInstanceId: env("AIOBS_SERVICE_INSTANCE_ID"),
    environment: env("AIOBS_ENVIRONMENT"),
    release: env("AIOBS_RELEASE"),
    gitCommit: env("AIOBS_GIT_COMMIT"),
    maxBatchSize: envNumber("AIOBS_MAX_BATCH_SIZE", 200),
    flushIntervalMs: envNumber("AIOBS_FLUSH_INTERVAL_SECONDS", 2) * 1000,
    maxQueueSize: envNumber("AIOBS_MAX_QUEUE_SIZE", 10_000),
    shutdownTimeoutMs: envNumber("AIOBS_SHUTDOWN_TIMEOUT_SECONDS", 5) * 1000,
    timeoutMs: envNumber("AIOBS_TIMEOUT_SECONDS", 10) * 1000,
    maxRetries: envNumber("AIOBS_MAX_RETRIES", 3),
    retryBaseDelayMs: 250,
    retryMaxDelayMs: 8_000,
    sampleRate: envNumber("AIOBS_SAMPLE_RATE", 1),
    capturePayloads: envBool("AIOBS_CAPTURE_PAYLOADS", true),
    maxPayloadChars: envNumber("AIOBS_MAX_PAYLOAD_CHARS", 8_192),
    redactKeys: envList("AIOBS_REDACT_KEYS"),
    allowedKeys: envList("AIOBS_ALLOWED_KEYS"),
    enabled: envBool("AIOBS_ENABLED", true),
    debug: envBool("AIOBS_DEBUG", false),
    resourceAttributes: {},
    ...overrides,
  };

  config.endpoint = config.endpoint.replace(/\/+$/, "");
  // Clamp rather than throw: an out-of-range value in a deployment variable
  // should not crash the application at import time.
  config.sampleRate = Math.min(Math.max(config.sampleRate, 0), 1);
  config.maxBatchSize = Math.max(1, config.maxBatchSize);
  config.maxQueueSize = Math.max(config.maxBatchSize, config.maxQueueSize);
  return config;
}

export function ingestUrl(config: Config): string {
  return `${config.endpoint}/v1/ingest/spans`;
}

export function canExport(config: Config): boolean {
  return Boolean(config.enabled && config.apiKey && config.endpoint);
}
