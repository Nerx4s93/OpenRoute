"""Unit tests for proxy personas (gateway.personas) — no real DB or network."""

from __future__ import annotations

import asyncio
import time

import pytest

import gateway.personas as personas_mod
from gateway import opencode
from gateway.personas import Persona, PersonaStore


def _assert_valid(persona: Persona) -> None:
    assert opencode.is_session_id(persona.session_id)
    assert opencode.is_project_id(persona.project_id)
    assert opencode.is_trace_id(persona.trace_id)
    assert isinstance(persona.created_at, float)
    assert persona.generation >= 0


def _row(persona: Persona) -> tuple[str, str, str, str, float, int]:
    return (persona.quota_key, persona.session_id, persona.project_id,
            persona.trace_id, persona.created_at, persona.generation)


class FakeDB:
    """In-memory PersonaDB double honouring the generation guard."""

    def __init__(self, rows=(), *, fail_upserts: int = 0):
        self.rows = list(rows)
        self.fail_upserts = fail_upserts
        self.saved: list[Persona] = []
        self.closed = False

    def load_all(self):
        return list(self.rows)

    def load_one(self, quota_key):
        for row in self.rows:
            if row[0] == quota_key:
                return row
        return None

    def upsert(self, persona):
        if self.fail_upserts:
            self.fail_upserts -= 1
            raise RuntimeError("db down")
        stored = self.load_one(persona.quota_key)
        if stored is not None and stored[5] > persona.generation:
            return False
        self.rows = [r for r in self.rows if r[0] != persona.quota_key]
        self.rows.append(_row(persona))
        self.saved.append(persona)
        return True

    def close(self):
        self.closed = True


class BadDB:
    """Every operation fails: the store must degrade to memory-only."""

    def load_all(self):
        raise RuntimeError("db down")

    def load_one(self, quota_key):
        raise RuntimeError("db down")

    def upsert(self, persona):
        raise RuntimeError("db down")

    def close(self):
        raise RuntimeError("db down")


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
    store = PersonaStore(None)
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
    store = PersonaStore(None)
    results = await asyncio.gather(
        *(store.get_or_create("bucket:1") for _ in range(20)))
    assert len({p.session_id for p in results}) == 1
    assert all(p == results[0] for p in results)
    assert store.get_cached("bucket:1") == results[0]
    await store.close()


@pytest.mark.asyncio
async def test_rotate_increments_generation_and_replaces_ids():
    store = PersonaStore(None)
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
async def test_bad_db_never_breaks_lookup_or_load():
    store = PersonaStore(BadDB())
    await store.load()  # must not raise
    assert store.get_cached("a.invalid:8080") is None

    persona = await store.get_or_create("a.invalid:8080")  # must not raise
    _assert_valid(persona)
    assert store.get_cached("a.invalid:8080") is persona

    rotated = await store.rotate("a.invalid:8080")  # must not raise
    assert rotated.generation == 1

    await store.close()  # gathers/quiesces failed writes, swallows close error
    await store.close()  # idempotent


@pytest.mark.asyncio
async def test_persist_retries_after_failure(monkeypatch):
    _fast_persist(monkeypatch)
    db = FakeDB(fail_upserts=1)
    store = PersonaStore(db)
    await store.load()

    persona = await store.get_or_create("bucket:1")
    await asyncio.sleep(0.05)  # first upsert fails -> backoff, then retried
    assert db.fail_upserts == 0

    assert await store.get_or_create("bucket:1") is persona
    await asyncio.sleep(0.05)
    assert db.saved, "dirty persona was never retried"
    assert db.saved[-1].session_id == persona.session_id
    assert db.saved[-1].generation == 0
    await store.close()


@pytest.mark.asyncio
async def test_rotation_during_backoff_persists_new_generation(monkeypatch):
    _fast_persist(monkeypatch)
    db = FakeDB(fail_upserts=1)
    store = PersonaStore(db)
    await store.load()

    first = await store.get_or_create("bucket:1")
    rotated = await store.rotate("bucket:1")  # generation 1, during backoff
    await asyncio.sleep(0.05)
    await store.get_or_create("bucket:1")  # cache hit -> retry if expired
    await asyncio.sleep(0.05)

    assert db.saved, "generation 1 was never written"
    assert db.saved[-1].generation == 1
    assert db.saved[-1].session_id == rotated.session_id
    assert rotated.session_id != first.session_id
    await store.close()


@pytest.mark.asyncio
async def test_reconcile_with_db_after_failed_load(monkeypatch):
    _fast_persist(monkeypatch)

    class LoadFailsDB(FakeDB):
        def load_all(self):
            raise RuntimeError("db down during load")

    stored = Persona.create("bucket:1", generation=5)
    db = LoadFailsDB(rows=[_row(stored)])
    store = PersonaStore(db)
    await store.load()
    assert store.get_cached("bucket:1") is None

    created = await store.get_or_create("bucket:1")  # generation 0, rejected
    assert created.generation == 0
    await asyncio.sleep(0.05)

    cached = store.get_cached("bucket:1")
    assert cached is not None
    assert cached.generation >= 5
    assert cached.session_id == stored.session_id  # DB row won

    rotated = await store.rotate("bucket:1")
    assert rotated.generation == 6
    await asyncio.sleep(0.05)
    assert db.saved[-1].generation == 6
    assert db.saved[-1].session_id == rotated.session_id
    await store.close()


@pytest.mark.asyncio
async def test_load_replaces_invalid_row_with_valid_persona(monkeypatch):
    _fast_persist(monkeypatch)
    row = ("bucket:1", "not-a-session", opencode.new_project_id(),
           opencode.new_trace_id(), 10.0, 3)
    db = FakeDB(rows=[row])
    store = PersonaStore(db)
    await store.load()
    await asyncio.sleep(0.05)

    cached = store.get_cached("bucket:1")
    assert cached is not None
    _assert_valid(cached)
    assert cached.generation == 4
    assert db.saved and db.saved[-1].generation == 4
    assert db.saved[-1].session_id == cached.session_id
    await store.close()


@pytest.mark.asyncio
async def test_load_restores_rows_without_downgrading_generation():
    class FakeLoadDB(FakeDB):
        def upsert(self, persona):
            return True

    db = FakeLoadDB(rows=[
        _row(Persona.create("a.invalid:1", generation=4)),
        _row(Persona.create("b.invalid:2", generation=2)),
    ])
    store = PersonaStore(db)
    await store.load()
    assert store.get_cached("a.invalid:1").generation == 4
    assert store.get_cached("b.invalid:2").generation == 2

    # A newer in-memory persona (created while load raced startup) wins.
    rotated = await store.rotate("a.invalid:1")
    assert rotated.generation == 5
    await store.load()
    assert store.get_cached("a.invalid:1") is rotated
    assert rotated.generation == 5
    await store.close()


@pytest.mark.asyncio
async def test_close_flushes_dirty_persona(monkeypatch):
    _fast_persist(monkeypatch)
    db = FakeDB()
    store = PersonaStore(db)
    persona = await store.get_or_create("bucket:1")
    await store.close()
    assert db.saved
    assert db.saved[-1].session_id == persona.session_id
    assert db.closed is True


@pytest.mark.asyncio
async def test_close_is_idempotent_and_store_keeps_working_in_memory():
    store = PersonaStore(None)
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
    class SlowDB(FakeDB):
        def upsert(self, persona):
            time.sleep(0.05)
            return super().upsert(persona)

    db = SlowDB()
    store = PersonaStore(db)
    persona = await store.get_or_create("k")
    await store.close()

    assert len(db.saved) == 1
    assert db.saved[0].session_id == persona.session_id
    assert db.closed is True


@pytest.mark.asyncio
async def test_close_is_bounded_when_db_hangs(monkeypatch):
    monkeypatch.setattr(personas_mod, "_CLOSE_TIMEOUT_SECONDS", 0.05)

    class HangingDB(FakeDB):
        def upsert(self, persona):
            time.sleep(0.2)
            return super().upsert(persona)

        def close(self):
            time.sleep(0.2)
            super().close()

    db = HangingDB()
    store = PersonaStore(db)
    for key in ("k1", "k2", "k3"):
        await store.get_or_create(key)

    started = time.monotonic()
    await store.close()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_fallback_persona_is_cached(monkeypatch):
    store = PersonaStore(None)

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
    store = PersonaStore(None)
    await store.get_or_create("k")
    current = await store.rotate("k")  # generation 1
    row = ("k", "not-a-session", opencode.new_project_id(),
           opencode.new_trace_id(), 1.0, 0)
    db = FakeDB(rows=[row])
    store._db = db

    await store.load()

    cached = store.get_cached("k")
    assert cached is current
    _assert_valid(cached)
    assert cached.generation == 1
    await asyncio.sleep(0.05)
    assert db.saved == []
    await store.close()
