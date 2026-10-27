"""auth-service — private authentication and session service for OpenRoute.

The React admin UI never talks to this service directly. Browser requests go to
``web-admin`` through ``edge``; web-admin sets/clears the HttpOnly session
cookie and calls this service's business API over the private
``openroute-auth-net`` network. Only auth-service reaches Redis, and only
auth-service knows ``ADMIN_PASSWORD``.

Endpoints are intentionally small and internal:
``POST /auth/login``, ``POST /auth/validate``, ``POST /auth/logout``,
``GET /auth/status`` and ``GET /health``. There is no public docs/OpenAPI
surface and no edge route.
"""

from __future__ import annotations

import hmac
import logging
import math
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .config import Settings, get_settings
from .schemas import LoginRequest, LogoutRequest, ValidateRequest
from .sessions import RedisSessionStore, SessionUnavailable

logger = logging.getLogger("openroute-auth")

LOGIN_FAILS_MAX = 10
LOGIN_FAILS_WINDOW = 60.0

router = APIRouter()


# --- pure auth helpers ------------------------------------------------------


def _ensure_enabled(settings: Settings) -> None:
    if not settings.admin_password:
        raise HTTPException(404, "Админка выключена (задайте ADMIN_PASSWORD)")


def _normalize_client_ip(value: str) -> str:
    text = (value or "").strip()
    return text[:128] or "unknown"


def _check_csrf(csrf: str, session: dict) -> None:
    """Constant-time comparison of the per-session CSRF token."""
    expected = str(session.get("csrf", ""))
    got = str(csrf or "")
    if not expected or not hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(403, "Неверный CSRF-токен")


def login_retry_after(fails: list[float], now: float | None = None) -> float:
    """Seconds until another login attempt is allowed (0 = allowed).

    Mutates ``fails`` in place, dropping attempts outside the rolling window.
    """
    now = time.time() if now is None else now
    cutoff = now - LOGIN_FAILS_WINDOW
    fails[:] = [t for t in fails if t >= cutoff]
    if len(fails) >= LOGIN_FAILS_MAX:
        return max(0.0, LOGIN_FAILS_WINDOW - (now - fails[0]))
    return 0.0


async def _load_session(store: Any, sid: str) -> dict | None:
    """Load, validate and (on expiry) delete a server-side session."""
    if not sid:
        return None
    session = await store.get(sid)
    if session is None:
        return None
    if time.time() > float(session.get("expires", 0)):
        await store.delete(sid)
        return None
    return {"sid": sid, **session}


# --- request-scoped settings/store ------------------------------------------


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _store(request: Request) -> Any:
    return request.app.state.store


# --- lifespan / error handling ---------------------------------------------


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    yield
    await application.state.store.aclose()


async def _sessions_unavailable(request: Request, exc: SessionUnavailable) -> JSONResponse:
    logger.error("session storage (Redis) unavailable: %s", exc)
    return JSONResponse({"detail": "Хранилище сессий недоступно"}, status_code=503)


# --- routes -----------------------------------------------------------------


@router.get("/", include_in_schema=False)
async def index() -> dict:
    return {"service": "openroute-auth", "status": "ok"}


@router.get("/health", include_in_schema=False)
async def health(request: Request) -> dict:
    """Liveness plus Redis/session state; disabled panel needs no Redis."""
    settings = _settings(request)
    if not settings.admin_password:
        return {"status": "ok", "enabled": False, "sessions": 0}
    sessions = await _store(request).count()
    return {"status": "ok", "enabled": True, "sessions": sessions}


@router.get("/auth/status", include_in_schema=False)
async def auth_status(request: Request) -> dict:
    """Whether the panel is enabled; deliberately does not touch Redis."""
    return {"enabled": bool(_settings(request).admin_password)}


@router.post("/auth/login", include_in_schema=False)
async def auth_login(body: LoginRequest, request: Request) -> dict:
    settings = _settings(request)
    _ensure_enabled(settings)

    client_ip = _normalize_client_ip(body.client_ip)
    fails = request.app.state.login_fails.setdefault(client_ip, [])
    retry_after = login_retry_after(fails)
    if retry_after > 0:
        raise HTTPException(
            429,
            "Слишком много неудачных попыток входа; попробуйте позже",
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )

    if not hmac.compare_digest(
        body.password.encode("utf-8"), settings.admin_password.encode("utf-8")
    ):
        fails.append(time.time())
        raise HTTPException(401, "Неверный пароль")

    request.app.state.login_fails.pop(client_ip, None)
    sid = secrets.token_urlsafe(32)
    session = {
        "csrf": secrets.token_urlsafe(24),
        "expires": time.time() + settings.admin_session_ttl_seconds,
    }
    await _store(request).put(sid, session, settings.admin_session_ttl_seconds)
    return {
        "sid": sid,
        "csrf": session["csrf"],
        "expires_at": session["expires"],
        "max_age": settings.admin_session_ttl_seconds,
    }


@router.post("/auth/validate", include_in_schema=False)
async def auth_validate(body: ValidateRequest, request: Request) -> dict:
    settings = _settings(request)
    _ensure_enabled(settings)
    session = await _load_session(_store(request), body.sid)
    if session is None:
        raise HTTPException(401, "Требуется вход в панель")
    if body.require_csrf:
        _check_csrf(body.csrf, session)
    return {
        "sid": session["sid"],
        "csrf": session["csrf"],
        "expires_at": float(session["expires"]),
    }


@router.post("/auth/logout", include_in_schema=False)
async def auth_logout(body: LogoutRequest, request: Request) -> Response:
    settings = _settings(request)
    _ensure_enabled(settings)
    session = await _load_session(_store(request), body.sid)
    if session is None:
        raise HTTPException(401, "Требуется вход в панель")
    _check_csrf(body.csrf, session)
    await _store(request).delete(body.sid)
    return Response(status_code=204)


# --- application factory ----------------------------------------------------


def create_app(settings: Settings | None = None, store: Any | None = None) -> FastAPI:
    """Build the application; settings/store are injectable for tests."""
    cfg = settings if settings is not None else get_settings()
    session_store = store if store is not None else RedisSessionStore(cfg.redis_url)

    application = FastAPI(
        title="OpenRoute auth-service",
        description="Private authentication and session API for OpenRoute.",
        version="0.1.0",
        lifespan=_lifespan,
        # Internal service: no docs/OpenAPI surface.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.settings = cfg
    application.state.store = session_store
    application.state.login_fails = {}
    application.add_exception_handler(SessionUnavailable, _sessions_unavailable)
    application.include_router(router)
    return application


app = create_app()


__all__ = ["app", "create_app"]
