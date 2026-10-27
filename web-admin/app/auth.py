"""Browser-side auth helpers for the OpenRoute admin API.

The auth-service owns password verification, sessions and CSRF. This module
only handles the HttpOnly session cookie and the real client IP forwarded by
edge; there is no Redis client here.
"""

from __future__ import annotations

from fastapi import Request, Response

SESSION_COOKIE = "openroute_session"


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


def set_session_cookie(
    resp: Response, sid: str, max_age: int, request: Request
) -> None:
    resp.set_cookie(
        SESSION_COOKIE,
        sid,
        max_age=max_age,
        httponly=True,
        samesite="strict",
        secure=is_https(request),
        path="/",
    )


def clear_session_cookie(resp: Response, request: Request) -> None:
    resp.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="strict",
        secure=is_https(request),
    )
