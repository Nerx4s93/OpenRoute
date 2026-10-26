"""Persistence domain: proxy personas (table ``proxy_personas``).

Exactly one persona per canonical quota bucket (``host:port`` with credentials
stripped). The ``generation`` guard
``ON CONFLICT ... WHERE proxy_personas.generation <= EXCLUDED.generation``
makes an out-of-order fire-and-forget write a no-op, while a retry with the
same generation may rewrite the row.
"""

from __future__ import annotations

from typing import Any

_FIELDS = "quota_key, session_id, project_id, trace_id, created_at, generation"

_UPSERT = (
    "INSERT INTO proxy_personas"
    " (quota_key, session_id, project_id, trace_id, created_at, generation)"
    " VALUES (%s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (quota_key) DO UPDATE SET"
    " session_id = EXCLUDED.session_id,"
    " project_id = EXCLUDED.project_id,"
    " trace_id = EXCLUDED.trace_id,"
    " created_at = EXCLUDED.created_at,"
    " generation = EXCLUDED.generation"
    " WHERE proxy_personas.generation <= EXCLUDED.generation"
    " RETURNING quota_key"
)


def _row(values: tuple[Any, ...]) -> dict[str, Any]:
    quota_key, session_id, project_id, trace_id, created_at, generation = values
    return {
        "quota_key": quota_key,
        "session_id": session_id,
        "project_id": project_id,
        "trace_id": trace_id,
        "created_at": float(created_at),
        "generation": int(generation),
    }


async def load_all(conn: Any) -> list[dict[str, Any]]:
    """All personas ordered by creation time."""
    cur = await conn.execute(
        f"SELECT {_FIELDS} FROM proxy_personas ORDER BY created_at"
    )
    return [_row(tuple(r)) for r in await cur.fetchall()]


async def load_one(conn: Any, quota_key: str) -> dict[str, Any] | None:
    cur = await conn.execute(
        f"SELECT {_FIELDS} FROM proxy_personas WHERE quota_key = %s",
        (quota_key,),
    )
    row = await cur.fetchone()
    return _row(tuple(row)) if row is not None else None


async def upsert(conn: Any, persona: Any) -> bool:
    """Insert/update one persona; False = stale generation rejected."""
    cur = await conn.execute(
        _UPSERT,
        (
            persona.quota_key,
            persona.session_id,
            persona.project_id,
            persona.trace_id,
            float(persona.created_at),
            int(persona.generation),
        ),
    )
    return await cur.fetchone() is not None


__all__ = ["load_all", "load_one", "upsert"]
