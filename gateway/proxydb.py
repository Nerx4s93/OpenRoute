"""Proxy pool persistence in PostgreSQL (table ``proxies``).

Mirrors ``NVRoute/app/acctdb.py``: lazy ``psycopg_pool.ConnectionPool``, schema
created on first use (DDL lives in ``pg.py``), idempotent ``close()`` that
waits for in-flight operations.

Semantics (frozen): this table is the persistent record of pool
membership, each proxy's manual ``enabled`` flag, and upstream
quota/abuse parking (``quota_until`` / ``banned_until``) and the non-expiring
``permanently_banned`` abusive-traffic verdict. Membership is seeded
exclusively by ``refresh_proxies`` — there is no manual-add path. The refresh
synchronises the fetched snapshot: new rows are inserted with
``enabled = TRUE``, existing flags are preserved, and rows absent from the
source are soft-removed with ``listed = FALSE``. Soft-removed rows stay in the
table so parking/affinity survive and a proxy that reappears can be re-listed.
The only manual admin action is ``set_enabled`` on the same table; physical
deletion is out of scope.

Parking values are wall-clock epochs (``time.time()``); ``permanently_banned``
never expires. They are written by ``ProxyManager`` and loaded at startup, so a
gateway restart does not lose the “quota exhausted” / “banned” state shown in
the admin panel. Expired epochs are harmless and are overwritten by the next
mark.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable

from .pg import ensure_schema, make_pool


class ProxyDB:
    """``proxies(id, proxy UNIQUE, enabled, added_at, quota_until,
    banned_until, permanently_banned, listed)``.

    ``enabled = FALSE`` — disabled by hand from the admin UI (persists across
    restarts, unlike the runtime circuit breaker); ``listed = FALSE`` — absent
    from the latest successful source fetch and therefore not part of the live
    pool; ``quota_until`` / ``banned_until`` persist timed upstream parking;
    ``permanently_banned`` persists the abusive-traffic verdict; ``id`` is
    insertion order (analogue of sqlite rowid) and defines ``all()`` ordering.
    Lifecycle: after ``close()`` the store never "resurrects"; ``close()`` is
    idempotent.
    """

    def __init__(self, dsn: str) -> None:
        self._pool = make_pool(dsn, "openroute-proxydb")
        self._schema_lock = threading.Lock()  # one-shot DDL init
        self._schema_ready = False
        self._closed = False

    def _ensure(self) -> None:
        """Create tables on a connection of THIS pool: the COMMIT happens
        before releasing ``_schema_lock``, otherwise concurrent inserts on
        another connection would not see the not-yet-committed tables."""
        if self._closed:
            raise RuntimeError("ProxyDB is closed")
        if self._schema_ready:
            return
        with self._schema_lock:
            if not self._schema_ready:
                with self._pool.connection() as conn:
                    ensure_schema(conn)
                self._schema_ready = True

    def is_empty(self) -> bool:
        self._ensure()
        with self._pool.connection() as conn:
            return not conn.execute(
                "SELECT EXISTS (SELECT 1 FROM proxies WHERE listed)"
            ).fetchone()[0]

    def all(self) -> list[tuple[str, bool]]:
        """``[(proxy, enabled)]`` for listed rows, in insertion order."""
        self._ensure()
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT proxy, enabled FROM proxies WHERE listed ORDER BY id"
            ).fetchall()
        return [(p, bool(e)) for p, e in rows]

    def parking_all(self) -> list[tuple[str, float, float, bool]]:
        """``[(proxy, quota_until, banned_until, permanently_banned)]``.

        Includes soft-removed (``listed = FALSE``) rows: their parking must
        survive while the proxy is absent and be restored if it reappears.
        Rows are in insertion order. Timed values are wall-clock epochs. The
        manager merges rows that share the same canonical quota key (max epoch
        wins; permanent ban ORs), so duplicated proxy endpoints do not lose the
        longest parking.
        """
        self._ensure()
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT proxy, quota_until, banned_until, permanently_banned"
                " FROM proxies ORDER BY id"
            ).fetchall()
        return [
            (p, float(q or 0.0), float(b or 0.0), bool(permanent))
            for p, q, b, permanent in rows
        ]

    def save_parking(self, proxies: Iterable[str], quota_until: float,
                     banned_until: float, permanently_banned: bool) -> int:
        """Persist parking for every URL of one quota bucket; returns rows hit."""
        self._ensure()
        urls = list(dict.fromkeys(p for p in proxies if p))
        if not urls:
            return 0
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE proxies SET quota_until = %s, banned_until = %s,"
                " permanently_banned = permanently_banned OR %s"
                " WHERE proxy = ANY(%s)",
                (float(quota_until), float(banned_until),
                 bool(permanently_banned), urls),
            )
            return cur.rowcount

    def upsert_many(self, proxies: Iterable[str]) -> int:
        """Insert every not-yet-known proxy with ``enabled = TRUE``; keep the
        flag of existing rows untouched. Returns the number of new rows.
        """
        self._ensure()
        deduped = list(dict.fromkeys(p for p in proxies if p))
        if not deduped:
            return 0
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO proxies (proxy, enabled, added_at) VALUES (%s, TRUE, %s)"
                    " ON CONFLICT (proxy) DO NOTHING",
                    [(p, time.time()) for p in deduped],
                )
                added = cur.rowcount if cur.rowcount is not None else 0
        return max(0, added)

    def sync_many(self, proxies: Iterable[str]) -> int:
        """Apply one successful source snapshot and return the number of new rows.

        Inserts new proxies, marks every fetched proxy as ``listed`` and every
        other row as not listed. Rows are never deleted; ``enabled`` and
        parking are preserved. The statements run in one transaction so there
        is no window where the whole pool is unlisted.
        """
        self._ensure()
        deduped = list(dict.fromkeys(p for p in proxies if p))
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO proxies (proxy, enabled, added_at, listed)"
                    " VALUES (%s, TRUE, %s, TRUE)"
                    " ON CONFLICT (proxy) DO NOTHING",
                    [(p, time.time()) for p in deduped],
                )
                added = cur.rowcount if cur.rowcount is not None else 0
            if deduped:
                conn.execute(
                    "UPDATE proxies SET listed = (proxy = ANY(%s))", (deduped,)
                )
            else:
                conn.execute("UPDATE proxies SET listed = FALSE")
        return max(0, added)

    def set_enabled(self, proxy: str, enabled: bool) -> bool:
        """True if a row was updated."""
        self._ensure()
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE proxies SET enabled = %s WHERE proxy = %s", (enabled, proxy)
            )
            return cur.rowcount > 0

    def close(self) -> None:
        """Close the pool; idempotent (waits for in-flight clients)."""
        self._closed = True
        self._pool.close()
