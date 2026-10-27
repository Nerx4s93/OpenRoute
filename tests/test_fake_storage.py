"""Semantics tests for the in-memory ``FakeStorage`` used by gateway tests.

No database or network is involved: the fake is the unit under test here, so
the gateway tests can rely on it to assert behaviour without PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fake_storage import FakeStorage
from gateway.storage import (
    StorageAPIError,
    StorageConflict,
    StorageNotFound,
    StorageUnavailable,
)

PROXY_A = "http://proxy-a.invalid:8080"
PROXY_B = "http://proxy-b.invalid:8080"
PROXY_C = "http://proxy-c.invalid:8080"


def _utc(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


# --- proxies ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_snapshot_semantics_parking_and_soft_delete():
    fake = FakeStorage()

    first = await fake.sync_proxies([PROXY_A, PROXY_B, PROXY_A, ""])
    assert first["added"] == 2
    assert [row["proxy"] for row in first["proxies"]] == [PROXY_A, PROXY_B]
    assert all(row["enabled"] is False for row in first["proxies"])
    assert all(row["listed"] is True for row in first["proxies"])

    assert await fake.set_proxy_enabled(PROXY_A, True) is True

    second = await fake.sync_proxies([PROXY_B, PROXY_C])
    assert second["added"] == 1
    # Existing flags survive; the new proxy is disabled.
    assert fake.proxies[PROXY_A]["enabled"] is True
    assert fake.proxies[PROXY_A]["listed"] is False
    assert fake.proxies[PROXY_B]["enabled"] is False
    assert fake.proxies[PROXY_C]["enabled"] is False
    assert [row["proxy"] for row in await fake.list_proxies()] == [
        PROXY_A, PROXY_B, PROXY_C
    ]
    assert [row["proxy"] for row in await fake.list_proxies(listed=True)] == [
        PROXY_B, PROXY_C
    ]
    assert [row["proxy"] for row in await fake.list_proxies(listed=False)] == [
        PROXY_A
    ]

    assert await fake.save_parking(
        [PROXY_A, PROXY_A, "http://missing.invalid:1"],
        quota_until=100.0, banned_until=200.0, permanently_banned=True,
    ) == 1
    assert fake.proxies[PROXY_A]["quota_until"] == 100.0
    assert fake.proxies[PROXY_A]["banned_until"] == 200.0

    # Parking survives a soft removal and comes back when the proxy reappears.
    await fake.sync_proxies([PROXY_B])
    assert fake.proxies[PROXY_A]["listed"] is False
    await fake.sync_proxies([PROXY_A])
    assert fake.proxies[PROXY_A]["listed"] is True
    assert fake.proxies[PROXY_A]["quota_until"] == 100.0

    # A permanent ban can only be added, never cleared by a later mark.
    await fake.save_parking([PROXY_A], 0.0, 0.0, False)
    assert fake.proxies[PROXY_A]["permanently_banned"] is True
    assert fake.proxies[PROXY_A]["quota_until"] == 0.0

    with pytest.raises(StorageNotFound):
        await fake.set_proxy_enabled("http://missing.invalid:1", True)


# --- keys -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_hash_add_rotate_idempotency_and_conflict():
    fake = FakeStorage()
    old = "a" * 64
    new = "b" * 64
    other = "c" * 64

    assert await fake.add_key(old, "k1", rpm=60, tpm=None, models=["m1"]) is True
    assert await fake.add_key(old, "ignored", rpm=1) is False
    row = (await fake.list_keys())[0]
    assert row == {
        "key_hash": old, "key_id": "k1", "rpm": 60, "tpm": None,
        "models": ["m1"], "enabled": True,
    }

    rotated = await fake.rotate_key(old, new)
    assert rotated == {"rotated": True, "already_rotated": False, "key_id": "k1"}
    assert old not in fake.keys
    assert fake.keys[new]["key_id"] == "k1"
    assert fake.keys[new]["rpm"] == 60
    assert fake.keys[new]["models"] == ["m1"]

    retry = await fake.rotate_key(old, new)
    assert retry == {"rotated": False, "already_rotated": True, "key_id": "k1"}

    assert await fake.add_key(other, "k2") is True
    with pytest.raises(StorageConflict):
        await fake.rotate_key(new, other)
    with pytest.raises(StorageNotFound):
        await fake.rotate_key("d" * 64, "e" * 64)

    assert await fake.set_key_enabled(new, False) is True
    assert fake.keys[new]["enabled"] is False
    assert await fake.delete_key(other) is True
    with pytest.raises(StorageNotFound):
        await fake.delete_key(other)


# --- affinity ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_affinity_moves_missing_proxy_and_clear_keep_created_at():
    fake = FakeStorage(proxies=[PROXY_A, PROXY_B])

    assert await fake.upsert_affinity([{
        "session_key": "sid:one",
        "proxy": PROXY_A,
        "created_at": 10.0,
        "last_used_at": 11.0,
    }]) == {"written": 1, "unbound": 0}

    # A move updates only proxy/last_used_at; created_at is immutable.
    assert await fake.upsert_affinity([
        ("sid:one", PROXY_B, 999.0, 12.0),
    ]) == {"written": 1, "unbound": 0}
    assert fake.affinity["sid:one"] == {
        "session_key": "sid:one",
        "proxy": PROXY_B,
        "created_at": 10.0,
        "last_used_at": 12.0,
    }

    # A binding to a vanished proxy is stored unbound (old FK fallback).
    assert await fake.upsert_affinity([{
        "session_key": "sid:gone",
        "proxy": "http://vanished.invalid:1",
        "created_at": 9.0,
        "last_used_at": 9.5,
    }]) == {"written": 1, "unbound": 1}
    assert fake.affinity["sid:gone"]["proxy"] is None

    loaded = await fake.load_affinity()
    assert [row["session_key"] for row in loaded] == ["sid:gone", "sid:one"]

    # Clearing keeps the row (and its S slot) and ignores duplicates/misses.
    assert await fake.clear_affinity(
        ["sid:one", "sid:one", "sid:missing"]
    ) == 1
    assert fake.affinity["sid:one"]["proxy"] is None
    assert fake.affinity["sid:one"]["created_at"] == 10.0


# --- personas ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_generation_guard_and_lookup():
    fake = FakeStorage()

    def persona(generation: int, **over) -> dict:
        row = {
            "quota_key": "proxy-a.invalid:8080",
            "session_id": "ses_1",
            "project_id": "b" * 40,
            "trace_id": "c" * 32,
            "created_at": 10.0,
            "generation": generation,
        }
        row.update(over)
        return row

    assert await fake.upsert_persona(persona(2)) is True
    assert await fake.get_persona("proxy-a.invalid:8080") == persona(2)

    stale = persona(1, session_id="ses_stale", created_at=99.0)
    assert await fake.upsert_persona(stale) is False
    assert fake.personas["proxy-a.invalid:8080"]["session_id"] == "ses_1"

    # Same generation may rewrite (retry of our own write).
    retry = persona(2, session_id="ses_retry")
    assert await fake.upsert_persona(retry) is True
    assert fake.personas["proxy-a.invalid:8080"]["session_id"] == "ses_retry"

    assert await fake.get_persona("missing.invalid:1") is None
    assert len(await fake.load_personas()) == 1


# --- request log / stats ----------------------------------------------------


def _entry(**over) -> dict:
    row = {
        "ts": _utc(2026, 5, 20, 23, 0),
        "key_id": "k1",
        "method": "POST",
        "path": "chat/completions",
        "model": "deepseek-v4.1-flash-free",
        "status": 200,
        "latency_ms": 100.0,
        "stream": False,
        "account": "a:1",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "error": None,
    }
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_request_log_recent_utc_daily_and_aggregates():
    fake = FakeStorage()
    day1 = _utc(2026, 5, 20, 23, 0)
    day2 = _utc(2026, 5, 21, 1, 0)

    await fake.log_request(_entry(ts=day1))
    await fake.log_request(_entry(
        ts=day1, key_id="k2", status=500, latency_ms=300.0,
        account=None, prompt_tokens=1, completion_tokens=1,
    ))
    await fake.log_request(_entry(
        ts=day2, account="b:2", latency_ms=200.0,
        prompt_tokens=2, completion_tokens=3,
    ))

    assert [row["ts"] for row in await fake.recent_requests(2)] == [day2, day1]
    with pytest.raises(StorageAPIError):
        await fake.recent_requests(0)

    summary = await fake.stats_summary()
    assert summary["k1"] == {
        "requests": 2, "errors": 0, "tokens": 20, "avg_latency_ms": 150.0,
    }
    assert summary["k2"] == {
        "requests": 1, "errors": 1, "tokens": 2, "avg_latency_ms": 300.0,
    }
    assert summary["total"] == {
        "requests": 3, "errors": 1, "tokens": 22, "avg_latency_ms": 200.0,
    }

    limited = await fake.stats_summary(since=day2)
    assert limited["total"]["requests"] == 1
    assert "k2" not in limited

    assert await fake.stats_by_account() == {
        "a:1": {"requests": 1, "errors": 0, "tokens": 15},
        "b:2": {"requests": 1, "errors": 0, "tokens": 5},
    }
    assert await fake.stats_by_account(since=day2) == {
        "b:2": {"requests": 1, "errors": 0, "tokens": 5},
    }

    daily = await fake.stats_daily()
    assert daily == [
        {"day": "2026-05-20", "account": "a:1",
         "requests": 1, "errors": 0, "tokens": 15},
        {"day": "2026-05-21", "account": "b:2",
         "requests": 1, "errors": 0, "tokens": 5},
    ]
    assert await fake.stats_daily(since=day2) == [daily[1]]


# --- hooks / lifecycle ------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_hooks_disable_cleanly_and_context_manager_closes():
    fake = FakeStorage(proxies=[PROXY_A])
    assert await fake.check() == {
        "status": "ok", "database": "ok", "proxies": 1,
    }
    assert await fake.ping() is True

    fake.raise_unavailable()
    with pytest.raises(StorageUnavailable):
        await fake.list_keys()
    assert await fake.ping() is False

    fake.raise_unavailable(False)

    fake.fail_next(StorageConflict("boom", status_code=409))
    with pytest.raises(StorageConflict):
        await fake.list_keys()
    assert await fake.list_keys() == []  # one-shot hook already consumed

    async with fake as entered:
        assert entered is fake
        assert fake.closed is False
    assert fake.closed is True
    with pytest.raises(StorageUnavailable):
        await fake.check()
