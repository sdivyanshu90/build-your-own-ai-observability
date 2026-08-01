/**
 * Canonical JSON serialisation and content addressing.
 *
 * The TypeScript half of a cross-language contract: this must produce
 * byte-identical output to `aiobs_schemas.canonical` in Python, or a prompt
 * hashed by a Node service and the same prompt hashed by a Python service would
 * be different "versions" of the same thing.
 *
 * Implements RFC 8785 (JSON Canonicalization Scheme):
 *  - object keys sorted by UTF-16 code unit (which is what `Array.sort` does
 *    natively here, and what the Python side goes out of its way to emulate);
 *  - no insignificant whitespace;
 *  - strings NFC-normalised and minimally escaped;
 *  - numbers formatted with `Number.prototype.toString`, which the Python side
 *    reimplements and is verified against a shared fixture.
 */

/** Values that can appear in canonical JSON. */
export type CanonicalValue =
  | string
  | number
  | boolean
  | null
  | CanonicalValue[]
  | { [key: string]: CanonicalValue };

export const HASH_ALGORITHM = "sha256";
const HASH_PREFIX = `${HASH_ALGORITHM}:`;

/** Guard against pathological or accidentally recursive structures. */
const MAX_DEPTH = 64;

export class CanonicalizationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CanonicalizationError";
  }
}

const ESCAPES: Record<number, string> = {
  0x08: "\\b",
  0x09: "\\t",
  0x0a: "\\n",
  0x0c: "\\f",
  0x0d: "\\r",
  0x22: '\\"',
  0x5c: "\\\\",
};

function escapeString(value: string): string {
  // NFC first: two strings that render identically must hash identically.
  const normalised = value.normalize("NFC");
  let out = '"';
  for (const character of normalised) {
    const code = character.codePointAt(0)!;
    const escape = ESCAPES[code];
    if (escape !== undefined) {
      out += escape;
    } else if (code < 0x20) {
      out += `\\u${code.toString(16).padStart(4, "0")}`;
    } else {
      out += character;
    }
  }
  return `${out}"`;
}

/**
 * Format a number exactly as the canonical form requires.
 *
 * `String(n)` is already the ECMAScript algorithm the specification names, so
 * this is mostly a guard: non-finite numbers have no JSON representation and
 * must fail loudly rather than becoming `null`.
 */
export function formatNumber(value: number): string {
  if (!Number.isFinite(value)) {
    throw new CanonicalizationError(
      `non-finite numbers cannot be canonicalised: ${value}`,
    );
  }
  // String(-0) is already "0", matching the Python side.
  return String(value);
}

function canonicalize(value: CanonicalValue, depth: number): string {
  if (depth > MAX_DEPTH) {
    throw new CanonicalizationError(
      `structure nested deeper than ${MAX_DEPTH} levels`,
    );
  }
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "string") return escapeString(value);
  if (typeof value === "number") return formatNumber(value);
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalize(item, depth + 1)).join(",")}]`;
  }
  if (typeof value === "object") {
    const record = value as Record<string, CanonicalValue>;
    // The normalised key is what gets emitted; the original is what the value
    // is stored under. NFC can change a key -- "\uFB2C" becomes two code
    // points -- and looking up the normalised form would yield undefined.
    const keys = Object.keys(record).map((key) => ({
      normalised: key.normalize("NFC"),
      original: key,
    }));
    if (new Set(keys.map((key) => key.normalised)).size !== keys.length) {
      throw new CanonicalizationError(
        "object keys collide after NFC normalisation",
      );
    }
    // Default string sort compares UTF-16 code units, which is what RFC 8785
    // requires.
    keys.sort((left, right) =>
      left.normalised < right.normalised
        ? -1
        : left.normalised > right.normalised
          ? 1
          : 0,
    );
    const parts = keys.map(
      ({ normalised, original }) =>
        `${escapeString(normalised)}:${canonicalize(record[original] as CanonicalValue, depth + 1)}`,
    );
    return `{${parts.join(",")}}`;
  }
  throw new CanonicalizationError(
    `type ${typeof value} has no canonical JSON representation`,
  );
}

/** Return the RFC 8785 canonical JSON text for `value`. */
export function canonicalJson(value: CanonicalValue): string {
  return canonicalize(value, 0);
}

/** Return the UTF-8 bytes of the canonical JSON encoding. */
export function canonicalBytes(value: CanonicalValue): Uint8Array {
  return new TextEncoder().encode(canonicalJson(value));
}

/**
 * Return the prefixed SHA-256 content hash of `value`.
 *
 * Async because Web Crypto is async. Works in Node 20+, Deno, Bun and the
 * browser without a polyfill.
 */
export async function contentHash(value: CanonicalValue): Promise<string> {
  const bytes = canonicalBytes(value);
  // TextEncoder always allocates a fresh, zero-offset ArrayBuffer, so handing
  // the backing store to Web Crypto is safe. The cast is needed because the
  // TypeScript lib types allow SharedArrayBuffer here and crypto.subtle does not.
  const digest = await crypto.subtle.digest(
    "SHA-256",
    bytes.buffer as ArrayBuffer,
  );
  const hex = Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  return HASH_PREFIX + hex;
}

/** Constant-time-ish comparison of a value's content hash against `expected`. */
export async function verifyHash(
  value: CanonicalValue,
  expected: string,
): Promise<boolean> {
  const actual = await contentHash(value);
  if (actual.length !== expected.length) return false;
  let difference = 0;
  for (let index = 0; index < actual.length; index += 1) {
    difference |= actual.charCodeAt(index) ^ expected.charCodeAt(index);
  }
  return difference === 0;
}

/**
 * Short, human-facing form of a prefixed content hash.
 *
 * Display only. A 12-character prefix collides after roughly 2^24 values, so it
 * is never used as a lookup key.
 */
export function shortHash(hash: string, length = 12): string {
  if (!hash.startsWith(HASH_PREFIX)) {
    throw new Error(`expected a '${HASH_PREFIX}' prefixed hash, got ${hash}`);
  }
  if (length < 4 || length > 64) {
    throw new Error("short hash length must be between 4 and 64 characters");
  }
  return hash.slice(HASH_PREFIX.length, HASH_PREFIX.length + length);
}
