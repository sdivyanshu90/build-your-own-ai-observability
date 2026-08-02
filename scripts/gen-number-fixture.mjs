#!/usr/bin/env node
/**
 * Regenerate packages/shared-schemas/json/number-canonicalization.json.
 *
 * The fixture pins the exact output of ECMAScript's `String(Number)` for a
 * spread of doubles: integers, sub-normals, values either side of the
 * exponential-notation thresholds (1e21 and 1e-7), and 300 deterministic
 * pseudo-random magnitudes. Both `aiobs_schemas.canonical.format_number`
 * (Python) and `formatNumber` (TypeScript) are asserted against it, which is
 * what guarantees that a prompt version hashed by the Python SDK matches the
 * same prompt hashed by the TypeScript SDK.
 *
 * Usage:  node scripts/gen-number-fixture.mjs
 */
import { writeFileSync } from 'node:fs';
import { Buffer } from 'node:buffer';

const values = [
  0, -0, 1, -1, 0.5, 0.7, -0.7, 0.1, 0.2, 0.30000000000000004,
  123.456, 1e20, 1e21, 1e-6, 1e-7, 5e-324, 1.7976931348623157e308,
  1 / 3, 2 / 3, 100, 1000000, 1e15, 1e16, 1e17, 12345678901234567890,
  0.000001, 0.0000001, 9007199254740991, 9007199254740993,
  -1e21, -1e-7, 3.141592653589793, 2.718281828459045, 1e-323,
  1.5e-10, 6.02e23, 1e-21, 255, 65535, 0.001, 1e-4,
];

// Deterministic LCG so the fixture is reproducible across regenerations.
let state = 42;
const next = () => {
  state = (state * 1103515245 + 12345) & 0x7fffffff;
  return state / 0x7fffffff;
};
for (let i = 0; i < 300; i += 1) {
  values.push(next() * Math.pow(10, Math.floor(next() * 40) - 20));
}

const seen = new Set();
const rows = [];
for (const value of values) {
  const hex = Buffer.from(new Float64Array([value]).buffer).toString('hex');
  if (seen.has(hex)) continue;
  seen.add(hex);
  rows.push({ hex, js: String(value) });
}

const document = {
  description:
    'Cross-language canonical number formatting fixture. Each entry is an IEEE-754 double ' +
    '(little-endian hex) and the exact string ECMAScript String(Number) produces. Python ' +
    'format_number() and TypeScript formatNumber() must both reproduce it.',
  generator: 'scripts/gen-number-fixture.mjs',
  values: rows,
};

const target = new URL(
  '../packages/shared-schemas/json/number-canonicalization.json',
  import.meta.url,
);
writeFileSync(target, `${JSON.stringify(document, null, 1)}\n`);
process.stdout.write(`wrote ${rows.length} unique values to ${target.pathname}\n`);
