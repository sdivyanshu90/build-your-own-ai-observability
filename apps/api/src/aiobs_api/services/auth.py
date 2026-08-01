"""Authentication: passwords, sessions, API keys, service accounts, OIDC.

Credential handling decisions, and the reasoning:

**Argon2id for passwords.** Memory-hard, so GPU and ASIC attackers lose most of
their advantage. Parameters are configurable because the right cost depends on
the hardware, and are validated at startup rather than guessed per call.

**API keys are hashed, never stored.** The plaintext is shown exactly once. A
database dump therefore yields no usable credentials. The *prefix* is stored in
the clear so the auth path can find the candidate row with an indexed lookup --
without it, verifying a key would mean running Argon2 against every key in the
table, which is both slow and a denial-of-service vector.

**API-key verification uses SHA-256, not Argon2.** This is a deliberate
departure and worth being explicit about: an API key is 256 bits of
cryptographic randomness, not a human-chosen password, so it has no meaningful
offline-guessing attack surface to slow down. Running Argon2 on the ingestion
hot path would add ~100 ms to every batch. The security property that matters
(a stolen hash is not a usable credential) holds either way.

**Token epochs.** Every user row carries a ``token_epoch``. Access tokens embed
it, and a password change, role change or explicit "sign out everywhere" bumps
it -- invalidating every outstanding token without a revocation list.

**Timing-safe failure.** A login attempt for an unknown email still performs a
hash comparison against a dummy value, so response time does not disclose
whether an account exists.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select

from aiobs_schemas.errors import ErrorCode
from aiobs_schemas.ids import IdPrefix, generate_id

from ..core.config import AuthSettings, Settings
from ..core.errors import AuthenticationError, ValidationFailedError
from ..core.logging import get_logger
from ..core.timeutil import Clock
from ..domain.principal import Principal, PrincipalType
from ..domain.rbac import Role
from ..storage.postgres.models import ApiKey, Environment, Membership, Project, ServiceAccount, User
from ..storage.postgres.session import Database

__all__ = ["AuthService", "IssuedApiKey", "TokenPair"]

log = get_logger(__name__)

#: Recognisable, greppable key format: ``aiobs_<env>_<prefix><secret>``.
_KEY_NAMESPACE = "aiobs"
_KEY_PREFIX_LENGTH = 8
_KEY_SECRET_BYTES = 32

#: Argon2 hash of a fixed string, compared against when the account does not
#: exist so that the failure path costs the same as the success path.
_DUMMY_PASSWORD = "aiobs-timing-equaliser"


@dataclass(frozen=True, slots=True)
class TokenPair:
    """Freshly issued access and refresh tokens."""

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    """A newly created API key. ``secret`` is unrecoverable after this object."""

    id: str
    name: str
    prefix: str
    #: The full plaintext credential. Returned once; never stored or logged.
    secret: str
    project_id: str
    environment_id: str
    scopes: tuple[str, ...]
    expires_at: datetime | None


class AuthService:
    """Issues and verifies every kind of credential the platform accepts."""

    def __init__(self, *, database: Database, settings: Settings, clock: Clock) -> None:
        self._database = database
        self._settings = settings
        self._auth: AuthSettings = settings.auth
        self._clock = clock
        self._hasher = PasswordHasher(
            time_cost=self._auth.argon2_time_cost,
            memory_cost=self._auth.argon2_memory_cost_kib,
            parallelism=self._auth.argon2_parallelism,
        )

    # ------------------------------------------------------------------
    # passwords
    # ------------------------------------------------------------------

    def hash_password(self, password: str) -> str:
        """Hash a password, enforcing a minimum length first."""
        if len(password) < 12:
            raise ValidationFailedError(
                "password must be at least 12 characters; length dominates "
                "composition rules for resisting offline attack"
            )
        if len(password) > 1024:
            # Unbounded input to a memory-hard function is a denial-of-service.
            raise ValidationFailedError("password must be at most 1024 characters")
        return self._hasher.hash(password)

    def verify_password(self, stored_hash: str | None, password: str) -> bool:
        """Constant-ish-time password verification.

        When ``stored_hash`` is ``None`` (no such user, or an OIDC-only user) a
        dummy verification still runs so the timing signal is flat.
        """
        if not stored_hash:
            try:
                self._hasher.verify(self._hasher.hash(_DUMMY_PASSWORD), password)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                pass
            return False
        try:
            self._hasher.verify(stored_hash, password)
            return True
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, stored_hash: str) -> bool:
        """Whether the hash was produced with weaker parameters than current."""
        try:
            return self._hasher.check_needs_rehash(stored_hash)
        except InvalidHashError:
            return True

    # ------------------------------------------------------------------
    # local login
    # ------------------------------------------------------------------

    async def authenticate_user(self, email: str, password: str) -> User:
        """Verify credentials, enforcing lockout. Raises on any failure."""
        if not self._auth.enable_local_auth:
            raise AuthenticationError(
                "local authentication is disabled; use the configured identity provider",
                code=ErrorCode.INVALID_CREDENTIALS,
            )
        now = self._clock.now()

        # The failure path must COMMIT before it raises. Recording the attempt
        # inside the transaction and then raising would roll back the very
        # counter that implements lockout, leaving brute-force protection
        # silently disabled -- so the outcome is decided first and the exception
        # is raised after the transaction closes.
        failure: AuthenticationError | None = None

        async with self._database.session_scope() as session:
            user = (
                await session.execute(select(User).where(User.email == email.lower().strip()))
            ).scalar_one_or_none()

            if user is not None and user.locked_until and user.locked_until > now:
                failure = AuthenticationError(
                    "account is temporarily locked after repeated failed sign-ins",
                    code=ErrorCode.INVALID_CREDENTIALS,
                    retry_after_seconds=(user.locked_until - now).total_seconds(),
                )
            else:
                valid = self.verify_password(user.password_hash if user else None, password)
                if user is None or not valid or not user.is_active:
                    if user is not None:
                        user.failed_login_count += 1
                        if user.failed_login_count >= self._auth.max_failed_logins:
                            user.locked_until = now + timedelta(seconds=self._auth.lockout_seconds)
                            log.warning(
                                "auth.account_locked",
                                user_id=user.id,
                                failed_attempts=user.failed_login_count,
                            )
                    # One generic message for every failure mode: wrong
                    # password, unknown account and deactivated account must be
                    # indistinguishable to an attacker enumerating users.
                    failure = AuthenticationError(
                        "invalid email or password", code=ErrorCode.INVALID_CREDENTIALS
                    )
                else:
                    user.failed_login_count = 0
                    user.locked_until = None
                    user.last_login_at = now
                    if user.password_hash and self.needs_rehash(user.password_hash):
                        user.password_hash = self._hasher.hash(password)
                    await session.flush()
                    session.expunge(user)

        if failure is not None:
            raise failure
        assert user is not None  # narrowed by the failure check above
        return user

    # ------------------------------------------------------------------
    # tokens
    # ------------------------------------------------------------------

    def issue_tokens(self, *, user: User, organization_id: str) -> TokenPair:
        """Mint an access/refresh pair scoped to one organisation.

        Tokens are organisation-scoped rather than global: a user belonging to
        three tenants holds three tokens, so a token leaked from one tenant
        cannot read another.
        """
        now = self._clock.now()
        access_claims = {
            "sub": user.id,
            "org": organization_id,
            "email": user.email,
            "epoch": user.token_epoch,
            "typ": "access",
            "iss": self._auth.jwt_issuer,
            "aud": self._auth.jwt_audience,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=self._auth.access_token_ttl_seconds)).timestamp()),
            "jti": secrets.token_urlsafe(16),
        }
        refresh_claims = {
            **access_claims,
            "typ": "refresh",
            "exp": int((now + timedelta(seconds=self._auth.refresh_token_ttl_seconds)).timestamp()),
            "jti": secrets.token_urlsafe(16),
        }
        secret = self._auth.jwt_secret.get_secret_value()
        return TokenPair(
            access_token=jwt.encode(access_claims, secret, algorithm=self._auth.jwt_algorithm),
            refresh_token=jwt.encode(refresh_claims, secret, algorithm=self._auth.jwt_algorithm),
            expires_in=self._auth.access_token_ttl_seconds,
        )

    def decode_token(self, token: str, *, expected_type: str = "access") -> dict[str, Any]:
        """Verify and decode a locally-issued token."""
        try:
            claims = jwt.decode(
                token,
                self._auth.jwt_secret.get_secret_value(),
                algorithms=[self._auth.jwt_algorithm],
                issuer=self._auth.jwt_issuer,
                audience=self._auth.jwt_audience,
                # Expiry is checked below against the service's own clock.
                # PyJWT would use the process wall clock, which diverges from
                # the injected clock and makes token lifetime untestable --
                # and, worse, means issuance and validation could disagree in a
                # deployment that deliberately skews time.
                options={"require": ["exp", "iat", "sub"], "verify_exp": False},
            )
        except jwt.InvalidTokenError as exc:
            raise AuthenticationError(
                "token is invalid", code=ErrorCode.INVALID_CREDENTIALS
            ) from exc

        expires_at = int(claims.get("exp", 0))
        if expires_at <= int(self._clock.now().timestamp()):
            raise AuthenticationError("token has expired", code=ErrorCode.TOKEN_EXPIRED)
        if claims.get("typ") != expected_type:
            # Refusing a refresh token where an access token is expected stops
            # a long-lived credential being used as a short-lived one.
            raise AuthenticationError(
                f"expected a {expected_type} token", code=ErrorCode.INVALID_CREDENTIALS
            )
        return claims

    async def principal_from_token(self, token: str) -> Principal:
        """Resolve a bearer token into a :class:`Principal`."""
        claims = self.decode_token(token)
        user_id = str(claims["sub"])
        organization_id = str(claims.get("org") or "")
        if not organization_id:
            raise AuthenticationError(
                "token is not scoped to an organization", code=ErrorCode.INVALID_CREDENTIALS
            )

        async with self._database.session_scope() as session:
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one_or_none()
            if user is None or not user.is_active:
                raise AuthenticationError(
                    "account is not active", code=ErrorCode.INVALID_CREDENTIALS
                )
            if int(claims.get("epoch", -1)) != user.token_epoch:
                # The epoch moved: password change, role change or global sign-out.
                raise AuthenticationError(
                    "token has been invalidated; sign in again",
                    code=ErrorCode.TOKEN_EXPIRED,
                )
            membership = (
                await session.execute(
                    select(Membership).where(
                        Membership.user_id == user_id,
                        Membership.organization_id == organization_id,
                    )
                )
            ).scalar_one_or_none()
            if membership is None:
                raise AuthenticationError(
                    "no membership in the requested organization",
                    code=ErrorCode.PERMISSION_DENIED,
                )
            return Principal.for_user(
                user_id=user.id,
                email=user.email,
                organization_id=organization_id,
                role=membership.role,
                project_scope=membership.project_scope or (),
                authenticated_at=self._clock.now(),
            )

    # ------------------------------------------------------------------
    # API keys
    # ------------------------------------------------------------------

    def _key_material(self, *, is_production: bool) -> tuple[str, str, str]:
        """Return ``(prefix, secret, lookup_hash)`` for a new key."""
        environment_tag = "live" if is_production else "test"
        body = secrets.token_urlsafe(_KEY_SECRET_BYTES)
        prefix = f"{_KEY_NAMESPACE}_{environment_tag}_{secrets.token_hex(_KEY_PREFIX_LENGTH // 2)}"
        secret = f"{prefix}_{body}"
        return prefix, secret, self._hash_key(secret)

    def _hash_key(self, secret: str) -> str:
        """Keyed SHA-256 of an API key.

        Keyed with the JWT secret so a stolen database alone is insufficient to
        build a rainbow table, and fast enough to run on every ingest request.
        See the module docstring for why this is not Argon2.
        """
        return hmac.new(
            self._auth.jwt_secret.get_secret_value().encode("utf-8"),
            secret.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def create_api_key(
        self,
        *,
        organization_id: str,
        project_id: str,
        environment_id: str,
        name: str,
        scopes: list[str],
        created_by: str | None,
        expires_at: datetime | None = None,
    ) -> IssuedApiKey:
        """Create an API key and return its plaintext exactly once."""
        if not scopes:
            raise ValidationFailedError("an API key must declare at least one scope")
        unknown = set(scopes) - {"ingest", "read"}
        if unknown:
            raise ValidationFailedError(f"unknown API key scopes: {sorted(unknown)}")

        async with self._database.session_scope() as session:
            environment = (
                await session.execute(
                    select(Environment).where(
                        Environment.id == environment_id,
                        Environment.organization_id == organization_id,
                    )
                )
            ).scalar_one_or_none()
            if environment is None or environment.project_id != project_id:
                raise ValidationFailedError("environment does not exist in the requested project")

            prefix, secret, secret_hash = self._key_material(
                is_production=environment.is_production
            )
            key = ApiKey(
                id=generate_id(IdPrefix.API_KEY),
                # Set explicitly from the service's clock rather than relying on
                # the server-side default. The `revoked_at >= created_at` CHECK
                # compares an application-written timestamp against this one, so
                # both must come from the same authority -- otherwise a
                # sub-second difference between the app and the database clock
                # can make a legitimate revocation violate the constraint.
                created_at=self._clock.now(),
                organization_id=organization_id,
                project_id=project_id,
                environment_id=environment_id,
                name=name,
                prefix=prefix,
                secret_hash=secret_hash,
                scopes=list(scopes),
                created_by=created_by,
                expires_at=expires_at,
            )
            session.add(key)
            await session.flush()
            return IssuedApiKey(
                id=key.id,
                name=name,
                prefix=prefix,
                secret=secret,
                project_id=project_id,
                environment_id=environment_id,
                scopes=tuple(scopes),
                expires_at=expires_at,
            )

    async def principal_from_api_key(self, secret: str) -> Principal:
        """Resolve an API key into a :class:`Principal`.

        Runs on every ingest request, so it is one indexed lookup plus one HMAC.
        """
        prefix = _prefix_of(secret)
        if prefix is None:
            raise AuthenticationError("malformed API key", code=ErrorCode.INVALID_CREDENTIALS)
        now = self._clock.now()
        expected = self._hash_key(secret)

        async with self._database.session_scope() as session:
            key = (
                await session.execute(select(ApiKey).where(ApiKey.prefix == prefix))
            ).scalar_one_or_none()
            if key is None or not hmac.compare_digest(key.secret_hash, expected):
                raise AuthenticationError("invalid API key", code=ErrorCode.INVALID_CREDENTIALS)
            if key.revoked_at is not None:
                raise AuthenticationError(
                    "API key has been revoked", code=ErrorCode.API_KEY_REVOKED
                )
            if key.expires_at is not None and key.expires_at <= now:
                raise AuthenticationError("API key has expired", code=ErrorCode.API_KEY_EXPIRED)
            if self._auth.api_key_max_age_days is not None:
                max_age = timedelta(days=self._auth.api_key_max_age_days)
                if key.created_at and key.created_at + max_age <= now:
                    raise AuthenticationError(
                        "API key exceeds the maximum permitted age; rotate it",
                        code=ErrorCode.API_KEY_EXPIRED,
                    )

            environment = (
                await session.execute(
                    select(Environment).where(Environment.id == key.environment_id)
                )
            ).scalar_one_or_none()
            if environment is None:
                raise AuthenticationError(
                    "API key references a deleted environment",
                    code=ErrorCode.INVALID_CREDENTIALS,
                )

            # last_used_at is throttled to one write per minute: updating it on
            # every ingest request would turn a read path into a write path at
            # full telemetry volume.
            if key.last_used_at is None or (now - key.last_used_at) > timedelta(minutes=1):
                key.last_used_at = now

            return Principal.for_api_key(
                key_id=key.id,
                name=key.name,
                organization_id=key.organization_id,
                project_id=key.project_id,
                environment_id=key.environment_id,
                environment_name=environment.name,
                scopes=key.scopes or [],
                expires_at=key.expires_at,
                authenticated_at=now,
            )

    async def revoke_api_key(self, *, organization_id: str, key_id: str, revoked_by: str) -> None:
        async with self._database.session_scope() as session:
            key = (
                await session.execute(
                    select(ApiKey).where(
                        ApiKey.id == key_id, ApiKey.organization_id == organization_id
                    )
                )
            ).scalar_one_or_none()
            if key is None:
                from ..core.errors import NotFoundError

                raise NotFoundError("api key", key_id)
            if key.revoked_at is None:
                key.revoked_at = self._clock.now()
                key.revoked_by = revoked_by

    # ------------------------------------------------------------------
    # service accounts
    # ------------------------------------------------------------------

    async def create_service_account(
        self,
        *,
        organization_id: str,
        name: str,
        role: Role,
        description: str | None = None,
        project_scope: list[str] | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[str, str]:
        """Create a service account, returning ``(account_id, secret)``."""
        body = secrets.token_urlsafe(_KEY_SECRET_BYTES)
        prefix = f"{_KEY_NAMESPACE}_svc_{secrets.token_hex(4)}"
        secret = f"{prefix}_{body}"
        async with self._database.session_scope() as session:
            account = ServiceAccount(
                id=generate_id(IdPrefix.SERVICE_ACCOUNT),
                organization_id=organization_id,
                name=name,
                description=description,
                role=role.value,
                prefix=prefix,
                secret_hash=self._hash_key(secret),
                project_scope=project_scope or [],
                expires_at=expires_at,
            )
            session.add(account)
            await session.flush()
            return account.id, secret

    async def principal_from_service_account(self, secret: str) -> Principal:
        prefix = _prefix_of(secret)
        if prefix is None:
            raise AuthenticationError(
                "malformed service account credential", code=ErrorCode.INVALID_CREDENTIALS
            )
        now = self._clock.now()
        expected = self._hash_key(secret)
        async with self._database.session_scope() as session:
            account = (
                await session.execute(select(ServiceAccount).where(ServiceAccount.prefix == prefix))
            ).scalar_one_or_none()
            if account is None or not hmac.compare_digest(account.secret_hash, expected):
                raise AuthenticationError(
                    "invalid service account credential", code=ErrorCode.INVALID_CREDENTIALS
                )
            if account.revoked_at is not None:
                raise AuthenticationError(
                    "service account has been revoked", code=ErrorCode.API_KEY_REVOKED
                )
            if account.expires_at is not None and account.expires_at <= now:
                raise AuthenticationError(
                    "service account credential has expired", code=ErrorCode.API_KEY_EXPIRED
                )
            if account.last_used_at is None or (now - account.last_used_at) > timedelta(minutes=1):
                account.last_used_at = now
            return Principal.for_service_account(
                account_id=account.id,
                name=account.name,
                organization_id=account.organization_id,
                role=account.role,
                project_scope=account.project_scope or (),
                expires_at=account.expires_at,
                authenticated_at=now,
            )

    # ------------------------------------------------------------------
    # credential dispatch
    # ------------------------------------------------------------------

    async def resolve_credential(self, raw: str) -> Principal:
        """Resolve any credential form into a principal.

        The credential's own prefix selects the verification path, so a JWT can
        never be checked as an API key or vice versa.
        """
        if raw.startswith(f"{_KEY_NAMESPACE}_svc_"):
            return await self.principal_from_service_account(raw)
        if raw.startswith(f"{_KEY_NAMESPACE}_"):
            return await self.principal_from_api_key(raw)
        return await self.principal_from_token(raw)

    async def bump_token_epoch(self, user_id: str) -> None:
        """Invalidate every outstanding token for a user."""
        async with self._database.session_scope() as session:
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one_or_none()
            if user is not None:
                user.token_epoch += 1

    async def resolve_ingest_target(self, principal: Principal) -> tuple[str, str, str, str]:
        """Return ``(organization_id, project_id, environment_id, environment_name)``.

        For an API key these come from the key itself. For a user or service
        account they must be supplied explicitly by the caller, so this raises
        rather than guessing a default project -- silently writing telemetry into
        the wrong project would be worse than an error.
        """
        if principal.type is PrincipalType.API_KEY:
            return (
                principal.organization_id,
                principal.project_id or "",
                principal.environment_id or "",
                principal.environment_name or "",
            )
        raise ValidationFailedError(
            "ingestion requires an API key; user tokens must specify a project "
            "and environment explicitly"
        )


def _prefix_of(secret: str) -> str | None:
    """Extract the indexed lookup prefix from a full credential."""
    parts = secret.split("_")
    if len(parts) < 4 or parts[0] != _KEY_NAMESPACE:
        return None
    return "_".join(parts[:3])


async def list_projects_for(database: Database, organization_id: str) -> list[Project]:
    """Convenience used by the bootstrap CLI and tests."""
    async with database.session_scope() as session:
        return list(
            (
                await session.execute(
                    select(Project).where(
                        Project.organization_id == organization_id,
                        Project.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )


def utc_expiry(days: int, *, now: datetime | None = None) -> datetime:
    base = now or datetime.now(timezone.utc)
    return base + timedelta(days=days)
