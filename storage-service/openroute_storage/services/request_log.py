"""Persistence domain: request log and its admin aggregations.

SQL is copied from ``gateway/reqlog.py`` so the existing response shapes and
UTC-day buckets do not change:

- ``summary`` aggregates by ``key_id`` (including an all-row ``total``);
- ``by_account`` aggregates by masked proxy and excludes NULL accounts;
- ``daily`` groups by ``FLOOR(ts / 86400)`` and formats the bucket as a UTC
  ``YYYY-MM-DD`` date, independent of the database/session timezone;
- ``recent`` returns newest first.

The gateway's in-memory deque is only a hot cache; this service is the source
of truth and always reads the committed log.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

_FIELDS = (
    "ts", "key_id", "method", "path", "model", "status", "latency_ms",
    "stream", "account", "prompt_tokens", "completion_tokens", "error",
)

_ERRORS = "SUM(CASE WHEN status >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END)"
_TOKENS = "SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0))"


async def append(conn: Any, entry: Any) -> None:
    await conn.execute(
        "INSERT INTO request_log (ts, key_id, method, path, model, status,"
        " latency_ms, stream, account, prompt_tokens, completion_tokens, error)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            entry.ts,
            entry.key_id,
            entry.method,
            entry.path,
            entry.model,
            entry.status,
            entry.latency_ms,
            entry.stream,
            entry.account,
            entry.prompt_tokens,
            entry.completion_tokens,
            entry.error,
        ),
    )


async def recent(conn: Any, limit: int) -> list[dict[str, Any]]:
    """Most recent entries, newest first."""
    cur = await conn.execute(
        f"SELECT {', '.join(_FIELDS)} FROM request_log"
        " ORDER BY ts DESC LIMIT %s",
        (limit,),
    )
    return [dict(zip(_FIELDS, r)) for r in await cur.fetchall()]


async def summary(conn: Any, since: float | None = None) -> dict[str, dict[str, Any]]:
    """Aggregates by ``key_id`` plus a top-level ``"total"`` entry.

    ``since`` (epoch) limits the window to ``ts >= since``; ``None`` means the
    whole log. Latency is the mean in milliseconds, rounded to one decimal.
    """
    where, params = "", ()
    if since is not None:
        where = " WHERE ts >= %s"
        params = (float(since),)
    cur = await conn.execute(
        "SELECT key_id, COUNT(*),"
        f" {_ERRORS},"
        f" {_TOKENS},"
        " AVG(latency_ms) FROM request_log" + where + " GROUP BY key_id",
        params,
    )
    rows = await cur.fetchall()

    out: dict[str, dict[str, Any]] = {}
    req_t = err_t = tok_t = 0
    lat_w = 0.0  # weighted latency sum for the overall mean
    for key_id, reqs, errs, toks, avg in rows:
        out[key_id] = {
            "requests": int(reqs),
            "errors": int(errs or 0),
            "tokens": int(toks or 0),
            "avg_latency_ms": round(float(avg or 0.0), 1),
        }
        req_t += int(reqs)
        err_t += int(errs or 0)
        tok_t += int(toks or 0)
        lat_w += float(avg or 0.0) * int(reqs)
    out["total"] = {
        "requests": req_t,
        "errors": err_t,
        "tokens": tok_t,
        "avg_latency_ms": round(lat_w / req_t, 1) if req_t else 0.0,
    }
    return out


async def by_account(conn: Any, since: float | None = None) -> dict[str, dict[str, int]]:
    """Aggregates by ``account`` (masked proxy), excluding NULL accounts."""
    clauses = ["account IS NOT NULL"]
    params: list[float] = []
    if since is not None:
        clauses.append("ts >= %s")
        params.append(float(since))
    where = " WHERE " + " AND ".join(clauses)
    cur = await conn.execute(
        "SELECT account, COUNT(*),"
        f" {_ERRORS},"
        f" {_TOKENS}"
        " FROM request_log" + where + " GROUP BY account",
        tuple(params),
    )
    rows = await cur.fetchall()
    return {
        acc: {
            "requests": int(reqs),
            "errors": int(errs or 0),
            "tokens": int(toks or 0),
        }
        for acc, reqs, errs, toks in rows
    }


async def daily_by_account(conn: Any, since: float) -> list[dict[str, Any]]:
    """Per-UTC-day, per-account totals at or after ``since`` (epoch).

    One row per ``(day, account)`` sorted by ``(day, account)``. The day bucket
    is derived from ``FLOOR(ts / 86400)`` and formatted in UTC, so the result
    does not depend on the DB/session timezone.
    """
    cur = await conn.execute(
        "SELECT FLOOR(ts / 86400)::bigint AS day, account, COUNT(*),"
        f" {_ERRORS},"
        f" {_TOKENS}"
        " FROM request_log"
        " WHERE account IS NOT NULL AND ts >= %s"
        " GROUP BY day, account",
        (float(since),),
    )
    rows = await cur.fetchall()
    out = [
        {
            "day": datetime.fromtimestamp(
                int(day) * 86400, tz=timezone.utc
            ).strftime("%Y-%m-%d"),
            "account": account,
            "requests": int(reqs),
            "errors": int(errs or 0),
            "tokens": int(toks or 0),
        }
        for day, account, reqs, errs, toks in rows
    ]
    out.sort(key=lambda row: (row["day"], row["account"]))
    return out


__all__ = ["append", "by_account", "daily_by_account", "recent", "summary"]
