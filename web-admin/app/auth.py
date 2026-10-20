"""Authentication, sessions and CSRF for the OpenRoute admin API.

The browser only receives an HttpOnly session cookie; the session itself lives
in Redis (``sessions.RedisSessionStore``). Every mutating request must also
carry the per-session CSRF token in the ``X-CSRF-Token`` header. There are no
signatures or claims in the cookie — validity comes solely from the server-side
store, so a forged cookie can never authenticate.
"""

from __future__ import annotations

import hmac
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request, Response

from .config import Settings

SESSION_COOKIE = "openroute_session"
LOGIN_FAILS_MAX = 10
LOGIN_FAILS_WINDOW = 60.0


def client_ip(request: Request) -> str:
    """Real client IP as seen by edge (the only public ingress).

    ``web-admin`` is never exposed directly, so ``X-Real-IP`` set by edge is
    trustworthy here. Fallback to the socket peer for local development.
    """
    forwarded = request.headers.get("x-real-ip", "").strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def is_https(request: Request) -> bool:
    """Whether the original browser request was HTTPS (edge sets the header)."""
    proto = request.headers.get("x-forwarded-proto", "").strip().lower()
    if proto:
        return proto.split(",", 1)[0].strip() == "https"
    return request.url.scheme == "https"


def check_password(password: str, settings: Settings) -> bool:
    return hmac.compare_digest(
        password.encode("utf-8"), settings.admin_password.encode("utf-8")
    )


async def new_session(settings: Settings, store: Any) -> dict:
    """Create a server-side session and return ``{sid, csrf, expires}``."""
    sid = secrets.token_urlsafe(32)
    session = {
        "csrf": secrets.token_urlsafe(24),
        "expires": time.time() + settings.admin_session_ttl_seconds,
    }
    await store.put(sid, session, settings.admin_session_ttl_seconds)
    return {"sid": sid, **session}


async def session_from_request(request: Request, settings: Settings, store: Any) -> dict | None:
    """Load and validate the session referenced by the cookie."""
    sid = request.cookies.get(SESSION_COOKIE, "")
    if not sid:
        return None
    session = await store.get(sid)
    if session is None:
        return None
    if time.time() > float(session.get("expires", 0)):
        await store.delete(sid)
        return None
    return {"sid": sid, **session}


def set_session_cookie(resp: Response, sid: str, request: Request, settings: Settings) -> None:
    resp.set_cookie(
        SESSION_COOKIE,
        sid,
        max_age=settings.admin_session_ttl_seconds,
        httponly=True,
        samesite="strict",
        secure=is_https(request),
        path="/",
    )


def clear_session_cookie(resp: Response, request: Request, settings: Settings) -> None:
    resp.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="strict",
        secure=is_https(request),
    )


def check_csrf(request: Request, session: dict) -> None:
    """Constant-time comparison of the per-session CSRF token."""
    got = request.headers.get("x-csrf-token", "")
    expected = str(session.get("csrf", ""))
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
