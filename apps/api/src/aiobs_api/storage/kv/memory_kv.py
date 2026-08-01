"""In-process key-value store.

Correct for a single process, which makes it right for tests and for a
single-replica local stack, and wrong the moment there are two replicas -- each
would keep its own rate-limit counters, so the effective limit would be
``replicas x configured``. That is why production configuration validation
rejects it.

The token-bucket arithmetic here is the reference implementation; the Redis
driver reproduces it in a Lua script, and both are covered by the same tests.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from ...core.timeutil import Clock, SystemClock
from .protocol import KeyValueStore, RateLimitResult

__all__ = ["InMemoryKeyValueStore"]


@dataclass(slots=True)
class _Entry:
    value: bytes
    expires_at: float | None = None

    def is_live(self, now: float) -> bool:
        return self.expires_at is None or self.expires_at > now


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float
    lock: Any = field(default=None)


class InMemoryKeyValueStore(KeyValueStore):
    """Dictionary-backed key-value store with TTL and token-bucket support."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._entries: dict[str, _Entry] = {}
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()
        self._clock = clock or SystemClock()

    def _now(self) -> float:
        return self._clock.now_unix_seconds()

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        self._entries.clear()
        self._buckets.clear()

    async def check_health(self) -> None:
        return None

    def _purge_expired(self, now: float) -> None:
        # Lazily evicting on access keeps this O(1) amortised rather than
        # running a sweep; the map only grows with live keys plus whatever
        # expired since the last touch of that key.
        expired = [key for key, entry in self._entries.items() if not entry.is_live(now)]
        for key in expired:
            self._entries.pop(key, None)

    async def get(self, key: str) -> bytes | None:
        now = self._now()
        entry = self._entries.get(key)
        if entry is None:
            return None
        if not entry.is_live(now):
            self._entries.pop(key, None)
            return None
        return entry.value

    async def set(self, key: str, value: bytes, *, ttl_seconds: int | None = None) -> None:
        expires_at = None if ttl_seconds is None else self._now() + ttl_seconds
        self._entries[key] = _Entry(value=value, expires_at=expires_at)

    async def set_if_absent(self, key: str, value: bytes, *, ttl_seconds: int) -> bool:
        async with self._lock:
            now = self._now()
            entry = self._entries.get(key)
            if entry is not None and entry.is_live(now):
                return False
            self._entries[key] = _Entry(value=value, expires_at=now + ttl_seconds)
            return True

    async def delete(self, key: str) -> bool:
        return self._entries.pop(key, None) is not None

    async def increment(self, key: str, amount: int = 1, *, ttl_seconds: int | None = None) -> int:
        async with self._lock:
            now = self._now()
            entry = self._entries.get(key)
            current = int(entry.value) if entry is not None and entry.is_live(now) else 0
            updated = current + amount
            expires_at = (
                entry.expires_at
                if entry is not None and entry.is_live(now)
                else (None if ttl_seconds is None else now + ttl_seconds)
            )
            self._entries[key] = _Entry(value=str(updated).encode(), expires_at=expires_at)
            return updated

    async def check_rate_limit(
        self, key: str, *, limit_per_minute: int, burst: int, cost: int = 1
    ) -> RateLimitResult:
        refill_rate = limit_per_minute / 60.0
        capacity = float(max(burst, 1))
        async with self._lock:
            now = self._now()
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=capacity, updated_at=now)
                self._buckets[key] = bucket
            # Refill for elapsed time, capped at capacity.
            elapsed = max(now - bucket.updated_at, 0.0)
            bucket.tokens = min(capacity, bucket.tokens + elapsed * refill_rate)
            bucket.updated_at = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                deficit = 0.0
                allowed = True
            else:
                deficit = cost - bucket.tokens
                allowed = False

            retry_after = deficit / refill_rate if refill_rate > 0 else 60.0
            reset_at = now + (capacity - bucket.tokens) / refill_rate if refill_rate > 0 else now
            return RateLimitResult(
                allowed=allowed,
                limit=limit_per_minute,
                remaining=int(bucket.tokens),
                retry_after=retry_after,
                reset_at=reset_at,
            )

    async def acquire_lock(self, key: str, *, owner: str, ttl_seconds: int) -> bool:
        return await self.set_if_absent(
            f"lock:{key}", owner.encode("utf-8"), ttl_seconds=ttl_seconds
        )

    async def release_lock(self, key: str, *, owner: str) -> bool:
        async with self._lock:
            entry = self._entries.get(f"lock:{key}")
            if entry is None or not entry.is_live(self._now()):
                return False
            if entry.value != owner.encode("utf-8"):
                return False
            self._entries.pop(f"lock:{key}", None)
            return True

    def _reset_for_tests(self) -> None:
        """Clear all state. Used by test fixtures between cases."""
        self._entries.clear()
        self._buckets.clear()


def monotonic_owner_token(prefix: str = "worker") -> str:
    """Return a unique lock-owner token for this process and moment."""
    import os
    import secrets

    return f"{prefix}:{os.getpid()}:{int(time.time())}:{secrets.token_hex(4)}"
