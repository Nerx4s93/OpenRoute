"""Shared PostgreSQL infrastructure (psycopg3 + psycopg_pool).

Implements the shared pool factory and the FROZEN ``SCHEMA`` DDL (the contract
for the gateway stores and the migration script).

All gateway stores (reqlog/proxydb/keydb) talk to one PostgreSQL server via
their own ``ConnectionPool``. The pool is lazy (``min_size=0``) and
thread-safe; ``ensure_schema`` is idempotent and runs on first DB access.
"""

from __future__ import annotations

from typing import Any

from psycopg_pool import ConnectionPool

SCHEMA: tuple[str, ...] = (
    # Request log (reqlog.py). IDENTICAL to NVRoute's request_log; the
    # ``account`` column holds the MASKED PROXY in OpenRoute.
    """
    CREATE TABLE IF NOT EXISTS request_log (
        ts DOUBLE PRECISION NOT NULL,
        key_id TEXT NOT NULL,
        method TEXT NOT NULL,
        path TEXT NOT NULL,
        model TEXT,
        status INTEGER NOT NULL,
        latency_ms DOUBLE PRECISION NOT NULL,
        stream BOOLEAN NOT NULL,
        account TEXT,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        error TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_request_log_ts ON request_log (ts)",
    "CREATE INDEX IF NOT EXISTS idx_request_log_key ON request_log (key_id)",
    # Proxy pool (proxydb.py) — replaces NVRoute's ``accounts`` table.
    """
    CREATE TABLE IF NOT EXISTS proxies (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        proxy TEXT NOT NULL UNIQUE,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        added_at DOUBLE PRECISION NOT NULL
    )
    """,
    # Sticky session -> exit proxy binding (affinitydb.py). ``ON DELETE SET
    # NULL`` unbinds a session when its proxy is removed; ``created_at`` is
    # never changed by a move (only ``proxy``/``last_used_at`` are updated).
    """
    CREATE TABLE IF NOT EXISTS session_affinity (
        session_key TEXT PRIMARY KEY,
        proxy TEXT REFERENCES proxies(proxy) ON DELETE SET NULL,
        created_at DOUBLE PRECISION NOT NULL,
        last_used_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_session_affinity_proxy
        ON session_affinity (proxy)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_session_affinity_used
        ON session_affinity (last_used_at)
    """,
    # Client keys (keydb.py); models is a JSONB array of model names.
    """
    CREATE TABLE IF NOT EXISTS keys (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        key_hash TEXT NOT NULL UNIQUE,
        key_id TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        admin BOOLEAN NOT NULL DEFAULT FALSE,
        rpm INTEGER,
        tpm INTEGER,
        models JSONB,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        added_at DOUBLE PRECISION NOT NULL
    )
    """,
)


def make_pool(dsn: str, name: str, max_size: int = 4) -> ConnectionPool:
    """Lazy connection pool.

    - ``min_size=0``: the constructor opens no connections and never fails,
      even when the server is unreachable — degradation surfaces on the first
      real use;
    - ``timeout=5``: how long ``pool.connection()`` waits for a free
      connection (unreachable server -> ``PoolTimeout`` instead of a hang);
    - ``connect_timeout=5``: ceiling for the background worker's TCP connect;
    - ``application_name=name`` is visible in ``pg_stat_activity``.
    """
    return ConnectionPool(
        dsn,
        min_size=0,
        max_size=max_size,
        timeout=5.0,
        name=name,
        open=True,
        kwargs={"application_name": name, "connect_timeout": 5},
    )


def ensure_schema(conn: Any) -> None:
    """Idempotently create all tables/indexes (the SAME DDL for the app and the
    migration script). Runs on a pooled connection; commits in the same
    transaction as the first use.

    ``pg_advisory_xact_lock`` serialises concurrent initialisation (e.g.
    ``RequestLog``/``ProxyDB``/``KeyDB`` coming up together): without it,
    parallel ``CREATE TABLE IF NOT EXISTS`` statements from different
    connections can race in the catalogue tables. The lock is held until COMMIT
    — which is exactly why the caller must commit in the same transaction.
    """
    conn.execute("SELECT pg_advisory_xact_lock(740514)")
    for ddl in SCHEMA:
        conn.execute(ddl)
