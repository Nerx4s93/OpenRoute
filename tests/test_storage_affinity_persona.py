"""Affinity and personas persist through storage-service.

These tests exercise the storage-backed gateway paths in isolation (unit
level) and the startup contract: when the initial affinity/persona load fails
after proxies and keys succeeded, the gateway must refuse to start with a
secret-free error.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import gateway.main as main
from fake_storage import FakeStorage
from gateway.affinity import AffinityManager, SessionIdentity, SessionState
from gateway.keys import KeyStore, hash_key
from gateway.personas import Persona, PersonaStore
from gateway.proxies import ProxyManager
from gateway.storage import StorageUnavailable

from gw import _quiet_refresh_loop, build_settings

_STORAGE_TOKEN = "STORAGE-TOKEN-DO-NOT-LEAK"


def _affinity(storage, proxies=("http://a.invalid:1", "http://b.invalid:1"),
              now: float = 1_000.0):
    settings = build_settings()
    manager = ProxyManager(settings)
    manager.apply_proxies([(url, True) for url in proxies])
    affinity = AffinityManager(
        settings, manager, storage=storage, now_wall=lambda: now)
    return affinity, manager


def _key_rows():
    return [{
        "key_hash": hash_key("test-key"),
        "key_id": "default",
        "rpm": None,
        "tpm": None,
        "models": None,
        "enabled": True,
    }]


# --- affinity ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_affinity_loads_from_storage_and_coalesces_writes():
    storage = FakeStorage(
        proxies=["http://a.invalid:1", "http://b.invalid:1"],
        affinity=[{
            "session_key": "sid:old",
            "proxy": "http://a.invalid:1",
            "created_at": 100.0,
            "last_used_at": 200.0,
        }],
    )
    affinity, _manager = _affinity(storage)

    await affinity.load()
    state = affinity._sessions["sid:old"]
    assert state.proxy == "http://a.invalid:1"
    assert state.created_at == 100.0 and state.last_used_at == 200.0
    assert list(state.events) == []

    # A new session is bound and persisted through the coalescing writer.
    lease = affinity.begin_request(
        SessionIdentity("ses_new", "sid:ses_new", "sid"))
    assert lease.state is not None and lease.state.proxy is not None
    await affinity.flush()
    stored = storage.affinity["sid:ses_new"]
    assert stored["proxy"] == lease.state.proxy
    assert stored["created_at"] == lease.state.created_at

    # Two snapshots of the same key coalesce to the latest one.
    state.last_used_at = 300.0
    affinity._schedule_write(state)
    state.last_used_at = 400.0
    affinity._schedule_write(state)
    await affinity.flush()
    assert storage.affinity["sid:old"]["last_used_at"] == 400.0
    assert storage.affinity["sid:old"]["proxy"] == "http://a.invalid:1"

    await affinity.aclose()
    # The shared storage client is owned by main and must stay open.
    assert storage.closed is False


@pytest.mark.asyncio
async def test_affinity_missing_proxy_persists_null_and_reconciles_memory():
    storage = FakeStorage(proxies=["http://a.invalid:1"])
    affinity, _manager = _affinity(
        storage, proxies=("http://a.invalid:1", "http://gone.invalid:2"))
    healthy = SessionState("sid:healthy", "http://a.invalid:1", 10.0, 11.0)
    vanished = SessionState("sid:gone", "http://gone.invalid:2", 10.0, 11.0)
    affinity._sessions[healthy.key] = healthy
    affinity._sessions[vanished.key] = vanished

    affinity._schedule_write(healthy)
    affinity._schedule_write(vanished)
    await affinity.flush()

    # The service's FK fallback stores the vanished binding as NULL; only that
    # session is unbound in memory, the healthy one keeps its proxy.
    assert storage.affinity["sid:gone"]["proxy"] is None
    assert vanished.proxy is None
    assert storage.affinity["sid:healthy"]["proxy"] == "http://a.invalid:1"
    assert healthy.proxy == "http://a.invalid:1"
    await affinity.aclose()


@pytest.mark.asyncio
async def test_affinity_load_reports_storage_failure():
    storage = FakeStorage(proxies=["http://a.invalid:1"])
    storage.raise_unavailable()
    affinity, _manager = _affinity(storage)
    with pytest.raises(StorageUnavailable):
        await affinity.load()
    assert affinity._sessions == {}


# --- personas ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_store_loads_creates_and_persists_via_storage():
    seeded = Persona.create("seed.invalid:1", generation=2)
    storage = FakeStorage(personas=[seeded])
    store = PersonaStore(storage=storage)

    await store.load()
    assert store.get_cached("seed.invalid:1") == seeded

    created = await store.get_or_create("new.invalid:2")
    await store.close()
    assert storage.personas["new.invalid:2"]["session_id"] == created.session_id
    assert storage.personas["new.invalid:2"]["generation"] == 0
    # The shared storage client is not closed by the store.
    assert storage.closed is False
    # The cache remains usable after close (requests still need a persona).
    assert await store.get_or_create("new.invalid:2") is created


@pytest.mark.asyncio
async def test_persona_generation_regression_is_not_overwritten_and_rotates():
    key = "proxy.invalid:8080"
    newer = Persona.create(key, generation=5)
    storage = FakeStorage(personas=[newer])
    store = PersonaStore(storage=storage)
    await store.load()
    assert store.get_cached(key) == newer

    # A stale fire-and-forget write is rejected by the service's generation
    # guard; the store adopts the newer persisted row instead of overwriting.
    stale = Persona.create(key, generation=3)
    store._dirty[key] = stale
    await store._flush_key(key)
    assert storage.personas[key]["generation"] == 5
    assert storage.personas[key]["session_id"] == newer.session_id
    assert store.get_cached(key) == newer
    assert key not in store._dirty

    rotated = await store.rotate(key)
    assert rotated.generation == 6
    assert rotated.session_id != newer.session_id
    await store.close()
    assert storage.personas[key]["generation"] == 6
    assert storage.personas[key]["session_id"] == rotated.session_id


# --- startup contract -------------------------------------------------------


class _AffinityLoadFails(FakeStorage):
    async def load_affinity(self):
        raise StorageUnavailable("affinity store unavailable")


class _PersonaLoadFails(FakeStorage):
    async def load_personas(self):
        raise StorageUnavailable("persona store unavailable")


def _configure_startup(monkeypatch, storage):
    settings = build_settings(storage_api_token=_STORAGE_TOKEN)
    manager = ProxyManager(settings)
    keystore = KeyStore()
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "manager", manager)
    monkeypatch.setattr(main, "keystore", keystore)
    monkeypatch.setattr(main, "storage", storage)
    monkeypatch.setattr(main, "affinity", None)
    monkeypatch.setattr(main, "personas", None)
    monkeypatch.setattr(main, "refresh_loop", _quiet_refresh_loop)


def _assert_secret_free(exc_info) -> None:
    message = str(exc_info.value)
    assert _STORAGE_TOKEN not in message
    assert "postgresql://" not in message  # no direct-DB configuration remains


def test_startup_fails_when_affinity_load_fails(monkeypatch):
    storage = _AffinityLoadFails(
        proxies=["http://proxy-a.invalid:8080"], keys=_key_rows())
    _configure_startup(monkeypatch, storage)

    with pytest.raises(RuntimeError, match="session affinity") as exc_info:
        with TestClient(main.app):
            pass

    _assert_secret_free(exc_info)
    # Proxies and keys loaded before the affinity failure (the fake is intact
    # apart from being closed) and no half-initialised gateway is left running.
    assert "http://proxy-a.invalid:8080" in storage.proxies
    assert storage.closed is True
    assert main.storage is None
    assert main.affinity is None


def test_startup_fails_when_persona_load_fails(monkeypatch):
    storage = _PersonaLoadFails(
        proxies=["http://proxy-a.invalid:8080"], keys=_key_rows())
    _configure_startup(monkeypatch, storage)

    with pytest.raises(RuntimeError, match="proxy personas") as exc_info:
        with TestClient(main.app):
            pass

    _assert_secret_free(exc_info)
    assert "http://proxy-a.invalid:8080" in storage.proxies
    assert storage.closed is True
    assert main.storage is None
    assert main.personas is None
