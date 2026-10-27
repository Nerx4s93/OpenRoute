"""Persistence domain: proxy pool (table ``proxies``).

Semantics:

- membership is seeded exclusively by :func:`sync` (one successful px6
  catalogue snapshot); new rows use the caller's ``enable_new`` initial flag
  (default ``FALSE``);
- an existing row keeps its manual ``enabled`` flag and its parking/ban state;
- rows absent from the snapshot are soft-removed with ``listed = FALSE`` —
  never deleted — so parking and affinity survive and a proxy can reappear;
- enabling a proxy is an explicit operation; physical deletion is not offered.
"""

from __future__ import annotations

from typing import Any

_PROXY_FIELDS = "proxy, enabled, quota_until, permanently_banned, listed"


def _row(values: tuple[Any, ...]) -> dict[str, Any]:
    proxy, enabled, quota_until, permanent, listed = values
    return {
        "proxy": proxy,
        "enabled": bool(enabled),
        "quota_until": float(quota_until or 0.0),
        "permanently_banned": bool(permanent),
        "listed": bool(listed),
    }


async def load_all(conn: Any, listed: bool | None = None) -> list[dict[str, Any]]:
    """All rows in insertion order; optionally only listed/unlisted ones.

    ``listed=None`` returns soft-removed rows too, which is what the gateway
    needs to restore parking. ``listed=True`` returns only active rows.
    """
    sql = f"SELECT {_PROXY_FIELDS} FROM proxies"
    params: tuple[Any, ...] = ()
    if listed is not None:
        sql += " WHERE listed = %s"
        params = (listed,)
    sql += " ORDER BY id"
    cur = await conn.execute(sql, params)
    return [_row(tuple(r)) for r in await cur.fetchall()]


async def sync(
    conn: Any, proxies: list[str], enable_new: bool = False
) -> tuple[int, list[dict[str, Any]]]:
    """Apply one catalogue snapshot inside the caller's transaction.

    Returns ``(new_row_count, all_rows)``. Duplicate/empty URL strings are
    ignored, matching the former gateway sync semantics. ``enable_new`` only
    affects rows inserted by this call; existing flags are never changed.
    """
    deduped = list(dict.fromkeys(p for p in proxies if p))
    added = 0
    for proxy in deduped:
        cur = await conn.execute(
            "INSERT INTO proxies (proxy, enabled, listed)"
            " VALUES (%s, %s, TRUE)"
            " ON CONFLICT (proxy) DO NOTHING",
            (proxy, bool(enable_new)),
        )
        added += cur.rowcount
    if deduped:
        await conn.execute(
            "UPDATE proxies SET listed = (proxy = ANY(%s))", (deduped,)
        )
    else:
        await conn.execute("UPDATE proxies SET listed = FALSE")
    return max(0, added), await load_all(conn)


async def set_enabled(conn: Any, proxy: str, enabled: bool) -> bool:
    """True if a row was updated."""
    cur = await conn.execute(
        "UPDATE proxies SET enabled = %s WHERE proxy = %s", (enabled, proxy)
    )
    return cur.rowcount > 0


async def save_parking(
    conn: Any,
    proxies: list[str],
    quota_until: float,
    permanently_banned: bool,
) -> int:
    """Persist parking for every URL of one quota bucket; returns rows hit.

    The epoch is written as-is (an expired epoch is harmless and the next mark
    overwrites it); a permanent ban can only be added, never removed.
    """
    urls = list(dict.fromkeys(p for p in proxies if p))
    if not urls:
        return 0
    cur = await conn.execute(
        "UPDATE proxies SET quota_until = %s,"
        " permanently_banned = permanently_banned OR %s"
        " WHERE proxy = ANY(%s)",
        (float(quota_until), bool(permanently_banned), urls),
    )
    return cur.rowcount


__all__ = ["load_all", "save_parking", "set_enabled", "sync"]
