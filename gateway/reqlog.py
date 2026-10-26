"""Persistent request log (PostgreSQL): key, model, tokens, latency, status, proxy.

Near-verbatim port of ``NVRoute/app/reqlog.py`` — same DDL (see ``pg.py``),
same behaviour: counters survive restarts, ``log()`` never breaks serving, and
entries are never pruned. ``LogEntry.account`` carries the MASKED PROXY
(host:port) here, because OpenRoute has no upstream accounts (the column name
is kept from NVRoute's schema).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .pg import ensure_schema, make_pool

logger = logging.getLogger(__name__)

_FIELDS = (
    "ts", "key_id", "method", "path", "model", "status", "latency_ms",
    "stream", "account", "prompt_tokens", "completion_tokens", "error",
)
_DEQUE_MAX = 200  # hot cache for recent() — avoids a DB round-trip


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
    """Request log in PostgreSQL; blocking calls are offloaded via ``to_thread``.

    Lifecycle: after ``close()`` ``log()`` silently drops entries (``dropped``
    counter) and the pool never "resurrects"; ``close()`` is idempotent. The
    ``ConnectionPool`` is thread-safe, but ``_write_lock`` stays for one reason:
    ``close()`` (from lifespan) must wait for in-flight inserts — ``pool.close()``
    alone does not wait for clients. The schema is created lazily on first use
    (DDL in ``pg.py``) and committed BEFORE the ready flag is set, otherwise a
    concurrent insert would not see the not-yet-committed tables.
    """

    def __init__(self, dsn: str = "postgresql://openroute:openroute@localhost:5432/openroute") -> None:
        self._pool = make_pool(dsn, "openroute-reqlog", max_size=8)
        self._buf: deque[dict] = deque(maxlen=_DEQUE_MAX)
        # close() from lifespan must not proceed while a to_thread worker is
        # writing a record: insert and close are serialised by this lock.
        self._write_lock = threading.Lock()
        self._schema_lock = threading.Lock()  # one-shot DDL init
        self._schema_ready = False
        self._closed = False
        self._dropped = 0  # entries silently dropped after close()
        # Per-proxy token statistics cache: key = stable copy of the limits
        # mapping, value = (monotonic timestamp, rows). The admin panel polls
        # ~1 Hz, so a short TTL avoids a DB query per poll.
        self._stats_cache: dict[tuple, tuple[float, dict[str, dict]]] = {}
        self._stats_lock = threading.Lock()

    # --- schema (lazy, on first use; idempotent) ---

    def _ensure_schema(self) -> None:
        """Create tables on a connection of THIS pool: the COMMIT happens
        before releasing ``_schema_lock``, so concurrent inserts see them."""
        if self._schema_ready:
            return
        with self._schema_lock:
            if not self._schema_ready:
                with self._pool.connection() as conn:
                    ensure_schema(conn)
                self._schema_ready = True

    # --- write ---

    def _insert(self, e: LogEntry) -> None:
        with self._write_lock:  # do not let close() pass an in-flight insert
            if self._closed:  # close() happened while this insert waited
                return
            self._ensure_schema()
            with self._pool.connection() as conn:
                conn.execute(
                    "INSERT INTO request_log (ts, key_id, method, path, model, status,"
                    " latency_ms, stream, account, prompt_tokens, completion_tokens, error)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (e.ts, e.key_id, e.method, e.path, e.model, e.status, e.latency_ms,
                     e.stream, e.account, e.prompt_tokens, e.completion_tokens, e.error),
                )

    async def log(self, entry: LogEntry) -> None:
        """Persist one entry. Never raises; post-close drops silently."""
        if self._closed:
            self._dropped += 1
            return
        self._buf.append(asdict(entry))
        try:
            await asyncio.to_thread(self._insert, entry)
        except Exception:
            logger.exception("Failed to persist request log entry: %s %s", entry.method, entry.path)

    # --- reads (the pool is thread-safe: no locks) ---

    def recent(self, limit: int = 100) -> list[dict]:
        """Most recent entries, newest first. From the deque when it suffices."""
        if limit <= len(self._buf):
            return list(self._buf)[-limit:][::-1]
        try:
            self._ensure_schema()
            with self._pool.connection() as conn:
                rows = conn.execute(
                    f"SELECT {', '.join(_FIELDS)} FROM request_log ORDER BY ts DESC LIMIT %s",
                    (limit,),
                ).fetchall()
            # psycopg returns native types (stream -> bool); no conversion needed
            return [dict(zip(_FIELDS, r)) for r in rows]
        except Exception:
            logger.exception("recent() failed")
            return list(self._buf)[::-1]

    async def recent_async(self, limit: int = 100) -> list[dict]:
        """async wrapper: the blocking PostgreSQL query goes to a thread so the
        event loop is not blocked (for async admin endpoints)."""
        return await asyncio.to_thread(self.recent, limit)

    def summary(self, since: float | None = None) -> dict:
        """Aggregates by key_id plus a top-level ``"total"``. Latency is the
        mean in ms. ``since`` (epoch) limits the window to ``ts >= since``;
        ``None`` means the whole log."""
        where, params = "", ()
        if since is not None:
            where = " WHERE ts >= %s"
            params = (float(since),)
        try:
            self._ensure_schema()
            with self._pool.connection() as conn:
                rows = conn.execute(
                    "SELECT key_id, COUNT(*),"
                    " SUM(CASE WHEN status >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END),"
                    " SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)),"
                    " AVG(latency_ms) FROM request_log" + where + " GROUP BY key_id",
                    params,
                ).fetchall()
        except Exception:
            logger.exception("summary() failed")
            return {}
        out: dict[str, dict] = {}
        req_t = err_t = tok_t = 0
        lat_w = 0.0  # weighted latency sum for the overall mean
        for key_id, reqs, errs, toks, avg in rows:
            out[key_id] = {
                "requests": reqs,
                "errors": errs or 0,
                "tokens": toks or 0,
                "avg_latency_ms": round(avg or 0.0, 1),
            }
            req_t += reqs
            err_t += errs or 0
            tok_t += toks or 0
            lat_w += (avg or 0.0) * reqs
        out["total"] = {
            "requests": req_t,
            "errors": err_t,
            "tokens": tok_t,
            "avg_latency_ms": round(lat_w / req_t, 1) if req_t else 0.0,
        }
        return out

    async def summary_async(self, since: float | None = None) -> dict:
        """async wrapper: the blocking query goes to a thread (admin endpoints)."""
        return await asyncio.to_thread(self.summary, since)

    def summary_by_account(self, since: float | None = None) -> dict:
        """Aggregates by account (the ``account`` column = masked proxy):
        {account: {requests, errors, tokens}}. The web admin matches by
        ``mask_proxy``. ``since`` (epoch) restricts the window."""
        clauses = ["account IS NOT NULL"]
        params: list[float] = []
        if since is not None:
            clauses.append("ts >= %s")
            params.append(float(since))
        where = " WHERE " + " AND ".join(clauses)
        try:
            self._ensure_schema()
            with self._pool.connection() as conn:
                rows = conn.execute(
                    "SELECT account, COUNT(*),"
                    " SUM(CASE WHEN status >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END),"
                    " SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0))"
                    " FROM request_log" + where + " GROUP BY account",
                    tuple(params),
                ).fetchall()
        except Exception:
            logger.exception("summary_by_account() failed")
            return {}
        return {
            acc: {"requests": reqs, "errors": errs or 0, "tokens": toks or 0}
            for acc, reqs, errs, toks in rows
        }

    async def summary_by_account_async(self, since: float | None = None) -> dict:
        """async wrapper: the blocking query goes to a thread."""
        return await asyncio.to_thread(self.summary_by_account, since)

    def proxy_stats(self, limits: dict[str, float],
                    ttl_seconds: float = 0.0) -> dict[str, dict]:
        """Per-account lifetime and since-limit token usage.

        ``limits`` maps account (masked proxy ``host:port``) -> quota-event
        epoch (``last_at``). Returns
        ``{account: {"requests", "errors", "tokens", "tokens_since_limit"}}``:

        - ``tokens`` is the all-time token sum;
        - ``tokens_since_limit`` sums requests with ``ts >= since`` for accounts
          present in ``limits`` (0 when no such rows exist) and is ``None`` for
          accounts without a quota event. With an empty ``limits`` mapping no
          join is made and every ``tokens_since_limit`` is ``None``.

        Results are cached per instance for ``ttl_seconds > 0``, keyed by a
        stable form of ``limits``; callers always get copies. A DB error
        returns ``{}`` without caching.
        """
        try:
            key = tuple(sorted((str(k), float(v)) for k, v in (limits or {}).items()))
        except (TypeError, ValueError):
            logger.exception("proxy_stats() received bad limits: %r", limits)
            return {}
        now = time.monotonic()
        if ttl_seconds > 0:
            with self._stats_lock:
                hit = self._stats_cache.get(key)
                if hit is not None and now - hit[0] < ttl_seconds:
                    return {acc: dict(row) for acc, row in hit[1].items()}
        try:
            self._ensure_schema()
            with self._pool.connection() as conn:
                if key:
                    values = ", ".join(
                        ["(%s::text, %s::double precision)"] * len(key))
                    params = tuple(x for pair in key for x in pair)
                    rows = conn.execute(
                        "SELECT r.account, COUNT(*),"
                        " SUM(CASE WHEN r.status >= 400 OR r.error IS NOT NULL"
                        " THEN 1 ELSE 0 END),"
                        " SUM(COALESCE(r.prompt_tokens,0)"
                        " + COALESCE(r.completion_tokens,0)),"
                        " SUM(COALESCE(r.prompt_tokens,0)"
                        " + COALESCE(r.completion_tokens,0))"
                        " FILTER (WHERE r.ts >= l.since),"
                        " (MAX(l.since) IS NOT NULL)"
                        " FROM request_log r"
                        " LEFT JOIN (VALUES " + values + ") AS l(account, since)"
                        " ON r.account = l.account"
                        " WHERE r.account IS NOT NULL GROUP BY r.account",
                        params,
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT account, COUNT(*),"
                        " SUM(CASE WHEN status >= 400 OR error IS NOT NULL"
                        " THEN 1 ELSE 0 END),"
                        " SUM(COALESCE(prompt_tokens,0)"
                        " + COALESCE(completion_tokens,0))"
                        " FROM request_log WHERE account IS NOT NULL"
                        " GROUP BY account",
                    ).fetchall()
        except Exception:
            logger.exception("proxy_stats() failed")
            return {}
        out: dict[str, dict] = {}
        for row in rows:
            if key:
                acc, reqs, errs, toks, since_toks, has_limit = row
                since_value = int(since_toks or 0) if has_limit else None
            else:
                acc, reqs, errs, toks = row
                since_value = None
            out[acc] = {
                "requests": int(reqs),
                "errors": int(errs or 0),
                "tokens": int(toks or 0),
                "tokens_since_limit": since_value,
            }
        if ttl_seconds > 0:
            with self._stats_lock:
                self._stats_cache[key] = (now, out)
                if len(self._stats_cache) > 32:  # bounded: drop the oldest half
                    kept = sorted(self._stats_cache.items(),
                                  key=lambda item: item[1][0])[-16:]
                    self._stats_cache = dict(kept)
        return {acc: dict(row) for acc, row in out.items()}

    async def proxy_stats_async(self, limits: dict[str, float],
                                ttl_seconds: float = 0.0) -> dict[str, dict]:
        """async wrapper: the blocking query goes to a thread. Never raises."""
        try:
            return await asyncio.to_thread(self.proxy_stats, limits, ttl_seconds)
        except Exception:
            logger.exception("proxy_stats_async() failed")
            return {}

    def daily_by_account(self, since: float) -> list[dict]:
        """Per-UTC-day, per-account totals at or after ``since`` (epoch).

        One row per ``(day, account)``: ``{"day": "YYYY-MM-DD", "account",
        "requests", "errors", "tokens"}``, sorted by ``(day, account)``. The day
        bucket is derived from ``FLOOR(ts / 86400)`` and formatted in UTC, so
        the result does not depend on the DB/session timezone. A DB error
        returns an empty list.
        """
        try:
            self._ensure_schema()
            with self._pool.connection() as conn:
                rows = conn.execute(
                    "SELECT FLOOR(ts / 86400)::bigint AS day, account, COUNT(*),"
                    " SUM(CASE WHEN status >= 400 OR error IS NOT NULL"
                    " THEN 1 ELSE 0 END),"
                    " SUM(COALESCE(prompt_tokens,0)"
                    " + COALESCE(completion_tokens,0))"
                    " FROM request_log"
                    " WHERE account IS NOT NULL AND ts >= %s"
                    " GROUP BY day, account",
                    (float(since),),
                ).fetchall()
        except Exception:
            logger.exception("daily_by_account() failed")
            return []
        out = [
            {
                "day": datetime.fromtimestamp(
                    int(day) * 86400, tz=timezone.utc).strftime("%Y-%m-%d"),
                "account": account,
                "requests": int(reqs),
                "errors": int(errs or 0),
                "tokens": int(toks or 0),
            }
            for day, account, reqs, errs, toks in rows
        ]
        out.sort(key=lambda row: (row["day"], row["account"]))
        return out

    async def daily_by_account_async(self, since: float) -> list[dict]:
        """async wrapper: the blocking query goes to a thread. Never raises."""
        try:
            return await asyncio.to_thread(self.daily_by_account, since)
        except Exception:
            logger.exception("daily_by_account_async() failed")
            return []

    # --- shutdown ---

    @property
    def dropped(self) -> int:
        """Entries silently dropped after ``close()`` (metrics/tests)."""
        return self._dropped

    def close(self) -> None:
        """Close the pool. Idempotent and thread-safe: after the first call
        ``_closed=True`` forever and ``log()`` only drops entries.
        ``_write_lock`` guarantees in-flight inserts (to_thread workers) reach
        COMMIT before the pool closes."""
        with self._write_lock:  # wait for in-flight inserts before closing
            self._closed = True
        self._pool.close()
