"""Request-log facade on top of ``storage-service`` (no SQL in the gateway).

``storage-service`` owns ``request_log`` and every aggregate query (see
``storage-service/openroute_storage/services/request_log.py``); this module is
the gateway's in-process façade:

- :meth:`RequestLog.log` appends one entry to a small bounded deque (the hot
  fallback for the admin "recent requests" table) and awaits
  ``StorageClient.log_request``. It **never raises**: a storage failure is
  logged by exception type only (messages/URLs may carry secrets) and serving
  continues.
- :meth:`RequestLog.log_soon` is the request-path entry point: it schedules the
  same write as a tracked fire-and-forget task on the running loop, so proxying
  never waits for the log service. Without a running loop the entry stays in
  the deque and the write is dropped safely.
- Reads (:meth:`recent_async`, :meth:`summary_async`,
  :meth:`summary_by_account_async`, :meth:`daily_by_account_async`,
  :meth:`proxy_stats_async`) return the storage-service payloads unchanged, so
  the admin API contract is exactly the old SQL shape. On a storage error they
  degrade to the local deque / empty aggregates instead of turning a panel
  request into a 500.
- :meth:`close` marks the façade closed, drains in-flight writes with a short
  bound and is idempotent. The shared storage client belongs to ``main`` and is
  never closed here.

``LogEntry`` is unchanged — it is the public shape used by
``main._log_request`` and the tests. ``LogEntry.account`` carries the MASKED
PROXY (host:port) here, because OpenRoute has no upstream accounts (the column
name is kept from NVRoute's schema).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .storage import StorageClient

logger = logging.getLogger(__name__)

_DEQUE_MAX = 200  # bounded hot buffer: fallback when the storage read fails
_CLOSE_DRAIN_TIMEOUT = 5.0  # seconds: bounded wait for in-flight log writes


@dataclass
class LogEntry:
    """One served request: who/what/how fast/through which proxy."""

    ts: float  # time.time()
    key_id: str  # client key id or "anonymous"
    method: str
    path: str  # e.g. "chat/completions"
    model: str | None
    status: int  # upstream or gateway status
    latency_ms: float
    stream: bool
    account: str | None  # MASKED proxy, e.g. "1.2.3.4:8080"
    prompt_tokens: int | None
    completion_tokens: int | None
    error: str | None


class RequestLog:
    """Storage-backed request log; every SQL aggregate lives in storage-service.

    Lifecycle: after ``close()`` ``log()``/``log_soon()`` silently drop entries
    (``dropped`` counter) and the façade never "resurrects"; ``close()`` is
    idempotent and waits (bounded) for writes scheduled before it. Reads stay
    available and fall back to the deque if the storage client is gone.

    The storage client is shared with the rest of the gateway (affinity,
    personas, proxies): this façade never closes it.
    """

    def __init__(self, storage: "StorageClient | None" = None) -> None:
        self._storage = storage
        self._buf: deque[dict[str, Any]] = deque(maxlen=_DEQUE_MAX)
        self._closed = False
        self._closing = False
        self._dropped = 0  # entries silently dropped after close()
        # Strong refs to fire-and-forget writes (create_task returns a
        # weakref-compatible Future — without a set the task can be GC'd).
        self._log_tasks: set[asyncio.Task] = set()
        # Per-proxy lifetime statistics cache. The admin panel polls ~1 Hz,
        # so a short TTL avoids a full aggregate per poll.
        self._stats_cache: dict[str, dict] | None = None
        self._stats_cache_ts = 0.0

    # --- write -------------------------------------------------------------

    async def log(self, entry: LogEntry) -> None:
        """Persist one entry. Never raises; post-close drops silently."""
        if self._closing or self._closed:
            self._dropped += 1
            return
        await self._write(entry)

    async def _write(self, entry: LogEntry) -> None:
        """Deque + storage write; swallows every storage failure."""
        self._buf.append(asdict(entry))
        storage = self._storage
        if storage is None:
            return
        try:
            await storage.log_request(entry)
        except asyncio.CancelledError:
            raise  # shutdown cancellation: the entry stays in the deque
        except Exception as exc:
            # Type only: an exception message may embed URLs/tokens.
            logger.warning("Failed to persist request log entry (%s)",
                           type(exc).__name__)

    def log_soon(self, entry: LogEntry) -> None:
        """Schedule the write behind :meth:`log` on the running loop (sync).

        Tracked so :meth:`close` can drain it. Called without a running loop
        (e.g. a worker thread), the entry is kept in the deque and the write is
        dropped safely instead of blocking the caller.
        """
        if self._closing or self._closed:
            self._dropped += 1
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._buf.append(asdict(entry))
            return
        task = loop.create_task(self._write(entry))
        self._log_tasks.add(task)  # strong ref: the task is not GC'd
        task.add_done_callback(self._log_tasks.discard)

    # --- reads -------------------------------------------------------------

    async def recent_async(self, limit: int = 100) -> list[dict]:
        """Most recent entries, newest first (storage-service; deque fallback)."""
        storage = self._storage
        if storage is not None:
            try:
                return await storage.recent_requests(limit)
            except Exception as exc:
                logger.warning(
                    "recent() falling back to the local buffer (%s)",
                    type(exc).__name__)
        return self._recent_local(limit)

    def _recent_local(self, limit: int) -> list[dict]:
        if limit <= 0:
            return []
        return list(self._buf)[-limit:][::-1]

    async def summary_async(self, since: float | None = None) -> dict:
        """Per-key aggregates + ``total``; empty dict when stats are unavailable."""
        storage = self._storage
        if storage is None:
            return {}
        try:
            return await storage.stats_summary(since)
        except Exception as exc:
            logger.warning("summary() unavailable (%s)", type(exc).__name__)
            return {}

    async def summary_by_account_async(self, since: float | None = None) -> dict:
        """Per-masked-proxy aggregates; empty dict when stats are unavailable."""
        storage = self._storage
        if storage is None:
            return {}
        try:
            return await storage.stats_by_account(since)
        except Exception as exc:
            logger.warning("summary_by_account() unavailable (%s)",
                           type(exc).__name__)
            return {}

    async def daily_by_account_async(self, since: float) -> list[dict]:
        """Per-UTC-day/per-account series; empty list when unavailable."""
        storage = self._storage
        if storage is None:
            return []
        try:
            return await storage.stats_daily(since)
        except Exception as exc:
            logger.warning("daily_by_account() unavailable (%s)",
                           type(exc).__name__)
            return []

    async def proxy_stats_async(self, ttl_seconds: float = 0.0) -> dict[str, dict]:
        """Per-account lifetime counters: ``{account: {requests, errors, tokens}}``.

        ``ttl_seconds > 0`` serves a cached snapshot younger than the TTL;
        callers always get copies. A storage error yields ``{}``. Never raises.
        """
        now = time.monotonic()
        if (ttl_seconds > 0 and self._stats_cache is not None
                and now - self._stats_cache_ts < ttl_seconds):
            return {acc: dict(row) for acc, row in self._stats_cache.items()}
        out = await self.summary_by_account_async(None)
        if ttl_seconds > 0:
            self._stats_cache = out
            self._stats_cache_ts = now
        return {acc: dict(row) for acc, row in out.items()}

    # --- shutdown ----------------------------------------------------------

    @property
    def dropped(self) -> int:
        """Entries silently dropped after ``close()`` (metrics/tests)."""
        return self._dropped

    async def close(self) -> None:
        """Stop accepting writes and drain in-flight ones (bounded).

        Idempotent and safe to call from the lifespan loop. The shared storage
        client is NOT closed here — ``main`` owns it and closes it last.
        """
        if self._closed:
            return
        self._closing = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - close is awaited from a loop
            loop = None
        pending = [
            task for task in self._log_tasks
            if not task.done() and (loop is None or task.get_loop() is loop)
        ]
        try:
            if pending:
                await asyncio.wait(pending, timeout=_CLOSE_DRAIN_TIMEOUT)
        except Exception:
            logger.debug("Request log drain failed", exc_info=True)
        finally:
            self._closed = True
