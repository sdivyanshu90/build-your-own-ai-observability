"""Object storage interface for large payloads.

Prompts, tool results and dataset chunks routinely exceed what belongs in a
span attribute or a database row. They go to object storage; the databases keep
only a reference, a checksum, a size and a content type.

Two properties matter more than they might appear:

**Content addressing.** Keys are derived from the SHA-256 of the payload, so
storing the same 200 KB system prompt across a million traces costs one object.
It also makes writes idempotent: a retried upload targets the same key with the
same bytes.

**Verified reads.** Every read re-checks the digest. Object storage is durable
but not infallible, and a silently corrupted prompt would be indistinguishable
from a model behaving strangely -- the worst possible failure mode for a
debugging tool.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Final

__all__ = [
    "ObjectMetadata",
    "ObjectNotFoundError",
    "ObjectStore",
    "PayloadKind",
    "build_object_key",
]


class PayloadKind:
    """Logical categories of stored object, used as a key prefix and for retention."""

    SPAN_INPUT: Final = "span_input"
    SPAN_OUTPUT: Final = "span_output"
    TOOL_RESULT: Final = "tool_result"
    RETRIEVAL_CHUNK: Final = "retrieval_chunk"
    DATASET_FILE: Final = "dataset_file"
    EXPORT: Final = "export"
    ATTACHMENT: Final = "attachment"

    ALL: Final[frozenset[str]] = frozenset(
        {
            "span_input",
            "span_output",
            "tool_result",
            "retrieval_chunk",
            "dataset_file",
            "export",
            "attachment",
        }
    )


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    """Everything the relational store records about a stored object."""

    key: str
    size_bytes: int
    checksum: str
    content_type: str
    created_at: datetime | None = None


class ObjectNotFoundError(KeyError):
    """The object is absent -- either never written, or purged by retention."""


def build_object_key(
    *,
    organization_id: str,
    kind: str,
    checksum: str,
    extension: str = "bin",
) -> str:
    """Derive a content-addressed object key.

    Layout: ``<org>/<kind>/<aa>/<bb>/<full-digest>.<ext>``.

    The two-level hex fan-out matters on filesystem-backed stores, where a
    single directory with millions of entries becomes pathologically slow, and
    is harmless on S3. Leading with the organisation id keeps a tenant's objects
    under one prefix, which is what makes a per-tenant lifecycle rule, a
    per-tenant IAM policy and a tenant deletion all expressible.
    """
    if kind not in PayloadKind.ALL:
        raise ValueError(f"unknown payload kind {kind!r}")
    algorithm, separator, digest = checksum.partition(":")
    if not separator or algorithm != "sha256" or len(digest) != 64:
        raise ValueError(f"expected a 'sha256:' prefixed checksum, got {checksum!r}")
    if not organization_id:
        raise ValueError("organization_id is required to build an object key")
    safe_extension = "".join(char for char in extension if char.isalnum()) or "bin"
    return f"{organization_id}/{kind}/{digest[:2]}/{digest[2:4]}/{digest}.{safe_extension}"


def compute_checksum(payload: bytes) -> str:
    """Return the prefixed SHA-256 digest of ``payload``."""
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class ObjectStore(ABC):
    """Read/write interface to blob storage."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def check_health(self) -> None:
        """Raise ``DependencyUnavailableError`` when storage is unusable."""

    @abstractmethod
    async def put(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> ObjectMetadata:
        """Store ``payload`` at ``key``. Overwriting with identical bytes is a no-op."""

    @abstractmethod
    async def get(self, key: str, *, expected_checksum: str | None = None) -> bytes:
        """Read an object, verifying its digest when ``expected_checksum`` is given.

        Raises :class:`ObjectNotFoundError` if absent, and ``ValueError`` if the
        stored bytes do not match the expected digest.
        """

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Delete an object. Returns ``False`` if it was already gone."""

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    @abstractmethod
    async def signed_url(self, key: str, *, expires_in: int = 300) -> str | None:
        """Return a time-limited download URL, or ``None`` if unsupported.

        Filesystem storage returns ``None``; callers fall back to streaming the
        object through the API, which is correct but does not scale, and is
        exactly why the filesystem driver is refused in production.
        """

    async def put_json(
        self, key: str, payload: object, *, metadata: dict[str, str] | None = None
    ) -> ObjectMetadata:
        """Convenience wrapper storing a JSON document."""
        import json

        encoded = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        return await self.put(key, encoded, content_type="application/json", metadata=metadata)
