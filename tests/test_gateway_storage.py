"""Gateway control plane over storage-service (``FakeStorage``).

Covers every path that no longer touches PostgreSQL directly:

- startup requires storage-service (``check()`` failure aborts the gateway);
- key add/rotate/set/delete persist through the storage client and update the
  in-memory ``KeyStore`` only after success;
- proxy enable/disable is persisted before the runtime/affinity side effects;
- ``ProxyManager.refresh_proxies`` syncs through storage, keeps manual flags
  and fails closed when the service is down;
- parking persistence is a fire-and-forget write that never breaks a request.

No PostgreSQL is needed for these tests: the storage fake is injected through
``main.storage`` exactly like the production client would be.
"""

import json
import time
import types

import httpx
import pytest
from fastapi.testclient import TestClient

import gateway.main as main
from fake_storage import FakeStorage
from gateway.keys import AuthError, KeyStore, hash_key
from gateway.proxies import ProxyManager
from gateway.storage import StorageUnavailable

from gw import _quiet_refresh_loop, build_settings

H = {"X-Internal-Token": "test-internal-token"}
FREE_MODEL = "deepseek-v4.1-flash-free"

SSE_BODY = (
    b'data: {"id":"cmpl-1","object":"chat.completion.chunk","model":'
    b'"deepseek-v4.1-flash-free","choices":[{"index":0,"delta":{"role":'
    b'"assistant","content":"hi"},"finish_reason":null}]}\n\n'
    b'data: {"id":"cmpl-1","choices":[{"index":0,"delta":{},'
    b'"finish_reason":"stop"}],"usage":{"prompt_tokens":6,'
    b'"completion_tokens":2,"total_tokens":8}}\n\n'
    b"data: [DONE]\n\n"
)


def _body():
    return {"model": FREE_MODEL, "messages": [{"role": "user", "content": "ping"}]}


def _sse_response():
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=SSE_BODY)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeFetcher:
    def __init__(self, payload):
        self.payload = payload

    async def get(self, url):
        return _FakeResp(self.payload)

    async def post(self, url, json=None):
        pass

    async def aclose(self):
        pass


async def _fake_fetch(m, payload):
    old = m._fetcher
    m._fetcher = _FakeFetcher(payload)
    await old.aclose()


async def _alive_probe(self, url):
    return True


# --- startup ----------------------------------------------------------------


def test_startup_fails_when_storage_check_fails(monkeypatch):
    settings = build_settings()
    failing = FakeStorage()
    failing.raise_unavailable()
    manager = ProxyManager(settings)
    keystore = KeyStore()
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "manager", manager)
    monkeypatch.setattr(main, "keystore", keystore)
    monkeypatch.setattr(main, "storage", failing)
    monkeypatch.setattr(main, "refresh_loop", _quiet_refresh_loop)

    with pytest.raises(RuntimeError, match="storage-service"):
        with TestClient(main.app):
            pass

    # The client was closed and no half-initialised gateway was left behind.
    assert failing.closed is True
    assert main.storage is None
    assert manager.storage is None
    assert manager.proxies == []
    assert keystore.key_ids() == []


def test_lifespan_reuses_injected_storage(monkeypatch):
    from gw import Gateway

    gw = Gateway(monkeypatch)
    try:
        # The lifespan used the injected fake; it never opens a database.
        assert main.storage is gw.storage
        assert gw.manager.storage is gw.storage
        assert set(gw.storage.proxies) == {
            "http://proxy-a.invalid:8080", "http://proxy-b.invalid:8080"}
        assert gw.storage.closed is False
        assert gw.keystore.authenticate("Bearer test-key").key_id == "default"
    finally:
        gw.close()


# --- keys -------------------------------------------------------------------


def test_key_crud_persists_to_storage_and_keystore(make_gateway):
    gw = make_gateway()

    r = gw.internal.post("/internal/keys/add",
                         json={"key_id": "n1", "rpm": 5, "tpm": 900,
                               "models": [FREE_MODEL]}, headers=H)
    assert r.status_code == 200, r.text
    issued = r.json()
    assert set(issued) == {"ok", "message", "key", "key_id"}
    secret = issued["key"]
    digest = hash_key(secret)
    assert digest in gw.storage.keys
    stored = gw.storage.keys[digest]
    assert stored["key_id"] == "n1" and stored["rpm"] == 5
    assert stored["tpm"] == 900 and stored["models"] == [FREE_MODEL]
    assert stored["enabled"] is True
    assert gw.keystore.authenticate(f"Bearer {secret}").key_id == "n1"

    # The secret is handed out once: the panel snapshot carries only the hash.
    data = gw.internal.get("/internal/keys-data", headers=H).json()
    row = next(k for k in data["keys"] if k["key_id"] == "n1")
    assert row["key_hash"] == digest
    assert "key" not in row and secret not in json.dumps(data)

    # Revoke -> storage flag + in-memory auth are in sync.
    r = gw.internal.post("/internal/keys/set",
                         json={"key_hash": digest, "enabled": False},
                         headers=H)
    assert r.status_code == 200
    assert gw.storage.keys[digest]["enabled"] is False
    with pytest.raises(AuthError):
        gw.keystore.authenticate(f"Bearer {secret}")

    # Delete -> row is gone on both sides.
    r = gw.internal.post("/internal/keys/delete",
                         json={"key_hash": digest}, headers=H)
    assert r.status_code == 200
    assert digest not in gw.storage.keys
    with pytest.raises(AuthError):
        gw.keystore.authenticate(f"Bearer {secret}")


def test_key_rotate_invalidates_old_secret_and_updates_both_sides(make_gateway):
    gw = make_gateway(keys=[{
        "key": "openroute-old-secret", "key_id": "r1",
        "rpm": 7, "tpm": 900, "models": [FREE_MODEL],
    }])
    old_hash = hash_key("openroute-old-secret")

    r = gw.internal.post("/internal/keys/rotate",
                         json={"key_hash": old_hash}, headers=H)
    assert r.status_code == 200, r.text
    rotated = r.json()
    new_secret = rotated["key"]
    new_hash = hash_key(new_secret)
    assert rotated["key_id"] == "r1"
    assert new_hash != old_hash

    # storage row moved in place and kept its config
    assert old_hash not in gw.storage.keys
    row = gw.storage.keys[new_hash]
    assert row["key_id"] == "r1"
    assert row["rpm"] == 7 and row["tpm"] == 900 and row["models"] == [FREE_MODEL]
    assert row["enabled"] is True

    # keystore moved in place: old secret is dead, the new one has the config
    with pytest.raises(AuthError):
        gw.keystore.authenticate("Bearer openroute-old-secret")
    info = gw.keystore.authenticate(f"Bearer {new_secret}")
    assert info.key_id == "r1" and info.rpm == 7 and info.tpm == 900

    missing = gw.internal.post("/internal/keys/rotate",
                               json={"key_hash": "no-such-hash"}, headers=H)
    assert missing.status_code == 404


def test_key_rotate_already_rotated_resyncs_from_storage(make_gateway, monkeypatch):
    """A retried hash pair is idempotent: the service reports ``already_rotated``
    and the gateway re-reads the persisted row so the new secret works."""
    gw = make_gateway()
    old_hash = hash_key("test-key")
    fixed = "openroute-fixed-secret"
    new_hash = hash_key(fixed)
    # Simulate a lost response: storage already applied old -> new, memory still
    # holds the old hash.
    gw.storage.keys.pop(old_hash)
    gw.storage.keys[new_hash] = {
        "key_hash": new_hash, "key_id": "default", "rpm": None,
        "tpm": None, "models": None, "enabled": True,
    }
    gw.keystore.remove(old_hash)
    monkeypatch.setattr(main, "generate_key", lambda: fixed)

    r = gw.internal.post("/internal/keys/rotate",
                         json={"key_hash": old_hash}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["key"] == fixed
    assert r.json()["key_id"] == "default"
    assert gw.keystore.authenticate(f"Bearer {fixed}").key_id == "default"


def test_key_mutations_return_503_when_storage_is_down(make_gateway):
    gw = make_gateway()
    before = dict(gw.storage.keys)
    before_ids = set(gw.keystore.key_ids())
    gw.storage.raise_unavailable()

    add = gw.internal.post("/internal/keys/add", json={"key_id": "n1"}, headers=H)
    assert add.status_code == 503
    digest = hash_key("test-key")
    rotate = gw.internal.post("/internal/keys/rotate",
                              json={"key_hash": digest}, headers=H)
    assert rotate.status_code == 503
    set_enabled = gw.internal.post(
        "/internal/keys/set", json={"key_hash": digest, "enabled": False},
        headers=H)
    assert set_enabled.status_code == 503
    delete = gw.internal.post("/internal/keys/delete",
                              json={"key_hash": digest}, headers=H)
    assert delete.status_code == 503

    # Nothing was persisted and the runtime store is unchanged.
    assert gw.storage.keys == before
    assert set(gw.keystore.key_ids()) == before_ids
    assert gw.keystore.authenticate("Bearer test-key").enabled is True


def test_keys_panel_falls_back_to_keystore_snapshot(make_gateway):
    gw = make_gateway()
    gw.storage.raise_unavailable()

    r = gw.internal.get("/internal/keys-data", headers=H)
    assert r.status_code == 200
    data = r.json()
    assert data["keys"] == gw.keystore.snapshot()
    assert data["keys"][0]["key_hash"] == hash_key("test-key")
    assert data["usage"]["total"]["requests"] == 0
    assert "test-key" not in json.dumps(data)


# --- proxies ----------------------------------------------------------------


def test_proxy_set_persists_before_runtime_change(make_gateway):
    gw = make_gateway()
    url = "http://proxy-a.invalid:8080"

    r = gw.internal.post("/internal/proxies/set",
                         json={"proxy": url, "enabled": False}, headers=H)
    assert r.status_code == 200
    assert gw.storage.proxies[url]["enabled"] is False
    assert gw.proxy(url).manually_disabled is True

    # A failing write must not touch the runtime pool (no split brain).
    gw.storage.raise_unavailable()
    r = gw.internal.post("/internal/proxies/set",
                         json={"proxy": url, "enabled": True}, headers=H)
    assert r.status_code == 503
    assert gw.storage.proxies[url]["enabled"] is False
    assert gw.proxy(url).manually_disabled is True

    gw.storage.raise_unavailable(False)
    r = gw.internal.post("/internal/proxies/set",
                         json={"proxy": url, "enabled": True}, headers=H)
    assert r.status_code == 200
    assert gw.storage.proxies[url]["enabled"] is True
    assert gw.proxy(url).manually_disabled is False


def test_proxy_set_unknown_is_404(make_gateway):
    gw = make_gateway()
    r = gw.internal.post("/internal/proxies/set",
                         json={"proxy": "http://nope.invalid:1", "enabled": True},
                         headers=H)
    assert r.status_code == 404


def test_refresh_proxies_syncs_storage_and_preserves_enabled():
    import asyncio

    async def run():
        storage = FakeStorage(proxies=[
            {"proxy": "http://h1:1", "enabled": True},
            {"proxy": "http://h2:2", "enabled": False},
        ])
        m = ProxyManager(build_settings())
        m.bind_storage(storage)
        m.apply_proxies(await storage.list_proxies(listed=True))
        m._probe_proxy = types.MethodType(_alive_probe, m)
        await _fake_fetch(m, {"proxies": [
            {"host": "h1", "port": 1},
            {"host": "h2", "port": 2},
            {"host": "h3", "port": 3},
        ]})
        assert await m.refresh_proxies() is True
        by_url = {p.url: p for p in m.proxies}
        assert by_url["http://h1:1"].manually_disabled is False
        assert by_url["http://h2:2"].manually_disabled is True
        assert by_url["http://h3:3"].manually_disabled is True  # new = disabled
        assert storage.proxies["http://h1:1"]["enabled"] is True
        assert storage.proxies["http://h2:2"]["enabled"] is False
        assert storage.proxies["http://h3:3"]["enabled"] is False
        await m.aclose()

    asyncio.run(run())


def test_refresh_proxies_fails_closed_when_storage_is_unavailable():
    import asyncio

    async def run():
        storage = FakeStorage(proxies=[
            {"proxy": "http://old-on:1", "enabled": True},
            {"proxy": "http://old-off:2", "enabled": False},
        ])
        m = ProxyManager(build_settings())
        m.bind_storage(storage)
        m.apply_proxies(await storage.list_proxies(listed=True))
        storage.raise_unavailable()  # sync fails, the fetch itself still works
        m._probe_proxy = types.MethodType(_alive_probe, m)
        await _fake_fetch(m, {"proxies": [
            {"host": "old-on", "port": 1},
            {"host": "old-off", "port": 2},
            {"host": "new", "port": 3},
        ]})
        assert await m.refresh_proxies() is True
        by_url = {p.url: p for p in m.proxies}
        assert by_url["http://old-on:1"].manually_disabled is False
        assert by_url["http://old-off:2"].manually_disabled is True
        assert by_url["http://new:3"].manually_disabled is True
        assert "1/3 enabled" in (m.last_fetch_status or "")
        # The failed sync made no writes: storage keeps its previous view.
        assert set(storage.proxies) == {"http://old-on:1", "http://old-off:2"}
        await m.aclose()

    asyncio.run(run())


# --- parking persistence ----------------------------------------------------


def test_parking_is_persisted_in_the_background():
    import asyncio

    async def run():
        storage = FakeStorage(proxies=["http://h1:1"])
        m = ProxyManager(build_settings())
        m.bind_storage(storage)
        m.apply_proxies(await storage.list_proxies(listed=True))
        group = m.egress_groups()[0]
        until = time.time() + 3600

        m.mark_quota_exhausted(group, until)
        # Scheduled, not awaited inline: the request path is never blocked.
        assert m._bg_tasks, "parking write was not scheduled"
        assert storage.proxies["http://h1:1"]["quota_until"] == 0.0

        await m.aclose()  # drains fire-and-forget writes
        assert m._bg_tasks == set()
        row = storage.proxies["http://h1:1"]
        assert row["quota_until"] == pytest.approx(until)

    asyncio.run(run())


def test_parking_persistence_failure_is_swallowed():
    import asyncio

    async def run():
        storage = FakeStorage(proxies=["http://h1:1"])
        m = ProxyManager(build_settings())
        m.bind_storage(storage)
        m.apply_proxies(await storage.list_proxies(listed=True))
        group = m.egress_groups()[0]
        until = time.time() + 3600
        storage.fail_next(StorageUnavailable("storage down"))

        m.mark_quota_exhausted(group, until)  # must not raise
        assert group.quota_until == pytest.approx(until)
        await m.aclose()  # drains the failed write without raising

    asyncio.run(run())


def test_free_usage_limit_parking_failure_keeps_serving(make_gateway):
    """A storage outage on the parking write must not break the client request."""
    gw = make_gateway()
    calls = {"n": 0}

    def router(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429, json={"error": {
                    "type": "FreeUsageLimitError", "message": "daily limit"}})
        return _sse_response()

    gw.script_upstream(router)
    gw.storage.raise_unavailable()
    r = gw.client.post("/v1/chat/completions", json=_body(),
                       headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200, r.text
    assert calls["n"] >= 2
    # The in-memory pool is parked; only the persistence write was lost.
    assert gw.manager.status()["egress_groups_quota_parked"] == 1
    assert all(row["quota_until"] == 0.0 for row in gw.storage.proxies.values())
