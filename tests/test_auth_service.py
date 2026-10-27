"""Tests for the private auth-service (password, sessions, CSRF, throttle).

Hermetic: the real auth-service application runs on ``TestClient`` with an
in-memory session store, so no Redis or network is needed.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TESTS_DIR = Path(__file__).resolve().parent
AUTH_DIR = TESTS_DIR.parent / "auth-service"


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


_load_package("authservice", AUTH_DIR / "app")

from authservice.config import Settings  # noqa: E402
from authservice.main import create_app  # noqa: E402
from authservice.sessions import SessionUnavailable  # noqa: E402

PW = "pw-secret"
TTL = 43200


class FakeStore:
    """In-memory twin of ``RedisSessionStore`` (no Redis in tests)."""

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}
        self.unavailable = False
        self.puts = 0
        self.gets = 0
        self.deletes = 0
        self.counts = 0

    def _check(self) -> None:
        if self.unavailable:
            raise SessionUnavailable("redis is down")

    async def put(self, sid, session, ttl_seconds):
        self._check()
        self.puts += 1
        self.data[sid] = dict(session)

    async def get(self, sid):
        self._check()
        self.gets += 1
        return dict(self.data[sid]) if sid in self.data else None

    async def delete(self, sid):
        self._check()
        self.deletes += 1
        self.data.pop(sid, None)

    async def count(self):
        self._check()
        self.counts += 1
        return len(self.data)

    async def aclose(self):
        pass


@pytest.fixture
def make_service():
    clients: list[TestClient] = []

    def _make(password: str = PW, ttl: int = TTL, unavailable: bool = False):
        store = FakeStore()
        store.unavailable = unavailable
        settings = Settings(
            admin_password=password,
            admin_session_ttl_seconds=ttl,
            redis_url="redis://redis.test.invalid:6379/0",
        )
        app = create_app(settings, store)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client, store

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


def login(client: TestClient, password: str = PW, ip: str = "203.0.113.10"):
    return client.post(
        "/auth/login", json={"password": password, "client_ip": ip}
    )


def test_health_disabled_needs_no_redis(make_service):
    client, store = make_service(password="")
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "enabled": False, "sessions": 0}
    assert store.counts == 0


def test_root_is_not_served(make_service):
    client, _ = make_service()
    assert client.get("/").status_code == 404


def test_health_reports_sessions(make_service):
    client, store = make_service()
    assert client.get("/health").json()["sessions"] == 0

    response = login(client)
    assert response.status_code == 200
    assert client.get("/health").json() == {
        "status": "ok",
        "enabled": True,
        "sessions": 1,
    }
    assert store.data


def test_status_is_enabled_flag_and_does_not_touch_redis(make_service):
    client, store = make_service()
    assert client.get("/auth/status").json() == {"enabled": True}

    store.unavailable = True
    assert client.get("/auth/status").json() == {"enabled": True}


def test_panel_disabled_returns_404(make_service):
    client, store = make_service(password="")
    assert client.get("/auth/status").json() == {"enabled": False}

    response = login(client, "anything")
    assert response.status_code == 404
    assert "ADMIN_PASSWORD" in response.json()["detail"]

    for path in ("/auth/validate", "/auth/logout"):
        response = client.post(path, json={})
        assert response.status_code == 404
    assert store.data == {}


def test_login_wrong_password(make_service):
    client, store = make_service()
    response = login(client, "wrong")
    assert response.status_code == 401
    assert response.json()["detail"] == "Неверный пароль"
    assert store.data == {}


def test_login_creates_session_and_validate_returns_it(make_service):
    client, store = make_service()
    response = login(client)
    assert response.status_code == 200

    body = response.json()
    assert body["sid"] and body["csrf"]
    assert body["expires_at"] > 0
    assert body["max_age"] == TTL
    assert store.data[body["sid"]]["csrf"] == body["csrf"]

    validated = client.post("/auth/validate", json={"sid": body["sid"]})
    assert validated.status_code == 200
    assert validated.json()["sid"] == body["sid"]
    assert validated.json()["csrf"] == body["csrf"]
    assert validated.json()["expires_at"] == pytest.approx(body["expires_at"])


def test_validate_requires_csrf_only_when_asked(make_service):
    client, _ = make_service()
    body = login(client).json()

    assert client.post(
        "/auth/validate", json={"sid": body["sid"], "require_csrf": False}
    ).status_code == 200

    wrong = client.post(
        "/auth/validate",
        json={"sid": body["sid"], "csrf": "forged", "require_csrf": True},
    )
    assert wrong.status_code == 403
    assert wrong.json()["detail"] == "Неверный CSRF-токен"

    right = client.post(
        "/auth/validate",
        json={"sid": body["sid"], "csrf": body["csrf"], "require_csrf": True},
    )
    assert right.status_code == 200


def test_expired_session_is_rejected_and_deleted(make_service):
    client, store = make_service()
    body = login(client).json()
    store.data[body["sid"]]["expires"] = 0.0

    response = client.post("/auth/validate", json={"sid": body["sid"]})
    assert response.status_code == 401
    assert body["sid"] not in store.data


def test_logout_requires_csrf_and_deletes_session(make_service):
    client, store = make_service()
    body = login(client).json()

    wrong = client.post(
        "/auth/logout", json={"sid": body["sid"], "csrf": "forged"}
    )
    assert wrong.status_code == 403
    assert body["sid"] in store.data

    response = client.post(
        "/auth/logout", json={"sid": body["sid"], "csrf": body["csrf"]}
    )
    assert response.status_code == 204
    assert store.data == {}
    assert client.post(
        "/auth/validate", json={"sid": body["sid"]}
    ).status_code == 401


def test_login_bruteforce_is_limited_per_ip(make_service):
    client, _ = make_service()
    for _ in range(10):
        assert login(client, "wrong", ip="203.0.113.10").status_code == 401

    locked = login(client, PW, ip="203.0.113.10")
    assert locked.status_code == 429
    assert "retry-after" in {key.lower() for key in locked.headers}

    # A different client IP is not affected by the first IP's failures.
    assert login(client, PW, ip="203.0.113.11").status_code == 200


def test_login_lockout_resets_on_success(make_service):
    client, _ = make_service()
    for _ in range(3):
        assert login(client, "wrong").status_code == 401
    assert login(client).status_code == 200
    assert login(client, "wrong").status_code == 401


def test_redis_unavailable_is_503_and_not_fail_open(make_service):
    client, store = make_service(unavailable=True)

    assert client.get("/health").status_code == 503
    assert client.get("/auth/status").json() == {"enabled": True}
    response = login(client)
    assert response.status_code == 503
    assert response.json()["detail"] == "Хранилище сессий недоступно"
    # An empty sid is rejected before touching Redis.
    assert client.post("/auth/validate", json={}).status_code == 401
    assert store.data == {}
