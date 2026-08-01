"""S3-compatible object store (AWS S3, MinIO, R2, GCS via the S3 API).

Uses ``aioboto3`` so that a slow upload does not block the event loop. The
session and client are created once and reused: constructing a botocore client
per request costs tens of milliseconds of credential resolution and TLS setup.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ...core.errors import DependencyUnavailableError, ValidationFailedError
from ...core.logging import get_logger
from .protocol import ObjectMetadata, ObjectNotFoundError, ObjectStore, compute_checksum

__all__ = ["S3ObjectStore"]

log = get_logger(__name__)


class S3ObjectStore(ObjectStore):
    """Object storage backed by any S3-compatible endpoint."""

    def __init__(
        self,
        *,
        bucket: str,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        max_object_bytes: int = 64 * 1024 * 1024,
        create_bucket: bool = False,
    ) -> None:
        self._bucket = bucket
        self._region = region
        self._endpoint_url = endpoint_url
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._max_object_bytes = max_object_bytes
        self._create_bucket = create_bucket
        self._session: Any = None
        self._exit_stack: Any = None
        self._client: Any = None

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"region_name": self._region}
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url
        if self._access_key_id and self._secret_access_key:
            kwargs["aws_access_key_id"] = self._access_key_id
            kwargs["aws_secret_access_key"] = self._secret_access_key
        # Otherwise fall through to the default credential chain (instance
        # role, web identity, environment) -- the only sane production setup.
        return kwargs

    async def start(self) -> None:
        if self._client is not None:
            return
        try:
            from contextlib import AsyncExitStack

            import aioboto3
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise DependencyUnavailableError(
                "object-store", cause="aioboto3 is not installed"
            ) from exc

        self._session = aioboto3.Session()
        self._exit_stack = AsyncExitStack()
        try:
            self._client = await self._exit_stack.enter_async_context(
                self._session.client("s3", **self._client_kwargs())
            )
        except Exception as exc:
            raise DependencyUnavailableError("object-store", cause=str(exc)) from exc

        if self._create_bucket:
            # Development convenience for MinIO. Never enabled in production:
            # bucket creation there is an infrastructure concern with lifecycle
            # rules, encryption and access policies attached.
            try:
                await self._client.head_bucket(Bucket=self._bucket)
            except Exception:
                try:
                    await self._client.create_bucket(Bucket=self._bucket)
                    log.info("object_store.bucket_created", bucket=self._bucket)
                except Exception as exc:
                    log.warning("object_store.bucket_create_failed", error=str(exc))

    async def close(self) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._client = None

    def _require_client(self) -> Any:
        if self._client is None:
            raise DependencyUnavailableError(
                "object-store", cause="store used before start() was awaited"
            )
        return self._client

    async def check_health(self) -> None:
        client = self._require_client()
        try:
            await client.head_bucket(Bucket=self._bucket)
        except Exception as exc:
            raise DependencyUnavailableError("object-store", cause=str(exc)) from exc

    async def put(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> ObjectMetadata:
        if len(payload) > self._max_object_bytes:
            raise ValidationFailedError(
                f"object of {len(payload)} bytes exceeds the {self._max_object_bytes} byte limit"
            )
        client = self._require_client()
        checksum = compute_checksum(payload)
        try:
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=payload,
                ContentType=content_type,
                # Server-side integrity check: S3 rejects the write if the body
                # does not match, so a truncated upload fails loudly.
                ChecksumAlgorithm="SHA256",
                Metadata={"aiobs-checksum": checksum, **(metadata or {})},
            )
        except Exception as exc:
            raise DependencyUnavailableError("object-store", cause=str(exc)) from exc
        return ObjectMetadata(
            key=key,
            size_bytes=len(payload),
            checksum=checksum,
            content_type=content_type,
            created_at=datetime.now(timezone.utc),
        )

    async def get(self, key: str, *, expected_checksum: str | None = None) -> bytes:
        client = self._require_client()
        try:
            response = await client.get_object(Bucket=self._bucket, Key=key)
            async with response["Body"] as stream:
                payload: bytes = await stream.read()
        except Exception as exc:
            if type(exc).__name__ in {"NoSuchKey", "ClientError"} and "404" in str(exc):
                raise ObjectNotFoundError(key) from exc
            if type(exc).__name__ == "NoSuchKey":
                raise ObjectNotFoundError(key) from exc
            raise DependencyUnavailableError("object-store", cause=str(exc)) from exc
        if expected_checksum is not None:
            actual = compute_checksum(payload)
            if actual != expected_checksum:
                raise ValueError(
                    f"object {key!r} failed integrity verification: "
                    f"expected {expected_checksum}, found {actual}"
                )
        return payload

    async def delete(self, key: str) -> bool:
        client = self._require_client()
        try:
            await client.delete_object(Bucket=self._bucket, Key=key)
            return True
        except Exception as exc:
            raise DependencyUnavailableError("object-store", cause=str(exc)) from exc

    async def exists(self, key: str) -> bool:
        client = self._require_client()
        try:
            await client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False

    async def signed_url(self, key: str, *, expires_in: int = 300) -> str | None:
        client = self._require_client()
        try:
            url: str = await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=int(expires_in),
            )
            return url
        except Exception as exc:
            log.warning("object_store.presign_failed", key=key, error=str(exc))
            return None
