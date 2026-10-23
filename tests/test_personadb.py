"""PostgreSQL tests for proxy_personas DDL and PersonaDB semantics."""

from __future__ import annotations

import pytest

from gateway import opencode
from gateway.personadb import PersonaDB
from gateway.personas import Persona


def _persona(quota_key: str, generation: int = 0,
             created_at: float = 100.0) -> Persona:
    return Persona(
        quota_key=quota_key,
        session_id=opencode.new_session_id(),
        project_id=opencode.new_project_id(),
        trace_id=opencode.new_trace_id(),
        created_at=created_at,
        generation=generation,
    )


def test_upsert_load_all_roundtrip(pg_dsn):
    db = PersonaDB(pg_dsn)
    p = _persona("a.invalid:8080", created_at=123.5)
    assert db.upsert(p) is True
    assert db.load_all() == [(
        p.quota_key, p.session_id, p.project_id, p.trace_id,
        p.created_at, p.generation,
    )]
    db.close()


def test_upsert_newer_generation_updates(pg_dsn):
    db = PersonaDB(pg_dsn)
    first = _persona("a.invalid:8080", generation=0)
    assert db.upsert(first) is True
    second = _persona("a.invalid:8080", generation=1, created_at=200.0)
    assert db.upsert(second) is True
    assert db.load_all() == [(
        second.quota_key, second.session_id, second.project_id,
        second.trace_id, second.created_at, 1,
    )]
    db.close()


def test_upsert_older_generation_does_not_overwrite(pg_dsn):
    db = PersonaDB(pg_dsn)
    newer = _persona("a.invalid:8080", generation=3)
    assert db.upsert(newer) is True
    stale = _persona("a.invalid:8080", generation=2, created_at=999.0)
    assert db.upsert(stale) is False
    rows = db.load_all()
    assert rows == [(
        newer.quota_key, newer.session_id, newer.project_id, newer.trace_id,
        newer.created_at, 3,
    )]
    db.close()


def test_load_one_existing_and_missing(pg_dsn):
    db = PersonaDB(pg_dsn)
    p = _persona("a.invalid:8080", generation=2, created_at=321.0)
    assert db.upsert(p) is True
    assert db.load_one("a.invalid:8080") == (
        p.quota_key, p.session_id, p.project_id, p.trace_id,
        p.created_at, p.generation,
    )
    assert db.load_one("missing.invalid:1") is None
    db.close()


def test_new_instance_sees_persisted_row(pg_dsn):
    """A fresh PersonaDB on the same DSN is the restart path."""
    first = PersonaDB(pg_dsn)
    p = _persona("a.invalid:8080", generation=7)
    assert first.upsert(p) is True
    first.close()

    second = PersonaDB(pg_dsn)
    assert second.load_all() == [(
        p.quota_key, p.session_id, p.project_id, p.trace_id,
        p.created_at, p.generation,
    )]
    second.close()


def test_close_is_idempotent(pg_dsn):
    db = PersonaDB(pg_dsn)
    db.ensure_schema()
    db.close()
    db.close()
    with pytest.raises(RuntimeError):
        db.load_all()
