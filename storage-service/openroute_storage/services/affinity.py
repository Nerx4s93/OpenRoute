"""Persistence domain: sticky session bindings (table ``session_affinity``).

The schema keeps ``proxy REFERENCES proxies(proxy) ON DELETE SET NULL`` and
binds a session to at most one row. A move updates only ``proxy`` and
``last_used_at``; ``created_at`` is never changed. Deleting/soft-removing a
proxy therefore unbinds its sessions exactly as before.

A batch upsert that references a proxy removed in a race would fail with a FK
violation. Instead of losing the whole coalesced batch, the service retries
row by row and stores the affected binding as ``proxy = NULL``; the healthy
rows in the same batch are still written.
"""

from __future__ import annotations

from typing import Any

from psycopg import errors
import psycopg_pool

_BINDING_FIELDS = "session_key, proxy, created_at, last_used_at"

_UPSERT = (
    "INSERT INTO session_affinity"
    " (session_key, proxy, created_at, last_used_at)"
    " VALUES (%s, %s, %s, %s)"
    " ON CONFLICT (session_key) DO UPDATE SET"
    " proxy = EXCLUDED.proxy,"
    " last_used_at = EXCLUDED.last_used_at"
)


async def load_all(conn: Any) -> list[dict[str, Any]]:
    """All bindings ordered by ``created_at`` (legacy affinity order)."""
    cur = await conn.execute(
        f"SELECT {_BINDING_FIELDS} FROM session_affinity ORDER BY created_at"
    )
    return [
        {
            "session_key": key,
            "proxy": proxy,
            "created_at": float(created),
            "last_used_at": float(used),
        }
        for key, proxy, created, used in await cur.fetchall()
    ]


async def _upsert_batch(conn: Any, rows: list[tuple[str, str | None, float, float]]) -> None:
    async with conn.cursor() as cur:
        await cur.executemany(_UPSERT, rows)


async def upsert_with_fk_fallback(
    pool: psycopg_pool.AsyncConnectionPool,
    rows: list[tuple[str, str | None, float, float]],
) -> tuple[int, int]:
    """Write bindings, unbinding rows whose proxy vanished.

    Returns ``(written, unbound)``. An empty input is a no-op.
    """
    if not rows:
        return 0, 0
    try:
        async with pool.connection() as conn:
            await _upsert_batch(conn, rows)
        return len(rows), 0
    except errors.ForeignKeyViolation:
        pass

    written = 0
    unbound = 0
    for row in rows:
        try:
            async with pool.connection() as conn:
                await _upsert_batch(conn, [row])
        except errors.ForeignKeyViolation:
            null_row = (row[0], None, row[2], row[3])
            async with pool.connection() as conn:
                await _upsert_batch(conn, [null_row])
            unbound += 1
        written += 1
    return written, unbound


__all__ = ["load_all", "upsert_with_fk_fallback"]
