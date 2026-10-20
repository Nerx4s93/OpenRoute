"""Tests for the OpenRoute web admin (``web-admin``) — ``/admin/ui``.

Hermetic by construction: the web-admin is driven by ``TestClient`` and its
internal-API client is pointed at an in-process fake gateway (``FastAPI`` +
``httpx.ASGITransport``), so no Redis, no PostgreSQL and no network are needed.
This mirrors ``NVRoute/tests/test_adminui.py``, adapted from accounts to proxies.

Covered: login (password only), server sessions in an HttpOnly cookie, CSRF on
forms, brute-force lock-out, session expiry, logout, proxy enable/disable/
delete, client-key add/revoke/delete, per-key stats from the request log, and
the auto-refresh HTML fragments. The fixtures live here on purpose — this file
owns its harness and does not touch ``tests/conftest.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import re
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

# --- load the web-admin ``app`` package under a unique name -------------
# ``web-admin`` has a hyphen, so it is not importable as a package; load it
# explicitly (same trick as NVRoute/tests/gw.py). Relative imports inside
# ``.config``/``.sessions``/``.adminui`` keep working.

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
        self.keys: dict[str, dict] = {}  # key_hash -> row
        self.usage_by_proxy: dict[str, dict] = {}  # masked -> stats
        self.usage_by_key: dict[str, dict] = {}  # key_id -> stats
        self.recent: list[dict] = []


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
            return None, f"Некорректный {field.upper()}: {raw}"
    else:
        v = raw
    if v < 0:
        return None, f"{field.upper()} не может быть отрицательным"
    return v, None


def build_fake_gateway(state: FakeState, token: str) -> FastAPI:
    """A tiny FastAPI app exposing the gateway's ``/internal/*`` contract."""
    app = FastAPI()

    @app.exception_handler(HTTPException)
    async def _openai_envelope(request: Request, exc: HTTPException) -> JSONResponse:
        """Mirror the REAL gateway: ALL HTTP errors use the OpenAI envelope.

        The gateway wraps every ``HTTPException`` as
        ``{"error": {"message", "type", "code"}}``, including
        internal ones — not FastAPI's ``{"detail": ...}``.
        """
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
            rows.append({
                "proxy": mask_proxy(url),          # host:port (display + usage key)
                "url": f"http://{mask_proxy(url)}",  # masked
                "real_proxy": url,                  # full URL for hidden form fields
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
                "banned_seconds_left": banned,
                "last_error": None,
            })
        ready = sum(
            1 for p in rows
            if p["enabled"] and not p["quota_seconds_left"]
            and not p["banned_seconds_left"]
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
                1 for p in rows if p["banned_seconds_left"] > 0),
            "last_proxies_fetch": None,
            "proxies": rows,
        }

    @app.get("/internal/proxies-data")
    async def proxies_data(req: Request) -> dict:
        require(req)
        return {
            "status": proxy_status(),
            "usage": {"total": _stats(state.usage_by_proxy)},
            "usage_by_proxy": state.usage_by_proxy,
            "recent": state.recent,
        }

    @app.post("/internal/proxies/set")
    async def proxies_set(req: Request) -> dict:
        require(req)
        data = await req.json()
        url = str(data.get("proxy", ""))
        enabled = bool(data.get("enabled"))
        if url in state.proxies:
            state.proxies[url] = enabled
            return {"ok": True, "message": "Прокси включён" if enabled else "Прокси отключён"}
        raise HTTPException(404, "Proxy not found")

    @app.post("/internal/proxies/delete")
    async def proxies_delete(req: Request) -> dict:
        require(req)
        data = await req.json()
        url = str(data.get("proxy", ""))
        if url in state.proxies:
            del state.proxies[url]
            return {"ok": True, "message": "Прокси удалён"}
        raise HTTPException(404, "Proxy not found")

    @app.get("/internal/keys-data")
    async def keys_data(req: Request) -> dict:
        require(req)
        usage = {"total": _stats(state.usage_by_key)}
        usage.update(state.usage_by_key)
        return {"keys": [dict(v) for v in state.keys.values()], "usage": usage}

    @app.post("/internal/keys/add")
    async def keys_add(req: Request) -> dict:
        require(req)
        data = await req.json()
        plaintext = str(data.get("key", "")).strip()
        if not plaintext:
            return {"ok": False, "message": "Ключ не указан"}
        if len(plaintext) > 200:
            return {"ok": False, "message": "Ключ слишком длинный (макс. 200 символов)"}
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
        kh = hash_key(plaintext)
        if kh in state.keys:
            return {"ok": False, "message": "Такой ключ уже есть"}
        state.keys[kh] = {
            "key_hash": kh, "key_id": key_id,
            "name": str(data.get("name", "")).strip(),
            "admin": bool(data.get("admin")),
            "rpm": rpm, "tpm": tpm, "models": models, "enabled": True,
        }
        return {"ok": True, "message": "Ключ добавлен"}

    @app.post("/internal/keys/set")
    async def keys_set(req: Request) -> dict:
        require(req)
        data = await req.json()
        kh = str(data.get("key_hash", ""))
        enabled = bool(data.get("enabled"))
        if kh in state.keys:
            state.keys[kh]["enabled"] = enabled
            return {"ok": True, "message": "Ключ включён" if enabled else "Ключ отозван"}
        raise HTTPException(404, "Key not found")

    @app.post("/internal/keys/delete")
    async def keys_delete(req: Request) -> dict:
        require(req)
        data = await req.json()
        kh = str(data.get("key_hash", ""))
        if kh in state.keys:
            del state.keys[kh]
            return {"ok": True, "message": "Ключ удалён"}
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

    s = dict(
        internal_api_token="test-internal-token",
        gateway_internal_url="http://gateway.internal",
        admin_session_ttl_seconds=43200,
        redis_url="redis://redis.test.invalid:6379/0",
    )
    s.update(over)
    s.setdefault("admin_password", "")
    return admin_config.Settings(**s)


class Panel:
    """A running web-admin wired to the fake gateway, driven by TestClient."""

    def __init__(self, monkeypatch, password: str = PW) -> None:
        self.sessions = FakeSessionStore()
        self.state = FakeState()
        self.settings = build_admin_settings(admin_password=password)
        monkeypatch.setattr(admin_main, "settings", self.settings)
        monkeypatch.setattr(admin_main, "_sessions", self.sessions)
        monkeypatch.setattr(admin_main, "_login_fails", {})
        self.fake_gw = build_fake_gateway(self.state, self.settings.internal_api_token)
        client = httpx.AsyncClient(
            base_url="http://gateway.internal",
            headers={"X-Internal-Token": self.settings.internal_api_token},
            transport=httpx.ASGITransport(app=self.fake_gw),
            timeout=30.0,
        )
        monkeypatch.setattr(admin_main, "_internal_client", client)
        # seed a default admin key (like a fresh deployment)
        kh = hash_key(DEFAULT_KEY)
        self.state.keys[kh] = {
            "key_hash": kh, "key_id": "default", "name": "", "admin": True,
            "rpm": None, "tpm": None, "models": None, "enabled": True,
        }
        self.client = TestClient(admin_main.app)
        self.client.__enter__()

    def close(self) -> None:
        self.client.__exit__(None, None, None)


@pytest.fixture
def make_panel(monkeypatch):
    created: list[Panel] = []

    def _make(password: str = PW) -> Panel:
        p = Panel(monkeypatch, password=password)
        created.append(p)
        return p

    yield _make
    for p in created:
        p.close()


@pytest.fixture
def panel(make_panel) -> Panel:
    return make_panel()


# --- helpers ----------------------------------------------------------------


def login(panel: Panel, pw: str = PW):
    """POST /login with only the password; the session cookie lands in the jar."""
    return panel.client.post("/login", data={"password": pw}, follow_redirects=False)


def login_ok(panel: Panel, pw: str = PW):
    r = login(panel, pw)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/ui"
    return r


_CSRF_RE = re.compile(r'name="_csrf" value="([^"]+)"')


def panel_csrf(panel: Panel, page: str = "/admin/ui") -> str:
    m = _CSRF_RE.search(panel.client.get(page).text)
    assert m, "CSRF token not found on the page"
    return m.group(1)


def seed_proxy(panel: Panel, url: str, enabled: bool = True) -> None:
    """Put a proxy straight into the fake gateway store.

    In production the pool is filled ONLY by the px6 refresh; the UI tests seed
    the store directly (there is no «add proxy» form to POST to anymore).
    """
    panel.state.proxies[url] = enabled


# --- login / session / CSRF -------------------------------------------------


def test_root_redirects_to_login(panel):
    r = panel.client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/login"


def test_login_page_password_only(panel):
    html = panel.client.get("/login").text
    assert html.count("<input") == 1
    assert 'type="password"' in html
    assert "username" not in html.lower()


def test_panel_disabled_without_password(make_panel):
    p = make_panel(password="")
    assert p.client.get("/admin/ui", follow_redirects=False).status_code == 404
    assert p.client.get("/login", follow_redirects=False).status_code == 404
    assert p.client.post("/login", data={"password": "x"},
                         follow_redirects=False).status_code == 404


def test_panel_requires_session(panel):
    for url in ("/admin/ui", "/admin/ui/keys", "/admin/ui/data", "/admin/ui/keys/data"):
        r = panel.client.get(url, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"

    assert login(panel, "wrong").status_code == 401
    assert panel.client.get("/admin/ui", follow_redirects=False).status_code == 303

    # a forged cookie does not pass (validity lives only in the server store)
    panel.client.cookies.set(admin_main._SESSION_COOKIE, "forged-sid")
    assert panel.client.get("/admin/ui", follow_redirects=False).status_code == 303
    panel.client.cookies.delete(admin_main._SESSION_COOKIE)


def test_login_sets_httponly_samesite_cookie(panel):
    r = login_ok(panel)
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie


def test_login_page_redirects_authenticated(panel):
    login_ok(panel)
    r = panel.client.get("/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/ui"


def test_login_bruteforce_locked_out(panel):
    for _ in range(10):
        assert login(panel, "wrong").status_code == 401
    r = login(panel, PW)
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers}


def test_session_expires_by_ttl(panel):
    login_ok(panel)
    for s in panel.sessions.data.values():
        s["expires"] = 0.0
    r = panel.client.get("/admin/ui", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_logout_kills_session(panel):
    login_ok(panel)
    csrf = panel_csrf(panel)
    r = panel.client.post("/admin/ui/logout", data={"_csrf": csrf},
                          follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert panel.sessions.data == {}
    assert panel.client.get("/admin/ui", follow_redirects=False).status_code == 303


def test_logout_requires_csrf(panel):
    login_ok(panel)
    r = panel.client.post("/admin/ui/logout", data={"_csrf": "forged"},
                          follow_redirects=False)
    assert r.status_code == 403
    assert panel.sessions.data  # session survives — logout did not go through


def test_csrf_required_on_post(panel):
    login_ok(panel)
    seed_proxy(panel, "http://u:p@h-new:8080")
    r = panel.client.post("/admin/ui/proxies/set",
                          data={"_csrf": "forged", "proxy": "http://u:p@h-new:8080",
                                "enabled": "0"},
                          follow_redirects=False)
    assert r.status_code == 403
    assert panel.state.proxies["http://u:p@h-new:8080"] is True  # untouched


# --- proxy management through the UI ----------------------------------------


def test_ui_add_proxy_route_removed(panel):
    """The manual «add proxy» path is gone (the pool comes from px6 only)."""
    login_ok(panel)
    csrf = panel_csrf(panel)
    r = panel.client.post("/admin/ui/proxies/add", follow_redirects=False,
                          data={"_csrf": csrf, "proxies": "http://u:p@h-new:8080"})
    assert r.status_code == 404
    assert "http://u:p@h-new:8080" not in panel.state.proxies


def test_ui_toggle_delete_proxy(panel):
    login_ok(panel)
    csrf = panel_csrf(panel)
    seed_proxy(panel, "http://u:p@h1:8080")
    seed_proxy(panel, "http://u:p@h2:8080")

    # disable: the manual flag flips in the store; the page shows «выключен»
    r = panel.client.post("/admin/ui/proxies/set", follow_redirects=False,
                          data={"_csrf": csrf, "proxy": "http://u:p@h1:8080", "enabled": "0"})
    assert r.status_code == 303
    assert panel.state.proxies["http://u:p@h1:8080"] is False
    html = panel.client.get("/admin/ui").text
    assert "выключен" in html

    # re-enable
    panel.client.post("/admin/ui/proxies/set", follow_redirects=False,
                      data={"_csrf": csrf, "proxy": "http://u:p@h1:8080", "enabled": "1"})
    assert panel.state.proxies["http://u:p@h1:8080"] is True

    # delete removes it
    r = panel.client.post("/admin/ui/proxies/delete", follow_redirects=False,
                          data={"_csrf": csrf, "proxy": "http://u:p@h2:8080"})
    assert r.status_code == 303
    assert "http://u:p@h2:8080" not in panel.state.proxies

    # unknown proxy → 404
    r = panel.client.post("/admin/ui/proxies/delete", follow_redirects=False,
                          data={"_csrf": csrf, "proxy": "http://nope:1"})
    assert r.status_code == 404


def test_proxy_rows_show_masked_hostport(panel):
    login_ok(panel)
    seed_proxy(panel, "http://user:supersecret@10.0.0.1:3128")
    html = panel.client.get("/admin/ui").text
    assert "10.0.0.1:3128" in html               # masked host:port in the table
    # the table cell shows host:port, not the full credentialised URL
    assert 'class="mono">10.0.0.1:3128<' in html


def test_proxy_quota_and_ban_are_visible(panel):
    """Daily quota parking is an operator-facing state, not just an API field."""
    login_ok(panel)
    quota_url = "http://u:p@quota:8080"
    ban_url = "http://u:p@banned:8080"
    seed_proxy(panel, quota_url)
    seed_proxy(panel, ban_url)
    panel.state.quota[quota_url] = 2 * 3600
    panel.state.banned[ban_url] = 125

    data = panel.client.get("/admin/ui/data").json()
    rows = data["proxies-rows"]
    assert "лимит 2ч 00м" in rows
    assert "бан 2м" in rows
    # The «ready» card no longer counts parked proxies; the quota card shows 1/2.
    assert "0/2" in data["cards"]
    assert "1 / 2" in data["cards"]
    assert "Прокси в лимите" in data["cards"]


def test_ui_add_key_validation(panel):
    login_ok(panel)
    csrf = panel_csrf(panel)
    for data in (
        {"_csrf": csrf, "key": ""},                     # no key
        {"_csrf": csrf, "key": "x" * 201},              # too long
        {"_csrf": csrf, "key": "sk-ok", "rpm": "abc"},  # junk RPM
        {"_csrf": csrf, "key": "sk-ok", "tpm": "-5"},   # negative TPM
    ):
        r = panel.client.post("/admin/ui/keys/add", follow_redirects=False, data=data)
        assert r.status_code == 303
    assert len(panel.state.keys) == 1  # nothing extra was written


def test_ui_key_csrf_required(panel):
    login_ok(panel)
    r = panel.client.post("/admin/ui/keys/add",
                          data={"_csrf": "forged", "key": "sk-forged"},
                          follow_redirects=False)
    assert r.status_code == 403
    assert len(panel.state.keys) == 1


def test_ui_add_toggle_delete_key(panel):
    login_ok(panel)
    csrf = panel_csrf(panel)

    # add: plaintext + limits; key_id auto-generated (k1)
    r = panel.client.post(
        "/admin/ui/keys/add", follow_redirects=False,
        data={"_csrf": csrf, "key": "sk-new-client", "rpm": "30", "tpm": "500",
              "models": "deepseek-v4.1-flash-free, gpt-oss", "admin": "1"},
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/ui/keys")
    ids = [v["key_id"] for v in panel.state.keys.values()]
    assert ids == ["default", "k1"]
    k1 = next(v for v in panel.state.keys.values() if v["key_id"] == "k1")
    assert k1["admin"] is True and k1["rpm"] == 30 and k1["tpm"] == 500
    assert k1["models"] == ["deepseek-v4.1-flash-free", "gpt-oss"]

    # duplicate plaintext rejected
    r = panel.client.post("/admin/ui/keys/add", follow_redirects=False,
                          data={"_csrf": csrf, "key": "sk-new-client"})
    assert r.status_code == 303
    assert len(panel.state.keys) == 2

    # revoke
    r = panel.client.post("/admin/ui/keys/set", follow_redirects=False,
                          data={"_csrf": csrf, "key_hash": k1["key_hash"], "enabled": "0"})
    assert r.status_code == 303
    assert panel.state.keys[k1["key_hash"]]["enabled"] is False

    # re-enable
    panel.client.post("/admin/ui/keys/set", follow_redirects=False,
                      data={"_csrf": csrf, "key_hash": k1["key_hash"], "enabled": "1"})
    assert panel.state.keys[k1["key_hash"]]["enabled"] is True

    # delete
    r = panel.client.post("/admin/ui/keys/delete", follow_redirects=False,
                          data={"_csrf": csrf, "key_hash": k1["key_hash"]})
    assert r.status_code == 303
    assert [v["key_id"] for v in panel.state.keys.values()] == ["default"]

    # unknown key → 404
    r = panel.client.post("/admin/ui/keys/delete", follow_redirects=False,
                          data={"_csrf": csrf, "key_hash": "no-such-hash"})
    assert r.status_code == 404


def test_ui_key_rows_rendered(panel):
    login_ok(panel)
    csrf = panel_csrf(panel)
    r = panel.client.post(
        "/admin/ui/keys/add", follow_redirects=False,
        data={"_csrf": csrf, "key": "sk-visible-secret", "key_id": "vis",
              "name": "Frontend", "rpm": "60"},
    )
    assert r.status_code == 303
    html = panel.client.get("/admin/ui/keys").text
    assert "Клиентские ключи" in html
    assert "vis" in html and "Frontend" in html
    assert "sk-visible-secret" not in html  # plaintext never lands on the page


# --- page rendering + auto-refresh fragments --------------------------------


def test_panel_page_renders(panel):
    login_ok(panel)
    seed_proxy(panel, "http://u:p@h1:8080")
    seed_proxy(panel, "http://u:p@h2:8080")
    r = panel.client.get("/admin/ui")
    assert r.status_code == 200
    assert "OpenRoute Admin" in r.text
    assert "h1:8080" in r.text and "h2:8080" in r.text
    assert "Токенов потрачено" in r.text


def test_panel_data_fragments(panel):
    login_ok(panel)
    seed_proxy(panel, "http://u:p@h1:8080")
    seed_proxy(panel, "http://u:p@h2:8080")
    r = panel.client.get("/admin/ui/data")
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"srv-now", "cards", "proxies-rows", "logs-rows"}
    assert "h1:8080" in d["proxies-rows"] and "h2:8080" in d["proxies-rows"]
    assert "Запросов всего" in d["cards"]
    assert d["srv-now"]


def test_keys_data_fragments(panel):
    login_ok(panel)
    r = panel.client.get("/admin/ui/keys/data")
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"srv-now", "keys-rows", "key-stats-rows"}
    assert "default" in d["keys-rows"]
    assert "Запросов ещё не было" in d["key-stats-rows"]  # log is empty
    assert d["srv-now"]


def test_panel_data_reflects_changes_live(panel):
    login_ok(panel)
    csrf = panel_csrf(panel)
    seed_proxy(panel, "http://u:p@live-1:8080")
    before = panel.client.get("/admin/ui/data").json()
    assert "live-1:8080" in before["proxies-rows"]
    r = panel.client.post("/admin/ui/proxies/delete", follow_redirects=False,
                          data={"_csrf": csrf, "proxy": "http://u:p@live-1:8080"})
    assert r.status_code == 303
    after = panel.client.get("/admin/ui/data").json()
    assert "live-1:8080" not in after["proxies-rows"]


def test_keys_data_reflects_added_key_live(panel):
    login_ok(panel)
    csrf = panel_csrf(panel)
    before = panel.client.get("/admin/ui/keys/data").json()
    assert "k-live" not in before["keys-rows"]
    r = panel.client.post("/admin/ui/keys/add", follow_redirects=False,
                          data={"_csrf": csrf, "key": "sk-live-key", "key_id": "k-live"})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/ui/keys")
    after = panel.client.get("/admin/ui/keys/data").json()
    assert "k-live" in after["keys-rows"]


def test_panel_no_manual_refresh_button(panel):
    login_ok(panel)
    acc = panel.client.get("/admin/ui").text
    keys = panel.client.get("/admin/ui/keys").text
    for html in (acc, keys):
        assert "Обновить" not in html
        assert "setInterval" in html
    assert "/admin/ui/data" in acc and "/admin/ui/keys/data" not in acc
    assert "/admin/ui/keys/data" in keys
    assert acc.count("<tbody") == 2 and 'id="proxies-rows"' in acc and 'id="logs-rows"' in acc
    assert keys.count("<tbody") == 2 and 'id="keys-rows"' in keys and 'id="key-stats-rows"' in keys


def test_pages_nav_forms_and_logout(panel):
    login_ok(panel)
    csrf = panel_csrf(panel)
    acc = panel.client.get("/admin/ui").text
    keys = panel.client.get("/admin/ui/keys").text

    # navigation: two links, the active one highlighted on its own page
    assert '<a href="/admin/ui" class="active">Прокси</a>' in acc
    assert '<a href="/admin/ui/keys">Ключи</a>' in acc
    assert '<a href="/admin/ui">Прокси</a>' in keys
    assert '<a href="/admin/ui/keys" class="active">Ключи</a>' in keys

    # no manual «add proxy» form anymore (the pool comes from px6 only);
    # the keys add-form sits ABOVE its table
    assert 'action="/admin/ui/proxies/add"' not in acc
    assert "Добавить прокси" not in acc
    assert 0 < keys.find('action="/admin/ui/keys/add"') < keys.find('id="keys-rows"')
    assert "Статистика по ключам" in keys and "Статистика по ключам" not in acc

    # «Выйти» is a POST form carrying the session CSRF token
    for html in (acc, keys):
        assert '<form method="post" action="/admin/ui/logout">' in html
        assert f'name="_csrf" value="{csrf}"' in html


def test_ui_shows_totals_and_per_proxy_usage(panel):
    login_ok(panel)
    seed_proxy(panel, "http://u:p@h1:8080")
    panel.state.usage_by_proxy["h1:8080"] = {"requests": 1, "errors": 0, "tokens": 110}
    html = panel.client.get("/admin/ui").text
    assert "110" in html  # tokens burned are visible on the page


def test_proxy_counters_come_from_persistent_usage(panel):
    """Per-proxy requests/errors come from the DB log, not in-memory counters
    (the fake gateway's status always reports 0, simulating a restart)."""
    login_ok(panel)
    seed_proxy(panel, "http://u:p@persist:8080")
    panel.state.usage_by_proxy["persist:8080"] = {
        "requests": 42, "errors": 7, "tokens": 100,
    }
    rows = panel.client.get("/admin/ui/data").json()["proxies-rows"]
    assert "<td>42</td>" in rows and "<td>7</td>" in rows


def test_key_stats_include_deleted_key(panel):
    login_ok(panel)
    csrf = panel_csrf(panel)
    kh = hash_key(DEFAULT_KEY)
    panel.state.usage_by_key["default"] = {"requests": 1, "errors": 0, "tokens": 110}
    stats = panel.client.get("/admin/ui/keys/data").json()["key-stats-rows"]
    assert "default" in stats and "активен" in stats

    r = panel.client.post("/admin/ui/keys/delete", follow_redirects=False,
                          data={"_csrf": csrf, "key_hash": kh})
    assert r.status_code == 303
    assert panel.state.keys == {}
    d = panel.client.get("/admin/ui/keys/data").json()
    assert "default" not in d["keys-rows"]
    assert "default" in d["key-stats-rows"] and "удалён" in d["key-stats-rows"]


def test_log_rows_render_recent(panel):
    login_ok(panel)
    seed_proxy(panel, "http://u:p@h1:8080")
    panel.state.recent = [{
        "ts": 1_700_000_000.0, "key_id": "default", "method": "POST",
        "path": "/v1/chat/completions", "model": "deepseek-v4.1-flash-free",
        "status": 200, "latency_ms": 12.5, "stream": True, "account": "h1:8080",
        "prompt_tokens": 60, "completion_tokens": 50, "error": None,
    }]
    rows = panel.client.get("/admin/ui/data").json()["logs-rows"]
    assert "default" in rows and "h1:8080" in rows and "110" in rows


def test_gateway_unreachable_502(panel, monkeypatch):
    login_ok(panel)

    def boom(request):
        raise httpx.ConnectError("no route", request=request)

    monkeypatch.setattr(admin_main, "_internal_client",
                        httpx.AsyncClient(base_url="http://gateway.internal",
                                          transport=httpx.MockTransport(boom)))
    r = panel.client.get("/admin/ui", follow_redirects=False)
    assert r.status_code == 502


# --- gateway error parsing (OpenAI envelope, never the raw body) ------------


def test_gateway_error_parses_openai_envelope():
    """``_gateway_error`` must surface ``error.message`` — not the raw envelope."""
    for status in (403, 404, 500):
        envelope = {"error": {"message": f"boom-{status}", "type": "invalid_request_error",
                              "code": status}}
        r = httpx.Response(status, json=envelope)
        exc = admin_main._gateway_error(r)
        assert exc.status_code == status
        assert exc.detail == f"boom-{status}"
        assert "error" not in exc.detail and "{" not in exc.detail

    # fallbacks: FastAPI ``detail``, then bare ``message``, then raw text
    assert admin_main._gateway_error(httpx.Response(404, json={"detail": "d1"})).detail == "d1"
    assert admin_main._gateway_error(httpx.Response(400, json={"message": "m1"})).detail == "m1"
    assert admin_main._gateway_error(httpx.Response(502, text="not json")).detail == "not json"


def test_gateway_envelope_error_surfaced_cleanly(panel):
    """End-to-end: an internal 404 surfaces a clean message, not the envelope."""
    login_ok(panel)
    csrf = panel_csrf(panel)
    r = panel.client.post("/admin/ui/proxies/delete", follow_redirects=False,
                          data={"_csrf": csrf, "proxy": "http://nope:1"})
    assert r.status_code == 404
    body = r.json()
    assert body == {"detail": "Proxy not found"}
    assert "error" not in body
    raw = r.text
    assert '"type"' not in raw and '"code"' not in raw


def test_login_lockout_resets_on_success(panel):
    for _ in range(3):
        assert login(panel, "wrong").status_code == 401
    assert login(panel, PW).status_code == 303
    # counter cleared: a fresh wrong attempt is 401, not 429
    assert login(panel, "wrong").status_code == 401


def test_health_reports_sessions(panel):
    login_ok(panel)
    r = panel.client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["sessions"] == 1
