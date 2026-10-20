"""Admin session store: Redis (survives web-admin restarts).

Key ``openroute:session:<sid>`` -> JSON ``{"csrf": str, "expires": float}``.
Redis TTL == session TTL, so expired entries evict themselves. Only the sid is
handed to the client (HttpOnly cookie); validity comes solely from the store.

The async interface is minimal (put/get/delete/count/aclose) so hermetic tests
can substitute an in-memory fake. Redis unavailability -> ``SessionUnavailable``
-> 503 in ``main.py`` (login/validation must not "fail open").
"""

import json

import redis.asyncio as aioredis


class SessionUnavailable(Exception):
    """Redis is down/failed: a session cannot be created or validated."""


class RedisSessionStore:
    """Sessions in Redis: sid -> ``{"csrf": str, "expires": float}``.

    The ``expires`` field duplicates the key TTL (``ex=ttl``) as a second line
    of defence, checked in ``main._session_from_request``.
    """

    PREFIX = "openroute:session:"

    def __init__(self, url: str) -> None:
        # Lazy client: the connection is opened on first operation, not on import.
        # decode_responses=True so get() returns str, not bytes.
        self._redis = aioredis.from_url(url, decode_responses=True)

    def _key(self, sid: str) -> str:
        return f"{self.PREFIX}{sid}"

    async def put(self, sid: str, session: dict, ttl_seconds: int) -> None:
        try:
            await self._redis.set(self._key(sid), json.dumps(session), ex=ttl_seconds)
        except aioredis.RedisError as exc:
            raise SessionUnavailable(str(exc)) from exc

    async def get(self, sid: str) -> dict | None:
        try:
            raw = await self._redis.get(self._key(sid))
        except aioredis.RedisError as exc:
            raise SessionUnavailable(str(exc)) from exc
        return json.loads(raw) if raw is not None else None

    async def delete(self, sid: str) -> None:
        try:
            await self._redis.delete(self._key(sid))
        except aioredis.RedisError as exc:
            raise SessionUnavailable(str(exc)) from exc

    async def count(self) -> int:
        """Live session count (for /health); SCAN, not KEYS (non-blocking)."""
        try:
            n = 0
            async for _ in self._redis.scan_iter(match=f"{self.PREFIX}*"):
                n += 1
            return n
        except aioredis.RedisError as exc:
            raise SessionUnavailable(str(exc)) from exc

    async def aclose(self) -> None:
        """Close the connection pool (on shutdown in lifespan)."""
        await self._redis.aclose()
