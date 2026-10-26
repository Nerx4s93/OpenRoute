"""Persistence domain: client keys (table ``keys``).

Only SHA-256 hex digests are stored; there is no plaintext column and no API
that accepts a plaintext secret. ``key_hash`` (UNIQUE) is the exact row
identifier — ``key_id`` is a non-unique display label, so neither update nor
delete ever targets a row by ``key_id``.
"""

from __future__ import annotations

import time
from typing import Any

from psycopg import errors
from psycopg.types.json import Jsonb

from ..errors import StorageError

_KEY_FIELDS = "key_hash, key_id, rpm, tpm, models, enabled"


def _row(values: tuple[Any, ...]) -> dict[str, Any]:
    key_hash, key_id, rpm, tpm, models, enabled = values
    # psycopg decodes jsonb into a Python object; validate: array or None.
    if not isinstance(models, list):
        models = None
    return {
        "key_hash": key_hash,
        "key_id": key_id,
        "rpm": rpm,
        "tpm": tpm,
        "models": models,
        "enabled": bool(enabled),
    }


async def load_all(conn: Any) -> list[dict[str, Any]]:
    """All keys in insertion order; ``models`` unpacked from JSONB."""
    cur = await conn.execute(
        f"SELECT {_KEY_FIELDS} FROM keys ORDER BY id"
    )
    return [_row(tuple(r)) for r in await cur.fetchall()]


async def add(
    conn: Any,
    key_hash: str,
    key_id: str,
    *,
    rpm: int | None = None,
    tpm: int | None = None,
    models: list[str] | None = None,
) -> bool:
    """True = inserted, False = duplicate hash (``ON CONFLICT DO NOTHING``)."""
    cur = await conn.execute(
        "INSERT INTO keys (key_hash, key_id, rpm, tpm, models, enabled, added_at)"
        " VALUES (%s, %s, %s, %s, %s, TRUE, %s)"
        " ON CONFLICT (key_hash) DO NOTHING",
        (
            key_hash,
            key_id,
            rpm,
            tpm,
            Jsonb(models) if models is not None else None,
            time.time(),
        ),
    )
    return cur.rowcount > 0


async def rotate(
    conn: Any, old_hash: str, new_hash: str
) -> dict[str, Any]:
    """Replace a key hash in place, keeping ``key_id``/limits/models/enabled.

    Idempotency: if ``old_hash`` is already gone but ``new_hash`` exists, the
    original rotation succeeded and a retry with the same pair is a success
    (``already_rotated = True``) rather than a 404. A different row already
    holding ``new_hash`` is a 409 conflict.
    """
    if old_hash == new_hash:
        cur = await conn.execute(
            "SELECT key_id FROM keys WHERE key_hash = %s", (old_hash,)
        )
        row = await cur.fetchone()
        if row is None:
            raise StorageError(404, "Key not found", "not_found_error")
        return {"rotated": True, "already_rotated": False, "key_id": row[0]}

    try:
        cur = await conn.execute(
            "UPDATE keys SET key_hash = %s WHERE key_hash = %s RETURNING key_id",
            (new_hash, old_hash),
        )
        row = await cur.fetchone()
    except errors.UniqueViolation as exc:
        raise StorageError(
            409, "New key hash already belongs to another key", "conflict_error"
        ) from exc

    if row is not None:
        return {"rotated": True, "already_rotated": False, "key_id": row[0]}

    # The old hash is gone: was this exact rotation already applied?
    cur = await conn.execute(
        "SELECT key_id FROM keys WHERE key_hash = %s", (new_hash,)
    )
    row = await cur.fetchone()
    if row is not None:
        return {"rotated": False, "already_rotated": True, "key_id": row[0]}
    raise StorageError(404, "Key not found", "not_found_error")


async def set_enabled(conn: Any, key_hash: str, enabled: bool) -> bool:
    cur = await conn.execute(
        "UPDATE keys SET enabled = %s WHERE key_hash = %s", (enabled, key_hash)
    )
    return cur.rowcount > 0


async def remove(conn: Any, key_hash: str) -> bool:
    cur = await conn.execute("DELETE FROM keys WHERE key_hash = %s", (key_hash,))
    return cur.rowcount > 0


__all__ = ["add", "load_all", "remove", "rotate", "set_enabled"]
