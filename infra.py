"""Shared Redis rate limiting and short-lived worker leases.

No Redis dependency is required for the default one-process SQLite demo. When
REDIS_URL is configured, failures fail closed (never silently disable limits).
"""
from __future__ import annotations

import secrets


class LimiterUnavailable(Exception):
    pass


def redis_client_from_url(url: str):
    import redis
    return redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)


class RedisWindowLimiter:
    """Sliding 1-window limit. One Lua script = atomic across application workers."""

    # Redis server time avoids clock skew between application replicas.
    LUA = """
    local key = KEYS[1]
    local now = redis.call('TIME')
    local ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
    local window = tonumber(ARGV[1])
    local max_hits = tonumber(ARGV[2])
    redis.call('ZREMRANGEBYSCORE', key, '-inf', ms - window)
    if redis.call('ZCARD', key) >= max_hits then
        return 0
    end
    redis.call('ZADD', key, ms, ARGV[3])
    redis.call('PEXPIRE', key, window)
    return 1
    """

    def __init__(self, client):
        self.client = client

    @staticmethod
    def name(scope: str, key: str) -> str:
        import hashlib
        digest = hashlib.sha256(key.encode()).hexdigest()
        return f"zeqoro:limit:{scope}:{digest}"

    def check(self, scope: str, key: str, limit: int, window: int) -> bool:
        try:
            return bool(self.client.eval(self.LUA, 1, self.name(scope, key), window * 1000, limit, secrets.token_hex(12)))
        except Exception as exc:
            raise LimiterUnavailable("Redis rate limiting unavailable") from exc

    def clear(self, scope: str, key: str) -> None:
        try:
            self.client.delete(self.name(scope, key))
        except Exception as exc:
            raise LimiterUnavailable("Redis rate limiting unavailable") from exc


class RedisLease:
    """Distributed worker lease; release only if the original owner still holds it."""
    RELEASE = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('DEL', KEYS[1])
    end
    return 0
    """

    def __init__(self, client):
        self.client = client

    def acquire(self, booking_id: int, ttl: int = 90) -> str | None:
        token = secrets.token_urlsafe(24)
        result = self.client.set(f"zeqoro:sync:{booking_id}", token, ex=ttl, nx=True)
        return token if result else None

    def release(self, booking_id: int, token: str) -> None:
        self.client.eval(self.RELEASE, 1, f"zeqoro:sync:{booking_id}", token)
