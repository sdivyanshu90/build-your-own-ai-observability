#!/usr/bin/env python3
"""Ingestion load generator.

Drives the native ingest endpoint at a target rate and reports what actually
happened. Two rules shape the output:

1. **It reports achieved rate, not requested rate.** A harness that prints the
   number you asked for tells you nothing. If the server could not keep up, the
   achieved rate is lower and the report says so.
2. **It reports latency as percentiles measured here**, at the client, because
   that is where queueing delay becomes visible. Server-side timings exclude
   exactly the time a saturated system spends waiting.

Nothing in this file asserts a threshold. Publishing a number that was never
measured on the hardware in question is worse than publishing none, so the
harness measures and prints; deciding whether a result is acceptable is the
operator's job, informed by `docs/operations/capacity.md`.

Usage::

    python tests/performance/run_load_test.py --rate 500 --duration 60 \
        --endpoint http://localhost:58000 --api-key aiobs_...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError as exc:  # pragma: no cover - the harness is optional tooling
    print("httpx is required: pip install httpx", file=sys.stderr)
    raise SystemExit(2) from exc


NANOS = 1_000_000_000

_MODELS = (
    ("openai", "gpt-4o"),
    ("openai", "gpt-4o-mini"),
    ("anthropic", "claude-sonnet-4"),
)


@dataclass
class Results:
    """Everything measured during a run."""

    started_at: float
    latencies_ms: list[float] = field(default_factory=list)
    accepted: int = 0
    rejected: int = 0
    failed: int = 0
    spans_sent: int = 0
    status_counts: dict[int, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)

    def record_status(self, status: int) -> None:
        self.status_counts[status] = self.status_counts.get(status, 0) + 1

    def record_error(self, error: str) -> None:
        key = error[:80]
        self.errors[key] = self.errors.get(key, 0) + 1

    def percentile(self, fraction: float) -> float | None:
        if not self.latencies_ms:
            return None
        ordered = sorted(self.latencies_ms)
        # Exact lower order statistic, the same definition the platform's own
        # percentile queries use, so the two are comparable.
        index = max(0, min(len(ordered) - 1, int(fraction * len(ordered)) - 1))
        return ordered[index]

    def as_dict(self, elapsed: float) -> dict[str, Any]:
        return {
            "elapsed_seconds": round(elapsed, 3),
            "requests": {
                "accepted": self.accepted,
                "rejected": self.rejected,
                "failed": self.failed,
                "by_status": dict(sorted(self.status_counts.items())),
            },
            "spans_sent": self.spans_sent,
            "achieved_spans_per_second": (
                round(self.spans_sent / elapsed, 2) if elapsed > 0 else 0.0
            ),
            "achieved_requests_per_second": (
                round((self.accepted + self.rejected + self.failed) / elapsed, 2)
                if elapsed > 0
                else 0.0
            ),
            "latency_ms": {
                "p50": _round(self.percentile(0.50)),
                "p90": _round(self.percentile(0.90)),
                "p95": _round(self.percentile(0.95)),
                "p99": _round(self.percentile(0.99)),
                "max": _round(max(self.latencies_ms) if self.latencies_ms else None),
                "mean": _round(statistics.fmean(self.latencies_ms) if self.latencies_ms else None),
            },
            "errors": self.errors,
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _hex(length: int, rng: random.Random) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(length))


def build_batch(rng: random.Random, spans_per_trace: int) -> dict[str, Any]:
    """One trace of realistic shape: a root, a retrieval and a generation."""
    trace_id = _hex(32, rng)
    root_id = _hex(16, rng)
    now = int(time.time() * NANOS)
    provider, model = rng.choice(_MODELS)

    spans: list[dict[str, Any]] = [
        {
            "trace_id": trace_id,
            "span_id": root_id,
            "parent_span_id": None,
            "name": "POST /chat",
            "kind": "server",
            "category": "workflow_step",
            "start_time_unix_nano": now,
            "end_time_unix_nano": now + rng.randint(200, 3_000) * 1_000_000,
            "status": "ok",
            "attributes": {"http.request.method": "POST", "http.route": "/chat"},
        }
    ]

    for index in range(max(0, spans_per_trace - 1)):
        child_id = _hex(16, rng)
        is_generation = index % 2 == 0
        start = now + (index + 1) * 5_000_000
        span: dict[str, Any] = {
            "trace_id": trace_id,
            "span_id": child_id,
            "parent_span_id": root_id,
            "name": f"{provider}.chat" if is_generation else "vector.search",
            "kind": "client",
            "category": "chat_completion" if is_generation else "retrieval",
            "start_time_unix_nano": start,
            "end_time_unix_nano": start + rng.randint(50, 1_500) * 1_000_000,
            "status": "error" if rng.random() < 0.03 else "ok",
            "attributes": {
                "gen_ai.system": provider,
                "gen_ai.request.model": model,
                "gen_ai.operation.name": "chat",
            },
        }
        if is_generation:
            span["usage"] = {
                "input_tokens": rng.randint(200, 4_000),
                "output_tokens": rng.randint(20, 800),
                "source": "provider",
            }
        spans.append(span)

    return {
        "resource": {
            "service_name": "load-generator",
            "service_version": "0.1.0",
            "environment": "development",
            "sdk_name": "aiobs-load-test",
            "sdk_version": "0.1.0",
            "sdk_language": "python",
        },
        "spans": spans,
    }


async def _worker(
    client: httpx.AsyncClient,
    endpoint: str,
    headers: dict[str, str],
    queue: asyncio.Queue[dict[str, Any] | None],
    results: Results,
) -> None:
    while True:
        batch = await queue.get()
        try:
            if batch is None:
                return
            started = time.perf_counter()
            try:
                response = await client.post(endpoint, json=batch, headers=headers)
            except Exception as exc:
                results.failed += 1
                results.record_error(f"{type(exc).__name__}: {exc}")
                continue
            results.latencies_ms.append((time.perf_counter() - started) * 1000)
            results.record_status(response.status_code)
            if response.status_code < 300:
                results.accepted += 1
                results.spans_sent += len(batch["spans"])
            elif response.status_code == 429:
                results.rejected += 1
            else:
                results.failed += 1
                results.record_error(f"HTTP {response.status_code}: {response.text[:120]}")
        finally:
            queue.task_done()


async def run(arguments: argparse.Namespace) -> int:
    rng = random.Random(arguments.seed)
    endpoint = f"{arguments.endpoint.rstrip('/')}/v1/ingest/spans"
    headers = {"content-type": "application/json"}
    api_key = arguments.api_key or os.environ.get("AIOBS_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key

    spans_per_trace = max(1, arguments.spans_per_trace)
    traces_per_second = max(1.0, arguments.rate / spans_per_trace)
    interval = 1.0 / traces_per_second

    results = Results(started_at=time.time())
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=arguments.concurrency * 4)

    limits = httpx.Limits(
        max_connections=arguments.concurrency, max_keepalive_connections=arguments.concurrency
    )
    async with httpx.AsyncClient(timeout=arguments.timeout, limits=limits) as client:
        workers = [
            asyncio.create_task(_worker(client, endpoint, headers, queue, results))
            for _ in range(arguments.concurrency)
        ]

        started = time.perf_counter()
        deadline = started + arguments.duration
        next_send = started
        backlog = 0

        while time.perf_counter() < deadline:
            now = time.perf_counter()
            if now < next_send:
                await asyncio.sleep(min(next_send - now, 0.01))
                continue
            try:
                queue.put_nowait(build_batch(rng, spans_per_trace))
            except asyncio.QueueFull:
                # The client itself is saturated. Counting this separately keeps
                # a client-side bottleneck from being reported as server latency.
                backlog += 1
                await asyncio.sleep(0.001)
            next_send += interval

        await queue.join()
        for _ in workers:
            queue.put_nowait(None)
        await asyncio.gather(*workers)
        elapsed = time.perf_counter() - started

    report = results.as_dict(elapsed)
    report["requested_spans_per_second"] = arguments.rate
    report["spans_per_trace"] = spans_per_trace
    report["concurrency"] = arguments.concurrency
    report["client_backpressure_events"] = backlog
    report["endpoint"] = endpoint

    if arguments.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report)

    if arguments.output:
        with Path(arguments.output).open("w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)

    # A run that could not deliver anything is a failure; anything else is a
    # measurement, and the operator decides what it means.
    return 0 if results.accepted > 0 else 1


def _print_human(report: dict[str, Any]) -> None:
    requests = report["requests"]
    latency = report["latency_ms"]
    print()
    print(f"  endpoint            {report['endpoint']}")
    print(f"  duration            {report['elapsed_seconds']}s")
    print(f"  requested rate      {report['requested_spans_per_second']} spans/s")
    print(f"  achieved rate       {report['achieved_spans_per_second']} spans/s")
    print(
        f"  requests            {requests['accepted']} accepted, "
        f"{requests['rejected']} rate-limited, {requests['failed']} failed"
    )
    print(f"  spans sent          {report['spans_sent']}")
    print(f"  latency p50/p95/p99 {latency['p50']} / {latency['p95']} / {latency['p99']} ms")
    print(f"  latency max         {latency['max']} ms")
    if report["client_backpressure_events"]:
        print(
            f"  client backpressure {report['client_backpressure_events']} events "
            "-- the generator, not the server, was the bottleneck"
        )
    if report["errors"]:
        print("  errors:")
        for message, count in sorted(report["errors"].items(), key=lambda item: -item[1]):
            print(f"    {count:>6}  {message}")
    print()
    print("  These numbers describe this machine and this configuration only.")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Observability ingestion load test")
    parser.add_argument("--endpoint", default="http://localhost:58000")
    parser.add_argument("--api-key", default=None, help="defaults to $AIOBS_API_KEY")
    parser.add_argument("--rate", type=float, default=100, help="target spans per second")
    parser.add_argument("--duration", type=float, default=30, help="seconds")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--spans-per-trace", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--output", default=None, help="also write the report to this path")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
