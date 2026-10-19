"""Proxy pool persistence in PostgreSQL (table ``proxies``).

Mirrors ``NVRoute/app/acctdb.py``: lazy ``psycopg_pool.ConnectionPool``, schema
created on first use (DDL lives in ``pg.py``), idempotent ``close()`` that
waits for in-flight operations.

Semantics (FROZEN, see DESIGN.md): this table is the persistent record of pool
membership and each proxy's manual ``enabled`` flag. Membership is seeded
exclusively by ``refresh_proxies`` — there is no manual-add path. The refresh
only ever UPSERTS fetched proxies (insert new rows with ``enabled = TRUE``;
never flips an existing row's flag; never deletes). Manual admin actions
(``set_enabled`` / ``remove``) mutate the same table.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable

from .pg import ensure_schema, make_pool


class ProxyDB:
    """``proxies(id, proxy UNIQUE, enabled, added_at)``.

    ``enabled = FALSE`` — disabled by hand from the admin UI (persists across
    restarts, unlike the runtime circuit breaker); ``id`` is insertion order
    (analogue of sqlite rowid) and defines ``all()`` ordering. Lifecycle:
    after ``close()`` the store never "resurrects"; ``close()`` is idempotent.
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
            return not conn.execute("SELECT EXISTS (SELECT 1 FROM proxies)").fetchone()[0]

    def all(self) -> list[tuple[str, bool]]:
        """``[(proxy, enabled)]`` in insertion order."""
        self._ensure()
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT proxy, enabled FROM proxies ORDER BY id"
            ).fetchall()
        return [(p, bool(e)) for p, e in rows]

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

    def set_enabled(self, proxy: str, enabled: bool) -> bool:
        """True if a row was updated."""
        self._ensure()
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE proxies SET enabled = %s WHERE proxy = %s", (enabled, proxy)
            )
            return cur.rowcount > 0

    def remove(self, proxy: str) -> bool:
        """True if a row was deleted."""
        self._ensure()
        with self._pool.connection() as conn:
            cur = conn.execute("DELETE FROM proxies WHERE proxy = %s", (proxy,))
            return cur.rowcount > 0

    def close(self) -> None:
        """Close the pool; idempotent (waits for in-flight clients)."""
        self._closed = True
        self._pool.close()
