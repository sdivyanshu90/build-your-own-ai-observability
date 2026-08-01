/**
 * Trace and span identifier generation and validation.
 *
 * W3C Trace Context ids: 16 and 8 random bytes rendered as lowercase hex. The
 * all-zero value is invalid per the specification and is rejected rather than
 * silently propagated -- an all-zero trace id from a misbehaving upstream would
 * merge every request in the system into one "trace".
 *
 * Uses Web Crypto, which is available in Node 20+, Deno, Bun and the browser.
 * `Math.random` is not acceptable here: predictable span ids let one tenant
 * guess another's identifiers.
 */

const TRACE_ID_RE = /^[0-9a-f]{32}$/;
const SPAN_ID_RE = /^[0-9a-f]{16}$/;

export const INVALID_TRACE_ID = "0".repeat(32);
export const INVALID_SPAN_ID = "0".repeat(16);

function randomHex(bytes: number): string {
  const buffer = new Uint8Array(bytes);
  crypto.getRandomValues(buffer);
  return Array.from(buffer)
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/** A random, valid 32-character trace id. */
export function generateTraceId(): string {
  let value = randomHex(16);
  while (value === INVALID_TRACE_ID) value = randomHex(16);
  return value;
}

/** A random, valid 16-character span id. */
export function generateSpanId(): string {
  let value = randomHex(8);
  while (value === INVALID_SPAN_ID) value = randomHex(8);
  return value;
}

export function isValidTraceId(
  value: string | null | undefined,
): value is string {
  return (
    typeof value === "string" &&
    TRACE_ID_RE.test(value) &&
    value !== INVALID_TRACE_ID
  );
}

export function isValidSpanId(
  value: string | null | undefined,
): value is string {
  return (
    typeof value === "string" &&
    SPAN_ID_RE.test(value) &&
    value !== INVALID_SPAN_ID
  );
}

/** Normalise an id to canonical lowercase hex, throwing if it is unusable. */
export function normalizeTraceId(value: string): string {
  const normalised = value.trim().toLowerCase();
  if (!isValidTraceId(normalised)) {
    throw new Error(`invalid trace id: ${value}`);
  }
  return normalised;
}

export function normalizeSpanId(value: string): string {
  const normalised = value.trim().toLowerCase();
  if (!isValidSpanId(normalised)) {
    throw new Error(`invalid span id: ${value}`);
  }
  return normalised;
}
