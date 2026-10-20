"""Sticky session -> proxy bindings in PostgreSQL (table ``session_affinity``).

Modelled on :mod:`gateway.proxydb`: a lazy ``psycopg_pool.ConnectionPool`` with the
DDL created on first use (the DDL lives in ``pg.py``), idempotent ``close()``.

Semantics:

- one row per session key (``sid:<session_id>`` / ``fp:<fingerprint>``),
- ``proxy`` references ``proxies(proxy)`` with ``ON DELETE SET NULL`` — an
  admin delete unbinds the session automatically,
- ``created_at`` is the session's binding creation time and is **never**
  changed by a move; only ``proxy`` and ``last_used_at`` are updated,
- rows live forever (completed sessions keep consuming their S slot).

All methods are blocking; ``AffinityManager`` calls them through
``asyncio.to_thread`` from its coalescing writer.
"""

from __future__ import annotations

import threading

from .pg import ensure_schema, make_pool


class AffinityDB:
    """``session_affinity(session_key, proxy, created_at, last_used_at)``."""

    def __init__(self, dsn: str) -> None:
        self._pool = make_pool(dsn, "openroute-affinitydb")
        self._schema_lock = threading.Lock()
        self._schema_ready = False
        self._closed = False

    # --- schema ---

    def _ensure(self) -> None:
        """Create DDL on THIS pool; commit before releasing the schema lock."""
        if self._closed:
            raise RuntimeError("AffinityDB is closed")
        if self._schema_ready:
            return
        with self._schema_lock:
            if not self._schema_ready:
                with self._pool.connection() as conn:
                    ensure_schema(conn)
                self._schema_ready = True

    def ensure_schema(self) -> None:
        """Public idempotent schema initialisation (alias of ``_ensure``)."""
        self._ensure()

    # --- reads ---

    def load_all(self) -> list[tuple[str, str | None, float, float]]:
        """``[(session_key, proxy|None, created_at, last_used_at)]``."""
        self._ensure()
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT session_key, proxy, created_at, last_used_at"
                " FROM session_affinity ORDER BY created_at"
            ).fetchall()
        return [(k, p, float(c), float(u)) for k, p, c, u in rows]

    # --- writes ---

    def upsert_many(
        self, rows: list[tuple[str, str | None, float, float]]
    ) -> int:
        """Insert/update bindings; a move never changes ``created_at``.

        Returns the number of rows written. Empty input is a no-op.
        """
        self._ensure()
        if not rows:
            return 0
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO session_affinity"
                    " (session_key, proxy, created_at, last_used_at)"
                    " VALUES (%s, %s, %s, %s)"
                    " ON CONFLICT (session_key) DO UPDATE SET"
                    " proxy = EXCLUDED.proxy,"
                    " last_used_at = EXCLUDED.last_used_at",
                    rows,
                )
                written = cur.rowcount if cur.rowcount is not None else 0
        return max(0, written)

    def clear(self, key: str) -> bool:
        """Unbind one session (``proxy = NULL``) while keeping its row/stats.

        Used when a parked group has no ready destination: the session keeps
        its ``created_at`` (and thus its S slot) and is rebound on the next
        request. Returns ``True`` when a row was updated.
        """
        self._ensure()
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE session_affinity SET proxy = NULL WHERE session_key = %s",
                (key,),
            )
            return cur.rowcount > 0

    def close(self) -> None:
        """Close the pool; idempotent (waits for in-flight clients)."""
        self._closed = True
        self._pool.close()
