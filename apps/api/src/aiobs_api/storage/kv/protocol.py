"""Key-value interface for rate limiting, de-duplication and locks.

Everything here is *ephemeral by design*. The key-value store is never the
source of truth: if it is wiped, the platform loses rate-limit counters and
de-duplication hints, and continues to serve correct data. That constraint is
what allows the ``fail_open`` policy -- when Redis is unreachable, ingestion
keeps working with the analytics store's own de-duplication as the backstop
rather than dropping customer telemetry.

The rate limiter is a **token bucket**, not a fixed window. A fixed window lets
a client send its whole minute's budget in the last millisecond of one window
and again in the first millisecond of the next -- twice the intended rate at the
worst possible moment. A token bucket smooths that while still allowing a
configured burst.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

__all__ = ["KeyValueStore", "RateLimitResult"]


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of a rate-limit check, shaped for the response headers."""

    allowed: bool
    limit: int
    remaining: int
    #: Seconds until the bucket has room for one more request.
    retry_after: float
    #: Unix seconds when the bucket is expected to be full again.
    reset_at: float

    def as_headers(self) -> dict[str, str]:
        """Render the IETF draft rate-limit headers."""
        headers = {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(max(self.remaining, 0)),
            "RateLimit-Reset": str(int(max(self.reset_at, 0))),
        }
        if not self.allowed:
            headers["Retry-After"] = str(max(int(self.retry_after), 1))
        return headers


class KeyValueStore(ABC):
    """Ephemeral shared state."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def check_health(self) -> None: ...

    @abstractmethod
    async def get(self, key: str) -> bytes | None: ...

    @abstractmethod
    async def set(self, key: str, value: bytes, *, ttl_seconds: int | None = None) -> None: ...

    @abstractmethod
    async def set_if_absent(self, key: str, value: bytes, *, ttl_seconds: int) -> bool:
        """Atomic set-if-not-exists. Returns ``True`` when the key was created.

        This is the de-duplication primitive: the first ingestion of a span id
        wins, every replay observes ``False`` and is dropped.
        """

    @abstractmethod
    async def delete(self, key: str) -> bool: ...

    @abstractmethod
    async def increment(self, key: str, amount: int = 1, *, ttl_seconds: int | None = None) -> int:
        """Atomically increment a counter, returning the new value."""

    @abstractmethod
    async def check_rate_limit(
        self,
        key: str,
        *,
        limit_per_minute: int,
        burst: int,
        cost: int = 1,
    ) -> RateLimitResult:
        """Consume ``cost`` tokens from a token bucket."""

    @abstractmethod
    async def acquire_lock(self, key: str, *, owner: str, ttl_seconds: int) -> bool:
        """Acquire a lease. Leases expire so a crashed holder cannot deadlock."""

    @abstractmethod
    async def release_lock(self, key: str, *, owner: str) -> bool:
        """Release a lease, but only if ``owner`` still holds it.

        The ownership check prevents the classic bug where a process whose lease
        already expired releases the lock a *different* process has since
        acquired.
        """

    async def exists(self, key: str) -> bool:
        return await self.get(key) is not None
