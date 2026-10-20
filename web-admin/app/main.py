"""web-admin — OpenRoute web admin as a separate microservice (control plane).

Session/login/CSRF plumbing mirrors ``NVRoute/admin-service/app/main.py``; the
page-rendering and mutation routes delegate to ``adminui.py`` and drive the
gateway's frozen internal API:

    GET  /internal/proxies-data        -> snapshot for the "Proxies" page
    GET  /internal/keys-data           -> snapshot for the "Keys" page
    POST /internal/proxies/set         {"proxy": str, "enabled": bool}
    POST /internal/proxies/delete      {"proxy": str}
    POST /internal/keys/add            {"key", "key_id", "name", "admin", "rpm", "tpm", "models"}
    POST /internal/keys/set            {"key_hash": str, "enabled": bool}
    POST /internal/keys/delete         {"key_hash": str}

This service never touches the gateway's PostgreSQL directly; it only calls the
gateway's internal API with ``X-Internal-Token``. It is exposed only via edge
(nginx).
"""

from __future__ import annotations

import hmac
import logging
import math
import secrets
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import parse_qsl, quote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .config import get_settings
from .sessions import RedisSessionStore, SessionUnavailable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("openroute-web-admin")

settings = get_settings()

_sessions = RedisSessionStore(settings.redis_url)

_PANEL = "/admin/ui"
_LOGIN_URL = "/login"
_SESSION_COOKIE = "openroute_session"
_LOGIN_FAILS_MAX = 10
_LOGIN_FAILS_WINDOW = 60.0
_login_fails: dict[str, list[float]] = {}

_internal_client = httpx.AsyncClient(
    base_url=settings.gateway_internal_url,
    headers={"X-Internal-Token": settings.internal_api_token},
    timeout=settings.internal_timeout_seconds,
)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await _sessions.aclose()
    await _internal_client.aclose()


app = FastAPI(
    title="OpenRoute Admin",
    description="Веб-админка OpenRoute (control plane); данные — через internal API шлюза.",
    version="0.1.0",
    lifespan=_lifespan,
)


# --- internal API client ---------------------------------------------------


def _gateway_error(r: httpx.Response) -> HTTPException:
    """Map a gateway error response to an ``HTTPException`` with a clean message.

    The gateway answers ALL HTTP errors (including internal 403/404/5xx) with
    the OpenAI envelope ``{"error": {"message", "type", "code"}}``, so prefer
    ``payload["error"]["message"]``; fall back to FastAPI's
    ``{"detail": ...}``, then a bare ``{"message": ...}``, then the raw body.
    The raw JSON envelope is never surfaced to the operator.
    """
    detail: str | None = None
    try:
        payload = r.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            detail = err["message"]
        elif isinstance(payload.get("detail"), str):
            detail = payload["detail"]
        elif isinstance(payload.get("message"), str):
            detail = payload["message"]
    if not detail:
        detail = r.text[:200]
    return HTTPException(r.status_code, detail)


async def _api_get(path: str) -> dict:
    """GET /internal/* -> dict; unreachable gateway -> 502."""
    try:
        r = await _internal_client.get(path)
    except httpx.HTTPError as exc:
        logger.error("gateway internal API unreachable: %s", exc)
        raise HTTPException(502, "Gateway internal API unreachable") from exc
    if r.status_code >= 400:
        raise _gateway_error(r)
    return r.json()


async def _api_post(path: str, payload: dict) -> dict:
    try:
        r = await _internal_client.post(path, json=payload)
    except httpx.HTTPError as exc:
        logger.error("gateway internal API unreachable: %s", exc)
        raise HTTPException(502, "Gateway internal API unreachable") from exc
    if r.status_code >= 400:
        raise _gateway_error(r)
    return r.json()


# --- sessions / auth (ported from NVRoute) ---------------------------------


async def _new_session() -> dict:
    sid = secrets.token_urlsafe(32)
    session = {"csrf": secrets.token_urlsafe(24), "expires": time.time() + settings.admin_session_ttl_seconds}
    await _sessions.put(sid, session, settings.admin_session_ttl_seconds)
    return {"sid": sid, **session}


async def _session_from_request(request: Request) -> dict | None:
    sid = request.cookies.get(_SESSION_COOKIE, "")
    if not sid:
        return None
    session = await _sessions.get(sid)
    if session is None:
        return None
    if time.time() > session["expires"]:
        await _sessions.delete(sid)
        return None
    return {"sid": sid, **session}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_throttled(ip: str) -> float:
    fails = [t for t in _login_fails.get(ip, []) if time.time() - t < _LOGIN_FAILS_WINDOW]
    _login_fails[ip] = fails
    if len(fails) >= _LOGIN_FAILS_MAX:
        return _LOGIN_FAILS_WINDOW - (time.time() - fails[0])
    return 0.0


def _check_password(pw: str) -> bool:
    return hmac.compare_digest(pw.encode("utf-8"), settings.admin_password.encode("utf-8"))


def _set_session_cookie(resp: Response, sid: str, request: Request) -> None:
    resp.set_cookie(
        _SESSION_COOKIE, sid,
        max_age=settings.admin_session_ttl_seconds,
        httponly=True, samesite="strict",
        secure=request.url.scheme == "https", path="/",
    )


async def require_panel(request: Request) -> dict:
    """All /admin/ui pages/actions require a live session (else redirect).

    Disabled panel (no ADMIN_PASSWORD) -> 404.
    """
    if not settings.admin_password:
        raise HTTPException(404, "Admin UI disabled (set ADMIN_PASSWORD)")
    session = await _session_from_request(request)
    if session is None:
        raise HTTPException(303, "Login required", headers={"Location": _LOGIN_URL})
    return session


async def _read_form(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > 64 * 1024:
        raise HTTPException(413, "Form body too large")
    return dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))


def _check_csrf(form: dict[str, str], session: dict) -> None:
    if not hmac.compare_digest(form.get("_csrf", ""), session["csrf"]):
        raise HTTPException(403, "Invalid CSRF token")


def _panel_redirect(msg: str, page: str = "") -> RedirectResponse:
    base = f"{_PANEL}/keys" if page == "keys" else _PANEL
    return RedirectResponse(f"{base}?msg={quote(msg)}", status_code=303)


@app.exception_handler(HTTPException)
async def _redirect_as_redirect(request: Request, exc: HTTPException):
    hdrs = exc.headers or {}
    loc = hdrs.get("Location") or hdrs.get("location")
    if loc and exc.status_code in (301, 302, 303, 307, 308):
        return RedirectResponse(loc, status_code=exc.status_code)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(SessionUnavailable)
async def _storage_unavailable(request: Request, exc: SessionUnavailable) -> JSONResponse:
    logger.error("session storage (Redis) unavailable: %s", exc)
    return JSONResponse({"detail": "Session storage unavailable"}, status_code=503)


# --- routes: root / login / logout -----------------------------------------


@app.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    return RedirectResponse(_LOGIN_URL, status_code=307)


@app.get(_LOGIN_URL, include_in_schema=False)
async def login_page(request: Request) -> Response:
    if not settings.admin_password:
        raise HTTPException(404, "Admin UI disabled (set ADMIN_PASSWORD)")
    if await _session_from_request(request) is not None:
        return RedirectResponse(_PANEL, status_code=303)
    from .adminui import render_login_page
    return HTMLResponse(render_login_page())


@app.post(_LOGIN_URL, include_in_schema=False)
async def login_submit(request: Request) -> Response:
    if not settings.admin_password:
        raise HTTPException(404, "Admin UI disabled (set ADMIN_PASSWORD)")
    ip = _client_ip(request)
    left = _login_throttled(ip) if ip in _login_fails else 0.0
    if left > 0:
        raise HTTPException(429, "Too many failed login attempts; try again later",
                            headers={"Retry-After": str(max(1, math.ceil(left)))})
    form = await _read_form(request)
    if not _check_password(form.get("password", "")):
        _login_fails.setdefault(ip, []).append(time.time())
        from .adminui import render_login_page
        return HTMLResponse(render_login_page("Неверный пароль"), status_code=401)
    _login_fails.pop(ip, None)
    session = await _new_session()
    resp = RedirectResponse(_PANEL, status_code=303)
    _set_session_cookie(resp, session["sid"], request)
    return resp


@app.post(f"{_PANEL}/logout", include_in_schema=False)
async def admin_ui_logout(request: Request, session: dict = Depends(require_panel)) -> Response:
    form = await _read_form(request)
    _check_csrf(form, session)
    await _sessions.delete(session["sid"])
    resp = RedirectResponse(_LOGIN_URL, status_code=303)
    resp.delete_cookie(_SESSION_COOKIE, path="/")
    return resp


# --- routes: pages + data (delegating to the adminui renderers) -------------


@app.get(_PANEL, include_in_schema=False)
async def admin_ui(msg: str = "", session: dict = Depends(require_panel)) -> HTMLResponse:
    """Page "Proxies" (default): data snapshot rendered by ``render_proxies_page``."""
    from .adminui import render_proxies_page
    return HTMLResponse(render_proxies_page(
        **await _api_get("/internal/proxies-data"), csrf=session["csrf"], msg=msg,
    ))


@app.get(f"{_PANEL}/keys", include_in_schema=False)
async def admin_ui_keys(msg: str = "", session: dict = Depends(require_panel)) -> HTMLResponse:
    """Page "Keys": data snapshot rendered by ``render_keys_page``."""
    from .adminui import render_keys_page
    return HTMLResponse(render_keys_page(
        **await _api_get("/internal/keys-data"), csrf=session["csrf"], msg=msg,
    ))


@app.get(f"{_PANEL}/data", include_in_schema=False)
async def admin_ui_data(session: dict = Depends(require_panel)) -> dict:
    """HTML fragments for the "Proxies" page (auto-refresh every 5s)."""
    from .adminui import render_proxies_fragments
    return render_proxies_fragments(**await _api_get("/internal/proxies-data"), csrf=session["csrf"])


@app.get(f"{_PANEL}/keys/data", include_in_schema=False)
async def admin_ui_keys_data(session: dict = Depends(require_panel)) -> dict:
    """HTML fragments for the "Keys" page (auto-refresh every 5s)."""
    from .adminui import render_keys_fragments
    return render_keys_fragments(**await _api_get("/internal/keys-data"), csrf=session["csrf"])


# --- routes: mutations (form -> gateway internal API -> redirect) -----------


@app.post(f"{_PANEL}/proxies/set", include_in_schema=False)
async def admin_ui_set_proxy(request: Request, session: dict = Depends(require_panel)) -> RedirectResponse:
    form = await _read_form(request)
    _check_csrf(form, session)
    res = await _api_post("/internal/proxies/set", {
        "proxy": form.get("proxy", ""),
        "enabled": form.get("enabled") == "1",
    })
    return _panel_redirect(res.get("message", "Готово"))


@app.post(f"{_PANEL}/proxies/delete", include_in_schema=False)
async def admin_ui_delete_proxy(request: Request, session: dict = Depends(require_panel)) -> RedirectResponse:
    form = await _read_form(request)
    _check_csrf(form, session)
    res = await _api_post("/internal/proxies/delete", {"proxy": form.get("proxy", "")})
    return _panel_redirect(res.get("message", "Прокси удалён"))


@app.post(f"{_PANEL}/keys/add", include_in_schema=False)
async def admin_ui_add_key(request: Request, session: dict = Depends(require_panel)) -> RedirectResponse:
    form = await _read_form(request)
    _check_csrf(form, session)
    res = await _api_post("/internal/keys/add", {
        "key": form.get("key", ""), "key_id": form.get("key_id", ""),
        "name": form.get("name", ""), "admin": form.get("admin") == "1",
        "rpm": form.get("rpm") or None, "tpm": form.get("tpm") or None,
        "models": form.get("models", ""),
    })
    return _panel_redirect(res.get("message", "Готово"), "keys")


@app.post(f"{_PANEL}/keys/set", include_in_schema=False)
async def admin_ui_set_key(request: Request, session: dict = Depends(require_panel)) -> RedirectResponse:
    form = await _read_form(request)
    _check_csrf(form, session)
    res = await _api_post("/internal/keys/set", {
        "key_hash": form.get("key_hash", ""),
        "enabled": form.get("enabled") == "1",
    })
    return _panel_redirect(res.get("message", "Готово"), "keys")


@app.post(f"{_PANEL}/keys/delete", include_in_schema=False)
async def admin_ui_delete_key(request: Request, session: dict = Depends(require_panel)) -> RedirectResponse:
    form = await _read_form(request)
    _check_csrf(form, session)
    res = await _api_post("/internal/keys/delete", {"key_hash": form.get("key_hash", "")})
    return _panel_redirect(res.get("message", "Ключ удалён"), "keys")


@app.get("/health", include_in_schema=False)
async def health() -> dict:
    return {"status": "ok", "sessions": await _sessions.count()}
