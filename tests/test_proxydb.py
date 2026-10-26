"""Tests for ProxyDB quota-event history (``proxy_quota_events``).

The table is append-only per ``(proxy_key, kind, quota_until)``: repeated marks
for the same quota window must not inflate the history, ``last_limits`` returns
``MAX(hit_at)``/``COUNT(*)`` per proxy, and its short TTL cache is invalidated
by a write through the same instance. Every test uses the isolated ``pg_dsn``
database from ``conftest.py``.
"""

import time

import pytest

from gateway.proxydb import ProxyDB


def test_record_quota_event_idempotent(pg_dsn):
    db = ProxyDB(pg_dsn)
    now = time.time()

    assert db.record_quota_event("1.2.3.4:8080", now, now + 3600) is True
    # Same proxy/kind/quota_until -> duplicate event, no new row, no new MAX.
    assert db.record_quota_event("1.2.3.4:8080", now + 5, now + 3600) is False

    limits = db.last_limits()
    assert set(limits) == {"1.2.3.4:8080"}
    assert limits["1.2.3.4:8080"]["count"] == 1
    assert limits["1.2.3.4:8080"]["last_at"] == pytest.approx(now)

    # A new quota window (and a different kind) are distinct events.
    assert db.record_quota_event("1.2.3.4:8080", now + 5, now + 7200) is True
    assert db.record_quota_event(
        "1.2.3.4:8080", now + 6, now + 7200, kind="ban") is True

    limits = db.last_limits()
    assert limits["1.2.3.4:8080"]["count"] == 3
    assert limits["1.2.3.4:8080"]["last_at"] == pytest.approx(now + 6)
    db.close()


def test_last_limits_groups_per_proxy_and_counts(pg_dsn):
    db = ProxyDB(pg_dsn)
    now = time.time()
    db.record_quota_event("a:1", now - 100, now + 60)
    db.record_quota_event("a:1", now - 10, now + 120)
    db.record_quota_event("b:2", now - 50, now + 240)

    limits = db.last_limits()
    assert set(limits) == {"a:1", "b:2"}
    assert limits["a:1"] == {"last_at": pytest.approx(now - 10), "count": 2}
    assert limits["b:2"] == {"last_at": pytest.approx(now - 50), "count": 1}
    db.close()


def test_last_limits_cache_invalidated_by_own_write(pg_dsn):
    db = ProxyDB(pg_dsn)
    now = time.time()
    db.record_quota_event("k:1", now, now + 60)
    assert db.last_limits(ttl_seconds=60.0)["k:1"]["count"] == 1

    # A write through this instance must invalidate the cached snapshot.
    db.record_quota_event("k:1", now + 1, now + 120)
    assert db.last_limits(ttl_seconds=60.0)["k:1"]["count"] == 2
    db.close()


def test_last_limits_ttl_cache_ignores_foreign_writes(pg_dsn):
    """The cache is per instance: another instance's write is invisible until
    the TTL expires or a caller asks for a fresh query (ttl_seconds = 0)."""
    db = ProxyDB(pg_dsn)
    other = ProxyDB(pg_dsn)
    now = time.time()
    db.record_quota_event("k:1", now, now + 60)
    assert db.last_limits(ttl_seconds=60.0)["k:1"]["count"] == 1

    other.record_quota_event("k:1", now + 1, now + 120)
    assert db.last_limits(ttl_seconds=60.0)["k:1"]["count"] == 1
    assert db.last_limits(ttl_seconds=0.0)["k:1"]["count"] == 2
    other.close()
    db.close()


def test_last_limits_empty_db(pg_dsn):
    db = ProxyDB(pg_dsn)
    assert db.last_limits() == {}
    assert db.last_limits(ttl_seconds=60.0) == {}
    db.close()


def test_last_limits_unreachable_db_returns_empty():
    """Any DB error is swallowed: the admin panel must keep working."""
    db = ProxyDB("postgresql://openroute:openroute@127.0.0.1:1/none")
    assert db.last_limits() == {}
    db.close()


def test_record_quota_event_propagates_db_errors():
    """The write path is guarded by the caller, not silently swallowed here."""
    db = ProxyDB("postgresql://openroute:openroute@127.0.0.1:1/none")
    with pytest.raises(Exception):
        db.record_quota_event("k:1", time.time(), time.time() + 60)
    db.close()
