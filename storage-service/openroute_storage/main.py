"""OpenRoute storage-service — the only component that talks to PostgreSQL.

Private FastAPI microservice (no host port, no edge route, no authentication):
it is reachable only by the gateway on the internal
``openroute-storage-api-net`` network. The service owns all SQL for the
gateway's persistence domains and exposes a typed HTTP API; request/response
examples live in ``README.md``.

Startup opens an ``AsyncConnectionPool``, waits for the first connection and
applies the frozen DDL defined in ``schema.py``. An unreachable database fails
startup with a clear, DSN-free error instead of degrading silently.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Annotated, Any

import psycopg_pool
from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings, get_settings
from .db import check_database, create_pool, get_pool, install_error_handlers
from .errors import StorageError
from .schema import ensure_schema
from .schemas import (
    AccountUsage,
    AffinityListResponse,
    AffinityUpsertRequest,
    AffinityUpsertResponse,
    DailyUsage,
    HealthResponse,
    KeyAddRequest,
    KeyAddResponse,
    KeyDeleteResponse,
    KeyListResponse,
    KeyRotateRequest,
    KeyRotateResponse,
    KeySetEnabledRequest,
    KeySetEnabledResponse,
    KeyUsage,
    LogAppendRequest,
    LogAppendResponse,
    PersonaListResponse,
    PersonaRow,
    PersonaUpsertRequest,
    PersonaUpsertResponse,
    ProxyListResponse,
    ProxyParkingRequest,
    ProxyParkingResponse,
    ProxySetEnabledRequest,
    ProxySetEnabledResponse,
    ProxySyncRequest,
    ProxySyncResponse,
    RecentLogResponse,
)
from .services import affinity as affinity_service
from .services import keys as keys_service
from .services import personas as personas_service
from .services import proxies as proxies_service
from .services import request_log as log_service

logger = logging.getLogger(__name__)

router = APIRouter()
PoolDep = Annotated[psycopg_pool.AsyncConnectionPool, Depends(get_pool)]


# --- lifespan ---------------------------------------------------------------


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Open/verify PostgreSQL, apply the schema, then serve.

    A connection failure aborts startup (the container restarts and shows a
    clear log line) instead of serving 503s until someone notices.
    """
    settings: Settings = application.state.settings
    pool = create_pool(settings)
    application.state.pool = pool
    try:
        await pool.open(wait=True, timeout=settings.db_startup_timeout_seconds)
        async with pool.connection() as conn:
            await ensure_schema(conn)
    except Exception as exc:  # noqa: BLE001 - startup must fail clearly
        application.state.pool = None
        try:
            await pool.close()
        except Exception:  # noqa: BLE001 - best effort on a failed start
            pass
        logger.error(
            "storage-service startup failed: PostgreSQL is not reachable (%s)",
            type(exc).__name__,
        )
        raise RuntimeError(
            "storage-service: PostgreSQL is unreachable; check POSTGRES_DSN"
        ) from exc

    logger.info("storage-service ready")
    try:
        yield
    finally:
        application.state.pool = None
        await pool.close()


# --- health -----------------------------------------------------------------


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> Any:
    """Liveness + database connectivity; open on the private network."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None or not await check_database(pool):
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "database": "unavailable"},
        )
    return {"status": "ok", "database": "ok"}


# --- proxies ----------------------------------------------------------------


@router.get("/proxies", response_model=ProxyListResponse)
async def list_proxies(
    pool: PoolDep,
    listed: bool | None = Query(
        None,
        description="true = listed only, false = soft-removed only,"
        " omit = every row (parking restore)",
    ),
) -> dict[str, Any]:
    async with pool.connection() as conn:
        rows = await proxies_service.load_all(conn, listed)
    return {"proxies": rows}


@router.post("/proxies/sync", response_model=ProxySyncResponse)
async def sync_proxies(
    body: ProxySyncRequest, pool: PoolDep
) -> dict[str, Any]:
    """Apply one successful px6 snapshot; returns new-row count + all rows."""
    async with pool.connection() as conn:
        added, rows = await proxies_service.sync(
            conn, body.proxies, enable_new=body.enable_new
        )
    return {"added": added, "proxies": rows}


@router.post("/proxies/set-enabled", response_model=ProxySetEnabledResponse)
async def set_proxy_enabled(
    body: ProxySetEnabledRequest, pool: PoolDep
) -> dict[str, Any]:
    async with pool.connection() as conn:
        updated = await proxies_service.set_enabled(
            conn, body.proxy, body.enabled
        )
    if not updated:
        raise StorageError(404, "Proxy not found", "not_found_error")
    return {"proxy": body.proxy, "enabled": body.enabled, "updated": True}


@router.post("/proxies/parking", response_model=ProxyParkingResponse)
async def save_proxy_parking(
    body: ProxyParkingRequest, pool: PoolDep
) -> dict[str, Any]:
    """Persist quota parking / permanent abusive ban."""
    async with pool.connection() as conn:
        updated = await proxies_service.save_parking(
            conn,
            body.proxies,
            body.quota_until,
            body.permanently_banned,
        )
    return {"updated": updated}


# --- keys -------------------------------------------------------------------


@router.get("/keys", response_model=KeyListResponse)
async def list_keys(pool: PoolDep) -> dict[str, Any]:
    async with pool.connection() as conn:
        rows = await keys_service.load_all(conn)
    return {"keys": rows}


@router.post("/keys", response_model=KeyAddResponse)
async def add_key(body: KeyAddRequest, pool: PoolDep) -> dict[str, Any]:
    """Store a caller-supplied SHA-256 hash; plaintext is never accepted."""
    async with pool.connection() as conn:
        created = await keys_service.add(
            conn,
            body.key_hash,
            body.key_id,
            rpm=body.rpm,
            tpm=body.tpm,
            models=body.models,
        )
    return {"created": created}


@router.post("/keys/rotate", response_model=KeyRotateResponse)
async def rotate_key(
    body: KeyRotateRequest, pool: PoolDep
) -> dict[str, Any]:
    """Replace the hash in place; retrying the same pair is idempotent."""
    async with pool.connection() as conn:
        result = await keys_service.rotate(
            conn, body.key_hash, body.new_key_hash
        )
    return result


@router.post("/keys/set-enabled", response_model=KeySetEnabledResponse)
async def set_key_enabled(
    body: KeySetEnabledRequest, pool: PoolDep
) -> dict[str, Any]:
    async with pool.connection() as conn:
        updated = await keys_service.set_enabled(
            conn, body.key_hash, body.enabled
        )
    if not updated:
        raise StorageError(404, "Key not found", "not_found_error")
    return {
        "key_hash": body.key_hash,
        "enabled": body.enabled,
        "updated": True,
    }


@router.delete("/keys/{key_hash}", response_model=KeyDeleteResponse)
async def delete_key(key_hash: str, pool: PoolDep) -> dict[str, Any]:
    async with pool.connection() as conn:
        deleted = await keys_service.remove(conn, key_hash)
    if not deleted:
        raise StorageError(404, "Key not found", "not_found_error")
    return {"key_hash": key_hash, "deleted": True}


# --- affinity ---------------------------------------------------------------


@router.get("/affinity", response_model=AffinityListResponse)
async def list_affinity(pool: PoolDep) -> dict[str, Any]:
    async with pool.connection() as conn:
        rows = await affinity_service.load_all(conn)
    return {"bindings": rows}


@router.post("/affinity", response_model=AffinityUpsertResponse)
async def upsert_affinity(
    body: AffinityUpsertRequest, pool: PoolDep
) -> dict[str, Any]:
    """Upsert bindings; a vanished proxy stores the binding as unbound."""
    rows = [
        (b.session_key, b.proxy, b.created_at, b.last_used_at)
        for b in body.bindings
    ]
    written, unbound = await affinity_service.upsert_with_fk_fallback(pool, rows)
    return {"written": written, "unbound": unbound}


# --- personas ---------------------------------------------------------------


@router.get("/personas", response_model=PersonaListResponse)
async def list_personas(pool: PoolDep) -> dict[str, Any]:
    async with pool.connection() as conn:
        rows = await personas_service.load_all(conn)
    return {"personas": rows}


@router.get("/personas/{quota_key}", response_model=PersonaRow)
async def get_persona(quota_key: str, pool: PoolDep) -> dict[str, Any]:
    async with pool.connection() as conn:
        row = await personas_service.load_one(conn, quota_key)
    if row is None:
        raise StorageError(404, "Persona not found", "not_found_error")
    return row


@router.post("/personas", response_model=PersonaUpsertResponse)
async def upsert_persona(
    body: PersonaUpsertRequest, pool: PoolDep
) -> dict[str, Any]:
    """Upsert one persona; older generations never overwrite newer ones."""
    async with pool.connection() as conn:
        written = await personas_service.upsert(conn, body)
    return {"written": written}


# --- request log / stats ----------------------------------------------------


@router.post("/request-log", response_model=LogAppendResponse)
async def append_request_log(
    body: LogAppendRequest, pool: PoolDep
) -> dict[str, Any]:
    async with pool.connection() as conn:
        await log_service.append(conn, body)
    return {"logged": True}


@router.get("/request-log/recent", response_model=RecentLogResponse)
async def recent_request_log(
    pool: PoolDep,
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    async with pool.connection() as conn:
        rows = await log_service.recent(conn, limit)
    return {"entries": rows, "limit": limit}


@router.get("/stats/summary", response_model=dict[str, KeyUsage])
async def stats_summary(
    pool: PoolDep, since: float | None = Query(None)
) -> dict[str, Any]:
    """Per-key aggregates + ``total`` (old ``RequestLog.summary``)."""
    async with pool.connection() as conn:
        return await log_service.summary(conn, since)


@router.get("/stats/by-account", response_model=dict[str, AccountUsage])
async def stats_by_account(
    pool: PoolDep, since: float | None = Query(None)
) -> dict[str, Any]:
    """Per-masked-proxy aggregates (old ``summary_by_account``)."""
    async with pool.connection() as conn:
        return await log_service.by_account(conn, since)


@router.get("/stats/daily", response_model=list[DailyUsage])
async def stats_daily(
    pool: PoolDep, since: float = Query(0.0)
) -> list[dict[str, Any]]:
    """Per-UTC-day, per-account series (old ``daily_by_account``)."""
    async with pool.connection() as conn:
        return await log_service.daily_by_account(conn, since)


# --- app factory ------------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application; ``settings`` is injectable for tests."""
    cfg = settings if settings is not None else get_settings()
    application = FastAPI(
        title="OpenRoute storage-service",
        description="Private persistence API for the OpenRoute gateway.",
        version=__version__,
        lifespan=lifespan,
        # The service is private (no host port, no edge route). Disable the
        # docs/OpenAPI surface entirely.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.settings = cfg
    application.state.pool = None
    install_error_handlers(application)
    application.include_router(router)
    return application


app = create_app()


__all__ = ["app", "create_app"]
