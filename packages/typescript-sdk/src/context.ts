/**
 * W3C Trace Context propagation, backed by `AsyncLocalStorage`.
 *
 * `AsyncLocalStorage` is the only mechanism in Node that survives `await`,
 * `setTimeout`, promise chains and event-emitter callbacks. A module-level
 * variable would leak context between concurrent requests -- which in a server
 * means one user's spans attaching to another user's trace.
 *
 * In a browser or edge runtime where it is unavailable, the SDK falls back to a
 * single slot and callers must pass parents explicitly. That degradation is
 * detectable via `hasAsyncContext()` rather than silent.
 */

import {
  generateSpanId,
  generateTraceId,
  isValidSpanId,
  isValidTraceId,
} from "@aiobs/schemas";

export const TRACEPARENT_HEADER = "traceparent";
export const TRACESTATE_HEADER = "tracestate";
export const BAGGAGE_HEADER = "baggage";

export const FLAG_SAMPLED = 0x01;

const TRACEPARENT_RE =
  /^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$/;
const MAX_TRACESTATE_ENTRIES = 32;
const MAX_BAGGAGE_BYTES = 8192;

export interface SpanContext {
  readonly traceId: string;
  readonly spanId: string;
  readonly traceFlags: number;
  readonly traceState: string;
  readonly baggage: Readonly<Record<string, string>>;
  /** True when this came from an inbound header rather than being created here. */
  readonly remote: boolean;
}

export function newRootContext(sampled = true): SpanContext {
  return {
    traceId: generateTraceId(),
    spanId: generateSpanId(),
    traceFlags: sampled ? FLAG_SAMPLED : 0x00,
    traceState: "",
    baggage: {},
    remote: false,
  };
}

/** A new span in the same trace, inheriting the sampling decision. */
export function childContext(parent: SpanContext): SpanContext {
  return {
    traceId: parent.traceId,
    spanId: generateSpanId(),
    traceFlags: parent.traceFlags,
    traceState: parent.traceState,
    baggage: { ...parent.baggage },
    remote: false,
  };
}

export function isSampled(context: SpanContext): boolean {
  return (context.traceFlags & FLAG_SAMPLED) !== 0;
}

export function toTraceparent(context: SpanContext): string {
  return `00-${context.traceId}-${context.spanId}-${context.traceFlags
    .toString(16)
    .padStart(2, "0")}`;
}

/**
 * Parse a `traceparent`, returning null when unusable.
 *
 * The specification requires an unparseable or future-version header to be
 * ignored and a new trace started -- a misbehaving upstream must never break
 * the downstream service.
 */
export function parseTraceparent(
  value: string | null | undefined,
): SpanContext | null {
  if (!value) return null;
  const match = TRACEPARENT_RE.exec(value.trim().toLowerCase());
  if (!match) return null;
  const [, version, traceId, spanId, flags] = match as unknown as [
    string,
    string,
    string,
    string,
    string,
  ];
  if (version === "ff") return null; // reserved as invalid
  if (!isValidTraceId(traceId) || !isValidSpanId(spanId)) return null;
  return {
    traceId,
    spanId,
    traceFlags: Number.parseInt(flags, 16),
    traceState: "",
    baggage: {},
    remote: true,
  };
}

export function parseBaggage(
  value: string | null | undefined,
): Record<string, string> {
  if (!value) return {};
  if (new TextEncoder().encode(value).length > MAX_BAGGAGE_BYTES) return {};
  const result: Record<string, string> = {};
  for (const member of value.split(",")) {
    const trimmed = member.trim();
    const equals = trimmed.indexOf("=");
    if (equals <= 0) continue;
    const key = trimmed.slice(0, equals).trim();
    // Properties after ';' are legal; only the value is preserved.
    const raw = trimmed.slice(equals + 1).split(";")[0] ?? "";
    try {
      result[decodeURIComponent(key)] = decodeURIComponent(raw.trim());
    } catch {
      // A malformed member is skipped rather than failing the whole header.
    }
  }
  return result;
}

export function formatBaggage(items: Record<string, string>): string {
  const encoded = Object.entries(items).map(
    ([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`,
  );
  let joined = encoded.join(",");
  // Truncate rather than emit a header a proxy will reject.
  while (
    new TextEncoder().encode(joined).length > MAX_BAGGAGE_BYTES &&
    encoded.length > 0
  ) {
    encoded.pop();
    joined = encoded.join(",");
  }
  return joined;
}

export type Carrier = Record<string, string | string[] | undefined>;

/** Build a context from inbound HTTP or message headers. */
export function extract(headers: Carrier): SpanContext | null {
  const lowered: Record<string, string> = {};
  for (const [key, value] of Object.entries(headers)) {
    if (value === undefined) continue;
    lowered[key.toLowerCase()] = Array.isArray(value)
      ? (value[0] ?? "")
      : value;
  }
  const base = parseTraceparent(lowered[TRACEPARENT_HEADER]);
  if (!base) return null;
  let state = (lowered[TRACESTATE_HEADER] ?? "").trim();
  const entries = state ? state.split(",") : [];
  if (entries.length > MAX_TRACESTATE_ENTRIES) {
    state = entries.slice(0, MAX_TRACESTATE_ENTRIES).join(",");
  }
  return {
    ...base,
    traceState: state,
    baggage: parseBaggage(lowered[BAGGAGE_HEADER]),
  };
}

/** Write a context into an outbound header carrier. */
export function inject(
  context: SpanContext | null | undefined,
  carrier: Record<string, string> = {},
): Record<string, string> {
  if (!context) return carrier;
  carrier[TRACEPARENT_HEADER] = toTraceparent(context);
  if (context.traceState) carrier[TRACESTATE_HEADER] = context.traceState;
  if (Object.keys(context.baggage).length > 0) {
    carrier[BAGGAGE_HEADER] = formatBaggage(
      context.baggage as Record<string, string>,
    );
  }
  return carrier;
}

// ---------------------------------------------------------------------------
// ambient context
// ---------------------------------------------------------------------------

interface Store {
  context: SpanContext | null;
}

type AsyncLocalStorageLike = {
  getStore(): Store | undefined;
  run<T>(store: Store, callback: () => T): T;
};

let storage: AsyncLocalStorageLike | null = null;
let fallback: SpanContext | null = null;

// Node only. Loaded eagerly but guarded, so a browser bundle that never reaches
// this code path still builds and runs.
try {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const { AsyncLocalStorage } = await import("node:async_hooks");
  storage = new AsyncLocalStorage<Store>() as unknown as AsyncLocalStorageLike;
} catch {
  storage = null;
}

/**
 * Whether real async context propagation is available.
 *
 * When false the SDK still works, but nested spans inside `await` boundaries
 * must be given an explicit parent.
 */
export function hasAsyncContext(): boolean {
  return storage !== null;
}

export function getCurrentContext(): SpanContext | null {
  if (storage) return storage.getStore()?.context ?? null;
  return fallback;
}

/** Run `callback` with `context` active. */
export function withContext<T>(
  context: SpanContext | null,
  callback: () => T,
): T {
  if (storage) return storage.run({ context }, callback);
  const previous = fallback;
  fallback = context;
  try {
    return callback();
  } finally {
    fallback = previous;
  }
}
