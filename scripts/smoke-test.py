#!/usr/bin/env python
"""End-to-end smoke test against a running API.

Exercises the read and write paths a new engineer would try first, and asserts
on *values* rather than status codes: an endpoint returning 200 with an empty
body is the failure mode this catches.

Usage:
    python scripts/smoke-test.py --api http://127.0.0.1:58000 \\
        --email admin@example.com --password change-me-immediately-please
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

import httpx

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  \033[32mPASS\033[0m  {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  \033[31mFAIL\033[0m  {label}" + (f"  ({detail})" if detail else ""))


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:58000")
    # Defaults match `aiobs-admin bootstrap`, so `make bootstrap && make smoke`
    # works with no arguments. Override both for any other deployment.
    parser.add_argument("--email", default="admin@example.com")
    parser.add_argument("--password", default="change-me-immediately-please")
    parser.add_argument("--api-key", default=None)
    arguments = parser.parse_args()

    client = httpx.Client(base_url=arguments.api, timeout=30.0)

    section("health")
    response = client.get("/live")
    check("GET /live returns 204", response.status_code == 204)
    response = client.get("/ready")
    body = response.json()
    check("GET /ready is ready", body.get("status") == "ready", json.dumps(body.get("checks")))

    section("authentication")
    response = client.post(
        "/v1/auth/login", json={"email": arguments.email, "password": arguments.password}
    )
    check("login succeeds", response.status_code == 200, str(response.status_code))
    if response.status_code != 200:
        print(response.text[:400])
        return 1
    tokens = response.json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    check("token is organization-scoped", bool(tokens.get("organization_id")))
    check("role is owner", tokens.get("role") == "owner", tokens.get("role", ""))

    check(
        "unauthenticated request is rejected",
        client.get("/v1/projects").status_code == 401,
    )
    check(
        "garbage token is rejected",
        client.get("/v1/projects", headers={"Authorization": "Bearer nonsense"}).status_code == 401,
    )

    section("projects")
    projects = client.get("/v1/projects", headers=headers).json()
    check("at least one project", len(projects) >= 1, f"{len(projects)} projects")
    project = projects[0]
    project_id = project["id"]
    check(
        "three default environments",
        len(project["environments"]) == 3,
        ",".join(item["name"] for item in project["environments"]),
    )
    check(
        "production environment is flagged",
        any(item["is_production"] for item in project["environments"]),
    )

    # A window inside the API's 400-day query limit; the demo data covers the
    # last 24 hours.
    now = datetime.now(timezone.utc)
    window = {
        "start": (now - timedelta(days=7)).isoformat().replace("+00:00", "Z"),
        "end": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
    }

    section("trace explorer")
    response = client.get(
        "/v1/traces", headers=headers, params={"project_id": project_id, "limit": 5, **window}
    )
    check("trace search succeeds", response.status_code == 200, str(response.status_code))
    page = response.json()
    traces = page["items"]
    check("traces returned", len(traces) > 0, f"{len(traces)} traces")
    check("pagination reports more", page["has_more"] is True)
    check("cursor issued", bool(page["next_cursor"]))

    second = client.get(
        "/v1/traces",
        headers=headers,
        params={"project_id": project_id, "limit": 5, "cursor": page["next_cursor"], **window},
    ).json()
    first_ids = {item["trace_id"] for item in traces}
    second_ids = {item["trace_id"] for item in second["items"]}
    check("second page is disjoint", not (first_ids & second_ids), f"{len(second_ids)} traces")

    priced = [item for item in traces if item["cost"] is not None]
    check("traces carry cost", len(priced) > 0, f"{len(priced)}/{len(traces)} priced")
    check(
        "cost is a decimal string",
        all(isinstance(item["cost"], str) for item in priced),
        priced[0]["cost"] if priced else "",
    )
    check("traces carry tokens", any(item["total_tokens"] > 0 for item in traces))
    check("traces carry models", any(item["models"] for item in traces))

    section("filtering and sorting")
    filtered = client.get(
        "/v1/traces",
        headers=headers,
        params={"project_id": project_id, "filter": "status:eq:ok", "limit": 50, **window},
    ).json()
    check(
        "status filter applies",
        all(item["status"] == "ok" for item in filtered["items"]),
        f"{len(filtered['items'])} matched",
    )
    sorted_page = client.get(
        "/v1/traces",
        headers=headers,
        params={"project_id": project_id, "sort": "-duration_ms", "limit": 5, **window},
    ).json()
    durations = [item["duration_ms"] for item in sorted_page["items"] if item["duration_ms"]]
    check("descending sort applies", durations == sorted(durations, reverse=True))
    bad = client.get(
        "/v1/traces",
        headers=headers,
        params={"project_id": project_id, "filter": "nonexistent:eq:x", **window},
    )
    check("unknown filter field rejected", bad.status_code == 422, str(bad.status_code))
    check("error envelope has a code", "code" in bad.json(), bad.json().get("code", ""))

    section("trace detail")
    trace_id = traces[0]["trace_id"]
    detail = client.get(
        f"/v1/traces/{trace_id}", headers=headers, params={"project_id": project_id}
    ).json()
    check("spans returned", len(detail["spans"]) > 0, f"{len(detail['spans'])} spans")
    check("critical path computed", len(detail["critical_path"]) > 0)
    check("span tree adjacency present", len(detail["children"]) > 0)
    check(
        "self time computed",
        any(item.get("self_time_ms") is not None for item in detail["spans"]),
    )
    check(
        "critical path flagged on spans",
        any(item["on_critical_path"] for item in detail["spans"]),
    )
    root = [item for item in detail["spans"] if item["parent_span_id"] is None]
    check("exactly one root span", len(root) == 1, f"{len(root)} roots")

    section("retrieval visualisation")
    rag = None
    for candidate in client.get(
        "/v1/traces",
        headers=headers,
        params={"project_id": project_id, "filter": "retrieval_count:gte:1", "limit": 5, **window},
    ).json()["items"]:
        rag = candidate
        break
    if rag:
        stages = client.get(
            f"/v1/traces/{rag['trace_id']}/retrieval",
            headers=headers,
            params={"project_id": project_id},
        ).json()
        check("retrieval stages returned", len(stages) > 0)
        if stages:
            stage = stages[0]
            check(
                "documents ranked", len(stage["documents"]) > 0, f"{len(stage['documents'])} docs"
            )
            check("query captured", bool(stage["query"]))
            check("pipeline stages described", len(stage["stages"]) == 6)
            check("diagnostics computed", "unused_ratio" in stage["diagnostics"])
            check(
                "rerank movement measured",
                stage["diagnostics"].get("mean_rank_movement") is not None,
            )
            check(
                "selection recorded",
                any(document["selected"] for document in stage["documents"]),
            )
    else:
        check("a retrieval trace exists", False, "none found")

    section("agent trajectory")
    agent_trace = None
    for candidate in client.get(
        "/v1/traces",
        headers=headers,
        params={"project_id": project_id, "filter": "agent_step_count:gte:2", "limit": 5, **window},
    ).json()["items"]:
        agent_trace = candidate
        break
    if agent_trace:
        trajectory = client.get(
            f"/v1/traces/{agent_trace['trace_id']}/trajectory",
            headers=headers,
            params={"project_id": project_id},
        ).json()
        graph = trajectory["graph"]
        check("graph nodes built", len(graph["nodes"]) > 0, f"{len(graph['nodes'])} nodes")
        check("graph edges built", len(graph["edges"]) > 0, f"{len(graph['edges'])} edges")
        check("steps listed", len(trajectory["steps"]) > 0)
        check(
            "critical path marked",
            any(node["on_critical_path"] for node in graph["nodes"]),
        )
        check("termination reason recorded", bool(graph["termination_reason"]))
    else:
        check("an agent trace exists", False, "none found")

    section("comparison")
    if len(traces) >= 2:
        comparison = client.get(
            "/v1/traces/compare",
            headers=headers,
            params={
                "project_id": project_id,
                "left": traces[0]["trace_id"],
                "right": traces[1]["trace_id"],
            },
        )
        check("compare succeeds", comparison.status_code == 200, str(comparison.status_code))
        body = comparison.json()
        check("summary deltas present", "duration_ms" in body["summary_deltas"])
        check("lineage differences present", "prompt_version_ids" in body["lineage_differences"])

    section("metrics and cost")
    overview = client.get(
        "/v1/metrics/overview",
        headers=headers,
        params={"project_id": project_id, "compare_previous": "true", **window},
    ).json()
    check("request count", overview["request_count"] > 0, str(overview["request_count"]))
    check("latency percentiles", overview["latency"] is not None)
    if overview["latency"]:
        check(
            "p95 >= p50",
            (overview["latency"]["p95"] or 0) >= (overview["latency"]["p50"] or 0),
            f"p50={overview['latency']['p50']:.1f}ms p95={overview['latency']['p95']:.1f}ms",
        )
    check("token totals", overview["total_tokens"] > 0, str(overview["total_tokens"]))
    check("cost total", overview["total_cost"] is not None, str(overview["total_cost"]))
    check("previous window compared", overview["previous"] is not None)

    series = client.get(
        "/v1/metrics/timeseries",
        headers=headers,
        params={
            "project_id": project_id,
            "metric": "total_tokens",
            "aggregation": "sum",
            "group_by": ["model"],
            "interval": "1h",
            **window,
        },
    ).json()
    check("timeseries grouped", len(series["groups"]) > 0, f"{len(series['groups'])} series")
    check("timeseries has points", any(group["points"] for group in series["groups"]))

    latency = client.get(
        "/v1/metrics/latency",
        headers=headers,
        params={"project_id": project_id, "group_by": ["model"], **window},
    ).json()
    check("latency by model", len(latency["groups"]) > 0, f"{len(latency['groups'])} groups")
    check("latency unit is ms", latency["unit"] == "ms")

    costs = client.get(
        "/v1/costs",
        headers=headers,
        params={"project_id": project_id, "group_by": ["model"], **window},
    ).json()
    check("cost breakdown", len(costs["groups"]) > 0, f"{len(costs['groups'])} models")
    check(
        "cost amounts are strings",
        all(isinstance(group["total"], str) for group in costs["groups"] if group["total"]),
    )

    values = client.get(
        "/v1/metrics/values",
        headers=headers,
        params={"project_id": project_id, "column": "model", **window},
    ).json()
    check("distinct values", len(values["values"]) > 0, f"{len(values['values'])} models")

    section("registries")
    created = client.post(
        "/v1/prompts",
        headers=headers,
        json={"project_id": project_id, "name": "smoke-test-prompt", "tags": ["smoke"]},
    )
    check(
        "prompt created or exists",
        created.status_code in (201, 409),
        str(created.status_code),
    )
    # Re-runs against the same database must exercise the same assertions, so
    # look the prompt up rather than skipping the block.
    if created.status_code == 201:
        prompt_id = created.json()["id"]
    else:
        existing_prompts = client.get(
            "/v1/prompts", headers=headers, params={"project_id": project_id}
        ).json()
        prompt_id = next(
            item["id"] for item in existing_prompts if item["name"] == "smoke-test-prompt"
        )
    if prompt_id:
        messages = [
            {"role": "system", "content": "You are a helpful support agent."},
            {"role": "user", "content": "{question}"},
        ]
        body = {"messages": messages, "variable_schema": {"question": {"type": "string"}}}
        first = client.post(f"/v1/prompts/{prompt_id}/versions", headers=headers, json=body).json()
        check("version has content hash", first["content_hash"].startswith("sha256:"))
        # Byte-identical content must converge; a differing variable schema is
        # a different version, which the next assertion covers.
        again = client.post(f"/v1/prompts/{prompt_id}/versions", headers=headers, json=body).json()
        check(
            "identical content converges on one version",
            again["id"] == first["id"],
            first["id"],
        )
        changed = client.post(
            f"/v1/prompts/{prompt_id}/versions",
            headers=headers,
            json={
                "messages": [
                    {"role": "system", "content": "You are a concise support agent."},
                    {"role": "user", "content": "{question}"},
                ]
            },
        ).json()
        check("changed content forks a version", changed["id"] != first["id"])
        schema_change = client.post(
            f"/v1/prompts/{prompt_id}/versions",
            headers=headers,
            json={"messages": messages, "variable_schema": {"topic": {"type": "string"}}},
        ).json()
        check(
            "changed variable schema forks a version",
            schema_change["id"] != first["id"],
        )
        promoted = client.post(
            f"/v1/prompts/{prompt_id}/aliases",
            headers=headers,
            json={"alias": "production", "version_id": first["id"]},
        )
        check("alias promoted", promoted.status_code == 200, str(promoted.status_code))
        resolved = client.get(
            "/v1/prompts/resolve",
            headers=headers,
            params={"project_id": project_id, "name": "smoke-test-prompt", "alias": "production"},
        ).json()
        check("alias resolves to the version", resolved["id"] == first["id"])
        rolled = client.post(
            f"/v1/prompts/{prompt_id}/aliases",
            headers=headers,
            json={"alias": "production", "version_id": changed["id"]},
        ).json()
        check("rollback records the previous target", rolled["previous_version_id"] == first["id"])
        diff = client.get(
            f"/v1/prompts/versions/{changed['id']}/diff",
            headers=headers,
            params={"against": first["id"]},
        ).json()
        check("diff detects a change", diff["identical"] is False)
        check("diff locates the message", len(diff["message_changes"]) == 1)

    model_version = client.post(
        "/v1/models/versions",
        headers=headers,
        json={
            "provider": "anthropic",
            "model_identifier": "claude-sonnet-4",
            "config": {"temperature": 0.2, "max_tokens": 2048},
        },
    )
    check(
        "model version registered", model_version.status_code == 201, str(model_version.status_code)
    )
    if model_version.status_code == 201:
        first_config = model_version.json()
        same = client.post(
            "/v1/models/versions",
            headers=headers,
            json={
                "provider": "anthropic",
                "model_identifier": "claude-sonnet-4",
                "config": {"temperature": 0.2, "max_tokens": 2048},
            },
        ).json()
        check("identical config converges", same["id"] == first_config["id"])
        different = client.post(
            "/v1/models/versions",
            headers=headers,
            json={
                "provider": "anthropic",
                "model_identifier": "claude-sonnet-4",
                "config": {"temperature": 0.9, "max_tokens": 2048},
            },
        ).json()
        check("different temperature forks a version", different["id"] != first_config["id"])

    section("price books")
    books = client.get("/v1/price-books", headers=headers).json()
    check("sample price book seeded", len(books) >= 1, f"{len(books)} books")
    if books:
        entries = client.get(f"/v1/price-books/{books[0]['id']}/entries", headers=headers).json()
        check("price entries present", len(entries) > 0, f"{len(entries)} entries")
        check(
            "prices are decimal strings",
            all(isinstance(entry["unit_price"], str) for entry in entries),
        )
        check("entries cite a source", any(entry["source_url"] for entry in entries))

    section("ingestion")
    limits = client.get("/v1/ingest/limits", headers=headers).json()
    check("limits advertised", limits["max_spans_per_batch"] > 0)
    check("otlp endpoint advertised", limits["otlp_endpoint"].endswith("/v1/traces"))

    section("exports")
    export = client.post(
        "/v1/exports",
        headers=headers,
        json={"project_id": project_id, "resource": "traces", "format": "jsonl"},
        params=window,
    )
    check("export accepted", export.status_code == 202, str(export.status_code))

    section("audit log")
    audit = client.get("/v1/audit-events", headers=headers, params=window).json()
    check("audit events recorded", len(audit["items"]) > 0, f"{len(audit['items'])} events")
    actions = {item["action"] for item in audit["items"]}
    check("login audited", "auth.login.succeeded" in actions, ",".join(sorted(actions)[:4]))

    section("openapi")
    schema = client.get("/openapi.json").json()
    check("schema generated", len(schema["paths"]) > 40, f"{len(schema['paths'])} paths")
    check("security schemes declared", "securitySchemes" in schema["components"])

    print(f"\n\033[1m{PASSED} passed, {FAILED} failed\033[0m")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
