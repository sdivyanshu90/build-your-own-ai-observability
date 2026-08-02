"""Startup configuration validation.

The platform refuses to run a production process with a development-shaped
configuration. These tests pin the specific mistakes that are easy to make and
expensive to discover in production.
"""

from __future__ import annotations

import pytest

from aiobs_api.core.config import Settings


def _production(**overrides: object) -> Settings:
    """A settings object that is production-shaped apart from the overrides."""
    base: dict[str, object] = {
        "environment": "production",
        "public_url": "https://observability.example.com",
        "database": {"url": "postgresql+asyncpg://aiobs:secret@db/aiobs"},
        "analytics": {"driver": "clickhouse", "clickhouse_url": "http://clickhouse:8123"},
        "kv": {"driver": "redis", "redis_url": "redis://redis:6379/0"},
        "objects": {"driver": "s3", "bucket": "aiobs-payloads"},
        "bus": {"driver": "kafka", "brokers": "kafka:9092"},
        "security": {
            "cors_allow_origins": ["https://observability.example.com"],
            "cookie_secure": True,
        },
        "ingest": {"allow_anonymous_ingest": False},
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestCorsValidation:
    def test_a_production_ready_configuration_has_no_problems(self) -> None:
        problems = _production().validate_for_runtime()
        assert [p for p in problems if "CORS" in p or "origin" in p] == []

    def test_a_wildcard_origin_is_rejected(self) -> None:
        problems = _production(
            security={"cors_allow_origins": ["*"], "cookie_secure": True}
        ).validate_for_runtime()
        assert any("wildcard CORS origin" in problem for problem in problems)

    @pytest.mark.parametrize(
        "origin",
        [
            "http://localhost:53000",
            "http://127.0.0.1:53000",
            "http://observability.internal",
        ],
    )
    def test_a_plaintext_credentialed_origin_is_rejected(self, origin: str) -> None:
        """No loopback exemption.

        A production deployment that still trusts ``http://localhost`` is either
        misconfigured or left a development default in place. Credentialed CORS
        over plaintext hands the session token to anyone on the path, and the
        error names the offending origin so the fix is obvious.
        """
        problems = _production(
            security={
                "cors_allow_origins": [origin],
                "cors_allow_credentials": True,
                "cookie_secure": True,
            }
        ).validate_for_runtime()
        assert any("must use https" in problem for problem in problems)
        assert any(origin in problem for problem in problems)

    def test_development_defaults_allow_both_spellings_of_loopback(self) -> None:
        """A browser treats localhost and 127.0.0.1 as different origins.

        Shipping only one of them makes a perfectly ordinary local setup fail
        its very first login with an unexplained network error.
        """
        development = Settings(environment="development")
        assert "http://localhost:53000" in development.security.cors_allow_origins
        assert "http://127.0.0.1:53000" in development.security.cors_allow_origins

    def test_a_development_configuration_is_not_validated_against_production_rules(
        self,
    ) -> None:
        development = Settings(environment="development")
        # The development defaults are deliberately permissive; the guard only
        # applies where it matters.
        assert development.validate_for_runtime() == [] or all(
            "https" not in problem for problem in development.validate_for_runtime()
        )
