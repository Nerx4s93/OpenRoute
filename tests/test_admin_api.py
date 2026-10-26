"""Tests for the OpenRoute admin JSON API (``web-admin`` — ``/api/admin/*``).

Hermetic by construction: the service is driven by ``TestClient`` and its
gateway client is pointed at an in-process fake gateway (``FastAPI`` +
``httpx.ASGITransport``), so no Redis, no PostgreSQL and no network are needed.

Covered: login (password only), server sessions in an HttpOnly cookie, CSRF on
mutations, brute-force lock-out, session expiry, logout, proxy enable/disable,
proxy credentials not leaking to the browser, client-key add/rotate/revoke/delete,
per-key stats from the request log, period forwarding, and the JSON SSE stream.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import secrets
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

# --- load the web-admin ``app`` package under a unique name -------------
TESTS_DIR = Path(__file__).resolve().parent
WEB_ADMIN_DIR = TESTS_DIR.parent / "web-admin"


def _load_package(name: str, pkg_dir: Path):
    spec = importlib.util.spec_from_file_location(
        name, str(pkg_dir / "__init__.py"),
        submodule_search_locations=[str(pkg_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_package("adminservice", WEB_ADMIN_DIR / "app")


def _load_admin_main():
    spec = importlib.util.spec_from_file_location(
        "adminservice.main", str(WEB_ADMIN_DIR / "app" / "main.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["adminservice.main"] = mod
    spec.loader.exec_module(mod)
    return mod


admin_main = _load_admin_main()

PW = "pw-secret"
DEFAULT_KEY = "test-key"

# Fixed clock for the fake `/internal/stats-data` (2026-01-01T00:00:00Z).
STATS_SERVER_TIME = 1_767_225_600.0
STATS_DATES = ["2025-12-30", "2025-12-31", "2026-01-01"]


def _stats_payload() -> dict:
    """Default fake `/internal/stats-data` payload: three days, no proxies."""
    zeros = [0, 0, 0]
    return {
        "days": len(STATS_DATES),
        "server_time": STATS_SERVER_TIME,
        "dates": list(STATS_DATES),
        "totals": {
            "tokens_total": 0, "requests_total": 0, "errors_total": 0,
        },
        "proxies": [],
        "series_total": {
            "tokens": list(zeros), "requests": list(zeros), "errors": list(zeros),
        },
        "series_by_proxy": {},
    }


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def mask_proxy(url: str) -> str:
    """host:port with the credentials stripped (the display/usage key)."""
    s = url.split("://", 1)[-1]
    return s.split("@", 1)[1] if "@" in s else s


# --- in-memory fake gateway (implements the internal API contract) ----------


class FakeState:
    """In-memory proxy/key stores + usage counters for the fake gateway."""

    def __init__(self) -> None:
        self.proxies: dict[str, bool] = {}  # full URL -> enabled (manual flag)
        self.quota: dict[str, float] = {}  # full URL -> seconds left
        self.banned: dict[str, float] = {}  # full URL -> seconds left
        self.permanent_bans: set[str] = set()  # full URL -> forever
        self.last_period = "all"  # last ?period= forwarded to the fake gateway
        self.refreshes = 0  # calls to the forced proxy refresh
        self.refresh_error = False  # make /internal/proxies/refresh fail
        self.keys: dict[str, dict] = {}  # key_hash -> row
        self.usage_by_proxy: dict[str, dict] = {}  # masked -> stats
        self.usage_by_proxy_lifetime: dict[str, dict] = {}  # masked -> lifetime
        self.usage_by_key: dict[str, dict] = {}  # key_id -> stats
        self.recent: list[dict] = []
        self.stats_payload: dict = _stats_payload()  # /internal/stats-data
        self.stats_days: int | None = None  # last ?days= forwarded
        self.stats_period: str | None = None  # last ?period= forwarded
        self.px6_buy_calls: list[dict] = []  # /internal/px6/buy payloads


def _stats(d: dict[str, dict]) -> dict:
    return {
        "requests": sum(v["requests"] for v in d.values()),
        "errors": sum(v["errors"] for v in d.values()),
        "tokens": sum(v["tokens"] for v in d.values()),
        "avg_latency_ms": 0,
    }


def _limit(raw, field):
    if raw is None or raw == "":
        return None, None
    if not isinstance(raw, int) or isinstance(raw, bool):
        try:
            v = int(str(raw))
        except ValueError:
            return None, f"Invalid {field.upper()}: {raw}"
    else:
        v = raw
    if v < 0:
        return None, f"{field.upper()} cannot be negative"
    return v, None


def build_fake_gateway(state: FakeState, token: str) -> FastAPI:
    """A tiny FastAPI app exposing the gateway's ``/internal/*`` contract."""
    app = FastAPI()

    @app.exception_handler(HTTPException)
    async def _openai_envelope(request: Request, exc: HTTPException) -> JSONResponse:
        """Mirror the REAL gateway: ALL HTTP errors use the OpenAI envelope."""
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": exc.detail, "type": "invalid_request_error",
                               "code": exc.status_code}},
            headers=exc.headers,
        )

    def require(req: Request) -> None:
        if not token:
            raise HTTPException(404, "Internal API disabled (set INTERNAL_API_TOKEN)")
        got = req.headers.get("x-internal-token", "")
        if not hmac.compare_digest(got.encode("utf-8"), token.encode("utf-8")):
            raise HTTPException(403, "Invalid internal token")

    def proxy_status() -> dict:
        rows = []
        for url, enabled in state.proxies.items():
            quota = state.quota.get(url, 0.0)
            banned = state.banned.get(url, 0.0)
            permanent = url in state.permanent_bans
            rows.append({
                "proxy": mask_proxy(url),
                "url": f"http://{mask_proxy(url)}",
                "real_proxy": url,
                "enabled": enabled,
                "manual": not enabled,
                "requests": 0,
                "errors": 0,
                "consecutive_errors": 0,
                "cooldown_seconds_left": 0.0,
                "rpm_window": 0,
                "rpm_capped": False,
                "egress_group": mask_proxy(url),
                "quota_seconds_left": quota,
                "banned": permanent,
                "banned_seconds_left": 0.0 if permanent else banned,
                "last_error": None,
            })
        ready = sum(
            1 for p in rows
            if p["enabled"] and not p["quota_seconds_left"]
            and not p["banned"] and not p["banned_seconds_left"]
        )
        return {
            "proxies_total": len(rows),
            "proxies_enabled": sum(1 for p in rows if p["enabled"]),
            "proxies_ready": ready,
            "proxies_manually_disabled": sum(1 for p in rows if p["manual"]),
            "proxies_cooling": 0,
            "proxies_rpm_capped": 0,
            "egress_groups_total": len(rows),
            "egress_groups_ready": ready,
            "egress_groups_quota_parked": sum(
                1 for p in rows if p["quota_seconds_left"] > 0),
            "egress_groups_banned": sum(
                1 for p in rows if p["banned"] or p["banned_seconds_left"] > 0),
            "last_proxies_fetch": None,
            "proxies": rows,
        }

    @app.get("/internal/proxies-data")
    async def proxies_data(req: Request) -> dict:
        require(req)
        state.last_period = req.query_params.get("period", "all")
        return {
            "status": proxy_status(),
            "usage": {"total": _stats(state.usage_by_proxy)},
            "usage_by_proxy": state.usage_by_proxy,
            "usage_by_proxy_lifetime": state.usage_by_proxy_lifetime,
            "recent": state.recent,
        }

    @app.get("/internal/stats-data")
    async def stats_data(req: Request) -> dict:
        require(req)
        state.stats_period = req.query_params.get("period")
        if state.stats_period is None:
            state.stats_days = int(req.query_params.get("days", "30"))
        else:
            state.stats_days = None
        payload = dict(state.stats_payload)
        payload.setdefault("server_time", STATS_SERVER_TIME)
        payload["days"] = (
            state.stats_days
            if state.stats_days is not None
            else len(payload.get("dates") or [])
        )
        return payload

    @app.post("/internal/proxies/set")
    async def proxies_set(req: Request) -> dict:
        require(req)
        data = await req.json()
        url = str(data.get("proxy", ""))
        enabled = bool(data.get("enabled"))
        if url in state.proxies:
            state.proxies[url] = enabled
            return {"ok": True, "message": "Proxy enabled" if enabled else "Proxy disabled"}
        raise HTTPException(404, "Proxy not found")

    @app.post("/internal/proxies/refresh")
    async def proxies_refresh(req: Request) -> dict:
        require(req)
        state.refreshes += 1
        if state.refresh_error:
            raise HTTPException(502, "Proxy refresh failed")
        return {"ok": True, "message": "Proxies refreshed"}

    @app.get("/internal/px6/account")
    async def px6_account(req: Request) -> dict:
        require(req)
        return {"user_id": "px6-user", "balance": 250.5, "currency": "RUB"}

    @app.get("/internal/px6/countries")
    async def px6_countries(req: Request) -> dict:
        require(req)
        version = int(req.query_params.get("version", "6"))
        return {"version": version, "countries": ["de", "ru"]}

    @app.get("/internal/px6/price")
    async def px6_price(req: Request) -> dict:
        require(req)
        version = int(req.query_params.get("version", "6"))
        count = int(req.query_params.get("count", "1"))
        period = int(req.query_params.get("period", "30"))
        return {
            "user_id": "px6-user",
            "balance": 250.5,
            "currency": "RUB",
            "price": round(12.5 * count * (period / 30), 2),
            "price_single": round(12.5 * (period / 30), 2),
            "period": period,
            "count": count,
            "version": version,
        }

    @app.post("/internal/px6/buy")
    async def px6_buy(req: Request) -> dict:
        require(req)
        data = await req.json()
        state.px6_buy_calls.append(data)
        count = int(data.get("count", 1))
        period = int(data.get("period", 30))
        return {
            "ok": True,
            "refresh_ok": True,
            "message": "Proxies purchased and list refreshed",
            "order_id": "order-1",
            "count": count,
            "price": round(12.5 * count * (period / 30), 2),
            "price_single": round(12.5 * (period / 30), 2),
            "period": period,
            "country": str(data.get("country", "ru")),
            "version": int(data.get("version", 6)),
            "currency": "RUB",
            "balance": 200.0,
        }

    @app.get("/internal/keys-data")
    async def keys_data(req: Request) -> dict:
        require(req)
        state.last_period = req.query_params.get("period", "all")
        usage = {"total": _stats(state.usage_by_key)}
        usage.update(state.usage_by_key)
        return {"keys": [dict(v) for v in state.keys.values()], "usage": usage}

    @app.post("/internal/keys/add")
    async def keys_add(req: Request) -> dict:
        require(req)
        data = await req.json()
        rpm, err = _limit(data.get("rpm"), "rpm")
        if err:
            return {"ok": False, "message": err}
        tpm, err = _limit(data.get("tpm"), "tpm")
        if err:
            return {"ok": False, "message": err}
        raw_models = data.get("models")
        if isinstance(raw_models, list):
            models = [str(m).strip() for m in raw_models if str(m).strip()] or None
        elif isinstance(raw_models, str):
            models = [m.strip() for m in raw_models.split(",") if m.strip()] or None
        else:
            models = None
        key_id = str(data.get("key_id", "")).strip()
        if not key_id:
            existing = {v["key_id"] for v in state.keys.values()}
            i = 1
            while f"k{i}" in existing:
                i += 1
            key_id = f"k{i}"
        plaintext = "openroute-" + secrets.token_hex(24)
        kh = hash_key(plaintext)
        state.keys[kh] = {
            "key_hash": kh, "key_id": key_id,
            "rpm": rpm, "tpm": tpm, "models": models, "enabled": True,
        }
        return {
            "ok": True,
            "message": f"Key {key_id} added",
            "key": plaintext,
            "key_id": key_id,
        }

    @app.post("/internal/keys/rotate")
    async def keys_rotate(req: Request) -> dict:
        require(req)
        data = await req.json()
        old_hash = str(data.get("key_hash", ""))
        row = state.keys.get(old_hash)
        if row is None:
            raise HTTPException(404, "Key not found")
        plaintext = "openroute-" + secrets.token_hex(24)
        new_hash = hash_key(plaintext)
        del state.keys[old_hash]
        row = dict(row, key_hash=new_hash)
        state.keys[new_hash] = row
        return {
            "ok": True,
            "message": "Key rotated",
            "key": plaintext,
            "key_id": row["key_id"],
        }

    @app.post("/internal/keys/set")
    async def keys_set(req: Request) -> dict:
        require(req)
        data = await req.json()
        kh = str(data.get("key_hash", ""))
        enabled = bool(data.get("enabled"))
        if kh in state.keys:
            state.keys[kh]["enabled"] = enabled
            return {"ok": True, "message": "Key enabled" if enabled else "Key revoked (401)"}
        raise HTTPException(404, "Key not found")

    @app.post("/internal/keys/delete")
    async def keys_delete(req: Request) -> dict:
        require(req)
        data = await req.json()
        kh = str(data.get("key_hash", ""))
        if kh in state.keys:
            del state.keys[kh]
            return {"ok": True, "message": "Key deleted"}
        raise HTTPException(404, "Key not found")

    return app


# --- web-admin harness --------------------------------------------------


class FakeSessionStore:
    """In-memory twin of ``RedisSessionStore`` (no Redis in tests)."""

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    async def put(self, sid, session, ttl_seconds):
        self.data[sid] = dict(session)

    async def get(self, sid):
        return dict(self.data[sid]) if sid in self.data else None

    async def delete(self, sid):
        self.data.pop(sid, None)

    async def count(self):
        return len(self.data)

    async def aclose(self):
        pass


def build_admin_settings(**over):
    import adminservice.config as admin_config

    values = dict(
        internal_api_token="test-internal-token",
        gateway_internal_url="http://gateway.internal",
        admin_session_ttl_seconds=43200,
        redis_url="redis://redis.test.invalid:6379/0",
    )
    values.update(over)
    values.setdefault("admin_password", "")
    return admin_config.Settings(**values)


class Panel:
    """A running web-admin wired to the fake gateway, driven by TestClient."""

    def __init__(self, monkeypatch, password: str = PW) -> None:
        self.sessions = FakeSessionStore()
        self.state = FakeState()
        self.settings = build_admin_settings(admin_password=password)
        monkeypatch.setattr(admin_main, "settings", self.settings)
        monkeypatch.setattr(admin_main, "_sessions", self.sessions)
        monkeypatch.setattr(admin_main, "_login_fails", {})
        fake_gw = build_fake_gateway(self.state, self.settings.internal_api_token)
        gateway_client = httpx.AsyncClient(
            base_url="http://gateway.internal",
            headers={"X-Internal-Token": self.settings.internal_api_token},
            transport=httpx.ASGITransport(app=fake_gw),
            timeout=30.0,
        )
        monkeypatch.setattr(
            admin_main, "gateway",
            admin_main.GatewayClient(self.settings, gateway_client),
        )
        # seed a default client key (like a fresh deployment)
        kh = hash_key(DEFAULT_KEY)
        self.state.keys[kh] = {
            "key_hash": kh, "key_id": "default",
            "rpm": None, "tpm": None, "models": None, "enabled": True,
        }
        self.client = TestClient(admin_main.app)
        self.client.__enter__()

    def close(self) -> None:
        self.client.__exit__(None, None, None)

    def login(self, password: str = PW):
        return self.client.post("/api/admin/login", json={"password": password})

    def login_ok(self, password: str = PW) -> str:
        response = self.login(password)
        assert response.status_code == 200, response.text
        return response.json()["csrf"]

    def csrf(self) -> str:
        response = self.client.get("/api/admin/session")
        assert response.status_code == 200
        return response.json()["csrf"]

    def seed_proxy(self, url: str, enabled: bool = True) -> None:
        self.state.proxies[url] = enabled


@pytest.fixture
def make_panel(monkeypatch):
    created: list[Panel] = []

    def _make(password: str = PW) -> Panel:
        panel = Panel(monkeypatch, password=password)
        created.append(panel)
        return panel

    yield _make
    for panel in created:
        panel.close()


@pytest.fixture
def panel(make_panel) -> Panel:
    return make_panel()


# --- helpers ----------------------------------------------------------------


def auth_headers(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf}


def _series_payload(length: int, last: int = 0) -> dict:
    """A three-series block aligned to ``length`` days with a spike on the end."""
    tokens = [0] * length
    requests = [0] * length
    errors = [0] * length
    if length:
        tokens[-1] = last
        requests[-1] = 1
    return {"tokens": tokens, "requests": requests, "errors": errors}


# --- service / login / session ---------------------------------------------


def test_root_returns_service_json(panel):
    response = panel.client.get("/")
    assert response.status_code == 200
    assert response.json()["service"] == "openroute-web-admin"


def test_old_html_pages_are_gone(panel):
    for path in ("/admin/ui", "/admin/ui/keys", "/login"):
        assert panel.client.get(path).status_code == 404


def test_session_unauthenticated_is_200(panel):
    response = panel.client.get("/api/admin/session")
    assert response.status_code == 200
    assert response.json() == {"authenticated": False}


def test_panel_disabled_without_password(make_panel):
    p = make_panel(password="")
    assert p.client.get("/api/admin/session").status_code == 404
    assert p.client.post("/api/admin/login", json={"password": "x"}).status_code == 404
    assert p.client.get("/api/admin/proxies").status_code == 404


def test_login_sets_httponly_samesite_cookie(panel):
    response = panel.client.post("/api/admin/login", json={"password": PW})
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True and body["csrf"]
    set_cookie = response.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "openroute_session=" in set_cookie


def test_login_wrong_password(panel):
    response = panel.client.post("/api/admin/login", json={"password": "wrong"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Неверный пароль"
    assert panel.sessions.data == {}


def test_session_after_login(panel):
    csrf = panel.login_ok()
    response = panel.client.get("/api/admin/session")
    body = response.json()
    assert body["authenticated"] is True
    assert body["csrf"] == csrf
    assert body["expires_at"] > 0
    assert response.headers["cache-control"] == "no-store"


def test_protected_endpoints_require_session(panel):
    for method, path in (
        ("get", "/api/admin/proxies"),
        ("get", "/api/admin/stats"),
        ("get", "/api/admin/keys"),
        ("get", "/api/admin/proxies/events"),
        ("post", "/api/admin/proxies/set"),
        ("post", "/api/admin/proxies/refresh"),
        ("get", "/api/admin/proxies/account"),
        ("get", "/api/admin/proxies/countries"),
        ("get", "/api/admin/proxies/price"),
        ("post", "/api/admin/proxies/buy"),
        ("post", "/api/admin/keys/add"),
        ("post", "/api/admin/keys/rotate"),
        ("post", "/api/admin/logout"),
    ):
        response = getattr(panel.client, method)(path)
        assert response.status_code == 401
    assert panel.client.get("/api/admin/proxies").json()["detail"] == "Требуется вход в панель"


def test_session_expiry(panel):
    panel.login_ok()
    for session in panel.sessions.data.values():
        session["expires"] = 0.0
    assert panel.client.get("/api/admin/proxies").status_code == 401


def test_logout_requires_csrf_and_kills_session(panel):
    csrf = panel.login_ok()
    response = panel.client.post("/api/admin/logout", headers=auth_headers("forged"))
    assert response.status_code == 403
    assert panel.sessions.data

    response = panel.client.post("/api/admin/logout", headers=auth_headers(csrf))
    assert response.status_code == 204
    assert panel.sessions.data == {}
    assert panel.client.get("/api/admin/proxies").status_code == 401


def test_login_bruteforce_locked_out_and_resets(panel):
    for _ in range(10):
        assert panel.client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401
    response = panel.client.post("/api/admin/login", json={"password": PW})
    assert response.status_code == 429
    assert "retry-after" in {k.lower() for k in response.headers}


def test_login_lockout_resets_on_success(panel):
    for _ in range(3):
        assert panel.client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401
    assert panel.client.post("/api/admin/login", json={"password": PW}).status_code == 200
    assert panel.client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401


# --- proxy management -------------------------------------------------------


def test_proxies_snapshot_and_period(panel):
    panel.login_ok()
    panel.seed_proxy("http://user:secret@10.0.0.1:3128")
    panel.seed_proxy("http://u:p@10.0.0.2:3128")
    panel.state.usage_by_proxy["10.0.0.1:3128"] = {
        "requests": 4, "errors": 1, "tokens": 110,
    }
    panel.state.usage_by_proxy_lifetime["10.0.0.1:3128"] = {
        "requests": 9, "errors": 2, "tokens": 110,
    }
    panel.state.recent = [{
        "ts": 1_700_000_000.0, "key_id": "default", "method": "POST",
        "path": "/v1/chat/completions", "model": "deepseek-v4.1-flash-free",
        "status": 200, "latency_ms": 12.5, "stream": True,
        "account": "http://user:secret@10.0.0.1:3128",
        "url": "http://user:secret@10.0.0.1:3128",
        "real_proxy": "http://user:secret@10.0.0.1:3128",
        "prompt_tokens": 60, "completion_tokens": 50,
        "error": "upstream http://user:secret@10.0.0.1:3128 failed",
    }]

    response = panel.client.get("/api/admin/proxies?period=90d")
    assert response.status_code == 200
    assert panel.state.last_period == "90d"
    body = response.json()
    assert body["server_time"]
    assert body["summary"] == {
        "requests": 4, "errors": 1, "tokens": 110,
        "proxies_ready": 2, "proxies_enabled": 2, "proxies_total": 2,
    }
    assert [p["display"] for p in body["proxies"]] == ["10.0.0.1:3128", "10.0.0.2:3128"]
    first = body["proxies"][0]
    assert first["usage"] == {"requests": 4, "errors": 1, "tokens": 110}
    assert "lifetime" not in first
    assert body["totals"] == {"tokens_total": 110}
    assert first["id"] and len(first["id"]) == 64
    # The full credentialised URL must never reach the browser.
    assert "secret" not in response.text
    assert "real_proxy" not in response.text
    assert body["recent"][0]["account"] == "10.0.0.1:3128"
    assert body["recent"][0]["error"] == "upstream http://***@10.0.0.1:3128 failed"
    assert "url" not in body["recent"][0]


def test_proxies_lifetime_totals(panel):
    panel.login_ok()
    panel.seed_proxy("http://user:secret@10.0.0.1:3128")
    panel.seed_proxy("http://u:p@10.0.0.2:3128")
    panel.seed_proxy("http://u:p@10.0.0.3:3128")
    panel.state.usage_by_proxy_lifetime["10.0.0.1:3128"] = {
        "requests": 10, "errors": 2, "tokens": 900,
    }
    panel.state.usage_by_proxy_lifetime["10.0.0.2:3128"] = {
        "requests": 3, "errors": 0, "tokens": 100,
    }
    # An archived proxy (not in the pool anymore) still counts in the totals.
    panel.state.usage_by_proxy_lifetime["archived:8080"] = {
        "requests": 1, "errors": 0, "tokens": 50,
    }

    response = panel.client.get("/api/admin/proxies")
    assert response.status_code == 200
    body = response.json()
    assert body["totals"] == {"tokens_total": 1050}
    assert all("lifetime" not in row for row in body["proxies"])
    assert "secret" not in response.text
    assert "real_proxy" not in response.text


def test_proxy_toggle_by_opaque_id(panel):
    csrf = panel.login_ok()
    url = "http://u:p@h1:8080"
    panel.seed_proxy(url)
    body = panel.client.get("/api/admin/proxies").json()
    proxy_id = body["proxies"][0]["id"]

    response = panel.client.post(
        "/api/admin/proxies/set",
        headers=auth_headers(csrf),
        json={"proxy_id": proxy_id, "enabled": False},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "message": "Прокси отключён"}
    assert panel.state.proxies[url] is False

    response = panel.client.post(
        "/api/admin/proxies/set",
        headers=auth_headers(csrf),
        json={"proxy_id": proxy_id, "enabled": True},
    )
    assert response.status_code == 200
    assert response.json()["message"] == "Прокси включён"
    assert panel.state.proxies[url] is True


def test_proxy_refresh_requires_csrf_and_calls_gateway(panel):
    csrf = panel.login_ok()

    forbidden = panel.client.post("/api/admin/proxies/refresh")
    assert forbidden.status_code == 403
    assert panel.state.refreshes == 0

    response = panel.client.post(
        "/api/admin/proxies/refresh", headers=auth_headers(csrf),
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "message": "Список прокси обновлён"}
    assert panel.state.refreshes == 1
    assert response.headers["cache-control"] == "no-store"


def test_proxy_refresh_gateway_failure_is_localized(panel):
    csrf = panel.login_ok()
    panel.state.refresh_error = True
    response = panel.client.post(
        "/api/admin/proxies/refresh", headers=auth_headers(csrf),
    )
    assert response.status_code == 502
    assert response.json() == {"detail": "Не удалось обновить список прокси"}
    assert panel.state.refreshes == 1


def test_px6_account_countries_and_price(panel):
    panel.login_ok()

    account = panel.client.get("/api/admin/proxies/account")
    assert account.status_code == 200
    assert account.json() == {
        "user_id": "px6-user", "balance": 250.5, "currency": "RUB",
    }

    countries = panel.client.get("/api/admin/proxies/countries?version=4")
    assert countries.status_code == 200
    assert countries.json() == {"version": 4, "countries": ["de", "ru"]}

    price = panel.client.get("/api/admin/proxies/price?version=6&count=2&period=30")
    assert price.status_code == 200
    assert price.json()["count"] == 2
    assert price.json()["version"] == 6
    assert price.json()["price"] == 25.0


def test_px6_buy_requires_csrf_and_forwards_payload(panel):
    csrf = panel.login_ok()

    forbidden = panel.client.post(
        "/api/admin/proxies/buy",
        json={"count": 2, "period": 7, "country": "ru", "version": 6},
    )
    assert forbidden.status_code == 403
    assert panel.state.px6_buy_calls == []

    response = panel.client.post(
        "/api/admin/proxies/buy",
        headers=auth_headers(csrf),
        json={
            "count": 2, "period": 7, "country": "RU", "version": 6,
            "descr": "test",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["count"] == 2
    assert body["refresh_ok"] is True
    assert body["balance"] == 200.0
    assert body["message"] == "Куплено 2 прокси. Включите их в списке."
    assert panel.state.px6_buy_calls == [{
        "count": 2, "period": 7, "country": "ru", "version": 6, "descr": "test",
    }]


def test_px6_buy_rejects_invalid_body(panel):
    csrf = panel.login_ok()
    response = panel.client.post(
        "/api/admin/proxies/buy",
        headers=auth_headers(csrf),
        json={"count": 0, "period": 30, "country": "ru", "version": 6},
    )
    assert response.status_code == 400
    assert "Количество должно быть от 1 до 1000" in response.text
    assert panel.state.px6_buy_calls == []


def test_proxy_toggle_requires_csrf(panel):
    panel.login_ok()
    url = "http://u:p@h1:8080"
    panel.seed_proxy(url)
    proxy_id = panel.client.get("/api/admin/proxies").json()["proxies"][0]["id"]
    response = panel.client.post(
        "/api/admin/proxies/set",
        json={"proxy_id": proxy_id, "enabled": False},
    )
    assert response.status_code == 403
    assert panel.state.proxies[url] is True


def test_proxy_toggle_unknown_id(panel):
    csrf = panel.login_ok()
    response = panel.client.post(
        "/api/admin/proxies/set",
        headers=auth_headers(csrf),
        json={"proxy_id": "nope", "enabled": True},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Прокси не найден"


def test_proxy_state_fields(panel):
    panel.login_ok()
    quota_url = "http://u:p@quota:8080"
    ban_url = "http://u:p@banned:8080"
    panel.seed_proxy(quota_url)
    panel.seed_proxy(ban_url)
    panel.state.quota[quota_url] = 2 * 3600
    panel.state.banned[ban_url] = 125
    body = panel.client.get("/api/admin/proxies").json()
    by_display = {p["display"]: p for p in body["proxies"]}
    assert by_display["quota:8080"]["quota_seconds_left"] == pytest.approx(7200)
    assert by_display["banned:8080"]["banned_seconds_left"] == pytest.approx(125)
    assert body["summary"]["proxies_ready"] == 0


# --- statistics --------------------------------------------------------------


def test_stats_snapshot_normalized(panel):
    panel.login_ok()
    days = 90
    dates = [(date(2025, 10, 1) + timedelta(days=i)).isoformat() for i in range(days)]
    series = _series_payload(days, 700)
    panel.state.stats_payload.update({
        "days": days,
        "server_time": STATS_SERVER_TIME,
        "dates": dates,
        "totals": {
            "tokens_total": 700, "requests_total": 2, "errors_total": 1,
        },
        "proxies": [
            {
                "proxy": "http://user:secret@10.0.0.1:3128",
                "in_pool": True,
                "tokens_total": 200,
                "requests_total": 2, "errors_total": 1,
            },
            {
                "proxy": "old:8080",
                "in_pool": False,
                "tokens_total": 500,
                "requests_total": 1, "errors_total": 0,
                # Unknown gateway fields must never reach the browser.
                "real_proxy": "http://user:secret@old:8080",
            },
        ],
        "series_total": series,
        "series_by_proxy": {
            "http://user:secret@10.0.0.1:3128": series,
            "old:8080": _series_payload(days),
            "broken": "not-an-object",
        },
    })

    response = panel.client.get(f"/api/admin/stats?days={days}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert panel.state.stats_days == days
    body = response.json()
    assert body["days"] == days
    assert body["server_time"] == "2026-01-01T00:00:00+00:00"
    assert body["dates"] == dates
    assert body["totals"] == {
        "tokens_total": 700, "requests_total": 2, "errors_total": 1,
    }
    assert [row["proxy"] for row in body["proxies"]] == ["10.0.0.1:3128", "old:8080"]
    live, archived = body["proxies"]
    expected_fields = {
        "proxy", "in_pool", "tokens_total", "requests_total", "errors_total",
    }
    assert set(live) == expected_fields
    assert live["in_pool"] is True
    # Archived proxies stay visible but out of the pool.
    assert archived["in_pool"] is False
    assert len(body["series_total"]["tokens"]) == days
    assert body["series_total"]["tokens"][-1] == 700
    assert len(body["series_total"]["errors"]) == days
    assert set(body["series_by_proxy"]) == {"10.0.0.1:3128", "old:8080"}
    assert len(body["series_by_proxy"]["old:8080"]["tokens"]) == days
    assert "secret" not in response.text
    assert "real_proxy" not in response.text
    assert "real_url" not in response.text


def test_stats_snapshot_tolerates_missing_fields(panel):
    panel.login_ok()
    panel.state.stats_payload = {"server_time": "not-a-number"}
    response = panel.client.get("/api/admin/stats")
    assert response.status_code == 200
    body = response.json()
    assert body["days"] == 30
    assert body["dates"] == []
    assert body["totals"] == {
        "tokens_total": 0, "requests_total": 0, "errors_total": 0,
    }
    assert body["proxies"] == []
    assert body["series_total"] == {"tokens": [], "requests": [], "errors": []}
    assert body["series_by_proxy"] == {}
    # An unusable gateway clock falls back to the current UTC time.
    assert body["server_time"].endswith("+00:00")


def test_stats_days_clamped(panel):
    panel.login_ok()
    for requested, expected in ((0, 1), (-5, 1), (9999, 365)):
        response = panel.client.get(f"/api/admin/stats?days={requested}")
        assert response.status_code == 200, response.text
        assert panel.state.stats_days == expected


def test_stats_period_forwarded(panel):
    panel.login_ok()
    response = panel.client.get("/api/admin/stats?period=7d")
    assert response.status_code == 200, response.text
    assert panel.state.stats_period == "7d"
    assert panel.state.stats_days is None
    assert response.json()["days"] == len(STATS_DATES)

    response = panel.client.get("/api/admin/stats?period=quarter")
    assert response.status_code == 200, response.text
    assert panel.state.stats_period == "all"


# --- key management ---------------------------------------------------------


def test_keys_snapshot_and_stats(panel):
    panel.login_ok()
    panel.state.usage_by_key["default"] = {"requests": 2, "errors": 0, "tokens": 110}
    panel.state.usage_by_key["gone"] = {"requests": 1, "errors": 0, "tokens": 5}
    body = panel.client.get("/api/admin/keys?period=30d").json()
    assert panel.state.last_period == "30d"
    assert body["keys"][0]["key_id"] == "default"
    stats = {row["key_id"]: row for row in body["stats"]}
    assert stats["default"]["state"] == "active"
    assert stats["default"]["tokens"] == 110
    assert stats["gone"]["state"] == "deleted"


def test_add_key_validation(panel):
    csrf = panel.login_ok()
    for payload, message in (
        ({"rpm": "abc"}, "Некорректный RPM"),
        ({"tpm": -5}, "TPM не может быть отрицательным"),
    ):
        response = panel.client.post(
            "/api/admin/keys/add", headers=auth_headers(csrf), json=payload,
        )
        assert response.status_code == 400
        assert message in response.json()["detail"]
    assert len(panel.state.keys) == 1


def test_add_toggle_delete_key(panel):
    csrf = panel.login_ok()
    response = panel.client.post(
        "/api/admin/keys/add",
        headers=auth_headers(csrf),
        json={
            "rpm": 30, "tpm": 500,
            "models": ["deepseek-v4.1-flash-free", "gpt-oss"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Ключ k1 добавлен"
    assert body["key_id"] == "k1"
    assert body["key"].startswith("openroute-")
    k1 = next(v for v in panel.state.keys.values() if v["key_id"] == "k1")
    assert "admin" not in k1 and "name" not in k1 and "key" not in k1
    assert k1["key_hash"] == hash_key(body["key"])
    assert k1["rpm"] == 30 and k1["tpm"] == 500
    assert k1["models"] == ["deepseek-v4.1-flash-free", "gpt-oss"]

    # revoke / re-enable
    response = panel.client.post(
        "/api/admin/keys/set", headers=auth_headers(csrf),
        json={"key_hash": k1["key_hash"], "enabled": False},
    )
    assert response.status_code == 200
    assert response.json()["message"] == "Ключ отозван"
    assert panel.state.keys[k1["key_hash"]]["enabled"] is False
    panel.client.post(
        "/api/admin/keys/set", headers=auth_headers(csrf),
        json={"key_hash": k1["key_hash"], "enabled": True},
    )
    assert panel.state.keys[k1["key_hash"]]["enabled"] is True

    snapshot = panel.client.get("/api/admin/keys").json()
    assert body["key"] not in json.dumps(snapshot)

    # delete
    response = panel.client.post(
        "/api/admin/keys/delete", headers=auth_headers(csrf),
        json={"key_hash": k1["key_hash"]},
    )
    assert response.status_code == 200
    assert [v["key_id"] for v in panel.state.keys.values()] == ["default"]

    response = panel.client.post(
        "/api/admin/keys/delete", headers=auth_headers(csrf),
        json={"key_hash": "no-such-hash"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Ключ не найден"


def test_rotate_key_issues_new_secret_and_preserves_config(panel):
    csrf = panel.login_ok()
    created = panel.client.post(
        "/api/admin/keys/add",
        headers=auth_headers(csrf),
        json={"key_id": "rot", "rpm": 12, "tpm": 700, "models": ["gpt-oss"]},
    ).json()
    old_secret = created["key"]
    old_row = next(v for v in panel.state.keys.values() if v["key_id"] == "rot")
    old_hash = old_row["key_hash"]

    response = panel.client.post(
        "/api/admin/keys/rotate",
        headers=auth_headers(csrf),
        json={"key_hash": old_hash},
    )
    assert response.status_code == 200, response.text
    rotated = response.json()
    assert rotated["message"] == "Ключ перевыпущен"
    assert rotated["key_id"] == "rot"
    assert rotated["key"].startswith("openroute-")
    assert rotated["key"] != old_secret

    assert old_hash not in panel.state.keys
    new_row = panel.state.keys[hash_key(rotated["key"])]
    assert new_row["key_id"] == "rot"
    assert new_row["rpm"] == 12 and new_row["tpm"] == 700
    assert new_row["models"] == ["gpt-oss"]
    assert new_row["enabled"] is True
    snapshot = panel.client.get("/api/admin/keys").json()
    assert rotated["key"] not in json.dumps(snapshot)

    response = panel.client.post(
        "/api/admin/keys/rotate",
        headers=auth_headers(csrf),
        json={"key_hash": "no-such-hash"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Ключ не найден"


def test_key_mutations_require_csrf(panel):
    panel.login_ok()
    response = panel.client.post(
        "/api/admin/keys/add", json={"rpm": 10},
    )
    assert response.status_code == 403
    assert len(panel.state.keys) == 1


# --- SSE --------------------------------------------------------------------


class _FakeStreamRequest:
    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_sse_response_headers():
    response = admin_main._sse_response(
        _FakeStreamRequest(), "proxies", "all", {"csrf": "token", "sid": ""},
    )
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_live_sse_emits_initial_and_changed_snapshot(monkeypatch):
    calls = []
    snapshots = [
        {"server_time": "1", "summary": {"requests": 1}},
        {"server_time": "2", "summary": {"requests": 2}},
    ]

    async def fake_snapshot(page, period):
        calls.append((page, period))
        return snapshots[min(len(calls) - 1, 1)]

    monkeypatch.setattr(admin_main, "_panel_snapshot", fake_snapshot)
    monkeypatch.setattr(admin_main, "_SSE_TICK_SECONDS", 0)
    generator = admin_main._sse_events(
        _FakeStreamRequest(), "proxies", "week", {"csrf": "token", "sid": ""},
    )
    assert await anext(generator) == "retry: 3000\n\n"
    first = await anext(generator)
    assert json.loads(first.removeprefix("data: "))["summary"]["requests"] == 1
    second = await anext(generator)
    assert json.loads(second.removeprefix("data: "))["summary"]["requests"] == 2
    assert calls[0] == ("proxies", "week")
    await generator.aclose()


# --- gateway failures -------------------------------------------------------


def test_gateway_unreachable_502(panel, monkeypatch):
    panel.login_ok()

    def boom(request):
        raise httpx.ConnectError("no route", request=request)

    client = httpx.AsyncClient(
        base_url="http://gateway.internal",
        transport=httpx.MockTransport(boom),
    )
    monkeypatch.setattr(
        admin_main, "gateway",
        admin_main.GatewayClient(panel.settings, client),
    )
    response = panel.client.get("/api/admin/proxies")
    assert response.status_code == 502
    assert response.json()["detail"] == "Внутренний API шлюза недоступен"


def test_stats_gateway_unreachable_502(panel, monkeypatch):
    panel.login_ok()

    def boom(request):
        raise httpx.ConnectError("no route", request=request)

    client = httpx.AsyncClient(
        base_url="http://gateway.internal",
        transport=httpx.MockTransport(boom),
    )
    monkeypatch.setattr(
        admin_main, "gateway",
        admin_main.GatewayClient(panel.settings, client),
    )
    response = panel.client.get("/api/admin/stats")
    assert response.status_code == 502
    assert response.json()["detail"] == "Внутренний API шлюза недоступен"


def test_gateway_error_is_unwrapped_and_localized(panel):
    csrf = panel.login_ok()
    response = panel.client.post(
        "/api/admin/proxies/set", headers=auth_headers(csrf),
        json={"proxy_id": "nope", "enabled": True},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Прокси не найден"}
    assert '"type"' not in response.text and '"code"' not in response.text


def test_health_reports_sessions(panel):
    panel.login_ok()
    response = panel.client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "sessions": 1}
