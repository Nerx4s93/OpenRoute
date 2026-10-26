"""Frozen PostgreSQL schema owned by storage-service.

The DDL is a contract: an existing ``pgdata`` volume must keep working
unchanged, so the statements are idempotent and no migration is required.
It was historically derived from the gateway's former ``pg.py``; that module
is removed and this service is now the sole SQL owner. Do not change these
statements without treating it as a schema migration.
"""

from __future__ import annotations

from typing import Any

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
    # Per-account daily statistics scan ``request_log`` by account + timestamp.
    "CREATE INDEX IF NOT EXISTS idx_request_log_account_ts"
    " ON request_log (account, ts)",
    # Proxy pool — replaces NVRoute's ``accounts`` table.
    # quota_until/banned_until persist timed parking; permanently_banned is the
    # non-expiring abusive-traffic verdict, so a gateway restart never forgets
    # that a proxy must not be used again.
    """
    CREATE TABLE IF NOT EXISTS proxies (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        proxy TEXT NOT NULL UNIQUE,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        added_at DOUBLE PRECISION NOT NULL,
        quota_until DOUBLE PRECISION NOT NULL DEFAULT 0,
        banned_until DOUBLE PRECISION NOT NULL DEFAULT 0,
        permanently_banned BOOLEAN NOT NULL DEFAULT FALSE,
        listed BOOLEAN NOT NULL DEFAULT TRUE
    )
    """,
    # Existing installations have the old table: add the parking columns in
    # place. IF NOT EXISTS makes the migration idempotent.
    "ALTER TABLE proxies ADD COLUMN IF NOT EXISTS quota_until"
    " DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE proxies ADD COLUMN IF NOT EXISTS banned_until"
    " DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE proxies ADD COLUMN IF NOT EXISTS permanently_banned"
    " BOOLEAN NOT NULL DEFAULT FALSE",
    # ``listed`` = present in the latest successful PROXIES_URL fetch. Rows
    # missing from the source are soft-removed (listed = FALSE) instead of
    # deleted, so parking and foreign keys survive and a proxy can come back.
    "ALTER TABLE proxies ADD COLUMN IF NOT EXISTS listed"
    " BOOLEAN NOT NULL DEFAULT TRUE",
    # Sticky session -> exit proxy binding. ``ON DELETE SET NULL`` unbinds a
    # session when its proxy is removed; ``created_at`` is never changed by a
    # move (only ``proxy``/``last_used_at`` are updated).
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
    # Client keys; models is a JSONB array of model names.
    """
    CREATE TABLE IF NOT EXISTS keys (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        key_hash TEXT NOT NULL UNIQUE,
        key_id TEXT NOT NULL,
        rpm INTEGER,
        tpm INTEGER,
        models JSONB,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        added_at DOUBLE PRECISION NOT NULL
    )
    """,
    # Client keys are ordinary API credentials with no roles. Drop the legacy
    # admin flag from existing installations so a key can only call /v1/*.
    "ALTER TABLE keys DROP COLUMN IF EXISTS admin",
    # key_id is the only key identifier; the separate display name was removed.
    "ALTER TABLE keys DROP COLUMN IF EXISTS name",
    # One synthetic "persona" (session/project/trace ids) per proxy quota
    # bucket. It survives a restart; ``generation`` orders rotations so a
    # stale fire-and-forget write can never overwrite a newer persona.
    """
    CREATE TABLE IF NOT EXISTS proxy_personas (
        quota_key TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        trace_id TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        generation INTEGER NOT NULL DEFAULT 0
    )
    """,
)


async def ensure_schema(conn: Any) -> None:
    """Idempotently create all tables/indexes on ``conn``.

    ``pg_advisory_xact_lock`` serialises concurrent initialisation (e.g. two
    storage-service replicas starting together). The lock is held until COMMIT,
    which is why the caller must run this inside one transaction.
    """
    await conn.execute("SELECT pg_advisory_xact_lock(740514)")
    for ddl in SCHEMA:
        await conn.execute(ddl)


__all__ = ["SCHEMA", "ensure_schema"]
