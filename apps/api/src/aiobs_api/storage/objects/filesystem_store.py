"""Filesystem object store for local development and tests.

Real, not a stub: it writes files, verifies checksums and enforces the same
size limits as the S3 driver. What it cannot do is survive a container being
rescheduled onto a different node, which is why
:meth:`Settings.validate_for_runtime` refuses to start a production process
configured to use it.

Path traversal is the obvious risk when a key becomes a path. Every key is
resolved and then checked to still live under the root, so a key containing
``../..`` is rejected rather than writing outside the store.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from ...core.errors import DependencyUnavailableError, ValidationFailedError
from ...core.logging import get_logger
from .protocol import ObjectMetadata, ObjectNotFoundError, ObjectStore, compute_checksum

__all__ = ["FilesystemObjectStore"]

log = get_logger(__name__)


class FilesystemObjectStore(ObjectStore):
    """Stores objects as files under a root directory."""

    def __init__(self, root: Path | str, *, max_object_bytes: int = 64 * 1024 * 1024) -> None:
        self._root = Path(root).resolve()
        self._max_object_bytes = max_object_bytes

    async def start(self) -> None:
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)

    async def close(self) -> None:
        return None

    async def check_health(self) -> None:
        try:
            probe = self._root / ".health"
            await asyncio.to_thread(probe.write_bytes, b"ok")
            await asyncio.to_thread(probe.unlink)
        except OSError as exc:
            raise DependencyUnavailableError("object-store", cause=str(exc)) from exc

    def _resolve(self, key: str) -> Path:
        """Map a key to a path, refusing anything that escapes the root."""
        if not key or key.startswith("/") or "\x00" in key:
            raise ValidationFailedError(f"invalid object key {key!r}")
        candidate = (self._root / key).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise ValidationFailedError(
                f"object key {key!r} resolves outside the object store root"
            ) from exc
        return candidate

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
        path = self._resolve(key)
        checksum = compute_checksum(payload)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temporary file and rename: a reader can never observe a
            # partially-written object, and a crash mid-write leaves no
            # corrupt object behind.
            temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
            temporary.write_bytes(payload)
            temporary.replace(path)

        await asyncio.to_thread(_write)
        return ObjectMetadata(
            key=key,
            size_bytes=len(payload),
            checksum=checksum,
            content_type=content_type,
            created_at=datetime.now(timezone.utc),
        )

    async def get(self, key: str, *, expected_checksum: str | None = None) -> bytes:
        path = self._resolve(key)
        try:
            payload = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc
        except OSError as exc:
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
        path = self._resolve(key)
        try:
            await asyncio.to_thread(path.unlink)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise DependencyUnavailableError("object-store", cause=str(exc)) from exc

    async def exists(self, key: str) -> bool:
        path = self._resolve(key)
        return await asyncio.to_thread(path.exists)

    async def signed_url(self, key: str, *, expires_in: int = 300) -> str | None:
        # No out-of-band URL scheme exists for a local directory; callers stream
        # the bytes through the API instead.
        return None
