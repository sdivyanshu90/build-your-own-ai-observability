"""Roles, permissions and the authorisation matrix.

Authorisation is a pure function of ``(role, permission)`` plus, for
project-scoped principals, a membership check. Keeping it pure and data-driven
has two payoffs: the whole policy is auditable by reading one table, and the
test suite can assert *every* cell of the matrix rather than the handful of
paths someone remembered to cover.

Design rules:

* **Deny by default.** A permission not listed for a role is denied. Adding a
  new permission therefore starts out unavailable to everyone, which is the
  safe direction to fail.
* **Permissions are verbs on resources**, not screens. ``prompt:publish`` is
  meaningful independent of which UI button triggers it, so an API-only client
  gets the same rules.
* **Read and write are always separate.** ``analyst`` can read cost data and
  cannot change a price book; conflating them is how reporting users end up
  able to rewrite history.
* **Enforcement happens in the service layer.** The frontend hides what a user
  cannot do as a courtesy; the backend is what makes it true.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from typing import Final

__all__ = [
    "PERMISSIONS_BY_ROLE",
    "Permission",
    "Role",
    "permissions_for",
    "role_can",
]


class Role(str, Enum):
    """Organisation-level roles, ordered from most to least privileged."""

    OWNER = "owner"
    ADMINISTRATOR = "administrator"
    DEVELOPER = "developer"
    ANALYST = "analyst"
    VIEWER = "viewer"

    @property
    def rank(self) -> int:
        """Lower is more privileged. Used only for display ordering."""
        return {
            Role.OWNER: 0,
            Role.ADMINISTRATOR: 1,
            Role.DEVELOPER: 2,
            Role.ANALYST: 3,
            Role.VIEWER: 4,
        }[self]


class Permission(str, Enum):
    """Every action the API can authorise."""

    # --- organisation ------------------------------------------------------
    ORG_READ = "org:read"
    ORG_UPDATE = "org:update"
    ORG_DELETE = "org:delete"

    # --- membership --------------------------------------------------------
    MEMBER_READ = "member:read"
    MEMBER_INVITE = "member:invite"
    MEMBER_UPDATE_ROLE = "member:update_role"
    MEMBER_REMOVE = "member:remove"

    # --- projects and environments ----------------------------------------
    PROJECT_READ = "project:read"
    PROJECT_CREATE = "project:create"
    PROJECT_UPDATE = "project:update"
    PROJECT_DELETE = "project:delete"
    ENVIRONMENT_READ = "environment:read"
    ENVIRONMENT_WRITE = "environment:write"

    # --- credentials -------------------------------------------------------
    API_KEY_READ = "api_key:read"
    API_KEY_CREATE = "api_key:create"
    API_KEY_REVOKE = "api_key:revoke"
    SERVICE_ACCOUNT_READ = "service_account:read"
    SERVICE_ACCOUNT_WRITE = "service_account:write"

    # --- telemetry ---------------------------------------------------------
    INGEST_WRITE = "ingest:write"
    TRACE_READ = "trace:read"
    TRACE_READ_PAYLOADS = "trace:read_payloads"
    SPAN_READ = "span:read"
    METRICS_READ = "metrics:read"

    # --- registries --------------------------------------------------------
    PROMPT_READ = "prompt:read"
    PROMPT_CREATE = "prompt:create"
    PROMPT_PUBLISH = "prompt:publish"
    PROMPT_PROMOTE = "prompt:promote"
    MODEL_READ = "model:read"
    MODEL_WRITE = "model:write"
    DATASET_READ = "dataset:read"
    #: Reading actual dataset rows, which may contain regulated content.
    DATASET_READ_SAMPLES = "dataset:read_samples"
    DATASET_WRITE = "dataset:write"
    DATASET_DELETE = "dataset:delete"
    EXPERIMENT_READ = "experiment:read"
    EXPERIMENT_WRITE = "experiment:write"

    # --- cost --------------------------------------------------------------
    COST_READ = "cost:read"
    PRICE_BOOK_READ = "price_book:read"
    PRICE_BOOK_WRITE = "price_book:write"

    # --- operations --------------------------------------------------------
    RETENTION_READ = "retention:read"
    RETENTION_WRITE = "retention:write"
    EXPORT_CREATE = "export:create"
    EXPORT_READ = "export:read"
    AUDIT_READ = "audit:read"
    SAVED_VIEW_READ = "saved_view:read"
    SAVED_VIEW_WRITE = "saved_view:write"
    #: Dead-letter inspection, replay, reconciliation triggers.
    OPERATIONS_ADMIN = "operations:admin"


_VIEWER: Final[frozenset[Permission]] = frozenset(
    {
        Permission.ORG_READ,
        Permission.PROJECT_READ,
        Permission.ENVIRONMENT_READ,
        Permission.TRACE_READ,
        Permission.SPAN_READ,
        Permission.METRICS_READ,
        Permission.PROMPT_READ,
        Permission.MODEL_READ,
        Permission.DATASET_READ,
        Permission.EXPERIMENT_READ,
        Permission.SAVED_VIEW_READ,
        Permission.MEMBER_READ,
    }
)

#: Analysts investigate cost and quality. They get cost and export rights but
#: no ability to change anything that alters what is recorded.
_ANALYST: Final[frozenset[Permission]] = _VIEWER | frozenset(
    {
        Permission.COST_READ,
        Permission.PRICE_BOOK_READ,
        Permission.EXPORT_CREATE,
        Permission.EXPORT_READ,
        Permission.SAVED_VIEW_WRITE,
        Permission.RETENTION_READ,
    }
)

#: Developers build and ship: they publish prompts, register models and read
#: payloads. They cannot manage people, credentials or pricing.
_DEVELOPER: Final[frozenset[Permission]] = _ANALYST | frozenset(
    {
        Permission.TRACE_READ_PAYLOADS,
        Permission.INGEST_WRITE,
        Permission.PROMPT_CREATE,
        Permission.PROMPT_PUBLISH,
        Permission.PROMPT_PROMOTE,
        Permission.MODEL_WRITE,
        Permission.DATASET_WRITE,
        Permission.DATASET_READ_SAMPLES,
        Permission.EXPERIMENT_WRITE,
        Permission.PROJECT_CREATE,
        Permission.PROJECT_UPDATE,
        Permission.ENVIRONMENT_WRITE,
        Permission.API_KEY_READ,
    }
)

#: Administrators run the tenant: credentials, people, retention, pricing.
_ADMINISTRATOR: Final[frozenset[Permission]] = _DEVELOPER | frozenset(
    {
        Permission.ORG_UPDATE,
        Permission.MEMBER_INVITE,
        Permission.MEMBER_UPDATE_ROLE,
        Permission.MEMBER_REMOVE,
        Permission.PROJECT_DELETE,
        Permission.API_KEY_CREATE,
        Permission.API_KEY_REVOKE,
        Permission.SERVICE_ACCOUNT_READ,
        Permission.SERVICE_ACCOUNT_WRITE,
        Permission.DATASET_DELETE,
        Permission.PRICE_BOOK_WRITE,
        Permission.RETENTION_WRITE,
        Permission.AUDIT_READ,
        Permission.OPERATIONS_ADMIN,
    }
)

#: Owners additionally hold the irreversible action: deleting the tenant.
_OWNER: Final[frozenset[Permission]] = _ADMINISTRATOR | frozenset({Permission.ORG_DELETE})


PERMISSIONS_BY_ROLE: Final[dict[Role, frozenset[Permission]]] = {
    Role.OWNER: _OWNER,
    Role.ADMINISTRATOR: _ADMINISTRATOR,
    Role.DEVELOPER: _DEVELOPER,
    Role.ANALYST: _ANALYST,
    Role.VIEWER: _VIEWER,
}


def permissions_for(role: Role | str) -> frozenset[Permission]:
    """Return the permission set for ``role``.

    An unknown role yields the empty set rather than raising: a corrupted or
    downgraded role string in the database must fail closed, not crash the
    request or -- far worse -- fall through to a permissive default.
    """
    try:
        resolved = role if isinstance(role, Role) else Role(role)
    except ValueError:
        return frozenset()
    return PERMISSIONS_BY_ROLE[resolved]


def role_can(role: Role | str, permission: Permission) -> bool:
    """Whether ``role`` grants ``permission``."""
    return permission in permissions_for(role)


def missing_permissions(role: Role | str, required: Iterable[Permission]) -> tuple[Permission, ...]:
    """Return the subset of ``required`` that ``role`` does not grant."""
    granted = permissions_for(role)
    return tuple(permission for permission in required if permission not in granted)


#: Scopes an API key may carry. Deliberately coarse: an SDK key should be able
#: to send telemetry and, optionally, read back its own project's data -- not to
#: administer the tenant. Anything richer needs a service account with a role.
class ApiKeyScope(str, Enum):
    INGEST = "ingest"
    READ = "read"

    @property
    def permissions(self) -> frozenset[Permission]:
        if self is ApiKeyScope.INGEST:
            return frozenset(
                {
                    Permission.INGEST_WRITE,
                    Permission.PROMPT_READ,
                    Permission.MODEL_READ,
                    Permission.PROJECT_READ,
                }
            )
        return frozenset(
            {
                Permission.TRACE_READ,
                Permission.SPAN_READ,
                Permission.METRICS_READ,
                Permission.PROMPT_READ,
                Permission.MODEL_READ,
                Permission.DATASET_READ,
                Permission.PROJECT_READ,
                Permission.COST_READ,
            }
        )


def permissions_for_scopes(scopes: Iterable[str]) -> frozenset[Permission]:
    """Union of permissions granted by a set of API-key scopes."""
    granted: set[Permission] = set()
    for scope in scopes:
        try:
            granted |= ApiKeyScope(scope).permissions
        except ValueError:
            continue
    return frozenset(granted)
