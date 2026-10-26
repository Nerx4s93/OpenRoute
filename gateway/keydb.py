"""Client-key persistence in PostgreSQL (table ``keys``).

Verbatim port of ``NVRoute/app/keydb.py`` — same DDL (see ``pg.py``), same
semantics. Keys are stored only as sha256 hashes: plaintext is never saved or
logged. The only way to manage them is the web admin UI. Style mirrors
``proxydb.py``: lazy ``ConnectionPool``, schema created on first use (DDL in
``pg.py``), idempotent ``close()``.
"""

from __future__ import annotations

import threading
import time

from psycopg.types.json import Jsonb

from .pg import ensure_schema, make_pool


class KeyDB:
    """``keys(id, key_hash UNIQUE, key_id, rpm, tpm, models, enabled,
    added_at)``.

    ``rpm``/``tpm`` NULL = unlimited; ``models`` JSONB array (NULL = all);
    ``enabled = FALSE`` = revoked (401) and survives a restart. ``id`` is
    insertion order and defines ``all()`` ordering. Lifecycle: after
    ``close()`` the store never "resurrects"; ``close()`` is idempotent.
    """

    def __init__(self, dsn: str) -> None:
        self._pool = make_pool(dsn, "openroute-keydb")
        self._schema_lock = threading.Lock()  # one-shot DDL init
        self._schema_ready = False
        self._closed = False

    def _ensure(self) -> None:
        """Create tables on a connection of THIS pool: the COMMIT happens
        before releasing ``_schema_lock`` (see proxydb.py for the rationale)."""
        if self._closed:
            raise RuntimeError("KeyDB is closed")
        if self._schema_ready:
            return
        with self._schema_lock:
            if not self._schema_ready:
                with self._pool.connection() as conn:
                    ensure_schema(conn)
                self._schema_ready = True

    def all(self) -> list[dict]:
        """All keys in insertion order; ``models`` unpacked from JSONB."""
        self._ensure()
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT key_hash, key_id, rpm, tpm, models, enabled"
                " FROM keys ORDER BY id"
            ).fetchall()
        out = []
        for h, kid, rpm, tpm, models, enabled in rows:
            # psycopg reads jsonb into a Python object; validate: array or None
            if not isinstance(models, list):
                models = None
            out.append({
                "key_hash": h,
                "key_id": kid,
                "rpm": rpm,
                "tpm": tpm,
                "models": models,
                "enabled": bool(enabled),
            })
        return out

    def add(self, key_hash: str, key_id: str, *,
            rpm: int | None = None, tpm: int | None = None,
            models: list[str] | None = None) -> bool:
        """True = inserted, False = duplicate hash (``ON CONFLICT DO NOTHING``)."""
        self._ensure()
        with self._pool.connection() as conn:
            cur = conn.execute(
                "INSERT INTO keys (key_hash, key_id, rpm, tpm, models,"
                " enabled, added_at) VALUES (%s, %s, %s, %s, %s, TRUE, %s)"
                " ON CONFLICT (key_hash) DO NOTHING",
                (key_hash, key_id, rpm, tpm,
                 Jsonb(models) if models is not None else None, time.time()),
            )
            return cur.rowcount > 0

    def set_enabled(self, key_hash: str, enabled: bool) -> bool:
        self._ensure()
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE keys SET enabled = %s WHERE key_hash = %s", (enabled, key_hash)
            )
            return cur.rowcount > 0

    def remove(self, key_hash: str) -> bool:
        self._ensure()
        with self._pool.connection() as conn:
            cur = conn.execute("DELETE FROM keys WHERE key_hash = %s", (key_hash,))
            return cur.rowcount > 0

    def close(self) -> None:
        """Close the pool; idempotent."""
        self._closed = True
        self._pool.close()
