"""PostgreSQL tests for session_affinity DDL, FK and AffinityDB semantics."""

from __future__ import annotations

import psycopg
import pytest

from gateway.affinity import AffinityManager, SessionState
from gateway.affinitydb import AffinityDB
from gateway.proxies import ProxyManager
from gateway.proxydb import ProxyDB

from gw import build_settings


def test_session_affinity_ddl_fk_set_null_and_upsert(pg_dsn):
    proxy = "http://user:secret@10.0.0.1:3128"
    pdb = ProxyDB(pg_dsn)
    assert pdb.upsert_many([proxy]) == 1

    db = AffinityDB(pg_dsn)
    db.ensure_schema()
    assert db.upsert_many([("sid:one", proxy, 100.0, 110.0)]) >= 1
    rows = db.load_all()
    assert rows == [("sid:one", proxy, 100.0, 110.0)]

    # Moving a session must NOT change created_at.
    assert db.upsert_many([("sid:one", proxy, 100.0, 220.0)]) >= 1
    assert db.load_all() == [("sid:one", proxy, 100.0, 220.0)]

    # The FK is ON DELETE SET NULL: removing the proxy unbinds the session.
    # Admin deletion is out of scope, so simulate an external row removal.
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        cur = conn.execute("DELETE FROM proxies WHERE proxy = %s", (proxy,))
    assert cur.rowcount == 1
    assert db.load_all() == [("sid:one", None, 100.0, 220.0)]
    db.close()
    pdb.close()


def test_clear_keeps_row_and_created_at(pg_dsn):
    pdb = ProxyDB(pg_dsn)
    proxy = "http://10.0.0.2:8080"
    pdb.upsert_many([proxy])
    db = AffinityDB(pg_dsn)
    db.upsert_many([("fp:abc", proxy, 10.0, 20.0)])

    assert db.clear("fp:abc") is True
    assert db.load_all() == [("fp:abc", None, 10.0, 20.0)]
    assert db.clear("fp:missing") is False
    db.close()
    pdb.close()


def test_schema_matches_contract(pg_dsn):
    db = AffinityDB(pg_dsn)
    db.ensure_schema()
    with psycopg.connect(pg_dsn) as conn:
        cols = conn.execute(
            "SELECT column_name, data_type, is_nullable"
            " FROM information_schema.columns"
            " WHERE table_name = 'session_affinity' ORDER BY ordinal_position"
        ).fetchall()
        idx = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'session_affinity'"
        ).fetchall()
        fk = conn.execute(
            "SELECT confdeltype FROM pg_constraint"
            " WHERE conrelid = 'session_affinity'::regclass AND contype = 'f'"
        ).fetchone()
    assert [c[0] for c in cols] == [
        "session_key", "proxy", "created_at", "last_used_at"]
    assert cols[0][2] == "NO"  # primary key NOT NULL
    assert cols[1][2] == "YES"  # proxy may be NULL (unbound)
    names = {row[0] for row in idx}
    assert "idx_session_affinity_proxy" in names
    assert "idx_session_affinity_used" in names
    assert fk is not None and fk[0] == "n"  # SET NULL
    db.close()


@pytest.mark.asyncio
async def test_fk_race_stale_batch_keeps_live_bindings(pg_dsn):
    """An external delete racing the writer must only unbind the deleted proxy.

    The FK rejects the whole coalesced batch once the proxy row is gone; the
    row-by-row retry then NULLs the vanished binding and keeps the healthy one
    (plus syncs only the vanished session's in-memory state).
    """
    a, b = "http://10.9.9.1:1", "http://10.9.9.2:2"
    pdb = ProxyDB(pg_dsn)
    assert pdb.upsert_many([a, b]) == 2
    db = AffinityDB(pg_dsn)
    settings = build_settings(pg_dsn)
    manager = ProxyManager(settings)
    manager.apply_proxies([(a, True), (b, True)])
    aff = AffinityManager(settings, manager, db=db)

    s1 = SessionState("sid:a", a, 10.0, 20.0)
    s2 = SessionState("sid:b", b, 10.0, 20.0)
    aff._sessions["sid:a"] = s1
    aff._sessions["sid:b"] = s2
    aff._schedule_write(s1)
    aff._schedule_write(s2)
    await aff.flush()
    assert {r[0]: r[1] for r in db.load_all()} == {"sid:a": a, "sid:b": b}

    # Race: the proxy row disappears while both stale snapshots are queued.
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        cur = conn.execute("DELETE FROM proxies WHERE proxy = %s", (a,))
    assert cur.rowcount == 1
    s1.proxy = a
    s1.last_used_at = 30.0
    s2.last_used_at = 30.0
    aff._schedule_write(s1)
    aff._schedule_write(s2)
    await aff.flush()
    assert {r[0]: r[1] for r in db.load_all()} == {"sid:a": None, "sid:b": b}
    assert s1.proxy is None and s2.proxy == b
    await aff.aclose()
    pdb.close()
