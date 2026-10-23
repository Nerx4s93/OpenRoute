"""Proxy persona persistence in PostgreSQL (table ``proxy_personas``).

Modelled on :mod:`gateway.proxydb`: a lazy ``psycopg_pool.ConnectionPool`` with
the DDL created on first use (the DDL lives in ``pg.py``), an idempotent
``close()`` that waits for in-flight operations.

Semantics: exactly one persona (session/project/trace ids) per proxy quota
bucket (``canonical_quota_key``). Rows survive a restart, so a persona is not
recycled just because the gateway was restarted. ``generation`` orders
rotations: a stale fire-and-forget write is rejected by the upsert guard and
can never overwrite a newer persona.

Operational assumption: the production gateway runs ONE uvicorn worker (see
``Dockerfile``), so two writers never race with equal generations; ``<=`` in
the guard exists only so our own retry of the same generation can rewrite it.

All methods are blocking; :class:`gateway.personas.PersonaStore` calls them
through ``asyncio.to_thread`` and treats every failure as non-fatal.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from psycopg_pool import ConnectionPool

from .pg import ensure_schema, make_pool

logger = logging.getLogger("openroute.personadb")


class PersonaDB:
    """``proxy_personas(quota_key, session_id, project_id, trace_id,
    created_at, generation)``.

    Lifecycle: after ``close()`` the store never "resurrects"; ``close()`` is
    idempotent. A pool that cannot be constructed leaves the store without a
    pool: reads/writes raise (the caller swallows them) instead of failing the
    gateway at startup.
    """

    def __init__(self, dsn: str) -> None:
        self._pool: ConnectionPool | None = None
        try:
            self._pool = make_pool(dsn, "openroute-personadb")
        except Exception:
            logger.exception(
                "PersonaDB pool init failed; persona persistence disabled")
        self._schema_lock = threading.Lock()  # one-shot DDL init
        self._schema_ready = False
        self._closed = False

    def _ensure(self) -> None:
        """Create DDL on THIS pool; commit before releasing the schema lock."""
        if self._closed:
            raise RuntimeError("PersonaDB is closed")
        if self._schema_ready:
            return
        with self._schema_lock:
            if not self._schema_ready:
                pool = self._pool
                if pool is None:
                    raise RuntimeError("PersonaDB has no pool")
                with pool.connection() as conn:
                    ensure_schema(conn)
                self._schema_ready = True

    def ensure_schema(self) -> None:
        """Public idempotent schema initialisation (alias of ``_ensure``)."""
        self._ensure()

    def load_all(self) -> list[tuple[str, str, str, str, float, int]]:
        """``[(quota_key, session_id, project_id, trace_id, created_at,
        generation)]`` ordered by creation time."""
        self._ensure()
        pool = self._pool
        if pool is None:
            raise RuntimeError("PersonaDB has no pool")
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT quota_key, session_id, project_id, trace_id,"
                " created_at, generation FROM proxy_personas ORDER BY created_at"
            ).fetchall()
        return [
            (k, s, p, t, float(c), int(g)) for k, s, p, t, c, g in rows
        ]

    def upsert(self, persona: Any) -> bool:
        """Insert/update one persona (duck-typed attributes, no import cycle).

        The ``WHERE`` guard makes an out-of-order write a no-op: only a
        generation at least as new as the stored one wins. Returns ``True``
        when the row was written and ``False`` when the guard rejected it.
        """
        self._ensure()
        pool = self._pool
        if pool is None:
            raise RuntimeError("PersonaDB has no pool")
        with pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO proxy_personas"
                " (quota_key, session_id, project_id, trace_id, created_at,"
                " generation)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (quota_key) DO UPDATE SET"
                " session_id = EXCLUDED.session_id,"
                " project_id = EXCLUDED.project_id,"
                " trace_id = EXCLUDED.trace_id,"
                " created_at = EXCLUDED.created_at,"
                " generation = EXCLUDED.generation"
                " WHERE proxy_personas.generation <= EXCLUDED.generation"
                " RETURNING quota_key",
                (
                    persona.quota_key,
                    persona.session_id,
                    persona.project_id,
                    persona.trace_id,
                    float(persona.created_at),
                    int(persona.generation),
                ),
            ).fetchone()
        return row is not None

    def load_one(
        self, quota_key: str
    ) -> tuple[str, str, str, str, float, int] | None:
        """Return one persona row in ``load_all`` shape, or ``None``."""
        self._ensure()
        pool = self._pool
        if pool is None:
            raise RuntimeError("PersonaDB has no pool")
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT quota_key, session_id, project_id, trace_id,"
                " created_at, generation FROM proxy_personas"
                " WHERE quota_key = %s",
                (quota_key,),
            ).fetchone()
        if row is None:
            return None
        k, s, p, t, c, g = row
        return (k, s, p, t, float(c), int(g))

    def close(self) -> None:
        """Close the pool; idempotent (waits for in-flight clients)."""
        if self._closed:
            return
        self._closed = True
        pool = self._pool
        self._pool = None
        if pool is not None:
            pool.close()
