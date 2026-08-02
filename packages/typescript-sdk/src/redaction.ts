/**
 * Client-side redaction.
 *
 * The only layer that can guarantee a secret never leaves the process. The
 * platform redacts again on ingestion; the two are complementary rather than
 * redundant.
 *
 * Deliberately small. Real PII classification belongs behind `detectors`, where
 * an application can plug in whatever it already uses.
 */

export const REDACTED = "[redacted]";

const KEY_PARTS = [
  "password",
  "passwd",
  "secret",
  "token",
  "authorization",
  "authentication",
  "api_key",
  "apikey",
  "api-key",
  "access_key",
  "private_key",
  "client_secret",
  "credential",
  "cookie",
  "bearer",
  "ssn",
  "credit_card",
  "card_number",
  "cvv",
] as const;

/**
 * Platform attributes that DO carry user content and must be redacted.
 *
 * The platform's own namespaces use an inverted rule: anything under `aiobs.`
 * or `gen_ai.` is safe *unless* listed here. Enumerating the safe names was
 * tried and was wrong -- `aiobs.usage.input_tokens` and
 * `aiobs.latency.time_to_first_token_ms` both contain "token" and were silently
 * destroyed. The sensitive set is small and closed; the safe set is not.
 */
const PLATFORM_SENSITIVE: ReadonlySet<string> = new Set([
  "aiobs.input.value",
  "aiobs.output.value",
  "aiobs.prompt.variables",
  "aiobs.retrieval.query",
  "aiobs.retrieval.rewritten_query",
  "aiobs.retrieval.documents",
  "aiobs.agent.goal",
  "aiobs.agent.decision_summary",
  "aiobs.agent.tool.arguments",
  "exception.message",
  "exception.stacktrace",
  "db.query.text",
  "url.full",
]);

/** Namespaces the platform owns. */
const PLATFORM_PREFIXES = ["aiobs.", "gen_ai."] as const;

function isPlatformAttribute(key: string): boolean {
  return PLATFORM_PREFIXES.some((prefix) => key.startsWith(prefix));
}

const VALUE_PATTERNS: readonly RegExp[] = [
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/g,
  /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g,
  /\bBearer\s+[A-Za-z0-9\-._~+/]{20,}=*/gi,
  /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/g,
];

export interface RedactorOptions {
  redactKeys?: string[];
  allowedKeys?: string[];
  maxChars?: number;
  detectors?: ((value: string) => boolean)[];
}

export class Redactor {
  private readonly extraKeys: string[];
  private readonly allowed: ReadonlySet<string>;
  private readonly maxChars: number;
  private readonly detectors: ((value: string) => boolean)[];

  constructor(options: RedactorOptions = {}) {
    this.extraKeys = (options.redactKeys ?? []).map((key) => key.toLowerCase());
    this.allowed = new Set(options.allowedKeys ?? []);
    this.maxChars = options.maxChars ?? 8192;
    this.detectors = options.detectors ?? [];
  }

  isSensitive(key: string): boolean {
    const lowered = key.toLowerCase();
    if (isPlatformAttribute(key)) {
      return PLATFORM_SENSITIVE.has(key) || this.extraKeys.includes(lowered);
    }
    return [...KEY_PARTS, ...this.extraKeys].some((part) =>
      lowered.includes(part),
    );
  }

  value(text: string): string {
    let result = text;
    for (const pattern of VALUE_PATTERNS) {
      // Reset lastIndex: these are module-level /g regexes and are reused.
      pattern.lastIndex = 0;
      result = result.replace(pattern, REDACTED);
    }
    for (const detector of this.detectors) {
      try {
        if (detector(result)) return REDACTED;
      } catch {
        // A broken detector must not drop data.
      }
    }
    return result;
  }

  payload(text: string | null | undefined): string | null {
    if (text === null || text === undefined) return null;
    const cleaned = this.value(text);
    return cleaned.length > this.maxChars
      ? `${cleaned.slice(0, this.maxChars)}…[${cleaned.length} chars truncated]`
      : cleaned;
  }

  attributes(input: Record<string, unknown>): Record<string, unknown> {
    const result: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(input)) {
      if (
        this.allowed.size > 0 &&
        !this.allowed.has(key) &&
        !isPlatformAttribute(key)
      )
        continue;
      result[key] = this.isSensitive(key) ? REDACTED : this.walk(value, 0);
    }
    return result;
  }

  private walk(value: unknown, depth: number): unknown {
    if (depth > 6) return REDACTED;
    if (typeof value === "string") {
      const cleaned = this.value(value);
      return cleaned.length > this.maxChars
        ? `${cleaned.slice(0, this.maxChars)}…`
        : cleaned;
    }
    if (Array.isArray(value))
      return value.map((item) => this.walk(item, depth + 1));
    if (value && typeof value === "object") {
      const out: Record<string, unknown> = {};
      for (const [key, item] of Object.entries(
        value as Record<string, unknown>,
      )) {
        out[key] = this.isSensitive(key)
          ? REDACTED
          : this.walk(item, depth + 1);
      }
      return out;
    }
    return value;
  }
}
