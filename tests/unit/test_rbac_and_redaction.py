"""Authorisation matrix and redaction policy.

The RBAC tests walk *every* cell of the matrix rather than spot-checking, so a
permission accidentally granted to a lower role is caught by construction.
"""

from __future__ import annotations

import pytest

from aiobs_api.core.errors import PermissionDeniedError, TenantMismatchError
from aiobs_api.domain.principal import Principal
from aiobs_api.domain.rbac import (
    PERMISSIONS_BY_ROLE,
    Permission,
    Role,
    missing_permissions,
    permissions_for,
    permissions_for_scopes,
    role_can,
)
from aiobs_api.domain.redaction import (
    REDACTED_MARKER,
    RedactionMode,
    RedactionPolicy,
    Redactor,
    merge_policies,
)

ROLE_ORDER = [Role.VIEWER, Role.ANALYST, Role.DEVELOPER, Role.ADMINISTRATOR, Role.OWNER]


class TestRoleMatrix:
    def test_every_role_is_defined(self) -> None:
        assert set(PERMISSIONS_BY_ROLE) == set(Role)

    @pytest.mark.parametrize("permission", list(Permission))
    def test_permissions_are_monotonic_across_roles(self, permission: Permission) -> None:
        """Once a role has a permission, every more-privileged role has it too.

        Without this, a "promotion" could silently remove access, which is the
        kind of thing nobody notices until an owner cannot do something an
        analyst can.
        """
        granted = [role_can(role, permission) for role in ROLE_ORDER]
        first_true = next((index for index, value in enumerate(granted) if value), None)
        if first_true is None:
            pytest.fail(f"{permission.value} is granted to no role at all")
        assert all(granted[first_true:]), (
            f"{permission.value} is granted to {ROLE_ORDER[first_true].value} "
            "but missing from a more privileged role"
        )

    def test_viewer_cannot_write_anything(self) -> None:
        writes = [
            Permission.PROMPT_PUBLISH,
            Permission.PROMPT_PROMOTE,
            Permission.MODEL_WRITE,
            Permission.DATASET_WRITE,
            Permission.API_KEY_CREATE,
            Permission.PRICE_BOOK_WRITE,
            Permission.RETENTION_WRITE,
            Permission.MEMBER_INVITE,
            Permission.EXPORT_CREATE,
        ]
        assert missing_permissions(Role.VIEWER, writes) == tuple(writes)

    def test_analyst_reads_cost_but_cannot_change_prices(self) -> None:
        assert role_can(Role.ANALYST, Permission.COST_READ)
        assert role_can(Role.ANALYST, Permission.PRICE_BOOK_READ)
        assert not role_can(Role.ANALYST, Permission.PRICE_BOOK_WRITE)

    def test_developer_cannot_manage_credentials_or_people(self) -> None:
        for permission in (
            Permission.API_KEY_CREATE,
            Permission.API_KEY_REVOKE,
            Permission.MEMBER_INVITE,
            Permission.MEMBER_UPDATE_ROLE,
            Permission.AUDIT_READ,
        ):
            assert not role_can(Role.DEVELOPER, permission), permission.value

    def test_only_owner_can_delete_the_organization(self) -> None:
        for role in ROLE_ORDER:
            expected = role is Role.OWNER
            assert role_can(role, Permission.ORG_DELETE) is expected

    def test_payload_access_requires_developer(self) -> None:
        """Prompt and completion text is the most sensitive data the platform
        holds; read-only viewers and analysts get metadata only."""
        assert not role_can(Role.VIEWER, Permission.TRACE_READ_PAYLOADS)
        assert not role_can(Role.ANALYST, Permission.TRACE_READ_PAYLOADS)
        assert role_can(Role.DEVELOPER, Permission.TRACE_READ_PAYLOADS)

    def test_dataset_samples_require_developer(self) -> None:
        assert not role_can(Role.ANALYST, Permission.DATASET_READ_SAMPLES)
        assert role_can(Role.DEVELOPER, Permission.DATASET_READ_SAMPLES)

    def test_unknown_role_grants_nothing(self) -> None:
        """A corrupted role string must fail closed, not fall through."""
        assert permissions_for("superuser") == frozenset()
        assert permissions_for("") == frozenset()


class TestApiKeyScopes:
    def test_ingest_scope_cannot_read_traces(self) -> None:
        granted = permissions_for_scopes(["ingest"])
        assert Permission.INGEST_WRITE in granted
        assert Permission.TRACE_READ not in granted

    def test_read_scope_cannot_ingest(self) -> None:
        granted = permissions_for_scopes(["read"])
        assert Permission.TRACE_READ in granted
        assert Permission.INGEST_WRITE not in granted

    def test_no_scope_grants_administration(self) -> None:
        granted = permissions_for_scopes(["ingest", "read"])
        for permission in (
            Permission.API_KEY_CREATE,
            Permission.MEMBER_INVITE,
            Permission.PRICE_BOOK_WRITE,
            Permission.ORG_DELETE,
            Permission.AUDIT_READ,
        ):
            assert permission not in granted, permission.value

    def test_unknown_scope_is_ignored(self) -> None:
        assert permissions_for_scopes(["admin", "root"]) == frozenset()


class TestPrincipal:
    def test_require_raises_for_a_missing_permission(self) -> None:
        principal = Principal.for_user(
            user_id="u", email="v@x.invalid", organization_id="org_1", role=Role.VIEWER
        )
        with pytest.raises(PermissionDeniedError):
            principal.require(Permission.PROMPT_PUBLISH)

    def test_cross_tenant_access_raises_tenant_mismatch(self) -> None:
        principal = Principal.for_user(
            user_id="u", email="o@x.invalid", organization_id="org_1", role=Role.OWNER
        )
        with pytest.raises(TenantMismatchError):
            principal.require_organization("org_2")

    def test_api_key_is_pinned_to_one_project(self) -> None:
        principal = Principal.for_api_key(
            key_id="key_1",
            name="k",
            organization_id="org_1",
            project_id="prj_1",
            environment_id="env_1",
            environment_name="production",
            scopes=["ingest"],
        )
        principal.require_project("prj_1")
        with pytest.raises(PermissionDeniedError):
            principal.require_project("prj_2")

    def test_api_key_is_pinned_to_one_environment(self) -> None:
        principal = Principal.for_api_key(
            key_id="key_1",
            name="k",
            organization_id="org_1",
            project_id="prj_1",
            environment_id="env_staging",
            environment_name="staging",
            scopes=["ingest"],
        )
        with pytest.raises(PermissionDeniedError):
            principal.require_environment("env_production")

    def test_unscoped_membership_reaches_every_project(self) -> None:
        principal = Principal.for_user(
            user_id="u", email="o@x.invalid", organization_id="org_1", role=Role.OWNER
        )
        principal.require_project("any-project-at-all")

    def test_scoped_membership_is_an_allowlist(self) -> None:
        principal = Principal.for_user(
            user_id="u",
            email="c@x.invalid",
            organization_id="org_1",
            role=Role.DEVELOPER,
            project_scope=["prj_allowed"],
        )
        principal.require_project("prj_allowed")
        with pytest.raises(PermissionDeniedError):
            principal.require_project("prj_other")

    def test_repr_does_not_leak_the_label(self) -> None:
        """The label is an email; it must not appear in a log line or traceback."""
        principal = Principal.for_user(
            user_id="u", email="secret.person@company.invalid", organization_id="o", role=Role.OWNER
        )
        assert "secret.person" not in repr(principal)


class TestRedaction:
    def test_redacts_by_key_name(self) -> None:
        redactor = Redactor()
        result = redactor.redact_attributes({"password": "hunter2", "user": "alice"})
        assert result.value["password"] == REDACTED_MARKER
        assert result.value["user"] == "alice"
        assert "password" in result.redacted_keys

    def test_never_redacts_registered_non_sensitive_attributes(self) -> None:
        """Regression: `aiobs.usage.input_tokens` contains "token" and was being
        destroyed by the generic key heuristic, silently zeroing every token
        count and every cost."""
        redactor = Redactor()
        result = redactor.redact_attributes(
            {
                "aiobs.usage.input_tokens": 1200,
                "aiobs.usage.output_tokens": 340,
                "gen_ai.request.max_tokens": 2048,
                "aiobs.session.id": "session-1",
            }
        )
        assert result.value["aiobs.usage.input_tokens"] == 1200
        assert result.value["aiobs.usage.output_tokens"] == 340
        assert result.value["gen_ai.request.max_tokens"] == 2048
        assert result.value["aiobs.session.id"] == "session-1"

    def test_redacts_registered_sensitive_attributes(self) -> None:
        redactor = Redactor()
        result = redactor.redact_attributes({"aiobs.prompt.variables": '{"x":1}'})
        assert result.value["aiobs.prompt.variables"] == REDACTED_MARKER

    def test_does_not_over_match_ordinary_words(self) -> None:
        redactor = Redactor()
        result = redactor.redact_attributes(
            {"author": "alice", "spinner.count": 3, "session_count": 7}
        )
        assert result.value["author"] == "alice"
        assert result.value["spinner.count"] == 3
        assert result.value["session_count"] == 7

    @pytest.mark.parametrize(
        "text",
        [
            # These are the literal shapes the redactor must catch, so the
            # test cannot avoid containing them. secret-scan-allow
            "-----BEGIN RSA PRIVATE KEY-----",  # secret-scan-allow
            "AKIAIOSFODNN7EXAMPLE",
            "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
        ],
    )
    def test_redacts_high_confidence_value_patterns(self, text: str) -> None:
        cleaned, fired = Redactor().redact_value(f"prefix {text} suffix")
        assert REDACTED_MARKER in cleaned
        assert fired

    def test_credit_card_detection_uses_a_luhn_check(self) -> None:
        """Without the checksum, every order number and long integer in a prompt
        would be destroyed."""
        redactor = Redactor()
        valid, _ = redactor.redact_value("card 4111111111111111 end")
        invalid, _ = redactor.redact_value("order 1234567890123456 end")
        assert REDACTED_MARKER in valid
        assert "1234567890123456" in invalid

    def test_nested_structures_are_walked(self) -> None:
        redactor = Redactor()
        result = redactor.redact_attributes({"config": {"api_key": "sk-secret", "model": "gpt-4o"}})
        assert result.value["config"]["api_key"] == REDACTED_MARKER
        assert result.value["config"]["model"] == "gpt-4o"

    def test_allowlist_drops_unknown_keys_entirely(self) -> None:
        """In allowlist mode, an unknown key's very presence is not disclosed."""
        redactor = Redactor(RedactionPolicy(allowlist=frozenset({"kept"})))
        result = redactor.redact_attributes({"kept": 1, "dropped": 2})
        assert "dropped" not in result.value
        assert result.value["kept"] == 1

    def test_strict_mode_removes_payloads_entirely(self) -> None:
        payload, removed = Redactor(RedactionPolicy.strict()).redact_payload("a prompt")
        assert payload is None and removed is True

    def test_disabled_mode_changes_nothing(self) -> None:
        redactor = Redactor(RedactionPolicy.disabled())
        result = redactor.redact_attributes({"password": "hunter2"})
        assert result.value["password"] == "hunter2"

    def test_long_payloads_are_truncated_and_flagged(self) -> None:
        redactor = Redactor(RedactionPolicy(max_value_length=50))
        result = redactor.redact_attributes({"note": "x" * 200})
        assert "truncated" in result.value["note"]
        assert "note" in result.truncated_keys

    def test_headers_and_urls_are_scrubbed(self) -> None:
        redactor = Redactor()
        headers = redactor.redact_headers({"authorization": "Bearer x", "accept": "json"})
        assert headers["authorization"] == REDACTED_MARKER
        assert headers["accept"] == "json"
        url = redactor.redact_url("https://user:pw@api.example.com/v1?api_key=secret&q=hello")
        assert "pw@" not in url
        assert "secret" not in url
        assert "q=hello" in url

    def test_project_settings_can_only_tighten_policy(self) -> None:
        """A tenant must not be able to opt out of the operator's posture."""
        base = RedactionPolicy(mode=RedactionMode.STANDARD)
        loosened = merge_policies(base, {"mode": "off"})
        tightened = merge_policies(base, {"mode": "strict"})
        assert loosened.mode is RedactionMode.STANDARD
        assert tightened.mode is RedactionMode.STRICT

    def test_project_settings_can_only_shorten_payload_limits(self) -> None:
        base = RedactionPolicy(max_value_length=1_000)
        assert merge_policies(base, {"max_value_length": 100}).max_value_length == 100
        assert merge_policies(base, {"max_value_length": 100_000}).max_value_length == 1_000

    def test_a_broken_custom_detector_does_not_drop_data(self) -> None:
        def explodes(_: str) -> bool:
            raise RuntimeError("detector bug")

        redactor = Redactor(RedactionPolicy(detectors=(("broken", explodes),)))
        cleaned, _ = redactor.redact_value("ordinary text")
        assert cleaned == "ordinary text"
