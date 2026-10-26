"""Tests for the private storage-service (openroute_storage).

Uses the isolated PostgreSQL database from ``tests/conftest.py`` (``pg_dsn``).
The service app is instantiated with an explicit test token; TestClient runs
its lifespan (pool open + frozen DDL + shutdown). Direct SQL is used only for
assertions and for simulating an external proxy row removal.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import quote

import psycopg
import pytest
from fastapi.testclient import TestClient

from openroute_storage.config import Settings
from openroute_storage.main import create_app
from openroute_storage.schema import SCHEMA

TOKEN = "test-storage-token"
H = {"X-Storage-Token": TOKEN}


def _settings(pg_dsn: str, **over) -> Settings:
    base = {
        "storage_api_token": TOKEN,
        "postgres_dsn": pg_dsn,
        "db_connect_timeout_seconds": 2,
        "db_startup_timeout_seconds": 10.0,
    }
    base.update(over)
    return Settings(**base)


@contextmanager
def _selector_loop_on_windows():
    """psycopg async cannot run on the Proactor loop (Windows test host).

    Production runs in Linux containers; this only makes TestClient's private
    event loop a Selector loop on Windows. The policy is restored immediately
    after the loop/portal has been created, so other tests are unaffected.
    """
    if sys.platform != "win32":
        yield
        return
    previous = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(previous)


@contextmanager
def _client(pg_dsn: str, **over):
    app = create_app(_settings(pg_dsn, **over))
    with _selector_loop_on_windows():
        client = TestClient(app)
        client.__enter__()
    try:
        yield client
    finally:
        client.__exit__(None, None, None)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _by_proxy(body: dict) -> dict[str, dict]:
    return {row["proxy"]: row for row in body["proxies"]}


def _utc(year: int, month: int, day: int, hour: int = 0,
         minute: int = 0, second: int = 0) -> float:
    return datetime(
        year, month, day, hour, minute, second, tzinfo=timezone.utc
    ).timestamp()


def _log_entry(**over) -> dict:
    entry = {
        "ts": _utc(2026, 5, 20),
        "key_id": "k1",
        "method": "POST",
        "path": "chat/completions",
        "model": "deepseek-v4.1-flash-free",
        "status": 200,
        "latency_ms": 100.0,
        "stream": False,
        "account": "1.2.3.4:8080",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "error": None,
    }
    entry.update(over)
    return entry


# --- contract / schema ------------------------------------------------------


def test_schema_contract_covers_every_storage_table():
    """The storage DDL is the frozen contract; drift must be visible.

    storage-service is the only SQL owner, so the schema itself is the
    contract: all five tables and the guarantees the gateway relies on
    (soft-delete, persistent parking, affinity FK fallback) must stay.
    """
    ddl = "\n".join(SCHEMA)
    for table in (
        "request_log", "proxies", "session_affinity", "keys", "proxy_personas",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in ddl
    assert "ON DELETE SET NULL" in ddl
    assert "permanently_banned" in ddl
    assert "listed" in ddl


def test_schema_created_and_repeat_startup_is_idempotent(pg_dsn):
    with _client(pg_dsn) as client:
        assert client.get("/health").status_code == 200

    # A second start on the same database applies the same idempotent DDL.
    with _client(pg_dsn) as client:
        assert client.get("/health").status_code == 200

    with psycopg.connect(pg_dsn) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = 'public'"
            ).fetchall()
        }
        assert {
            "request_log", "proxies", "session_affinity", "keys",
            "proxy_personas",
        } <= tables
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes"
                " WHERE tablename IN ('request_log', 'session_affinity')"
            ).fetchall()
        }
        assert {
            "idx_request_log_ts", "idx_request_log_key",
            "idx_request_log_account_ts", "idx_session_affinity_proxy",
            "idx_session_affinity_used",
        } <= indexes
        fk = conn.execute(
            "SELECT confdeltype FROM pg_constraint"
            " WHERE conrelid = 'session_affinity'::regclass AND contype = 'f'"
        ).fetchone()
    assert fk is not None and fk[0] == "n"  # ON DELETE SET NULL


def test_startup_fails_clearly_when_postgres_is_unreachable():
    settings = Settings(
        storage_api_token=TOKEN,
        postgres_dsn="postgresql://openroute:topsecret@127.0.0.1:1/none",
        db_connect_timeout_seconds=1,
        db_startup_timeout_seconds=2.0,
    )
    with pytest.raises(RuntimeError) as exc:
        with _selector_loop_on_windows():
            with TestClient(create_app(settings)):
                pass
    message = str(exc.value)
    assert "PostgreSQL is unreachable" in message
    assert "topsecret" not in message
    assert "postgresql://" not in message


# --- auth / health ----------------------------------------------------------


def test_health_is_public_and_domains_require_the_token(pg_dsn):
    with _client(pg_dsn) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "database": "ok"}

        missing = client.get("/keys")
        assert missing.status_code == 401
        body = missing.json()
        assert body["error"]["type"] == "authentication_error"
        assert body["error"]["code"] == 401
        assert TOKEN not in missing.text

        wrong = client.get("/keys", headers={"X-Storage-Token": "wrong"})
        assert wrong.status_code == 401

        ok = client.get("/keys", headers=H)
        assert ok.status_code == 200
        assert ok.json() == {"keys": []}

        # The auth guard also covers paths that do not exist / are disabled.
        assert client.get("/openapi.json").status_code == 401
        assert client.post("/proxies/sync", json={"proxies": []}).status_code == 401


def test_empty_configured_token_fails_closed(pg_dsn):
    with _client(pg_dsn, storage_api_token="") as client:
        assert client.get("/health").status_code == 200
        assert client.get("/keys", headers={"X-Storage-Token": ""}).status_code == 401
        assert client.get("/keys", headers={"X-Storage-Token": "anything"}).status_code == 401


def test_token_is_never_logged(pg_dsn, caplog):
    caplog.set_level(logging.DEBUG)
    with _client(pg_dsn) as client:
        client.get("/keys", headers={"X-Storage-Token": "wrong-" + TOKEN})
    assert TOKEN not in caplog.text


def test_unavailable_database_maps_to_503_without_dsn(pg_dsn):
    with _client(pg_dsn) as client:
        client.app.state.pool = None
        health = client.get("/health")
        assert health.status_code == 503
        assert health.json() == {
            "status": "degraded", "database": "unavailable"
        }

        failed = client.get("/keys", headers=H)
        assert failed.status_code == 503
        assert failed.json()["error"]["type"] == "storage_unavailable"
        assert "postgresql://" not in failed.text

        # Auth still runs before the database dependency.
        assert client.get("/keys").status_code == 401


# --- proxies ----------------------------------------------------------------


def test_proxy_sync_preserves_flags_and_parking_and_soft_deletes(pg_dsn):
    a = "http://u:p@proxy-a.invalid:8080"
    b = "http://proxy-b.invalid:8080"
    c = "http://proxy-c.invalid:8080"

    with _client(pg_dsn) as client:
        first = client.post(
            "/proxies/sync", json={"proxies": [a, b, a, ""]}, headers=H
        )
        assert first.status_code == 200
        body = first.json()
        assert body["added"] == 2
        rows = _by_proxy(body)
        assert set(rows) == {a, b}
        assert rows[a]["enabled"] is False  # new rows are always disabled
        assert rows[b]["enabled"] is False
        assert all(row["listed"] is True for row in rows.values())

        enabled = client.post(
            "/proxies/set-enabled",
            json={"proxy": a, "enabled": True},
            headers=H,
        )
        assert enabled.status_code == 200
        assert enabled.json()["updated"] is True

        parked = client.post(
            "/proxies/parking",
            json={
                "proxies": [a],
                "quota_until": 111.0,
                "banned_until": 222.0,
                "permanently_banned": True,
            },
            headers=H,
        )
        assert parked.status_code == 200
        assert parked.json() == {"updated": 1}

        # b disappears from the source; c is new. a keeps enabled + parking.
        second = client.post(
            "/proxies/sync", json={"proxies": [a, c]}, headers=H
        )
        assert second.status_code == 200
        body = second.json()
        assert body["added"] == 1
        rows = _by_proxy(body)
        assert set(rows) == {a, b, c}
        assert rows[a]["enabled"] is True
        assert rows[a]["quota_until"] == 111.0
        assert rows[a]["banned_until"] == 222.0
        assert rows[a]["permanently_banned"] is True
        assert rows[a]["listed"] is True
        assert rows[b]["listed"] is False  # soft delete, row retained
        assert rows[c]["enabled"] is False and rows[c]["listed"] is True

        listed = client.get("/proxies?listed=true", headers=H).json()
        assert [row["proxy"] for row in listed["proxies"]] == [a, c]
        unlisted = client.get("/proxies?listed=false", headers=H).json()
        assert [row["proxy"] for row in unlisted["proxies"]] == [b]
        everything = client.get("/proxies", headers=H).json()
        assert [row["proxy"] for row in everything["proxies"]] == [a, b, c]

        # Parking writes values as-is; permanent ban is only ORed on.
        cleared = client.post(
            "/proxies/parking",
            json={
                "proxies": [a],
                "quota_until": 0.0,
                "banned_until": 0.0,
                "permanently_banned": False,
            },
            headers=H,
        )
        assert cleared.json() == {"updated": 1}
        row = _by_proxy(client.get("/proxies", headers=H).json())[a]
        assert row["quota_until"] == 0.0 and row["banned_until"] == 0.0
        assert row["permanently_banned"] is True  # never cleared

        # An empty snapshot unlists every row but keeps the rows.
        emptied = client.post("/proxies/sync", json={"proxies": []}, headers=H)
        assert emptied.json()["added"] == 0
        assert all(not row["listed"] for row in emptied.json()["proxies"])

        missing = client.post(
            "/proxies/set-enabled",
            json={"proxy": "http://nope:1", "enabled": True},
            headers=H,
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["type"] == "not_found_error"


# --- keys -------------------------------------------------------------------


def test_keys_hash_only_crud_and_duplicate_key_id_targeting(pg_dsn):
    plaintext = "sk-very-secret-plaintext"
    h1 = _hash(plaintext)
    h2 = _hash("sk-two")

    with _client(pg_dsn) as client:
        added = client.post(
            "/keys",
            json={
                "key_hash": h1,
                "key_id": "dup",
                "rpm": 60,
                "tpm": 1000,
                "models": ["m1", "m2"],
            },
            headers=H,
        )
        assert added.status_code == 200
        assert added.json() == {"created": True}
        assert client.post(
            "/keys", json={"key_hash": h2, "key_id": "dup"}, headers=H
        ).json() == {"created": True}

        # Duplicate hash: no new row, no error.
        assert client.post(
            "/keys", json={"key_hash": h1, "key_id": "other"}, headers=H
        ).json() == {"created": False}

        # Plaintext is never persisted and never returned.
        with psycopg.connect(pg_dsn) as conn:
            stored = conn.execute(
                "SELECT key_hash, key_id FROM keys ORDER BY id"
            ).fetchall()
        blob = "\n".join(" ".join(str(v) for v in row) for row in stored)
        assert plaintext not in blob
        assert h1 in blob and h2 in blob

        listed = client.get("/keys", headers=H).json()["keys"]
        assert [row["key_id"] for row in listed] == ["dup", "dup"]
        assert listed[0] == {
            "key_hash": h1,
            "key_id": "dup",
            "rpm": 60,
            "tpm": 1000,
            "models": ["m1", "m2"],
            "enabled": True,
        }

        # Rotate the first row: same record, metadata preserved, old hash gone.
        h3 = _hash("sk-rotated")
        rotated = client.post(
            "/keys/rotate",
            json={"key_hash": h1, "new_key_hash": h3},
            headers=H,
        )
        assert rotated.status_code == 200
        assert rotated.json() == {
            "rotated": True, "already_rotated": False, "key_id": "dup",
        }
        # Retrying the same pair after a lost response is an idempotent success.
        retry = client.post(
            "/keys/rotate",
            json={"key_hash": h1, "new_key_hash": h3},
            headers=H,
        )
        assert retry.status_code == 200
        assert retry.json() == {
            "rotated": False, "already_rotated": True, "key_id": "dup",
        }
        assert [row["key_hash"] for row in client.get("/keys", headers=H).json()["keys"]] == [
            h3, h2
        ]
        first = client.get("/keys", headers=H).json()["keys"][0]
        assert first["rpm"] == 60 and first["tpm"] == 1000
        assert first["models"] == ["m1", "m2"] and first["enabled"] is True

        # key_id is a label, not an identifier: rotate only the second row.
        h4 = _hash("sk-two-rotated")
        second = client.post(
            "/keys/rotate",
            json={"key_hash": h2, "new_key_hash": h4},
            headers=H,
        )
        assert second.status_code == 200
        assert second.json()["key_id"] == "dup"
        assert [row["key_hash"] for row in client.get("/keys", headers=H).json()["keys"]] == [
            h3, h4
        ]

        # A new hash belonging to another row is a conflict, not a merge.
        h5 = _hash("sk-conflict")
        assert client.post(
            "/keys", json={"key_hash": h5, "key_id": "third"}, headers=H
        ).json() == {"created": True}
        conflict = client.post(
            "/keys/rotate",
            json={"key_hash": h3, "new_key_hash": h5},
            headers=H,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["type"] == "conflict_error"

        # Revoke/enable by exact hash.
        revoked = client.post(
            "/keys/set-enabled",
            json={"key_hash": h3, "enabled": False},
            headers=H,
        )
        assert revoked.status_code == 200
        assert revoked.json()["updated"] is True
        rows = client.get("/keys", headers=H).json()["keys"]
        assert rows[0]["key_hash"] == h3 and rows[0]["enabled"] is False
        assert rows[1]["enabled"] is True

        # Delete by exact hash; repeated delete is 404.
        deleted = client.delete(f"/keys/{h3}", headers=H)
        assert deleted.status_code == 200
        assert deleted.json() == {"key_hash": h3, "deleted": True}
        assert client.delete(f"/keys/{h3}", headers=H).status_code == 404
        assert client.post(
            "/keys/set-enabled",
            json={"key_hash": h3, "enabled": True},
            headers=H,
        ).status_code == 404

        unknown_rotate = client.post(
            "/keys/rotate",
            json={"key_hash": _hash("missing"), "new_key_hash": _hash("new")},
            headers=H,
        )
        assert unknown_rotate.status_code == 404

        # Plaintext-looking or malformed hashes are rejected before SQL.
        assert client.post(
            "/keys", json={"key_hash": "not-a-sha256", "key_id": "x"}, headers=H
        ).status_code == 422
        assert client.post(
            "/keys/rotate",
            json={"key_hash": h4, "new_key_hash": "UPPERCASE"}, headers=H,
        ).status_code == 422


# --- affinity ---------------------------------------------------------------


def test_affinity_upsert_move_clear_and_missing_proxy(pg_dsn):
    p1 = "http://p1.invalid:8080"
    p2 = "http://p2.invalid:8080"
    missing = "http://missing.invalid:1"

    with _client(pg_dsn) as client:
        assert client.post(
            "/proxies/sync", json={"proxies": [p1, p2]}, headers=H
        ).status_code == 200

        first = client.post(
            "/affinity",
            json={"bindings": [{
                "session_key": "sid:one", "proxy": p1,
                "created_at": 100.0, "last_used_at": 110.0,
            }]},
            headers=H,
        )
        assert first.status_code == 200
        assert first.json() == {"written": 1, "unbound": 0}
        assert client.get("/affinity", headers=H).json() == {"bindings": [{
            "session_key": "sid:one", "proxy": p1,
            "created_at": 100.0, "last_used_at": 110.0,
        }]}

        # A move never changes created_at.
        moved = client.post(
            "/affinity",
            json={"bindings": [{
                "session_key": "sid:one", "proxy": p2,
                "created_at": 999.0, "last_used_at": 120.0,
            }]},
            headers=H,
        )
        assert moved.json() == {"written": 1, "unbound": 0}
        assert client.get("/affinity", headers=H).json()["bindings"][0] == {
            "session_key": "sid:one", "proxy": p2,
            "created_at": 100.0, "last_used_at": 120.0,
        }

        # A coalesced batch with one vanished proxy: healthy row kept, the
        # vanished binding stored as unbound.
        batch = client.post(
            "/affinity",
            json={"bindings": [
                {"session_key": "sid:two", "proxy": p2,
                 "created_at": 200.0, "last_used_at": 210.0},
                {"session_key": "sid:three", "proxy": missing,
                 "created_at": 300.0, "last_used_at": 310.0},
            ]},
            headers=H,
        )
        assert batch.status_code == 200
        assert batch.json() == {"written": 2, "unbound": 1}
        rows = {
            row["session_key"]: row
            for row in client.get("/affinity", headers=H).json()["bindings"]
        }
        assert rows["sid:two"]["proxy"] == p2
        assert rows["sid:three"]["proxy"] is None

        cleared = client.post(
            "/affinity/clear",
            json={"session_keys": ["sid:one", "sid:missing"]},
            headers=H,
        )
        assert cleared.json() == {"cleared": 1}
        rows = {
            row["session_key"]: row
            for row in client.get("/affinity", headers=H).json()["bindings"]
        }
        assert rows["sid:one"] == {
            "session_key": "sid:one", "proxy": None,
            "created_at": 100.0, "last_used_at": 120.0,
        }

        # FK ON DELETE SET NULL: an external proxy removal unbinds the session.
        assert client.post(
            "/affinity",
            json={"bindings": [{
                "session_key": "sid:four", "proxy": p1,
                "created_at": 400.0, "last_used_at": 410.0,
            }]},
            headers=H,
        ).json() == {"written": 1, "unbound": 0}
        with psycopg.connect(pg_dsn, autocommit=True) as conn:
            cur = conn.execute("DELETE FROM proxies WHERE proxy = %s", (p1,))
        assert cur.rowcount == 1
        rows = {
            row["session_key"]: row
            for row in client.get("/affinity", headers=H).json()["bindings"]
        }
        assert rows["sid:four"]["proxy"] is None
        assert rows["sid:four"]["created_at"] == 400.0


# --- personas ---------------------------------------------------------------


def test_persona_generation_guard_and_reload(pg_dsn):
    quota = "proxy-a.invalid:8080"

    def persona(**over):
        base = {
            "quota_key": quota,
            "session_id": "ses_one",
            "project_id": "a" * 40,
            "trace_id": "b" * 32,
            "created_at": 100.0,
            "generation": 3,
        }
        base.update(over)
        return base

    with _client(pg_dsn) as client:
        first = client.post("/personas", json=persona(), headers=H)
        assert first.json() == {"written": True}
        path = f"/personas/{quote(quota, safe='')}"
        stored = client.get(path, headers=H).json()
        assert stored == persona()

        stale = client.post(
            "/personas",
            json=persona(session_id="ses_stale", created_at=999.0,
                         generation=2),
            headers=H,
        )
        assert stale.json() == {"written": False}
        assert client.get(path, headers=H).json()["session_id"] == "ses_one"

        # The guard is `<=`: a retry with the same generation may rewrite.
        same = client.post(
            "/personas",
            json=persona(session_id="ses_same_gen", created_at=150.0),
            headers=H,
        )
        assert same.json() == {"written": True}
        assert client.get(path, headers=H).json()["session_id"] == "ses_same_gen"

        newer = client.post(
            "/personas",
            json=persona(session_id="ses_new", created_at=200.0,
                         generation=4),
            headers=H,
        )
        assert newer.json() == {"written": True}
        stored = client.get(path, headers=H).json()
        assert stored["session_id"] == "ses_new" and stored["generation"] == 4

        # A second bucket, and URL-safe access for IPv6 quota keys.
        other = persona(quota_key="proxy-b.invalid:8080", session_id="ses_b",
                        created_at=50.0, generation=0)
        assert client.post("/personas", json=other, headers=H).json() == {
            "written": True
        }
        ipv6 = persona(quota_key="[2001:db8::1]:8080", session_id="ses_v6")
        assert client.post("/personas", json=ipv6, headers=H).json() == {
            "written": True
        }
        assert client.get(
            f"/personas/{quote(ipv6['quota_key'], safe='')}", headers=H
        ).json()["session_id"] == "ses_v6"

        # load_all is ordered by the stored created_at, which
        # the same-generation retry updated to 200.0 and the IPv6 row to 100.0.
        listed = client.get("/personas", headers=H).json()["personas"]
        assert [row["quota_key"] for row in listed] == [
            "proxy-b.invalid:8080", "[2001:db8::1]:8080", quota,
        ]

        assert client.get(
            f"/personas/{quote('missing.invalid:1', safe='')}", headers=H
        ).status_code == 404


# --- request log / stats ----------------------------------------------------


def test_request_log_append_recent_and_stats_utc(pg_dsn):
    day1 = _utc(2026, 5, 20)
    day1_end = _utc(2026, 5, 20, 23, 59, 59)
    day2 = _utc(2026, 5, 21)
    old = _utc(2026, 5, 19, 23, 0, 0)

    entries = [
        _log_entry(ts=day1, key_id="kA", status=200, latency_ms=100.0,
                   account="a:1", prompt_tokens=100, completion_tokens=50),
        _log_entry(ts=day1_end, key_id="kA", status=500, latency_ms=300.0,
                   account="a:1", prompt_tokens=10, completion_tokens=0,
                   error="boom"),
        _log_entry(ts=day2, key_id="kB", status=200, latency_ms=200.0,
                   account="b:2", prompt_tokens=30, completion_tokens=20),
        # status 200 but error set -> counted as an error; NULL account is
        # excluded from per-account/daily aggregates but not from summary().
        _log_entry(ts=_utc(2026, 5, 20, 12), key_id="kC", status=200,
                   latency_ms=200.0, account=None, prompt_tokens=7,
                   completion_tokens=0, error="soft"),
        _log_entry(ts=old, key_id="old", status=200, latency_ms=1.0,
                   account="c:3", prompt_tokens=7, completion_tokens=7),
    ]

    with _client(pg_dsn) as client:
        for entry in entries:
            response = client.post("/request-log", json=entry, headers=H)
            assert response.status_code == 200
            assert response.json() == {"logged": True}

        recent = client.get(
            "/request-log/recent?limit=3", headers=H
        )
        assert recent.status_code == 200
        body = recent.json()
        assert body["limit"] == 3
        assert [row["key_id"] for row in body["entries"]] == [
            "kB", "kA", "kC"
        ]
        assert body["entries"][0]["stream"] is False
        assert body["entries"][0]["ts"] == day2
        assert client.get(
            "/request-log/recent?limit=0", headers=H
        ).status_code == 422

        summary = client.get(
            f"/stats/summary?since={day1}", headers=H
        ).json()
        assert summary["kA"] == {
            "requests": 2, "errors": 1, "tokens": 160,
            "avg_latency_ms": 200.0,
        }
        assert summary["kB"] == {
            "requests": 1, "errors": 0, "tokens": 50,
            "avg_latency_ms": 200.0,
        }
        assert summary["kC"] == {
            "requests": 1, "errors": 1, "tokens": 7,
            "avg_latency_ms": 200.0,
        }
        assert "old" not in summary
        assert summary["total"] == {
            "requests": 4, "errors": 2, "tokens": 217,
            "avg_latency_ms": 200.0,
        }

        # since is inclusive: the boundary row is included, earlier is not.
        boundary = client.get(
            f"/stats/summary?since={day1_end}", headers=H
        ).json()
        assert boundary["kA"] == {
            "requests": 1, "errors": 1, "tokens": 10,
            "avg_latency_ms": 300.0,
        }
        assert "kB" in boundary

        lifetime = client.get("/stats/summary", headers=H).json()
        assert lifetime["old"] == {
            "requests": 1, "errors": 0, "tokens": 14,
            "avg_latency_ms": 1.0,
        }
        assert lifetime["total"] == {
            "requests": 5, "errors": 2, "tokens": 231,
            "avg_latency_ms": 160.2,
        }

        by_account = client.get(
            f"/stats/by-account?since={day1}", headers=H
        ).json()
        assert by_account == {
            "a:1": {"requests": 2, "errors": 1, "tokens": 160},
            "b:2": {"requests": 1, "errors": 0, "tokens": 50},
        }
        lifetime_by_account = client.get(
            "/stats/by-account", headers=H
        ).json()
        assert lifetime_by_account["c:3"] == {
            "requests": 1, "errors": 0, "tokens": 14,
        }

        daily = client.get(f"/stats/daily?since={day1}", headers=H).json()
        assert daily == [
            {"day": "2026-05-20", "account": "a:1", "requests": 2,
             "errors": 1, "tokens": 160},
            {"day": "2026-05-21", "account": "b:2", "requests": 1,
             "errors": 0, "tokens": 50},
        ]
        assert client.get(
            f"/stats/daily?since={day2}", headers=H
        ).json() == [daily[1]]
        all_days = client.get("/stats/daily", headers=H).json()
        assert {"day": "2026-05-19", "account": "c:3", "requests": 1,
                "errors": 0, "tokens": 14} in all_days

        with psycopg.connect(pg_dsn) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM request_log"
            ).fetchone()[0]
        assert count == len(entries)
