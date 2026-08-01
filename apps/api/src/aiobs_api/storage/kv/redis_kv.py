"""Redis-backed key-value store.

The interesting parts are the two Lua scripts. Both exist because the operations
they implement are *read-modify-write* sequences that must be atomic across
replicas; doing them with separate GET and SET round-trips would let two API
instances both conclude a request is within budget.

Redis runs a script atomically on the server, so the whole token-bucket update
is one indivisible step.
"""

from __future__ import annotations

from typing import Any

from ...core.errors import DependencyUnavailableError
from ...core.logging import get_logger
from .protocol import KeyValueStore, RateLimitResult

__all__ = ["RedisKeyValueStore"]

log = get_logger(__name__)

#: Token bucket. KEYS[1] = bucket key. ARGV = capacity, refill/sec, cost, now.
#: Returns {allowed, remaining_tokens, retry_after_seconds}.
_RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

local state = redis.call('HMGET', key, 'tokens', 'updated_at')
local tokens = tonumber(state[1])
local updated_at = tonumber(state[2])

if tokens == nil then
  tokens = capacity
  updated_at = now
end

local elapsed = math.max(now - updated_at, 0)
tokens = math.min(capacity, tokens + elapsed * refill_rate)

local allowed = 0
local retry_after = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  if refill_rate > 0 then
    retry_after = (cost - tokens) / refill_rate
  else
    retry_after = 60
  end
end

redis.call('HSET', key, 'tokens', tokens, 'updated_at', now)
redis.call('EXPIRE', key, ttl)
return {allowed, tostring(tokens), tostring(retry_after)}
"""

#: Release a lock only if we still own it. Prevents releasing someone else's
#: lease after our own expired.
_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisKeyValueStore(KeyValueStore):
    """Distributed ephemeral state backed by Redis."""

    def __init__(
        self,
        url: str,
        *,
        connect_timeout: float = 2.0,
        namespace: str = "aiobs",
    ) -> None:
        self._url = url
        self._connect_timeout = connect_timeout
        self._namespace = namespace
        self._client: Any = None
        self._rate_limit_script: Any = None
        self._release_script: Any = None

    def _key(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    async def start(self) -> None:
        if self._client is not None:
            return
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise DependencyUnavailableError("redis", cause="redis is not installed") from exc
        self._client = Redis.from_url(
            self._url,
            socket_connect_timeout=self._connect_timeout,
            socket_timeout=self._connect_timeout * 2,
            health_check_interval=30,
            decode_responses=False,
        )
        self._rate_limit_script = self._client.register_script(_RATE_LIMIT_SCRIPT)
        self._release_script = self._client.register_script(_RELEASE_LOCK_SCRIPT)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require(self) -> Any:
        if self._client is None:
            raise DependencyUnavailableError("redis", cause="store used before start() was awaited")
        return self._client

    async def check_health(self) -> None:
        try:
            await self._require().ping()
        except Exception as exc:
            raise DependencyUnavailableError("redis", cause=str(exc)) from exc

    async def get(self, key: str) -> bytes | None:
        try:
            value: bytes | None = await self._require().get(self._key(key))
            return value
        except Exception as exc:
            raise DependencyUnavailableError("redis", cause=str(exc)) from exc

    async def set(self, key: str, value: bytes, *, ttl_seconds: int | None = None) -> None:
        try:
            await self._require().set(self._key(key), value, ex=ttl_seconds)
        except Exception as exc:
            raise DependencyUnavailableError("redis", cause=str(exc)) from exc

    async def set_if_absent(self, key: str, value: bytes, *, ttl_seconds: int) -> bool:
        try:
            created = await self._require().set(self._key(key), value, ex=ttl_seconds, nx=True)
            return bool(created)
        except Exception as exc:
            raise DependencyUnavailableError("redis", cause=str(exc)) from exc

    async def delete(self, key: str) -> bool:
        try:
            removed: int = await self._require().delete(self._key(key))
            return removed > 0
        except Exception as exc:
            raise DependencyUnavailableError("redis", cause=str(exc)) from exc

    async def increment(self, key: str, amount: int = 1, *, ttl_seconds: int | None = None) -> int:
        client = self._require()
        try:
            pipeline = client.pipeline()
            pipeline.incrby(self._key(key), amount)
            if ttl_seconds is not None:
                # NX so a long-lived counter's TTL is not extended on every hit.
                pipeline.expire(self._key(key), ttl_seconds, nx=True)
            results = await pipeline.execute()
            return int(results[0])
        except Exception as exc:
            raise DependencyUnavailableError("redis", cause=str(exc)) from exc

    async def check_rate_limit(
        self, key: str, *, limit_per_minute: int, burst: int, cost: int = 1
    ) -> RateLimitResult:
        import time

        now = time.time()
        refill_rate = limit_per_minute / 60.0
        capacity = float(max(burst, 1))
        # Expire idle buckets after two full refills; keeping them longer only
        # wastes memory since a full bucket is indistinguishable from a new one.
        ttl = max(int(capacity / refill_rate * 2), 60) if refill_rate > 0 else 300
        try:
            raw = await self._rate_limit_script(
                keys=[self._key(f"ratelimit:{key}")],
                args=[capacity, refill_rate, cost, now, ttl],
            )
        except Exception as exc:
            raise DependencyUnavailableError("redis", cause=str(exc)) from exc

        allowed = bool(int(raw[0]))
        tokens = float(raw[1])
        retry_after = float(raw[2])
        reset_at = now + ((capacity - tokens) / refill_rate if refill_rate > 0 else 60)
        return RateLimitResult(
            allowed=allowed,
            limit=limit_per_minute,
            remaining=int(tokens),
            retry_after=retry_after,
            reset_at=reset_at,
        )

    async def acquire_lock(self, key: str, *, owner: str, ttl_seconds: int) -> bool:
        return await self.set_if_absent(
            f"lock:{key}", owner.encode("utf-8"), ttl_seconds=ttl_seconds
        )

    async def release_lock(self, key: str, *, owner: str) -> bool:
        try:
            released = await self._release_script(
                keys=[self._key(f"lock:{key}")], args=[owner.encode("utf-8")]
            )
            return bool(int(released))
        except Exception as exc:
            raise DependencyUnavailableError("redis", cause=str(exc)) from exc
