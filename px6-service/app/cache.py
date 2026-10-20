import asyncio
import time
from typing import Any, Optional


class TTLCache:
    """Single-value TTL cache with async de-duplication of concurrent fills."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._value: Optional[Any] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    def get(self) -> Optional[Any]:
        if self._value is None:
            return None
        if time.monotonic() >= self._expires_at:
            return None
        return self._value

    async def get_or_set(self, factory) -> Any:
        cached = self.get()
        if cached is not None:
            return cached
        async with self._lock:
            cached = self.get()
            if cached is not None:
                return cached
            value = await factory()
            self._value = value
            self._expires_at = time.monotonic() + self._ttl
            return value

    def invalidate(self) -> None:
        self._value = None
        self._expires_at = 0.0
