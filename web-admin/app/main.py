"""web-admin — OpenRoute admin JSON API (control plane).

The React SPA (separate ``web-admin-ui`` container) talks to this service via
``/api/admin/*``. Session/login/CSRF handling mirrors the previous server-side
panel, but pages are no longer rendered here: every response is JSON or SSE.

The service never touches the gateway's PostgreSQL directly; it only calls the
gateway's internal API with ``X-Internal-Token`` (see ``gateway.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from . import auth
from .config import get_settings
from .gateway import GatewayClient
from .schemas import KeyAddBody, KeyDeleteBody, KeySetBody, LoginBody, ProxySetBody
from .sessions import RedisSessionStore, SessionUnavailable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("openroute-web-admin")

settings = get_settings()

_sessions = RedisSessionStore(settings.redis_url)
gateway = GatewayClient(settings)

_PANEL_API = "/api/admin"
_SESSION_COOKIE = auth.SESSION_COOKIE
_PERIODS = {"day", "week", "month", "all"}
_SSE_TICK_SECONDS = 1.0
_SSE_HEARTBEAT_SECONDS = 15.0
_SSE_AUTH_CHECK_SECONDS = 30.0
_login_fails: dict[str, list[float]] = {}


def _normalize_period(period: str) -> str:
    value = (period or "all").strip().lower()
    return value if value in _PERIODS else "all"


def _ensure_enabled() -> None:
    if not settings.admin_password:
        raise HTTPException(404, "Админка выключена (задайте ADMIN_PASSWORD)")


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await _sessions.aclose()
    await gateway.aclose()


app = FastAPI(
    title="OpenRoute Admin API",
    description="JSON API админки OpenRoute; данные — через internal API шлюза.",
    version="0.2.0",
    lifespan=_lifespan,
)


@app.middleware("http")
async def _api_no_store(request: Request, call_next):
    """Admin API responses (including SSE) must never be cached."""
    response = await call_next(request)
    if request.url.path.startswith(_PANEL_API):
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


# --- request parsing -------------------------------------------------------


async def _json_body(request: Request, model: type[BaseModel]) -> BaseModel:
    """Read a small JSON object and validate it with a Pydantic model."""
    raw = await request.body()
    if len(raw) > 64 * 1024:
        raise HTTPException(413, "Тело запроса слишком большое")
    if not raw:
        data: object = {}
    else:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(400, "Некорректный JSON") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, "JSON должен быть объектом")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        messages = exc.errors()
        message = str(messages[0].get("msg", "Некорректные данные")) if messages else "Некорректные данные"
        if message.startswith("Value error, "):
            message = message[len("Value error, "):]
        raise HTTPException(400, message) from exc


# --- auth dependency -------------------------------------------------------


async def require_panel(request: Request) -> dict:
    """Require an enabled panel and a live server-side session."""
    _ensure_enabled()
    session = await auth.session_from_request(request, settings, _sessions)
    if session is None:
        raise HTTPException(401, "Требуется вход в панель")
    return session


# --- live updates (SSE) ----------------------------------------------------


async def _panel_snapshot(page: str, period: str) -> dict:
    if page == "keys":
        return await gateway.keys_snapshot(period)
    return await gateway.proxies_snapshot(period)


def _snapshot_fingerprint(snapshot: dict) -> str:
    """Compare only real data, not the clock label ``server_time``."""
    return json.dumps(
        {key: value for key, value in snapshot.items() if key != "server_time"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sse_data(snapshot: dict) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    return f"data: {payload}\n\n"


async def _sse_events(request: Request, page: str, period: str,
                      session: dict) -> AsyncIterator[str]:
    """Push JSON snapshots until the browser disconnects.

    The gateway is sampled once per second; an event is emitted only when
    something visible changed. A heartbeat keeps nginx and intermediate
    proxies from closing an idle stream. Expired/deleted sessions stop the
    stream within ``_SSE_AUTH_CHECK_SECONDS``.
    """
    sid = session.get("sid", "")
    last_key: str | None = None
    last_sent = time.monotonic()
    last_auth_check = last_sent
    error_logged = False
    yield "retry: 3000\n\n"

    while True:
        if await request.is_disconnected():
            return

        now = time.monotonic()
        if sid and now - last_auth_check >= _SSE_AUTH_CHECK_SECONDS:
            try:
                current = await _sessions.get(sid)
            except Exception:
                logger.exception("SSE session check failed")
                return
            if current is None or time.time() > float(current.get("expires", 0)):
                return
            last_auth_check = now

        try:
            snapshot = await _panel_snapshot(page, period)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not error_logged:
                logger.warning("SSE snapshot failed: %s", exc)
                error_logged = True
        else:
            error_logged = False
            key = _snapshot_fingerprint(snapshot)
            now = time.monotonic()
            if key != last_key:
                last_key = key
                last_sent = now
                yield _sse_data(snapshot)

        now = time.monotonic()
        if now - last_sent >= _SSE_HEARTBEAT_SECONDS:
            last_sent = now
            yield ": keepalive\n\n"

        if await request.is_disconnected():
            return
        await asyncio.sleep(_SSE_TICK_SECONDS)


def _sse_response(request: Request, page: str, period: str,
                  session: dict) -> StreamingResponse:
    return StreamingResponse(
        _sse_events(request, page, period, session),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --- exception handlers ----------------------------------------------------


@app.exception_handler(SessionUnavailable)
async def _storage_unavailable(request: Request, exc: SessionUnavailable) -> JSONResponse:
    logger.error("session storage (Redis) unavailable: %s", exc)
    return JSONResponse({"detail": "Хранилище сессий недоступно"}, status_code=503)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    message = "Некорректные параметры запроса"
    if errors:
        message = str(errors[0].get("msg", message))
        if message.startswith("Value error, "):
            message = message[len("Value error, "):]
    return JSONResponse({"detail": message}, status_code=422)


# --- routes: service / session --------------------------------------------


@app.get("/", include_in_schema=False)
async def index() -> dict:
    return {"service": "openroute-web-admin", "status": "ok"}


@app.get("/health", include_in_schema=False)
async def health() -> dict:
    return {"status": "ok", "sessions": await _sessions.count()}


@app.get(f"{_PANEL_API}/session")
async def api_session(request: Request) -> dict:
    """Current browser session + CSRF token (200 even when not signed in)."""
    _ensure_enabled()
    session = await auth.session_from_request(request, settings, _sessions)
    if session is None:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "csrf": session["csrf"],
        "expires_at": session["expires"],
    }


@app.post(f"{_PANEL_API}/login")
async def api_login(request: Request) -> Response:
    _ensure_enabled()
    ip = auth.client_ip(request)
    fails = _login_fails.setdefault(ip, [])
    retry_after = auth.login_retry_after(fails)
    if retry_after > 0:
        raise HTTPException(
            429,
            "Слишком много неудачных попыток входа; попробуйте позже",
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )

    body = await _json_body(request, LoginBody)
    assert isinstance(body, LoginBody)
    if not auth.check_password(body.password, settings):
        fails.append(time.time())
        raise HTTPException(401, "Неверный пароль")

    _login_fails.pop(ip, None)
    session = await auth.new_session(settings, _sessions)
    response = JSONResponse({
        "authenticated": True,
        "csrf": session["csrf"],
        "expires_at": session["expires"],
    })
    auth.set_session_cookie(response, session["sid"], request, settings)
    return response


@app.post(f"{_PANEL_API}/logout", status_code=204)
async def api_logout(request: Request, session: dict = Depends(require_panel)) -> Response:
    auth.check_csrf(request, session)
    await _sessions.delete(session["sid"])
    response = Response(status_code=204)
    auth.clear_session_cookie(response, request, settings)
    return response


# --- routes: proxies -------------------------------------------------------


@app.get(f"{_PANEL_API}/proxies")
async def api_proxies(period: str = "all", session: dict = Depends(require_panel)) -> dict:
    return await gateway.proxies_snapshot(_normalize_period(period))


@app.post(f"{_PANEL_API}/proxies/set")
async def api_proxy_set(request: Request, session: dict = Depends(require_panel)) -> dict:
    auth.check_csrf(request, session)
    body = await _json_body(request, ProxySetBody)
    assert isinstance(body, ProxySetBody)
    return await gateway.set_proxy(body.proxy_id, body.enabled)


@app.post(f"{_PANEL_API}/proxies/refresh")
async def api_proxy_refresh(request: Request, session: dict = Depends(require_panel)) -> dict:
    """Force-fetch the proxy list, bypassing the px6-service TTL cache."""
    auth.check_csrf(request, session)
    return await gateway.refresh_proxies()


@app.get(f"{_PANEL_API}/proxies/events")
async def api_proxy_events(request: Request, period: str = "all",
                           session: dict = Depends(require_panel)) -> StreamingResponse:
    return _sse_response(request, "proxies", _normalize_period(period), session)


# --- routes: keys ----------------------------------------------------------


@app.get(f"{_PANEL_API}/keys")
async def api_keys(period: str = "all", session: dict = Depends(require_panel)) -> dict:
    return await gateway.keys_snapshot(_normalize_period(period))


@app.post(f"{_PANEL_API}/keys/add")
async def api_key_add(request: Request, session: dict = Depends(require_panel)) -> dict:
    auth.check_csrf(request, session)
    body = await _json_body(request, KeyAddBody)
    assert isinstance(body, KeyAddBody)
    return await gateway.add_key(body)


@app.post(f"{_PANEL_API}/keys/set")
async def api_key_set(request: Request, session: dict = Depends(require_panel)) -> dict:
    auth.check_csrf(request, session)
    body = await _json_body(request, KeySetBody)
    assert isinstance(body, KeySetBody)
    return await gateway.set_key(body.key_hash, body.enabled)


@app.post(f"{_PANEL_API}/keys/delete")
async def api_key_delete(request: Request, session: dict = Depends(require_panel)) -> dict:
    auth.check_csrf(request, session)
    body = await _json_body(request, KeyDeleteBody)
    assert isinstance(body, KeyDeleteBody)
    return await gateway.delete_key(body.key_hash)


@app.get(f"{_PANEL_API}/keys/events")
async def api_key_events(request: Request, period: str = "all",
                         session: dict = Depends(require_panel)) -> StreamingResponse:
    return _sse_response(request, "keys", _normalize_period(period), session)
