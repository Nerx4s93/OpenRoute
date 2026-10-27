"""Unit tests for web-admin's auth-service client (no network, no Redis).

The client is exercised over ``httpx.MockTransport``; status -> exception
mapping and response parsing are checked without a live auth-service.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

TESTS_DIR = Path(__file__).resolve().parent
WEB_ADMIN_DIR = TESTS_DIR.parent / "web-admin"


def _load_package(name: str, pkg_dir: Path):
    spec = importlib.util.spec_from_file_location(
        name,
        str(pkg_dir / "__init__.py"),
        submodule_search_locations=[str(pkg_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_package("adminservice", WEB_ADMIN_DIR / "app")

from adminservice.auth_client import (  # noqa: E402
    AuthClient,
    AuthUnavailable,
    CsrfInvalid,
    InvalidCredentials,
    LoginThrottled,
    PanelDisabled,
    SessionInvalid,
)
from adminservice.config import Settings  # noqa: E402

BASE = "http://auth.test"


def make_client(handler) -> AuthClient:
    settings = Settings(
        auth_service_url=BASE,
        auth_timeout_seconds=2.0,
        internal_api_token="test-token",
    )
    http = httpx.AsyncClient(
        base_url=BASE,
        transport=httpx.MockTransport(handler),
    )
    return AuthClient(settings, http)


def session_payload(**over) -> dict:
    payload = {
        "sid": "sid-1",
        "csrf": "csrf-1",
        "expires_at": 1_700_000_000.0,
        "max_age": 43200,
    }
    payload.update(over)
    return payload


@pytest.mark.asyncio
async def test_status_and_health():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/status":
            return httpx.Response(200, json={"enabled": True})
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "sessions": 3})
        raise AssertionError(request.url.path)

    client = make_client(handler)
    assert await client.status() is True
    assert await client.health() == 3
    await client.aclose()


@pytest.mark.asyncio
async def test_login_success_parses_session():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/login"
        return httpx.Response(200, json=session_payload())

    client = make_client(handler)
    session = await client.login("pw", "203.0.113.1")
    assert session.sid == "sid-1"
    assert session.csrf == "csrf-1"
    assert session.expires_at == 1_700_000_000.0
    assert session.max_age == 43200


@pytest.mark.asyncio
async def test_login_error_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/login":
            password = request.content.decode()
            if "wrong" in password:
                return httpx.Response(401, json={"detail": "Неверный пароль"})
            if "disabled" in password:
                return httpx.Response(404, json={"detail": "disabled"})
            if "throttle" in password:
                return httpx.Response(429, headers={"Retry-After": "42"})
            return httpx.Response(400, json={"detail": "bad"})
        raise AssertionError(path)

    client = make_client(handler)

    with pytest.raises(InvalidCredentials):
        await client.login("wrong", "ip")
    with pytest.raises(PanelDisabled):
        await client.login("disabled", "ip")
    with pytest.raises(LoginThrottled) as exc_info:
        await client.login("throttle", "ip")
    assert exc_info.value.retry_after == 42
    with pytest.raises(AuthUnavailable):
        await client.login("other", "ip")


@pytest.mark.asyncio
async def test_validate_and_logout_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/validate":
            body = request.content.decode()
            if "missing" in body:
                return httpx.Response(401)
            if "forged" in body:
                return httpx.Response(403)
            if "disabled" in body:
                return httpx.Response(404)
            return httpx.Response(200, json=session_payload())
        if request.url.path == "/auth/logout":
            body = request.content.decode()
            if "missing" in body:
                return httpx.Response(401)
            if "forged" in body:
                return httpx.Response(403)
            return httpx.Response(204)
        raise AssertionError(request.url.path)

    client = make_client(handler)

    session = await client.validate("sid-1", "csrf-1", require_csrf=True)
    assert session.sid == "sid-1"

    with pytest.raises(SessionInvalid):
        await client.validate("missing")
    with pytest.raises(CsrfInvalid):
        await client.validate("sid-1", "forged", require_csrf=True)
    with pytest.raises(PanelDisabled):
        await client.validate("disabled")

    await client.logout("sid-1", "csrf-1")
    with pytest.raises(SessionInvalid):
        await client.logout("missing", "csrf-1")
    with pytest.raises(CsrfInvalid):
        await client.logout("sid-1", "forged")


@pytest.mark.asyncio
async def test_transport_and_server_errors_are_503():
    def transport_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    client = make_client(transport_error)
    with pytest.raises(AuthUnavailable) as exc_info:
        await client.health()
    assert "недоступен" in str(exc_info.value)
    await client.aclose()

    def redis_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503, json={"detail": "Хранилище сессий недоступно"}
        )

    client = make_client(redis_error)
    with pytest.raises(AuthUnavailable) as exc_info:
        await client.health()
    assert str(exc_info.value) == "Хранилище сессий недоступно"
    await client.aclose()


@pytest.mark.asyncio
async def test_invalid_responses_fail_closed():
    for payload in (
        {"enabled": "yes"},
        [],
    ):
        def handler(request: httpx.Request, payload=payload) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = make_client(handler)
        with pytest.raises(AuthUnavailable):
            await client.status()
        await client.aclose()

    def bad_login(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sid": "x"})

    client = make_client(bad_login)
    with pytest.raises(AuthUnavailable):
        await client.login("pw", "ip")
    await client.aclose()


@pytest.mark.asyncio
async def test_login_password_is_not_echoed_in_error():
    secret = "super-secret-password"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Неверный пароль"})

    client = make_client(handler)
    with pytest.raises(InvalidCredentials) as exc_info:
        await client.login(secret, "ip")
    assert secret not in str(exc_info.value)
    await client.aclose()
