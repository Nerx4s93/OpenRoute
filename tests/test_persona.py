"""Unit tests for proxy personas (gateway.personas) — no real DB or network.

Persistence is exercised against the in-memory ``FakeStorage``; local async
subclasses model slow/hanging/failing storage-service calls.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import gateway.personas as personas_mod
from fake_storage import FakeStorage
from gateway import opencode
from gateway.personas import Persona, PersonaStore
from gateway.storage import StorageUnavailable


def _assert_valid(persona: Persona) -> None:
    assert opencode.is_session_id(persona.session_id)
    assert opencode.is_project_id(persona.project_id)
    assert opencode.is_trace_id(persona.trace_id)
    assert isinstance(persona.created_at, float)
    assert persona.generation >= 0


def _row(persona: Persona) -> dict:
    return {
        "quota_key": persona.quota_key,
        "session_id": persona.session_id,
        "project_id": persona.project_id,
        "trace_id": persona.trace_id,
        "created_at": persona.created_at,
        "generation": persona.generation,
    }


class TrackingStorage(FakeStorage):
    """FakeStorage that records successful persona upserts."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.upserts: list[Persona] = []

    async def upsert_persona(self, persona):
        written = await super().upsert_persona(persona)
        if written:
            self.upserts.append(persona)
        return written


def _fast_persist(monkeypatch):
    monkeypatch.setattr(personas_mod, "_PERSIST_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(personas_mod, "_FLUSH_INTERVAL_SECONDS", 0.01)


def test_persona_create_has_valid_fields():
    p = Persona.create("a.invalid:8080")
    _assert_valid(p)
    assert p.quota_key == "a.invalid:8080"
    assert p.generation == 0
    assert Persona.create("k", generation=7).generation == 7


@pytest.mark.asyncio
async def test_get_or_create_is_stable_and_keyed_by_bucket():
    store = PersonaStore()
    first = await store.get_or_create("a.invalid:8080")
    again = await store.get_or_create("a.invalid:8080")
    other = await store.get_or_create("b.invalid:8080")
    _assert_valid(first)
    assert first is again
    assert other.session_id != first.session_id
    assert other.project_id != first.project_id
    assert other.trace_id != first.trace_id
    assert store.get_cached("a.invalid:8080") is first
    assert store.get_cached("missing") is None
    await store.close()


@pytest.mark.asyncio
async def test_get_or_create_parallel_calls_share_one_persona():
    store = PersonaStore()
    results = await asyncio.gather(
        *(store.get_or_create("bucket:1") for _ in range(20)))
    assert len({p.session_id for p in results}) == 1
    assert all(p == results[0] for p in results)
    assert store.get_cached("bucket:1") == results[0]
    await store.close()


@pytest.mark.asyncio
async def test_rotate_increments_generation_and_replaces_ids():
    store = PersonaStore()
    before = await store.get_or_create("a.invalid:8080")
    after = await store.rotate("a.invalid:8080")
    _assert_valid(after)
    assert after.generation == before.generation + 1
    assert after.session_id != before.session_id
    assert after.project_id != before.project_id
    assert after.trace_id != before.trace_id
    assert store.get_cached("a.invalid:8080") is after
    # Rotation of an unknown bucket starts at generation 1.
    assert (await store.rotate("new.invalid:1")).generation == 1
    await store.close()


@pytest.mark.asyncio
async def test_unavailable_storage_load_fails_but_lookup_stays_valid():
    """A failed initial load is reportable (startup); requests keep working."""
    storage = FakeStorage()
    storage.raise_unavailable()
    store = PersonaStore(storage=storage)

    with pytest.raises(StorageUnavailable):
        await store.load()
    assert store.get_cached("a.invalid:8080") is None

    persona = await store.get_or_create("a.invalid:8080")  # must not raise
    _assert_valid(persona)
    assert store.get_cached("a.invalid:8080") is persona

    rotated = await store.rotate("a.invalid:8080")  # must not raise
    assert rotated.generation == 1

    await store.close()  # swallows failed writes
    await store.close()  # idempotent


@pytest.mark.asyncio
async def test_persist_retries_after_failure(monkeypatch):
    _fast_persist(monkeypatch)
    storage = FakeStorage()
    store = PersonaStore(storage=storage)
    await store.load()

    storage.fail_next(StorageUnavailable("storage down"))
    persona = await store.get_or_create("bucket:1")
    await asyncio.sleep(0)  # let the scheduled write run and fail
    assert "bucket:1" not in storage.personas
    assert "bucket:1" in store._dirty  # still queued for a retry

    assert await store.get_or_create("bucket:1") is persona
    await asyncio.sleep(0.05)
    assert "bucket:1" in storage.personas, "dirty persona was never retried"
    assert storage.personas["bucket:1"]["session_id"] == persona.session_id
    assert storage.personas["bucket:1"]["generation"] == 0
    await store.close()


@pytest.mark.asyncio
async def test_rotation_during_backoff_persists_new_generation(monkeypatch):
    _fast_persist(monkeypatch)
    storage = FakeStorage()
    store = PersonaStore(storage=storage)
    await store.load()

    storage.fail_next(StorageUnavailable("storage down"))
    first = await store.get_or_create("bucket:1")
    rotated = await store.rotate("bucket:1")  # generation 1, during backoff
    await asyncio.sleep(0.05)
    await store.get_or_create("bucket:1")  # cache hit -> retry if expired
    await asyncio.sleep(0.05)

    assert "bucket:1" in storage.personas, "generation 1 was never written"
    assert storage.personas["bucket:1"]["generation"] == 1
    assert storage.personas["bucket:1"]["session_id"] == rotated.session_id
    assert rotated.session_id != first.session_id
    await store.close()


@pytest.mark.asyncio
async def test_reconcile_with_storage_after_failed_load(monkeypatch):
    _fast_persist(monkeypatch)

    class LoadFailsStorage(FakeStorage):
        async def load_personas(self):
            raise StorageUnavailable("storage down during load")

    stored = Persona.create("bucket:1", generation=5)
    storage = LoadFailsStorage(personas=[stored])
    store = PersonaStore(storage=storage)
    with pytest.raises(StorageUnavailable):
        await store.load()
    assert store.get_cached("bucket:1") is None

    created = await store.get_or_create("bucket:1")  # generation 0, rejected
    assert created.generation == 0
    await asyncio.sleep(0.05)

    cached = store.get_cached("bucket:1")
    assert cached is not None
    assert cached.generation >= 5
    assert cached.session_id == stored.session_id  # stored row won

    rotated = await store.rotate("bucket:1")
    assert rotated.generation == 6
    await asyncio.sleep(0.05)
    assert storage.personas["bucket:1"]["generation"] == 6
    assert storage.personas["bucket:1"]["session_id"] == rotated.session_id
    await store.close()


@pytest.mark.asyncio
async def test_load_replaces_invalid_row_with_valid_persona(monkeypatch):
    _fast_persist(monkeypatch)
    storage = TrackingStorage(personas=[{
        "quota_key": "bucket:1",
        "session_id": "not-a-session",
        "project_id": opencode.new_project_id(),
        "trace_id": opencode.new_trace_id(),
        "created_at": 10.0,
        "generation": 3,
    }])
    store = PersonaStore(storage=storage)
    await store.load()
    await asyncio.sleep(0.05)

    cached = store.get_cached("bucket:1")
    assert cached is not None
    _assert_valid(cached)
    assert cached.generation == 4
    assert storage.upserts and storage.upserts[-1].generation == 4
    assert storage.upserts[-1].session_id == cached.session_id
    assert storage.personas["bucket:1"]["session_id"] == cached.session_id
    await store.close()


@pytest.mark.asyncio
async def test_load_restores_rows_without_downgrading_generation():
    a = Persona.create("a.invalid:1", generation=4)
    b = Persona.create("b.invalid:2", generation=2)
    storage = FakeStorage(personas=[a, b])
    store = PersonaStore(storage=storage)
    await store.load()
    assert store.get_cached("a.invalid:1").generation == 4
    assert store.get_cached("b.invalid:2").generation == 2

    # A newer in-memory persona (created while load raced startup) wins over
    # the older row on a repeated load.
    rotated = await store.rotate("a.invalid:1")
    assert rotated.generation == 5
    await store.load()
    assert store.get_cached("a.invalid:1") is rotated
    assert rotated.generation == 5
    await store.close()


@pytest.mark.asyncio
async def test_close_flushes_dirty_persona(monkeypatch):
    _fast_persist(monkeypatch)
    storage = FakeStorage()
    store = PersonaStore(storage=storage)
    persona = await store.get_or_create("bucket:1")
    await store.close()
    assert storage.personas["bucket:1"]["session_id"] == persona.session_id
    # The shared storage client is owned by main and must stay open.
    assert storage.closed is False


@pytest.mark.asyncio
async def test_close_is_idempotent_and_store_keeps_working_in_memory():
    store = PersonaStore()
    persona = await store.get_or_create("a.invalid:8080")
    await store.close()
    await store.close()

    assert store.get_cached("a.invalid:8080") is persona
    assert await store.get_or_create("a.invalid:8080") is persona
    rotated = await store.rotate("a.invalid:8080")
    assert rotated.generation == persona.generation + 1
    assert store.get_cached("a.invalid:8080") is rotated


@pytest.mark.asyncio
async def test_close_does_not_duplicate_inflight_write():
    class SlowStorage(TrackingStorage):
        async def upsert_persona(self, persona):
            await asyncio.sleep(0.05)
            return await super().upsert_persona(persona)

    storage = SlowStorage()
    store = PersonaStore(storage=storage)
    persona = await store.get_or_create("k")
    await store.close()

    assert len(storage.upserts) == 1
    assert storage.upserts[0].session_id == persona.session_id


@pytest.mark.asyncio
async def test_close_is_bounded_when_storage_hangs(monkeypatch):
    monkeypatch.setattr(personas_mod, "_CLOSE_TIMEOUT_SECONDS", 0.05)

    class HangingStorage(TrackingStorage):
        async def upsert_persona(self, persona):
            await asyncio.sleep(0.2)
            return await super().upsert_persona(persona)

    storage = HangingStorage()
    store = PersonaStore(storage=storage)
    for key in ("k1", "k2", "k3"):
        await store.get_or_create(key)

    started = time.monotonic()
    await store.close()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_fallback_persona_is_cached(monkeypatch):
    store = PersonaStore()

    def boom(persona):
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "_persist", boom)
    first = await store.get_or_create("k")
    second = await store.get_or_create("k")

    assert first is second
    assert store.get_cached("k") is first
    await store.close()


@pytest.mark.asyncio
async def test_load_invalid_row_keeps_newer_cached_persona():
    store = PersonaStore()
    await store.get_or_create("k")
    current = await store.rotate("k")  # generation 1
    storage = TrackingStorage(personas=[{
        "quota_key": "k",
        "session_id": "not-a-session",
        "project_id": opencode.new_project_id(),
        "trace_id": opencode.new_trace_id(),
        "created_at": 1.0,
        "generation": 0,
    }])
    store.bind_storage(storage)

    await store.load()

    cached = store.get_cached("k")
    assert cached is current
    _assert_valid(cached)
    assert cached.generation == 1
    await asyncio.sleep(0.05)
    assert storage.upserts == []
    await store.close()
