"""Tests for reqlog.py: deque/DB paths, aggregates, "never raises",
persistence, close semantics."""

import threading
import time

import pytest

from app.reqlog import LogEntry, RequestLog


def e(i, key="k1", status=200, prompt=10, completion=5, error=None, latency=100.0, stream=False):
    return LogEntry(
        ts=time.time() + i, key_id=key, method="POST", path="chat/completions",
        model="deepseek-v4.1-flash-free", status=status, latency_ms=latency, stream=stream,
        account="1.2.3.4:8080", prompt_tokens=prompt, completion_tokens=completion, error=error,
    )


@pytest.mark.asyncio
async def test_log_and_recent_deque_path(pg_dsn):
    rl = RequestLog(pg_dsn)
    await rl.log(e(1))
    await rl.log(e(2))

    rows = rl.recent(2)  # limit <= len(buf) -> hot cache (deque)
    assert len(rows) == 2
    assert rows[0]["ts"] > rows[1]["ts"]  # newest first
    assert rows[0]["key_id"] == "k1" and rows[0]["model"] == "deepseek-v4.1-flash-free"
    assert isinstance(rows[0]["stream"], bool)

    top = rl.recent(1)
    assert len(top) == 1 and top[0]["ts"] == rows[0]["ts"]
    rl.close()


@pytest.mark.asyncio
async def test_recent_db_path_beyond_deque(pg_dsn):
    rl = RequestLog(pg_dsn)
    for i in range(3):
        await rl.log(e(i, stream=True))

    rows = rl.recent(100)  # limit > len(buf) -> go to PostgreSQL
    assert len(rows) == 3
    tss = [r["ts"] for r in rows]
    assert tss == sorted(tss, reverse=True)
    assert all(r["stream"] is True for r in rows)
    rl.close()


@pytest.mark.asyncio
async def test_summary_aggregation(pg_dsn):
    rl = RequestLog(pg_dsn)
    await rl.log(e(1, key="kA", status=200, prompt=100, completion=50, latency=100.0))
    await rl.log(e(2, key="kA", status=500, prompt=10, completion=0, error="boom", latency=300.0))
    await rl.log(e(3, key="kB", status=200, prompt=30, completion=20, latency=200.0))

    s = rl.summary()
    assert s["kA"] == {"requests": 2, "errors": 1, "tokens": 160, "avg_latency_ms": 200.0}
    assert s["kB"] == {"requests": 1, "errors": 0, "tokens": 50, "avg_latency_ms": 200.0}
    assert s["total"]["requests"] == 3
    assert s["total"]["errors"] == 1
    assert s["total"]["tokens"] == 210
    assert s["total"]["avg_latency_ms"] == 200.0
    rl.close()


@pytest.mark.asyncio
async def test_summary_by_account(pg_dsn):
    rl = RequestLog(pg_dsn)
    entry = e(1, key="kA", prompt=10, completion=5)
    entry.account = "5.5.5.5:3128"
    await rl.log(entry)
    by = rl.summary_by_account()
    assert by["5.5.5.5:3128"]["requests"] == 1
    assert by["5.5.5.5:3128"]["tokens"] == 15
    rl.close()


@pytest.mark.asyncio
async def test_unreachable_db_never_raises():
    """An unreachable PostgreSQL must not break serving: log() swallows,
    recent() serves the hot cache, summary() returns empty. The pool waits for
    a connection no longer than the timeout (5s in make_pool), then PoolTimeout."""
    rl = RequestLog("postgresql://openroute:openroute@127.0.0.1:1/none")
    await rl.log(e(1))
    rows = rl.recent(10)
    assert len(rows) == 1 and rows[0]["key_id"] == "k1"
    assert rl.summary() == {}
    rl.close()


@pytest.mark.asyncio
async def test_persistence_across_instances(pg_dsn):
    rl1 = RequestLog(pg_dsn)
    for i in range(3):
        await rl1.log(e(i, key="pk"))
    rl1.close()

    rl2 = RequestLog(pg_dsn)  # new instance = "restart"
    rows = rl2.recent(100)
    assert len(rows) == 3
    assert all(r["key_id"] == "pk" for r in rows)
    rl2.close()


def test_close_waits_while_insert_inflight(pg_dsn):
    """Regression for the close-vs-insert race: pool.close() itself does not
    wait for clients, so close() must hold _write_lock: while an insert is in
    flight, close() must not finish."""
    rl = RequestLog(pg_dsn)
    rl._insert(e(1))  # warm-up: schema created, connections open

    rl._write_lock.acquire()  # "insert" in flight
    closed = []
    closer = threading.Thread(target=lambda: (rl.close(), closed.append(True)), daemon=True)
    closer.start()
    time.sleep(0.1)
    assert not closed, "close() must wait for the in-flight insert"

    rl._write_lock.release()
    closer.join(timeout=10)
    assert closed, "close() must finish after the lock is released"
    assert rl._pool.closed


def test_concurrent_writes_and_reads(pg_dsn):
    """The ConnectionPool is thread-safe: parallel inserts from different
    threads do not interleave and are not lost."""
    rl = RequestLog(pg_dsn)
    errors = []

    def writer(n):
        try:
            for i in range(20):
                rl._insert(e(n * 100 + i, key=f"kw{n}"))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(rl.recent(1000)) == 80
    rl.close()


@pytest.mark.asyncio
async def test_closed_then_log_drops_safely(pg_dsn):
    """Regression for 'resurrection after close()': a late log() must NOT open a
    new pool — it silently drops the entry and increments dropped."""
    rl = RequestLog(pg_dsn)
    await rl.log(e(1, key="keep"))
    rl.close()

    await rl.log(e(2, key="late"))

    assert rl.dropped == 1
    assert rl._pool.closed

    rl2 = RequestLog(pg_dsn)
    rows = rl2.recent(100)
    assert [r["key_id"] for r in rows] == ["keep"]
    rl2.close()


def test_close_idempotent_and_thread_safe(pg_dsn):
    rl = RequestLog(pg_dsn)
    rl.close()
    rl.close()
    assert rl._pool.closed and rl.dropped == 0


@pytest.mark.asyncio
async def test_async_wrappers_offload_reads(pg_dsn):
    rl = RequestLog(pg_dsn)
    await rl.log(e(1, key="kA"))
    await rl.log(e(2, key="kB"))

    rows = await rl.recent_async(100)
    assert len(rows) == 2
    assert {r["key_id"] for r in rows} == {"kA", "kB"}

    s = await rl.summary_async()
    assert s["total"]["requests"] == 2
    assert s["kA"]["requests"] == 1
    rl.close()


@pytest.mark.asyncio
async def test_no_retention_old_entries_kept_forever(pg_dsn):
    """The log is kept indefinitely: entries "older than 30 days" are not
    deleted on connect or in the write path."""
    rl = RequestLog(pg_dsn)
    old = LogEntry(
        ts=time.time() - 400 * 86400, key_id="ancient", method="POST",
        path="chat/completions", model="m1", status=200, latency_ms=1.0,
        stream=False, account=None, prompt_tokens=1, completion_tokens=1, error=None,
    )
    await rl.log(old)
    rl.close()

    rl2 = RequestLog(pg_dsn)
    await rl2.log(e(1, key="fresh"))
    rows = rl2.recent(100)
    assert {r["key_id"] for r in rows} == {"ancient", "fresh"}
    rl2.close()
