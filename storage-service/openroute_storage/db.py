"""PostgreSQL access shared by all storage domains.

The pool is opened once in the FastAPI lifespan. Startup waits for the first
connection and applies the frozen schema, so the service never starts in a
half-working state. Query errors are translated into the stable API envelope
without leaking driver messages, SQL or the DSN.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
import psycopg_pool
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .errors import StorageError, error_body

logger = logging.getLogger(__name__)


def create_pool(settings: Settings) -> psycopg_pool.AsyncConnectionPool:
    """Build a closed async pool; the lifespan opens and verifies it."""
    return psycopg_pool.AsyncConnectionPool(
        conninfo=settings.postgres_dsn,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        timeout=settings.db_pool_timeout_seconds,
        open=False,
        kwargs={
            "application_name": settings.db_application_name,
            "connect_timeout": settings.db_connect_timeout_seconds,
        },
    )


def get_pool(request: Request) -> psycopg_pool.AsyncConnectionPool:
    """FastAPI dependency: the live pool, or a safe 503."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None or pool.closed:
        raise StorageError(
            503, "Storage database unavailable", "storage_unavailable"
        )
    return pool


async def check_database(pool: psycopg_pool.AsyncConnectionPool) -> bool:
    """Run ``SELECT 1``; return False instead of raising (``/health``)."""
    if pool.closed:
        return False
    try:
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception as exc:  # noqa: BLE001 - health must answer, never crash
        logger.warning("Database health check failed (%s)", type(exc).__name__)
        return False


def is_db_unavailable(exc: BaseException) -> bool:
    """Connection-level failures: the client should retry later (503)."""
    return isinstance(
        exc,
        (
            psycopg_pool.PoolTimeout,
            psycopg_pool.PoolClosed,
            psycopg.OperationalError,
            psycopg.InterfaceError,
        ),
    )


def _json_error(
    status_code: int, message: str, error_type: str
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=error_body(status_code, message, error_type)
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register handlers that never expose driver/DSN details to callers."""

    @app.exception_handler(StorageError)
    async def _storage_error(_: Request, exc: StorageError) -> JSONResponse:
        return _json_error(exc.status_code, exc.message, exc.error_type)

    @app.exception_handler(psycopg.OperationalError)
    async def _unavailable(_: Request, exc: psycopg.OperationalError) -> JSONResponse:
        logger.error("Storage database unavailable (%s)", type(exc).__name__)
        return _json_error(503, "Storage database unavailable", "storage_unavailable")

    @app.exception_handler(psycopg.InterfaceError)
    async def _interface_error(_: Request, exc: psycopg.InterfaceError) -> JSONResponse:
        logger.error("Storage connection interface error (%s)", type(exc).__name__)
        return _json_error(503, "Storage database unavailable", "storage_unavailable")

    @app.exception_handler(psycopg.Error)
    async def _storage_db_error(_: Request, exc: psycopg.Error) -> JSONResponse:
        # Fixed text: the driver message may contain SQL or connection details.
        if is_db_unavailable(exc):
            logger.error("Storage database unavailable (%s)", type(exc).__name__)
            return _json_error(
                503, "Storage database unavailable", "storage_unavailable"
            )
        logger.error("Storage operation failed (%s)", type(exc).__name__)
        return _json_error(500, "Storage operation failed", "storage_error")


__all__ = [
    "check_database",
    "create_pool",
    "get_pool",
    "install_error_handlers",
    "is_db_unavailable",
]
