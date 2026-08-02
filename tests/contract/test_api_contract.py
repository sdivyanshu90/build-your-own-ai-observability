"""API contract: the OpenAPI schema, error envelope, pagination and headers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiobs_schemas.errors import STATUS_FOR_CODE, ErrorCode


def window() -> dict[str, str]:
    now = datetime.now(timezone.utc)
    return {
        "start": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "end": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    }


class TestOpenApi:
    async def test_schema_is_generated(self, client) -> None:
        schema = (await client.get("/openapi.json")).json()
        assert schema["openapi"].startswith("3.")
        assert len(schema["paths"]) > 40

    async def test_security_schemes_are_declared(self, client) -> None:
        schema = (await client.get("/openapi.json")).json()
        schemes = schema["components"]["securitySchemes"]
        assert "bearerAuth" in schemes and "apiKeyAuth" in schemes

    async def test_every_required_endpoint_family_exists(self, client) -> None:
        schema = (await client.get("/openapi.json")).json()
        paths = set(schema["paths"])
        for required in (
            "/v1/traces",
            "/v1/traces/{trace_id}",
            "/v1/traces/{trace_id}/spans",
            "/v1/traces/{trace_id}/retrieval",
            "/v1/traces/{trace_id}/trajectory",
            "/v1/traces/compare",
            "/v1/prompts",
            "/v1/prompts/{prompt_id}/versions",
            "/v1/models",
            "/v1/models/{model_id}/versions",
            "/v1/datasets",
            "/v1/datasets/{dataset_id}/versions",
            "/v1/costs",
            "/v1/price-books",
            "/v1/exports",
            "/v1/audit-events",
            "/v1/api-keys",
            "/v1/projects",
            "/health",
            "/ready",
            "/live",
        ):
            assert required in paths, f"missing endpoint {required}"

    async def test_otlp_endpoint_is_at_the_specified_path(self, client) -> None:
        """OTLP exporters append /v1/traces and cannot be reconfigured."""
        schema = (await client.get("/openapi.json")).json()
        assert "post" in schema["paths"]["/v1/traces"]


class TestErrorEnvelope:
    def test_every_error_code_has_a_status(self) -> None:
        for code in ErrorCode:
            assert code in STATUS_FOR_CODE, f"{code.value} has no HTTP status"

    async def test_validation_errors_use_the_envelope(self, authenticated_client) -> None:
        response = await authenticated_client.get(
            "/v1/traces", params={"project_id": "prj_x", "filter": "nope:eq:1", **window()}
        )
        body = response.json()
        assert response.status_code == 422
        for field in ("code", "message", "request_id", "details", "context"):
            assert field in body
        assert body["code"] == "validation_failed"
        assert body["documentation_url"]

    async def test_not_found_uses_the_envelope(self, authenticated_client) -> None:
        response = await authenticated_client.get("/v1/projects/prj_nonexistent")
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    async def test_a_request_id_is_always_returned(self, client) -> None:
        response = await client.get("/live")
        assert response.headers.get("x-request-id", "").startswith("req_")

    async def test_the_client_request_id_is_echoed(self, client) -> None:
        response = await client.get("/live", headers={"X-Request-Id": "my-correlation-id"})
        assert response.headers["x-request-id"] == "my-correlation-id"

    async def test_a_malicious_request_id_is_sanitised(self, client) -> None:
        """A header value is untrusted input and must not reach a log line raw."""
        response = await client.get("/live", headers={"X-Request-Id": "bad\r\nInjected: header"})
        assert "\r" not in response.headers["x-request-id"]
        assert "\n" not in response.headers["x-request-id"]


class TestSecurityHeaders:
    async def test_hardening_headers_are_present(self, client) -> None:
        response = await client.get("/live")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "content-security-policy" in response.headers

    async def test_headers_are_present_on_error_responses_too(self, client) -> None:
        response = await client.get("/v1/projects")
        assert response.status_code == 401
        assert response.headers["x-content-type-options"] == "nosniff"


class TestPagination:
    async def test_limit_is_bounded(self, authenticated_client, tenant) -> None:
        response = await authenticated_client.get(
            "/v1/traces", params={"project_id": tenant["project_id"], "limit": 10_000, **window()}
        )
        assert response.status_code == 422

    async def test_a_tampered_cursor_is_rejected(self, authenticated_client, tenant) -> None:
        """Cursors are signed: an edited one must not reach the query builder."""
        response = await authenticated_client.get(
            "/v1/traces",
            params={
                "project_id": tenant["project_id"],
                "cursor": "bm90LWEtcmVhbC1jdXJzb3I",
                **window(),
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "validation_failed"

    async def test_the_page_envelope_is_uniform(self, authenticated_client, tenant) -> None:
        response = await authenticated_client.get(
            "/v1/traces", params={"project_id": tenant["project_id"], **window()}
        )
        body = response.json()
        assert set(body) == {"items", "next_cursor", "has_more"}


class TestIngestLimits:
    async def test_limits_are_advertised(self, authenticated_client) -> None:
        body = (await authenticated_client.get("/v1/ingest/limits")).json()
        for field in (
            "max_spans_per_batch",
            "max_attributes_per_span",
            "max_body_bytes",
            "max_clock_skew_future_seconds",
            "otlp_endpoint",
            "native_endpoint",
        ):
            assert field in body

    async def test_advertised_limits_match_the_enforced_ones(self, authenticated_client) -> None:
        from aiobs_schemas.wire import LIMITS

        body = (await authenticated_client.get("/v1/ingest/limits")).json()
        assert body["max_spans_per_batch"] == LIMITS.MAX_SPANS_PER_BATCH
        assert body["max_attributes_per_span"] == LIMITS.MAX_ATTRIBUTES_PER_SPAN


class TestCrossOriginBehaviour:
    """A browser client must be able to read every response, including failures.

    Starlette's ServerErrorMiddleware sits outside the CORS layer, so an
    unhandled exception used to reach the browser with no
    ``Access-Control-Allow-Origin`` header. The fetch then failed with an opaque
    "Failed to fetch" and the request id -- the one thing that makes a 500
    diagnosable -- never reached the operator.
    """

    ORIGIN = "http://localhost:53000"

    async def test_a_successful_response_is_readable_cross_origin(self, client) -> None:
        response = await client.get("/health", headers={"Origin": self.ORIGIN})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == self.ORIGIN

    async def test_an_error_response_is_readable_cross_origin(self, authenticated_client) -> None:
        response = await authenticated_client.get(
            "/v1/traces/does-not-exist",
            params={"project_id": "prj_missing"},
            headers={"Origin": self.ORIGIN},
        )
        assert response.status_code >= 400
        assert response.headers.get("access-control-allow-origin") == self.ORIGIN

    async def test_an_unhandled_exception_returns_the_envelope_with_cors(self, app, client) -> None:
        # A route that raises past every guard, so this exercises the
        # last-resort handler rather than a validation path.
        @app.get("/internal-test/explode")
        async def explode() -> None:  # pragma: no cover - invoked via HTTP
            raise RuntimeError("synthetic failure with a secret in it")

        response = await client.get("/internal-test/explode", headers={"Origin": self.ORIGIN})

        assert response.status_code == 500
        body = response.json()
        assert body["code"] == ErrorCode.INTERNAL_ERROR.value
        assert body["request_id"], "a 500 without a request id is undiagnosable"
        # The exception text must not leak to the caller.
        assert "synthetic failure" not in response.text
        assert response.headers.get("access-control-allow-origin") == self.ORIGIN

    async def test_a_disallowed_origin_gets_no_cors_grant(self, client) -> None:
        response = await client.get("/health", headers={"Origin": "https://evil.example.test"})
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers


class TestMetricNaming:
    """Metric names follow the same vocabulary as filters and sort."""

    async def test_a_logical_field_name_is_accepted(self, authenticated_client, tenant) -> None:
        response = await authenticated_client.get(
            "/v1/metrics/timeseries",
            params={
                "project_id": tenant["project_id"],
                "metric": "duration_ms",
                "aggregation": "avg",
                "source": "traces",
                **window(),
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["unit"] == "ms"

    async def test_the_physical_column_name_still_works(self, authenticated_client, tenant) -> None:
        response = await authenticated_client.get(
            "/v1/metrics/timeseries",
            params={
                "project_id": tenant["project_id"],
                "metric": "duration_ns",
                "aggregation": "avg",
                "source": "traces",
                **window(),
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["unit"] == "ns"

    async def test_an_unknown_metric_lists_the_valid_ones(
        self, authenticated_client, tenant
    ) -> None:
        response = await authenticated_client.get(
            "/v1/metrics/timeseries",
            params={
                "project_id": tenant["project_id"],
                "metric": "not_a_column",
                "aggregation": "sum",
                "source": "traces",
                **window(),
            },
        )
        assert response.status_code == STATUS_FOR_CODE[ErrorCode.VALIDATION_FAILED]
        # The error must be actionable: it names what *is* aggregatable.
        assert "duration_ms" in response.json()["message"]
