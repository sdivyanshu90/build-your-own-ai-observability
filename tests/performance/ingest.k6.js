/**
 * k6 ingestion load profile.
 *
 * The Python harness in this directory is the portable one and needs nothing
 * but the virtualenv. This script exists for the case the Python client itself
 * becomes the bottleneck -- k6 drives far more concurrency per core, which
 * matters once you are measuring a ClickHouse-backed deployment rather than a
 * laptop.
 *
 * Thresholds here are *guards against regression on the machine you run it on*,
 * not published performance claims. Override them per environment:
 *
 *   k6 run -e ENDPOINT=https://api.example.com -e API_KEY=aiobs_... \
 *          -e P95_MS=750 tests/performance/ingest.k6.js
 */

import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Trend } from "k6/metrics";
import { randomSeed } from "k6";

const ENDPOINT = (__ENV.ENDPOINT || "http://localhost:58000").replace(
  /\/$/,
  "",
);
const API_KEY = __ENV.API_KEY || "";
const SPANS_PER_TRACE = Number(__ENV.SPANS_PER_TRACE || 5);
const P95_MS = Number(__ENV.P95_MS || 1500);

const spansAccepted = new Counter("aiobs_spans_accepted");
const spansRejected = new Counter("aiobs_spans_rate_limited");
const batchLatency = new Trend("aiobs_batch_latency_ms", true);

export const options = {
  scenarios: {
    // A ramp rather than a step: a step hides the difference between "slow at
    // steady state" and "slow while warming up", and those have different fixes.
    ramp: {
      executor: "ramping-arrival-rate",
      startRate: 10,
      timeUnit: "1s",
      preAllocatedVUs: 20,
      maxVUs: 200,
      stages: [
        { target: 50, duration: "30s" },
        { target: 200, duration: "1m" },
        { target: 200, duration: "2m" },
        { target: 0, duration: "30s" },
      ],
    },
  },
  thresholds: {
    // A rate limit is a correct response under load, not an error, so it is
    // excluded from the failure budget and counted separately.
    http_req_failed: ["rate<0.01"],
    aiobs_batch_latency_ms: [`p(95)<${P95_MS}`],
  },
};

randomSeed(1234);

function hex(length) {
  let out = "";
  for (let index = 0; index < length; index += 1) {
    out += "0123456789abcdef"[Math.floor(Math.random() * 16)];
  }
  return out;
}

function buildBatch() {
  const traceId = hex(32);
  const rootId = hex(16);
  const now = Date.now() * 1e6;
  const spans = [
    {
      trace_id: traceId,
      span_id: rootId,
      parent_span_id: null,
      name: "POST /chat",
      kind: "server",
      category: "workflow_step",
      start_time_unix_nano: now,
      end_time_unix_nano: now + 800 * 1e6,
      status: "ok",
      attributes: { "http.request.method": "POST", "http.route": "/chat" },
    },
  ];

  for (let index = 0; index < SPANS_PER_TRACE - 1; index += 1) {
    const start = now + (index + 1) * 5e6;
    const isGeneration = index % 2 === 0;
    const span = {
      trace_id: traceId,
      span_id: hex(16),
      parent_span_id: rootId,
      name: isGeneration ? "openai.chat" : "vector.search",
      kind: "client",
      category: isGeneration ? "chat_completion" : "retrieval",
      start_time_unix_nano: start,
      end_time_unix_nano: start + 300 * 1e6,
      status: Math.random() < 0.03 ? "error" : "ok",
      attributes: {
        "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.operation.name": "chat",
      },
    };
    if (isGeneration) {
      span.usage = {
        input_tokens: 200 + Math.floor(Math.random() * 3800),
        output_tokens: 20 + Math.floor(Math.random() * 780),
        source: "provider",
      };
    }
    spans.push(span);
  }

  return {
    resource: {
      service_name: "k6-load-generator",
      service_version: "0.1.0",
      environment: "development",
      sdk_name: "aiobs-k6",
      sdk_version: "0.1.0",
      sdk_language: "javascript",
    },
    spans,
  };
}

export default function ingest() {
  const batch = buildBatch();
  const headers = { "Content-Type": "application/json" };
  if (API_KEY) headers["X-API-Key"] = API_KEY;

  const response = http.post(
    `${ENDPOINT}/v1/ingest/spans`,
    JSON.stringify(batch),
    {
      headers,
      tags: { name: "ingest_spans" },
    },
  );

  batchLatency.add(response.timings.duration);

  const accepted = response.status >= 200 && response.status < 300;
  if (accepted) spansAccepted.add(batch.spans.length);
  if (response.status === 429) spansRejected.add(batch.spans.length);

  check(response, {
    "accepted or deliberately rate-limited": (r) =>
      (r.status >= 200 && r.status < 300) || r.status === 429,
    "no server error": (r) => r.status < 500,
  });
}

export function handleSummary(data) {
  // Printed rather than asserted: what counts as acceptable depends on the
  // deployment, and a number measured on one machine is not a claim about any
  // other.
  return {
    stdout: JSON.stringify(
      {
        spans_accepted: data.metrics.aiobs_spans_accepted?.values?.count ?? 0,
        spans_rate_limited:
          data.metrics.aiobs_spans_rate_limited?.values?.count ?? 0,
        batch_latency_ms: data.metrics.aiobs_batch_latency_ms?.values ?? {},
        http_req_failed: data.metrics.http_req_failed?.values ?? {},
      },
      null,
      2,
    ),
  };
}
