"""In-memory proxy personas (one synthetic client identity per quota bucket).

A ``Persona`` is the cluster of identifiers the opencode client normally owns:
session id, project id and trace id. The gateway substitutes it into the
upstream headers of every request that leaves through the bucket, so a bucket
looks like one long-lived client to the upstream regardless of how many
different callers / conversations share it.

Balance and affinity are deliberately untouched: affinity still keys requests
by the CLIENT session/fingerprint, and the persona is applied only after a
proxy has been selected. The store is memory-only in the request hot path;
storage-service writes happen on background tasks, retry after failures, and
reconcile with a newer row already present in the store (e.g. when startup
load failed).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import opencode
from .storage import StorageClient

logger = logging.getLogger("openroute.personas")

#: Minimum delay between persistence attempts for one bucket after a failure.
_PERSIST_RETRY_SECONDS = 30.0

#: How often the background flusher scans dirty personas (also the maximum
#: extra delay after a backoff expires).
_FLUSH_INTERVAL_SECONDS = 5.0

#: Hard upper bound for a graceful shutdown, including the flush.
_CLOSE_TIMEOUT_SECONDS = 5.0

#: Safety valve for the reconcile loop: a single flush task never retries
#: forever even if the store keeps returning a different newer row.
_MAX_FLUSH_PASSES = 8


def _remaining(deadline: float) -> float:
    """Seconds left until ``deadline`` (never negative)."""
    return max(0.0, deadline - asyncio.get_running_loop().time())


@dataclass(frozen=True)
class Persona:
    """One synthetic client identity bound to a proxy quota bucket."""

    quota_key: str
    session_id: str
    project_id: str
    trace_id: str
    created_at: float
    generation: int

    @classmethod
    def create(cls, quota_key: str, *, generation: int = 0) -> Persona:
        """Generate a fresh persona with valid opencode-shaped identifiers."""
        return cls(
            quota_key=quota_key,
            session_id=opencode.new_session_id(),
            project_id=opencode.new_project_id(),
            trace_id=opencode.new_trace_id(),
            created_at=time.time(),
            generation=generation,
        )


def _persona_from_row(row: Mapping[str, Any]) -> Persona:
    """Build a ``Persona`` from a storage-service row dict."""
    return Persona(
        quota_key=str(row["quota_key"]),
        session_id=str(row["session_id"]),
        project_id=str(row["project_id"]),
        trace_id=str(row["trace_id"]),
        created_at=float(row["created_at"]),
        generation=int(row["generation"]),
    )


def _is_valid_persona(persona: Persona) -> bool:
    """True when all three identifiers have the expected wire shape."""
    return (
        opencode.is_session_id(persona.session_id)
        and opencode.is_project_id(persona.project_id)
        and opencode.is_trace_id(persona.trace_id)
    )


class PersonaStore:
    """Cache of ``quota_key -> Persona`` with lazy, non-blocking persistence.

    Reads are lock-free on the hot path; creation/rotation are serialised by one
    ``asyncio.Lock``. A missing/unreachable backend degrades to memory-only:
    the request path never awaits persistence work. Dirty personas stay
    queued until a background flush succeeds, so a transient outage does not
    lose the identity.

    Persistence is ``storage-service`` (``storage``). The shared storage client
    is owned by ``main`` and is never closed here.
    """

    def __init__(self, *, storage: StorageClient | None = None) -> None:
        self._storage = storage
        self._cache: dict[str, Persona] = {}
        self._lock = asyncio.Lock()
        self._dirty: dict[str, Persona] = {}
        self._inflight: set[str] = set()
        self._retry_at: dict[str, float] = {}
        self._flusher: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    def bind_storage(self, storage: StorageClient) -> None:
        """Use ``storage`` for persistence."""
        self._storage = storage

    @property
    def _persistent(self) -> bool:
        return self._storage is not None

    async def load(self) -> None:
        """Restore persisted personas; failures leave the store empty.

        A failed initial load is propagated: the gateway must not start
        without its persistence.

        Rows with malformed identifiers are replaced by a fresh persona at
        ``generation + 1`` and queued for persistence: the upstream must never
        see a corrupt identity.
        """
        if not self._persistent:
            return
        rows = await self._storage.load_personas()
        pending: list[Persona] = []
        async with self._lock:
            for row in rows:
                persona = _persona_from_row(row)
                if not _is_valid_persona(persona):
                    current = self._cache.get(persona.quota_key)
                    replacement = Persona.create(
                        persona.quota_key,
                        generation=max(persona.generation, 0) + 1,
                    )
                    # Only a strictly newer replacement may displace a cached
                    # valid persona; an equal-generation one is kept as-is.
                    if (current is None
                            or replacement.generation > current.generation):
                        self._cache[persona.quota_key] = replacement
                        pending.append(replacement)
                    continue
                current = self._cache.get(persona.quota_key)
                if current is not None and current.generation > persona.generation:
                    continue
                self._cache[persona.quota_key] = persona
        # Persist replacements only after leaving the lock (no awaits inside).
        for persona in pending:
            self._persist(persona)

    async def get_or_create(self, quota_key: str) -> Persona:
        """Return the bucket's persona, creating (and persisting) it on miss."""
        persona = self._cache.get(quota_key)
        if persona is not None:
            self._retry_dirty(quota_key)
            return persona
        try:
            async with self._lock:
                persona = self._cache.get(quota_key)
                if persona is None:
                    persona = Persona.create(quota_key)
                    self._cache[quota_key] = persona
                    self._persist(persona)
            return persona
        except Exception:
            # A persona is needed for the request even if bookkeeping failed.
            logger.exception("Persona creation failed for %s; using the "
                             "cached persona if one was already created",
                             quota_key)
            # If creation stored a persona before _persist raised, return that
            # exact object; otherwise create and cache the fallback persona.
            return self._cache.setdefault(quota_key, Persona.create(quota_key))

    async def rotate(self, quota_key: str) -> Persona:
        """Replace the bucket's persona with the next generation."""
        async with self._lock:
            current = self._cache.get(quota_key)
            generation = current.generation + 1 if current is not None else 1
            persona = Persona.create(quota_key, generation=generation)
            self._cache[quota_key] = persona
            self._persist(persona)
            return persona

    def get_cached(self, quota_key: str) -> Persona | None:
        """Return the cached persona without creating one (tests/debug)."""
        return self._cache.get(quota_key)

    # --- persistence ------------------------------------------------------

    def _persist(self, persona: Persona) -> None:
        """Queue ``persona`` for a background write (sync, never blocks)."""
        if not self._persistent or self._closed:
            return
        key = persona.quota_key
        current = self._dirty.get(key)
        if current is None or persona.generation >= current.generation:
            self._dirty[key] = persona
        self._ensure_flusher()
        # Backoff only postpones the attempt; a newer generation is still
        # queued and will be written by the flusher when the backoff expires.
        if time.monotonic() >= self._retry_at.get(key, 0.0):
            self._schedule_flush(key)

    def _retry_dirty(self, quota_key: str) -> None:
        """Retry a queued write for a cache hit once its backoff has expired."""
        if quota_key not in self._dirty:
            return
        if not self._persistent or self._closed:
            return
        if time.monotonic() < self._retry_at.get(quota_key, 0.0):
            return
        self._ensure_flusher()
        self._schedule_flush(quota_key)

    def _ensure_flusher(self) -> None:
        """Start the periodic flusher once (no weak refs needed)."""
        if not self._persistent or self._closed:
            return
        if self._flusher is not None and not self._flusher.done():
            return
        try:
            task = asyncio.create_task(self._flush_loop())
        except RuntimeError:  # pragma: no cover - called from the event loop
            logger.warning("Cannot start persona flusher: no running event loop")
            return
        self._flusher = task
        task.add_done_callback(self._flusher_done)

    def _flusher_done(self, task: asyncio.Task) -> None:
        if self._flusher is task:
            self._flusher = None

    async def _flush_loop(self) -> None:
        """Periodically retry dirty personas whose backoff has expired."""
        try:
            while not self._closed and self._persistent:
                await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)
                for key in list(self._dirty):
                    if time.monotonic() >= self._retry_at.get(key, 0.0):
                        self._schedule_flush(key)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Persona flush loop stopped unexpectedly",
                           exc_info=True)

    def _schedule_flush(self, quota_key: str) -> None:
        """Start one writer for ``quota_key`` if none is in flight."""
        if not self._persistent or self._closed:
            return
        if quota_key in self._inflight:
            return
        if self._dirty.get(quota_key) is None:
            return
        self._inflight.add(quota_key)
        try:
            task = asyncio.create_task(self._flush_key(quota_key))
        except RuntimeError:  # pragma: no cover - called from the event loop
            self._inflight.discard(quota_key)
            logger.warning("Cannot schedule persona flush: no running event loop")
            return
        self._tasks.add(task)
        task.add_done_callback(
            lambda t, key=quota_key: self._flush_done(key, t))

    def _flush_done(self, quota_key: str, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        self._inflight.discard(quota_key)

    async def _flush_key(self, quota_key: str) -> None:
        """Write the newest dirty persona; reconcile if the store is ahead."""
        storage = self._storage
        passes = 0
        while storage is not None and not self._closed:
            if passes >= _MAX_FLUSH_PASSES:
                logger.warning(
                    "Persona flush for %s exceeded %d passes; leaving it "
                    "dirty for the next flush",
                    quota_key, _MAX_FLUSH_PASSES,
                )
                return
            passes += 1
            persona = self._dirty.get(quota_key)
            if persona is None:
                return
            try:
                applied = await storage.upsert_persona(persona)
            except Exception as exc:
                self._retry_at[quota_key] = (
                    time.monotonic() + _PERSIST_RETRY_SECONDS)
                logger.warning("Persona persist failed for %s: %s",
                               quota_key, exc)
                return
            if applied:
                self._retry_at.pop(quota_key, None)
                if self._dirty.get(quota_key) is persona:
                    self._dirty.pop(quota_key, None)
                    return
                continue  # a newer generation was queued while writing
            # The generation guard rejected the row: the store already holds a
            # newer persona (typically because load() failed at startup).
            try:
                row = await storage.get_persona(quota_key)
            except Exception as exc:
                self._retry_at[quota_key] = (
                    time.monotonic() + _PERSIST_RETRY_SECONDS)
                logger.warning("Persona reconcile failed for %s: %s",
                               quota_key, exc)
                return
            if row is None:
                self._retry_at[quota_key] = (
                    time.monotonic() + _PERSIST_RETRY_SECONDS)
                return
            stored_persona = _persona_from_row(row)
            if not _is_valid_persona(stored_persona):
                replacement = Persona.create(
                    quota_key,
                    generation=max(stored_persona.generation, 0) + 1,
                )
                self._dirty[quota_key] = replacement
                async with self._lock:
                    current = self._cache.get(quota_key)
                    if (current is None
                            or replacement.generation >= current.generation):
                        self._cache[quota_key] = replacement
                continue  # write the valid replacement immediately
            # The stored row wins: adopt it and drop this dirty snapshot.
            async with self._lock:
                current = self._cache.get(quota_key)
                if (current is None
                        or stored_persona.generation >= current.generation):
                    self._cache[quota_key] = stored_persona
                if self._dirty.get(quota_key) is persona:
                    self._dirty.pop(quota_key, None)
            self._retry_at.pop(quota_key, None)
            return

    async def _write_once(
        self,
        persona: Persona,
        storage: StorageClient | None,
    ) -> None:
        """One best-effort shutdown write: no reconcile, no retries."""
        try:
            if storage is not None:
                await storage.upsert_persona(persona)
        except Exception as exc:
            logger.warning("Persona close write failed for %s: %s",
                           persona.quota_key, exc)

    async def close(self) -> None:
        """Flush pending writes within a fixed deadline.

        Idempotent and does not raise ordinary errors; cancellation of the
        caller is propagated. The shared storage client is NOT closed here
        (``main`` owns it and closes it last). The in-memory cache stays usable
        after close: requests still need their persona, they just stop being
        persisted.
        """
        if self._closed:
            return
        self._closed = True
        deadline = (asyncio.get_running_loop().time()
                    + _CLOSE_TIMEOUT_SECONDS)

        flusher = self._flusher
        self._flusher = None
        if flusher is not None and flusher is not asyncio.current_task():
            flusher.cancel()
            try:
                await asyncio.wait_for(flusher, timeout=_remaining(deadline))
            except asyncio.CancelledError:
                current = asyncio.current_task()
                # Our own flusher cancellation is expected; a cancellation of
                # close() itself must still propagate.
                if current is not None and current.cancelling():
                    raise
                if not flusher.cancelled():
                    raise
            except TimeoutError:
                logger.warning("Persona flusher did not stop within close "
                               "timeout")
            except Exception:
                logger.warning("Persona flusher stop failed", exc_info=True)

        storage = self._storage
        self._storage = None

        if self._tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(self._tasks),
                                   return_exceptions=True),
                    timeout=_remaining(deadline),
                )
            except TimeoutError:
                logger.warning("Persona flush tasks did not finish within "
                               "close timeout")
            except Exception:
                logger.warning("Persona flush tasks failed during close",
                               exc_info=True)
        self._inflight.clear()

        pending = dict(self._dirty)
        self._dirty.clear()
        if storage is not None and pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(self._write_once(persona, storage)
                          for persona in pending.values()),
                        return_exceptions=True,
                    ),
                    timeout=_remaining(deadline),
                )
            except TimeoutError:
                logger.warning("Persona close flush timed out")
            except Exception:
                logger.warning("Persona close flush failed", exc_info=True)


__all__ = ["Persona", "PersonaStore"]
