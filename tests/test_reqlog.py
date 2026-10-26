"""Tests for the storage-backed request-log facade (``gateway/reqlog.py``).

No PostgreSQL and no ``pg_dsn``: the facade is exercised against the in-memory
``FakeStorage``. SQL persistence/aggregation itself is covered by
``tests/test_storage_service.py``; here we assert the gateway-observable
behaviour:

- the deque hot path and storage-backed reads;
- admin aggregate shapes (summary/total, by-account, UTC-day series);
- degradation on storage failure ("stats never 500, log never raises");
- close/drain/drop semantics;
- per-proxy lifetime stats TTL + copy isolation.
"""

import asyncio
import time
from datetime import datetime, timezone

import pytest

from fake_storage import FakeStorage
from gateway.reqlog import LogEntry, RequestLog


def e(i, key="k1", status=200, prompt=10, completion=5, error=None,
      latency=100.0, stream=False, account="1.2.3.4:8080", ts=None):
    return LogEntry(
        ts=time.time() + i if ts is None else ts, key_id=key, method="POST",
        path="chat/completions", model="deepseek-v4.1-flash-free", status=status,
        latency_ms=latency, stream=stream, account=account,
        prompt_tokens=prompt, completion_tokens=completion, error=error,
    )


def _entry(ts, account="1.2.3.4:8080", prompt=10, completion=5, status=200,
           error=None, key="k1"):
    return LogEntry(
        ts=ts, key_id=key, method="POST", path="chat/completions",
        model="deepseek-v4.1-flash-free", status=status, latency_ms=1.0,
        stream=False, account=account, prompt_tokens=prompt,
        completion_tokens=completion, error=error,
    )


# --- log / recent -----------------------------------------------------------


@pytest.mark.asyncio
async def test_log_and_recent_through_storage():
    storage = FakeStorage()
    rl = RequestLog(storage)
    await rl.log(e(1))
    await rl.log(e(2))

    assert len(storage.log) == 2  # the write reached the storage client
    rows = await rl.recent_async(2)
    assert len(rows) == 2
    assert rows[0]["ts"] > rows[1]["ts"]  # newest first
    assert rows[0]["key_id"] == "k1" and rows[0]["model"] == "deepseek-v4.1-flash-free"
    assert isinstance(rows[0]["stream"], bool)

    top = await rl.recent_async(1)
    assert len(top) == 1 and top[0]["ts"] == rows[0]["ts"]
    await rl.close()


@pytest.mark.asyncio
async def test_recent_reads_storage_beyond_the_hot_buffer():
    """Recent rows come from storage (the deque is only a fallback)."""
    storage = FakeStorage(log=[e(-3, stream=True), e(-2, stream=True),
                               e(-1, stream=True)])
    rl = RequestLog(storage)  # empty local deque: the rows are storage-only

    rows = await rl.recent_async(100)
    assert len(rows) == 3
    tss = [r["ts"] for r in rows]
    assert tss == sorted(tss, reverse=True)
    assert all(r["stream"] is True for r in rows)
    await rl.close()


# --- aggregates -------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_aggregation():
    rl = RequestLog(FakeStorage())
    await rl.log(e(1, key="kA", status=200, prompt=100, completion=50, latency=100.0))
    await rl.log(e(2, key="kA", status=500, prompt=10, completion=0, error="boom", latency=300.0))
    await rl.log(e(3, key="kB", status=200, prompt=30, completion=20, latency=200.0))

    s = await rl.summary_async()
    assert s["kA"] == {"requests": 2, "errors": 1, "tokens": 160, "avg_latency_ms": 200.0}
    assert s["kB"] == {"requests": 1, "errors": 0, "tokens": 50, "avg_latency_ms": 200.0}
    assert s["total"]["requests"] == 3
    assert s["total"]["errors"] == 1
    assert s["total"]["tokens"] == 210
    assert s["total"]["avg_latency_ms"] == 200.0
    await rl.close()


@pytest.mark.asyncio
async def test_summary_by_account():
    rl = RequestLog(FakeStorage())
    entry = e(1, key="kA", prompt=10, completion=5)
    entry.account = "5.5.5.5:3128"
    await rl.log(entry)

    by = await rl.summary_by_account_async()
    assert by["5.5.5.5:3128"]["requests"] == 1
    assert by["5.5.5.5:3128"]["tokens"] == 15
    await rl.close()


@pytest.mark.asyncio
async def test_summary_since_filters_entries_and_accounts():
    """The admin spending window filters both per-key and per-proxy aggregates."""
    rl = RequestLog(FakeStorage())
    now = time.time()
    old = e(0, key="old", prompt=100, completion=0)
    old.ts = now - 40 * 24 * 60 * 60
    old.account = "old.example:8080"
    recent = e(0, key="new", prompt=7, completion=3)
    recent.ts = now - 60
    recent.account = "new.example:8080"
    await rl.log(old)
    await rl.log(recent)

    assert (await rl.summary_async())["total"]["requests"] == 2
    day = await rl.summary_async(since=now - 24 * 60 * 60)
    assert set(day) == {"new", "total"}
    assert day["total"]["requests"] == 1
    assert day["total"]["tokens"] == 10

    by = await rl.summary_by_account_async(since=now - 7 * 24 * 60 * 60)
    assert set(by) == {"new.example:8080"}
    assert by["new.example:8080"]["tokens"] == 10
    await rl.close()


@pytest.mark.asyncio
async def test_daily_by_account_utc_buckets():
    rl = RequestLog(FakeStorage())

    def ts(*args):
        return datetime(*args, tzinfo=timezone.utc).timestamp()

    await rl.log(_entry(ts(2026, 5, 20, 0, 0, 0), account="a:1",
                        prompt=10, completion=5))
    await rl.log(_entry(ts(2026, 5, 20, 23, 59, 59), account="a:1",
                        prompt=1, completion=0, status=500, error="x"))
    await rl.log(_entry(ts(2026, 5, 21, 0, 0, 0), account="b:2",
                        prompt=2, completion=3))
    # Excluded: no account and before the cutoff.
    await rl.log(_entry(ts(2026, 5, 20, 12, 0, 0), account=None))
    await rl.log(_entry(ts(2026, 5, 19, 23, 0, 0), account="c:3"))

    rows = await rl.daily_by_account_async(ts(2026, 5, 20, 0, 0, 0))
    assert rows == [
        {"day": "2026-05-20", "account": "a:1", "requests": 2,
         "errors": 1, "tokens": 16},
        {"day": "2026-05-21", "account": "b:2", "requests": 1,
         "errors": 0, "tokens": 5},
    ]
    assert await rl.daily_by_account_async(ts(2026, 5, 22, 0, 0, 0)) == []
    await rl.close()


# --- degradation ------------------------------------------------------------


@pytest.mark.asyncio
async def test_storage_failure_never_raises_and_degrades():
    """Storage down: log() swallows, recent() serves the hot buffer and every
    aggregate returns the empty shape instead of a 500."""
    storage = FakeStorage()
    rl = RequestLog(storage)
    await rl.log(e(1))
    storage.raise_unavailable()

    await rl.log(e(2))  # must not raise
    rows = await rl.recent_async(10)
    assert [r["key_id"] for r in rows] == ["k1", "k1"]  # deque fallback
    assert await rl.summary_async() == {}
    assert await rl.summary_by_account_async() == {}
    assert await rl.daily_by_account_async(0.0) == []
    assert await rl.proxy_stats_async(0.0) == {}
    await rl.close()


@pytest.mark.asyncio
async def test_no_storage_facade_degrades_to_the_buffer():
    rl = RequestLog()  # no storage client at all (tests/early startup)
    await rl.log(e(1, key="solo"))

    rows = await rl.recent_async(10)
    assert len(rows) == 1 and rows[0]["key_id"] == "solo"
    assert await rl.summary_async() == {}
    assert await rl.summary_by_account_async() == {}
    assert await rl.daily_by_account_async(0.0) == []
    assert rl.dropped == 0
    await rl.close()


# --- log_soon / close -------------------------------------------------------


@pytest.mark.asyncio
async def test_log_soon_schedules_a_write_on_the_running_loop():
    storage = FakeStorage()
    rl = RequestLog(storage)
    rl.log_soon(e(1))
    for _ in range(10):
        if storage.log:
            break
        await asyncio.sleep(0)

    assert len(storage.log) == 1 and storage.log[0]["key_id"] == "k1"
    await rl.close()


def test_log_soon_without_running_loop_keeps_the_deque_entry():
    """Sync context (no loop): the entry is buffered and the write dropped."""
    storage = FakeStorage()
    rl = RequestLog(storage)
    rl.log_soon(e(1))

    assert storage.log == []
    assert len(rl._buf) == 1
    assert rl.dropped == 0


@pytest.mark.asyncio
async def test_hot_buffer_is_bounded_and_newest_first():
    rl = RequestLog(FakeStorage())
    for i in range(250):
        await rl.log(e(i))

    assert len(rl._buf) == 200
    local = rl._recent_local(5)
    assert len(local) == 5
    assert [row["ts"] for row in local] == sorted(
        (row["ts"] for row in local), reverse=True)
    await rl.close()


@pytest.mark.asyncio
async def test_close_drops_late_entries_and_is_idempotent():
    storage = FakeStorage()
    rl = RequestLog(storage)
    await rl.log(e(1, key="keep"))
    await rl.close()

    await rl.log(e(2, key="late"))
    rl.log_soon(e(3, key="late-2"))

    assert rl.dropped == 2
    assert [row["key_id"] for row in storage.log] == ["keep"]
    assert not storage.closed  # the shared client is owned by main

    await rl.close()  # idempotent: no extra drops, no error
    assert rl.dropped == 2


@pytest.mark.asyncio
async def test_close_drains_in_flight_writes():
    """close() awaits a write scheduled before it (bounded wait)."""

    class SlowStorage(FakeStorage):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def log_request(self, entry):
            self.started.set()
            await self.release.wait()
            await super().log_request(entry)

    storage = SlowStorage()
    rl = RequestLog(storage)
    rl.log_soon(e(1))
    await asyncio.wait_for(storage.started.wait(), timeout=1.0)

    closer = asyncio.create_task(rl.close())
    await asyncio.sleep(0.05)
    assert not closer.done(), "close() must wait for the in-flight write"

    storage.release.set()
    await asyncio.wait_for(closer, timeout=1.0)
    assert len(storage.log) == 1 and storage.log[0]["key_id"] == "k1"


@pytest.mark.asyncio
async def test_close_is_bounded_when_a_write_hangs(monkeypatch):
    """A stuck storage write must not block shutdown forever."""
    import gateway.reqlog as reqlog_module

    class HangingStorage(FakeStorage):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def log_request(self, entry):
            self.started.set()
            await self.release.wait()  # ignores the close timeout
            await super().log_request(entry)

    monkeypatch.setattr(reqlog_module, "_CLOSE_DRAIN_TIMEOUT", 0.05)
    storage = HangingStorage()
    rl = RequestLog(storage)
    rl.log_soon(e(1))
    await asyncio.wait_for(storage.started.wait(), timeout=1.0)

    await asyncio.wait_for(rl.close(), timeout=1.0)  # bounded, did not hang
    assert rl._closed

    storage.release.set()  # let the abandoned write finish cleanly
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_reads_still_serve_the_buffer_after_close():
    """close() only stops writes; recent() keeps working from the deque."""
    storage = FakeStorage()
    rl = RequestLog(storage)
    await rl.log(e(1, key="kept"))
    await rl.close()
    await storage.aclose()  # main closes the shared client last

    rows = await rl.recent_async(10)
    assert [row["key_id"] for row in rows] == ["kept"]


# --- per-proxy lifetime statistics -----------------------------------------


@pytest.mark.asyncio
async def test_proxy_stats_lifetime():
    """All-time totals per account (no window)."""
    rl = RequestLog(FakeStorage())
    base = time.time()
    for row in (
        _entry(base - 100, account="a:1", prompt=100, completion=0),
        _entry(base - 50, account="a:1", prompt=10, completion=5),
        _entry(base - 40, account="a:1", prompt=0, completion=0,
               status=500, error="boom"),
        _entry(base - 10, account="b:2", prompt=1, completion=1),
        _entry(base - 300, account="c:3", prompt=7, completion=7),
    ):
        await rl.log(row)

    stats = await rl.proxy_stats_async()
    assert stats["a:1"] == {"requests": 3, "errors": 1, "tokens": 115}
    assert stats["b:2"] == {"requests": 1, "errors": 0, "tokens": 2}
    assert stats["c:3"] == {"requests": 1, "errors": 0, "tokens": 14}
    await rl.close()


@pytest.mark.asyncio
async def test_proxy_stats_ttl_cache_and_fresh_query():
    rl = RequestLog(FakeStorage())
    now = time.time()
    await rl.log(_entry(now, account="a:1", prompt=10, completion=0))

    first = await rl.proxy_stats_async(ttl_seconds=60.0)
    assert first["a:1"]["requests"] == 1
    await rl.log(_entry(now + 1, account="a:1", prompt=10, completion=0))

    cached = await rl.proxy_stats_async(ttl_seconds=60.0)
    assert cached["a:1"]["requests"] == 1  # served from the TTL cache

    # Callers get copies: mutating the result cannot poison the cache.
    cached["a:1"]["requests"] = 999
    again = await rl.proxy_stats_async(ttl_seconds=60.0)
    assert again["a:1"]["requests"] == 1

    # ttl <= 0 always queries.
    fresh = await rl.proxy_stats_async(ttl_seconds=0.0)
    assert fresh["a:1"]["requests"] == 2
    assert fresh["a:1"]["tokens"] == 20
    await rl.close()
