"""Typed HTTP client for the private auth-service.

``web-admin`` never talks to Redis. It calls auth-service business operations
over the internal ``openroute-auth-net`` network and maps their results to the
existing browser-facing HTTP contract (401/403/404/429/503).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import httpx

from .config import Settings

logger = logging.getLogger("openroute-web-admin.auth")


class AuthError(Exception):
    """Base class for auth-service failures."""


class AuthUnavailable(AuthError):
    """auth-service is unreachable, timed out, or returned an unexpected error."""


class PanelDisabled(AuthError):
    """ADMIN_PASSWORD is empty; the whole panel must answer 404."""


class InvalidCredentials(AuthError):
    """The submitted admin password is wrong."""


class LoginThrottled(AuthError):
    """Too many failed login attempts from this client IP."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("too many login attempts")
        self.retry_after = retry_after


class SessionInvalid(AuthError):
    """The session cookie is missing, unknown or expired."""


class CsrfInvalid(AuthError):
    """The X-CSRF-Token does not match the server-side session."""


@dataclass(frozen=True)
class AuthSession:
    """A validated server-side session returned by auth-service."""

    sid: str
    csrf: str
    expires_at: float
    max_age: int | None = None


def _detail(response: httpx.Response, default: str) -> str:
    """Extract a safe short error detail without echoing response bodies."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()[:200]
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()[:200]
    return default


def _retry_after(response: httpx.Response) -> int:
    raw = response.headers.get("retry-after", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return 60
    return max(1, math.ceil(value))


def _session(response: httpx.Response) -> AuthSession:
    try:
        payload = response.json()
    except ValueError as exc:
        raise AuthUnavailable("Сервис авторизации вернул некорректный ответ") from exc
    if not isinstance(payload, dict):
        raise AuthUnavailable("Сервис авторизации вернул некорректный ответ")
    sid = payload.get("sid")
    csrf = payload.get("csrf")
    expires_at = payload.get("expires_at")
    max_age = payload.get("max_age")
    if not isinstance(sid, str) or not sid or not isinstance(csrf, str) or not csrf:
        raise AuthUnavailable("Сервис авторизации вернул некорректный ответ")
    try:
        expires = float(expires_at)
    except (TypeError, ValueError) as exc:
        raise AuthUnavailable("Сервис авторизации вернул некорректный ответ") from exc
    if max_age is not None and (
        not isinstance(max_age, int) or isinstance(max_age, bool)
    ):
        raise AuthUnavailable("Сервис авторизации вернул некорректный ответ")
    return AuthSession(sid=sid, csrf=csrf, expires_at=expires, max_age=max_age)


class AuthClient:
    """Async business client for auth-service."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=settings.auth_service_url,
            timeout=settings.auth_timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning("auth-service request failed: %s", type(exc).__name__)
            raise AuthUnavailable("Сервис авторизации недоступен") from exc
        if response.status_code >= 500:
            raise AuthUnavailable(
                _detail(response, "Сервис авторизации недоступен")
            )
        return response

    async def status(self) -> bool:
        response = await self._request("GET", "/auth/status")
        if response.status_code != 200:
            raise AuthUnavailable(_detail(response, "Сервис авторизации недоступен"))
        try:
            payload = response.json()
        except ValueError as exc:
            raise AuthUnavailable("Сервис авторизации вернул некорректный ответ") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            raise AuthUnavailable("Сервис авторизации вернул некорректный ответ")
        return payload["enabled"]

    async def health(self) -> int:
        response = await self._request("GET", "/health")
        if response.status_code != 200:
            raise AuthUnavailable(_detail(response, "Сервис авторизации недоступен"))
        try:
            payload = response.json()
        except ValueError as exc:
            raise AuthUnavailable("Сервис авторизации вернул некорректный ответ") from exc
        if not isinstance(payload, dict):
            raise AuthUnavailable("Сервис авторизации вернул некорректный ответ")
        sessions = payload.get("sessions", 0)
        if not isinstance(sessions, int) or isinstance(sessions, bool):
            raise AuthUnavailable("Сервис авторизации вернул некорректный ответ")
        return sessions

    async def login(self, password: str, client_ip: str) -> AuthSession:
        response = await self._request(
            "POST",
            "/auth/login",
            json={"password": password, "client_ip": client_ip},
        )
        if response.status_code == 404:
            raise PanelDisabled()
        if response.status_code == 401:
            raise InvalidCredentials()
        if response.status_code == 429:
            raise LoginThrottled(_retry_after(response))
        if response.status_code != 200:
            raise AuthUnavailable(_detail(response, "Сервис авторизации недоступен"))
        return _session(response)

    async def validate(
        self,
        sid: str,
        csrf: str = "",
        require_csrf: bool = False,
    ) -> AuthSession:
        response = await self._request(
            "POST",
            "/auth/validate",
            json={"sid": sid, "csrf": csrf, "require_csrf": require_csrf},
        )
        if response.status_code == 404:
            raise PanelDisabled()
        if response.status_code == 401:
            raise SessionInvalid()
        if response.status_code == 403:
            raise CsrfInvalid()
        if response.status_code != 200:
            raise AuthUnavailable(_detail(response, "Сервис авторизации недоступен"))
        return _session(response)

    async def logout(self, sid: str, csrf: str) -> None:
        response = await self._request(
            "POST",
            "/auth/logout",
            json={"sid": sid, "csrf": csrf},
        )
        if response.status_code == 404:
            raise PanelDisabled()
        if response.status_code == 401:
            raise SessionInvalid()
        if response.status_code == 403:
            raise CsrfInvalid()
        if response.status_code != 204:
            raise AuthUnavailable(_detail(response, "Сервис авторизации недоступен"))


__all__ = [
    "AuthClient",
    "AuthError",
    "AuthSession",
    "AuthUnavailable",
    "CsrfInvalid",
    "InvalidCredentials",
    "LoginThrottled",
    "PanelDisabled",
    "SessionInvalid",
]
